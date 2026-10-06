"""
Domain/service layer for autopilot — mirrors csp/services.py's role: every
generation function here reads real numbers through csp.services (never
raw CALLING SHEET text, never invents a figure), asks Nemotron for a
strictly-shaped JSON response, validates that shape before trusting it, and
degrades to "produced nothing this run" on any failure rather than raising
into a management command or dashboard view.

Nothing here sends anything anywhere — see models.py's DraftMessage
docstring for why that's a deliberate boundary, not an oversight.
"""

from __future__ import annotations

import json

import structlog
from csp.models import IngestLog, MonthlySummary
from csp.services import (
    current_month,
    get_all_csps_account_growth,
    get_network_account_growth,
    get_overview,
    list_monthly_summaries,
)
from django.conf import settings
from django.utils import timezone

from .models import AnomalyFlag, DraftMessage, Insight, PriorityCall, ReviewedIngestRun
from .nemotron_client import NemotronUnavailable, chat_json

logger = structlog.get_logger("autopilot")


def _candidate_rows(month: str, limit: int = 50) -> list[dict]:
    """CSPs with room to improve (not already S4), cheapest deterministic
    pre-sort (smallest gap first — same idea as the dashboard's "Quick
    wins"). Bounds what actually gets sent to Nemotron instead of shipping
    the whole network's numbers in every prompt."""
    summaries, _ = list_monthly_summaries(month=month, limit=100000)
    growth = get_all_csps_account_growth()
    rows = []
    for ms in summaries:
        if ms.slab == MonthlySummary.Slab.S4:
            continue
        gap = ms.gap_to_min if ms.slab == MonthlySummary.Slab.NIL else ms.gap_to_next_slab
        if gap is None:
            continue
        g = growth.get(ms.csp_id)
        mom_change_pct = float(ms.mom_change_pct) if ms.mom_change_pct is not None else None
        rows.append(
            {
                "csp_code": ms.csp_id,
                "name": ms.csp.name,
                "slab": ms.slab,
                "gap": float(gap),
                "account_count": ms.csp.account_count or 0,
                "trend_flag": ms.trend_flag or "new",
                "mom_change_pct": mom_change_pct,
                "net_new_accounts": g["net_new"] if g else None,
            }
        )
    rows.sort(key=lambda r: r["gap"])
    return rows[:limit]


def generate_daily_briefing(month: str | None = None) -> Insight | None:
    """One narrative paragraph over real network-wide numbers — read-only,
    nothing here influences any other CSP-facing behaviour."""
    month = month or current_month()
    overview = get_overview(month)
    recent_growth = get_network_account_growth()[-7:]
    summaries, _ = list_monthly_summaries(month=month, limit=100000)
    trend_counts = {"improving": 0, "declining": 0, "stable": 0}
    for ms in summaries:
        key = ms.trend_flag or "stable"
        trend_counts[key] = trend_counts.get(key, 0) + 1

    context = {
        "month": month,
        "csp_total": overview["csp_total"],
        "csp_with_data": overview["csp_with_data"],
        "slab_distribution": overview["slab_distribution"],
        "avg_mab": round(overview["avg_mab"], 2) if overview["avg_mab"] is not None else None,
        "trend_counts": trend_counts,
        "recent_account_growth": [
            {"date": str(g["date"]), "total_accounts": g["total_accounts"], "net_new": g["net_new"]}
            for g in recent_growth
        ],
    }
    system_prompt = (
        "You are a data analyst for an SBI kiosk-banking CSP network. You will be given "
        "real, already-computed network statistics as JSON. Write a short, factual daily "
        "briefing for the operations team. Do not invent any number not present in the "
        "input. If a field is null or an empty list, say there isn't enough data yet rather "
        "than guessing. Respond with ONLY a JSON object, no other text: "
        '{"headline": "<one sentence, under 20 words>", "body": "<2-4 plain-text sentences>"}'
    )
    try:
        result = chat_json(system_prompt, json.dumps(context, default=str))
    except NemotronUnavailable as exc:
        logger.warning("briefing_skipped", reason=str(exc))
        return None

    headline = str(result.get("headline") or "").strip()[:255]
    body = str(result.get("body") or "").strip()
    if not headline or not body:
        logger.warning("briefing_invalid_shape", result=result)
        return None

    return Insight.objects.create(
        month=month, headline=headline, body=body, model_used=settings.NEMOTRON_MODEL
    )


def generate_priority_calls(month: str | None = None, limit: int = 15) -> list[PriorityCall]:
    """Replaces a plain smallest-gap sort with reasoning over gap + trend +
    account growth together. Informational only — ops still decides who to
    actually call; this just orders the candidate list."""
    month = month or current_month()
    candidates = _candidate_rows(month, limit=50)
    if not candidates:
        return []

    system_prompt = (
        "You are prioritizing which CSPs (kiosk banking agents) an operations team should "
        "call first, to help them raise their average balance before a monthly incentive "
        "slab is assessed. You will be given a JSON list of candidate CSPs with real "
        "computed signals: gap (rupees needed to reach the next slab or the minimum), "
        "account_count, trend_flag (improving/stable/declining/new), mom_change_pct "
        f"(month-over-month balance change %), and net_new_accounts. Pick and rank the top "
        f"{limit} to call first, weighing ALL signals together — a declining CSP with a "
        "bigger gap may be more urgent than a stable one with a tiny gap. Use each CSP's "
        "exact csp_code from the input; never invent one not in the list. Respond with ONLY "
        'a JSON object, no other text: {"priority_calls": [{"csp_code": "...", "reason": '
        f'"<one sentence, under 25 words, referencing the actual signals>"}}, ...]}}, ordered '
        f"highest priority first, at most {limit} entries."
    )
    try:
        # 8192, not 2048: this is a reasoning-tuned model that spends a
        # visible chain-of-thought budget *before* the final JSON — with a
        # 50-candidate input, 2048 was observed truncating mid-reasoning
        # (finish_reason="length", no JSON ever emitted). Confirmed fixed
        # at 8192 against the real API, not just guessed.
        result = chat_json(
            system_prompt, json.dumps({"candidates": candidates}, default=str), max_tokens=8192
        )
    except NemotronUnavailable as exc:
        logger.warning("priority_calls_skipped", reason=str(exc))
        return []

    entries = result.get("priority_calls")
    if not isinstance(entries, list):
        logger.warning("priority_calls_invalid_shape", result=result)
        return []

    signals_by_code = {c["csp_code"]: c for c in candidates}
    created = []
    rank = 0
    for entry in entries:
        if rank >= limit or not isinstance(entry, dict):
            continue
        code = str(entry.get("csp_code") or "")
        if code not in signals_by_code:
            continue  # never trust a hallucinated CSP code
        reason = str(entry.get("reason") or "").strip()[:500]
        if not reason:
            continue
        rank += 1
        created.append(
            PriorityCall(
                month=month, csp_id=code, rank=rank, reason=reason,
                signals_used=signals_by_code[code],
            )
        )

    PriorityCall.objects.filter(month=month).delete()
    PriorityCall.objects.bulk_create(created)
    return created


def flag_ingestion_anomalies(ingest_log: IngestLog) -> list[AnomalyFlag]:
    """Reviews one just-finished ingestion run for signs of a data-entry or
    pipeline error worth a human look. Purely observational — never
    blocks, retries, or modifies the run it reviews."""
    growth = get_all_csps_account_growth()
    biggest_moves = sorted(
        ((code, g) for code, g in growth.items() if g["net_new"] is not None),
        key=lambda pair: abs(pair[1]["net_new"]),
        reverse=True,
    )[:10]
    biggest_moves_payload = [
        {
            "csp_code": code,
            "date": str(g["date"]),
            "account_count": g["account_count"],
            "net_new": g["net_new"],
        }
        for code, g in biggest_moves
    ]

    context = {
        "source": ingest_log.source,
        "status": ingest_log.status,
        "rows_read": ingest_log.rows_read,
        "rows_valid": ingest_log.rows_valid,
        "rows_rejected": ingest_log.rows_rejected,
        "rows_upserted": ingest_log.rows_upserted,
        "error_summary": ingest_log.error_summary,
        "biggest_account_count_moves": biggest_moves_payload,
    }
    system_prompt = (
        "You are reviewing one data-ingestion run for a CSP banking network for signs of a "
        "possible data-entry or pipeline error worth a human look. You will be given the "
        "run's stats and the 10 biggest single-reading-to-reading account-count changes "
        "across all CSPs. Flag ONLY things that look like plausible errors (huge jumps, an "
        "unusually high rejection rate) — do not flag normal small variation, and return an "
        "empty list if nothing looks wrong. Respond with ONLY a JSON object, no other text: "
        '{"anomalies": [{"description": "<one sentence>", "severity": "low"|"medium"|"high"}'
        ", ...]}"
    )
    try:
        result = chat_json(system_prompt, json.dumps(context, default=str))
    except NemotronUnavailable as exc:
        logger.warning("anomaly_review_skipped", reason=str(exc), ingest_log_id=ingest_log.id)
        return []

    entries = result.get("anomalies")
    if not isinstance(entries, list):
        logger.warning("anomaly_review_invalid_shape", result=result)
        return []

    valid_severities = {c.value for c in AnomalyFlag.Severity}
    created = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        description = str(entry.get("description") or "").strip()
        if not description:
            continue
        severity = entry.get("severity")
        if severity not in valid_severities:
            severity = AnomalyFlag.Severity.LOW
        created.append(
            AnomalyFlag(
                ingest_log=ingest_log,
                severity=severity,
                description=description[:2000],
                raw_context=context,
            )
        )
    AnomalyFlag.objects.bulk_create(created)
    # Marks the run checked regardless of outcome — "reviewed, nothing
    # found" must be distinguishable from "not reviewed yet", or this
    # would re-check (and re-bill Nemotron for) the same clean run forever.
    ReviewedIngestRun.objects.get_or_create(ingest_log=ingest_log)
    return created


def draft_csp_nudges(month: str | None = None, limit: int = 10) -> list[DraftMessage]:
    """Drafts short nudge messages for CSPs close to a slab threshold —
    DRAFT ONLY, see models.DraftMessage's docstring. Reuses the current
    month's priority-call list as candidates when one exists, so the two
    features stay consistent about who's currently a priority."""
    month = month or current_month()
    priority = list(
        PriorityCall.objects.filter(month=month).select_related("csp").order_by("rank")[:limit]
    )
    if priority:
        candidates = [
            {
                "csp_code": pc.csp_id,
                "name": pc.csp.name,
                "slab": pc.signals_used.get("slab"),
                "gap": pc.signals_used.get("gap"),
                "reason": pc.reason,
            }
            for pc in priority
        ]
    else:
        candidates = _candidate_rows(month, limit=limit)
    if not candidates:
        return []

    system_prompt = (
        "You are drafting short SMS nudges (under 300 characters each) to CSPs (kiosk "
        "banking agents) who are close to reaching a higher monthly-average-balance "
        "incentive slab. You will be given real per-CSP data as JSON. Write one warm, "
        "specific, actionable message per CSP, referencing their actual gap in rupees — do "
        "not invent any number not present in the input. These are DRAFTS a human will "
        "review before sending; never write as if the message has already been sent. "
        'Respond with ONLY a JSON object, no other text: {"drafts": [{"csp_code": "...", '
        '"text": "..."}, ...]}'
    )
    try:
        # See generate_priority_calls' comment above — same reasoning-model
        # token-budget issue, same fix.
        result = chat_json(
            system_prompt, json.dumps({"candidates": candidates}, default=str), max_tokens=8192
        )
    except NemotronUnavailable as exc:
        logger.warning("draft_nudges_skipped", reason=str(exc))
        return []

    valid_codes = {c["csp_code"] for c in candidates}
    entries = result.get("drafts")
    if not isinstance(entries, list):
        logger.warning("draft_nudges_invalid_shape", result=result)
        return []

    created = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("csp_code") or "")
        if code not in valid_codes:
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        created.append(
            DraftMessage(csp_id=code, channel=DraftMessage.Channel.SMS, text=text[:2000])
        )
    DraftMessage.objects.bulk_create(created)
    return created


def approve_draft_message(message: DraftMessage, user) -> bool:
    """Moves one DraftMessage from DRAFT to APPROVED — the single-message
    version of admin.py's approve_drafts bulk action, used by the
    dashboard's Messaging page. Returns False (no-op) if the message
    wasn't in DRAFT status, so a stale page/double-click can't silently
    re-approve something already handled."""
    if message.status != DraftMessage.Status.DRAFT:
        return False
    message.status = DraftMessage.Status.APPROVED
    message.reviewed_at = timezone.now()
    message.reviewed_by = user
    message.save(update_fields=["status", "reviewed_at", "reviewed_by"])
    return True


def reject_draft_message(message: DraftMessage, user) -> bool:
    """Moves one DraftMessage to REJECTED, from any status except SENT —
    the single-message version of admin.py's reject_drafts bulk action."""
    if message.status == DraftMessage.Status.SENT:
        return False
    message.status = DraftMessage.Status.REJECTED
    message.reviewed_at = timezone.now()
    message.reviewed_by = user
    message.save(update_fields=["status", "reviewed_at", "reviewed_by"])
    return True
