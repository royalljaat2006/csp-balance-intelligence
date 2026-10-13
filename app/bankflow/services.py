"""
Read-side service layer for the bank-flow feature -- consumed by BOTH the
dashboard page and the API router (same "two front doors, one domain layer"
rule as csp.services). Read-only: nothing here writes.
"""

from __future__ import annotations

import datetime as dt
import decimal

from csp.models import Csp, DailyBalance
from django.conf import settings
from django.db.models import Count, Max, Min, Sum
from django.utils import timezone

from . import flows
from .evaluation import EvalConfig, _usable, build_datasets
from .ingest import variant_rule_confirmed
from .models import (
    CodeResolution,
    EvaluationRun,
    FlowAggregate,
    ImportRun,
    PredictionRecord,
    StatementFile,
    StatementReconciliation,
    StatementTransaction,
    UnresolvedFlowDaily,
)
from .targets import detect_refresh_events, load_observations, target_report

ZERO = decimal.Decimal("0")
STATUS_LABELS = {
    "NOT_EVALUATED": "NOT EVALUATED",
    "INSUFFICIENT_EVIDENCE": "INSUFFICIENT EVIDENCE",
    "EVALUATION_FAILED": "EVALUATION FAILED",
    "VALIDATION_PASSED": "VALIDATION PASSED",
}


def ingestion_summary() -> dict:
    files = StatementFile.objects
    latest = (
        files.filter(status=StatementFile.Status.IMPORTED)
        .order_by("-statement_end", "-imported_at")
        .first()
    )
    by_status = {r["status"]: r["n"] for r in files.values("status").annotate(n=Count("id"))}
    recon = {
        r["status"]: r["n"]
        for r in StatementReconciliation.objects.filter(
            statement__status=StatementFile.Status.IMPORTED
        )
        .values("status")
        .annotate(n=Count("id"))
    }
    tx = StatementTransaction.objects.aggregate(
        n=Count("id"),
        credit=Sum("credit"),
        debit=Sum("debit"),
        first=Min("txn_date"),
        last=Max("txn_date"),
    )
    run = ImportRun.objects.first()
    latest_recon = getattr(latest, "reconciliation", None) if latest else None
    return {
        "latest_statement": (
            {
                "filename": latest.filename,
                "statement_end": latest.statement_end,
                "imported_at": latest.imported_at,
                "rows": latest.row_count,
                "reconciliation": latest_recon.status if latest_recon else None,
                "header_gap": latest_recon.header_gap if latest_recon else None,
                "notes": latest_recon.notes if latest_recon else [],
            }
            if latest
            else None
        ),
        "files": {
            "imported": by_status.get("imported", 0),
            "quarantined": by_status.get("quarantined", 0),
            "failed": by_status.get("failed", 0),
        },
        "reconciliation_status_counts": recon,
        "transactions": {
            "count": tx["n"] or 0,
            "credit_amount": tx["credit"] or ZERO,
            "debit_amount": tx["debit"] or ZERO,
            "first_date": tx["first"],
            "last_date": tx["last"],
        },
        "last_import_run": (
            {
                "started_at": run.started_at,
                "seen": run.files_seen,
                "imported": run.files_imported,
                "duplicate": run.files_duplicate,
                "quarantined": run.files_quarantined,
                "failed": run.files_failed,
                "skipped": run.files_skipped,
            }
            if run
            else None
        ),
    }


def quarantined_files(limit: int = 20) -> list[dict]:
    return [
        {
            "filename": f.filename,
            "status": f.status,
            "reason": f.status_reason,
            "imported_at": f.imported_at,
        }
        for f in StatementFile.objects.exclude(status=StatementFile.Status.IMPORTED).order_by(
            "-imported_at"
        )[:limit]
    ]


CODE_FORMAT_RULE = (
    "A KO code is '1A85' + 4 digits (8 characters); anything after the 8th character is dropped "
    "(owner-confirmed 2026-10-13). Shorter, cut-off codes and other formats are never guessed."
)


def code_resolution_stats() -> dict:
    """Match rates by transaction COUNT and by AMOUNT (credit + debit), so a
    high row-match percentage cannot hide unmapped money."""
    rows = list(
        CodeResolution.objects.values("status").annotate(
            n=Count("id"),
            credit=Sum("transaction__credit"),
            debit=Sum("transaction__debit"),
        )
    )
    total_n = sum(r["n"] for r in rows) or 0
    total_amt = sum((r["credit"] or ZERO) + (r["debit"] or ZERO) for r in rows) or ZERO
    out = []
    for r in sorted(rows, key=lambda r: -r["n"]):
        amt = (r["credit"] or ZERO) + (r["debit"] or ZERO)
        out.append(
            {
                "status": r["status"],
                "label": dict(CodeResolution.Status.choices).get(r["status"], r["status"]),
                "count": r["n"],
                "count_share": (r["n"] / total_n) if total_n else None,
                "amount": amt,
                "amount_share": float(amt / total_amt) if total_amt else None,
            }
        )
    trimmed = CodeResolution.objects.filter(method="first_8_chars").aggregate(
        n=Count("id"), credit=Sum("transaction__credit"), debit=Sum("transaction__debit")
    )
    return {
        "total_transactions": total_n,
        "total_amount": total_amt,
        "by_status": out,
        "code_format_rule": CODE_FORMAT_RULE,
        "trimmed_to_8_chars": {
            "count": trimmed["n"],
            "amount": (trimmed["credit"] or ZERO) + (trimmed["debit"] or ZERO),
        },
        "variant_rule_confirmed": variant_rule_confirmed(),
        "variant_rule_confirmation_ref": (
            settings.BANKFLOW_VARIANT_RULE_CONFIRMATION_REF if variant_rule_confirmed() else ""
        ),
    }


def unresolved_by_day(limit_days: int = 14) -> list[dict]:
    return [
        {
            "date": u.flow_date,
            "status": u.status,
            "count": u.txn_count,
            "credit": u.credit_amount,
            "debit": u.debit_amount,
        }
        for u in UnresolvedFlowDaily.objects.order_by("-flow_date", "status")[: limit_days * 6]
    ]


def quality_flags() -> dict:
    tx = StatementTransaction.objects
    return {
        "duplicate_reference_rows": tx.filter(is_duplicate_ref=True).count(),
        "reversal_candidate_rows": tx.filter(is_reversal_candidate=True).count(),
        "unclassified_type_rows": tx.filter(category="unclassified").count(),
        "quarantined_files": StatementFile.objects.filter(
            status=StatementFile.Status.QUARANTINED
        ).count(),
        "overlap_rows_skipped": StatementReconciliation.objects.aggregate(n=Sum("overlap_rows"))[
            "n"
        ]
        or 0,
    }


def csp_flow_table(
    *,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    mode: str = flows.MODE_EXACT,
    definition: str = flows.DEFAULT_DEFINITION,
    limit: int = 50,
    offset: int = 0,
    order: str = "gross",
) -> tuple[list[dict], int]:
    qs = FlowAggregate.objects.filter(resolution_mode=mode)
    if date_from:
        qs = qs.filter(flow_date__gte=date_from)
    if date_to:
        qs = qs.filter(flow_date__lte=date_to)
    acc: dict[str, dict] = {}
    for a in qs.iterator():
        r = acc.setdefault(
            a.csp_id,
            {
                "csp_code": a.csp_id,
                "txn_count": 0,
                "credit": ZERO,
                "debit": ZERO,
                "signed": ZERO,
                "types": {},
            },
        )
        r["txn_count"] += a.txn_count
        r["credit"] += a.credit_amount
        r["debit"] += a.debit_amount
        r["signed"] += flows.signed_flow(a.by_category, definition)
        for k, v in a.by_type.items():
            r["types"][k] = r["types"].get(k, 0) + v["count"]
    keyf = {
        "gross": lambda r: r["credit"] + r["debit"],
        "net": lambda r: abs(r["signed"]),
        "count": lambda r: r["txn_count"],
    }.get(order, lambda r: r["credit"] + r["debit"])
    ordered = sorted(acc.values(), key=keyf, reverse=True)
    total = len(ordered)
    page = ordered[offset : offset + limit]

    codes = [r["csp_code"] for r in page]
    names = dict(Csp.objects.filter(csp_code__in=codes).values_list("csp_code", "name"))
    latest_bal: dict[str, tuple] = {}
    for b in (
        DailyBalance.objects.filter(csp_id__in=codes)
        .order_by("csp_id", "balance_date")
        .values("csp_id", "balance_date", "daily_avg_balance", "account_count")
    ):
        prev = latest_bal.get(b["csp_id"])
        latest_bal[b["csp_id"]] = (b, prev[0] if prev else None)
    preds = {}
    run = latest_run(EvaluationRun.Target.AVG_CHANGE)
    if run:
        for p in PredictionRecord.objects.filter(run=run, csp_id__in=codes).order_by("event_date"):
            preds[p.csp_id] = p
    out = []
    for r in page:
        b, prev = latest_bal.get(r["csp_code"], (None, None))
        pred = preds.get(r["csp_code"])
        out.append(
            {
                **r,
                "name": names.get(r["csp_code"], ""),
                "gross": r["credit"] + r["debit"],
                "top_types": sorted(r["types"].items(), key=lambda kv: -kv[1])[:3],
                "latest_avg_balance": b["daily_avg_balance"] if b else None,
                "latest_balance_date": b["balance_date"] if b else None,
                "account_count": b["account_count"] if b else None,
                "account_count_change": (
                    b["account_count"] - prev["account_count"]
                    if b
                    and prev
                    and b["account_count"] is not None
                    and prev["account_count"] is not None
                    else None
                ),
                "prediction": (
                    {
                        "event_date": pred.event_date,
                        "horizon_days": pred.horizon_days,
                        "observed": pred.observed_new,
                        "predicted_model": pred.predicted_model,
                        "predicted_baseline": pred.predicted_baseline,
                        # the run these figures come from: a prediction from a run that did
                        # not pass validation is shown for audit only, never as a forecast.
                        "run_status": run.status if run else None,
                    }
                    if pred
                    else None
                ),
            }
        )
    return out, total


def latest_run(target: str) -> EvaluationRun | None:
    return EvaluationRun.objects.filter(target=target).first()


def evaluation_overview() -> list[dict]:
    out = []
    for target, label in EvaluationRun.Target.choices:
        run = latest_run(target)
        out.append(
            {
                "target": target,
                "label": label,
                "status": run.status if run else "NOT_EVALUATED",
                "status_label": STATUS_LABELS[run.status if run else "NOT_EVALUATED"],
                "detail": run.outcome_detail if run else "No evaluation has been run.",
                "n_events": run.n_events if run else 0,
                "n_test_events": run.n_test_events if run else 0,
                "n_observations": run.n_observations if run else 0,
                "train_period": run.train_period if run else "",
                "test_period": run.test_period if run else "",
                "metrics": run.metrics if run else {},
                "config": run.config if run else {},
                "notes": run.notes if run else [],
                "created_at": run.created_at if run else None,
                "model": f"{run.model.name} v{run.model.version}" if run and run.model else "",
            }
        )
    return out


def target_overview() -> dict:
    return target_report()


def coverage_report(min_events: int = 8) -> dict:
    """Is there enough history to evaluate? Everything here is measured, nothing
    assumed: statement date coverage and gaps, ledger-continuity-proven days,
    overlap between hourly exports, independent refresh events and how many of
    them actually have usable flow data, plus attribution coverage by count/amount."""
    # Days covered = every day from a statement's first to last ledger row (a day with no
    # postings inside a continuous statement is still covered), not just its start date.
    covered: set[dt.date] = set()
    imported = list(
        StatementFile.objects.filter(
            status=StatementFile.Status.IMPORTED, first_row_date__isnull=False
        ).values_list("first_row_date", "last_row_date")
    )
    for first, last in imported:
        if first is None or last is None:
            continue
        day = first
        while day <= last:
            covered.add(day)
            day += dt.timedelta(days=1)
    statement_dates = sorted(covered)
    missing: list[dt.date] = []
    if statement_dates:
        day = statement_dates[0]
        while day < statement_dates[-1]:
            if day not in covered:
                missing.append(day)
            day += dt.timedelta(days=1)
    complete = flows.proven_complete_dates()
    cov = day_coverage()

    obs = load_observations()
    events = detect_refresh_events(obs)
    cfg = EvalConfig()
    datasets = build_datasets(obs, events, cfg, complete_days=complete)
    usable = sum(1 for e in datasets if _usable(e, cfg))

    # Refreshes happen at ANY moment, per CSP: no refresh dates are assumed. A refresh is a
    # change in one CSP's value between two readings; evidence is counted in such changes and in
    # calendar-month cycles (CSPs refreshing on different days of one month are one cycle).
    from . import model_comparison
    from .account_hypothesis import (
        MIN_CYCLES_VALIDATED,
        MIN_PAIRS_VALIDATED,
        load_series,
        pairs_from_series,
    )

    pairs = pairs_from_series(load_series())
    basis = model_comparison.compare(pairs)["basis"]
    cycles = len({(p.event_date.year, p.event_date.month) for p in pairs})
    flow_ready = (
        basis["pairs_with_flow_coverage"] >= MIN_PAIRS_VALIDATED
        and cycles >= MIN_CYCLES_VALIDATED
    )

    recon = StatementReconciliation.objects.aggregate(
        overlap=Sum("overlap_rows"), conflicts=Sum("conflicting_rows")
    )
    stats = code_resolution_stats()
    exact = next((r for r in stats["by_status"] if r["status"] == "exact"), None)
    return {
        "statement_files": len(imported),
        "days_covered": len(statement_dates),
        "missing_date_ranges": _ranges(missing),
        "first_statement_date": statement_dates[0] if statement_dates else None,
        "last_statement_date": statement_dates[-1] if statement_dates else None,
        "proven_complete_days": len(complete),
        "complete_days": sum(1 for v in cov.values() if v["class"] == COMPLETE_DAY),
        "partial_days": sum(1 for v in cov.values() if v["class"] == PARTIAL_DAY),
        "unverified_days": sum(1 for v in cov.values() if v["class"] == UNVERIFIED_DAY),
        "unverified_date_ranges": _ranges(
            sorted(d for d, v in cov.items() if v["class"] == UNVERIFIED_DAY)
        ),
        "partial_snapshots": partial_snapshot_report(),
        "overlap_rows_skipped": recon["overlap"] or 0,
        "conflicting_rows": recon["conflicts"] or 0,
        "calling_sheet_observation_dates": len(obs),
        "refresh_events_detected": len(events),
        "refresh_events_with_usable_flow_data": usable,
        "events_required": min_events,
        "events_still_needed": max(0, min_events - usable),
        "attribution_exact_share_by_count": exact["count_share"] if exact else None,
        "attribution_exact_share_by_amount": exact["amount_share"] if exact else None,
        "refresh_note": (
            "No refresh dates are assumed: a refresh is any change in a CSP's value between two "
            "readings, at any moment."
        ),
        "csp_value_changes_observed": len(pairs),
        "refresh_cycles_months": cycles,
        "changes_with_flow_coverage": basis["pairs_with_flow_coverage"],
        "validation_needs": {
            "changes_with_flow_coverage": MIN_PAIRS_VALIDATED,
            "monthly_cycles": MIN_CYCLES_VALIDATED,
        },
        "ready_for_evaluation": usable >= min_events or flow_ready,
    }


def _ranges(days: list[dt.date]) -> list[str]:
    """[2026-09-01, 09-02, 09-03, 09-10] -> ['2026-09-01..2026-09-03', '2026-09-10']"""
    out: list[str] = []
    start: dt.date | None = None
    prev: dt.date | None = None
    for d in days:
        if start is None or prev is None:
            start = prev = d
        elif (d - prev).days == 1:
            prev = d
        else:
            out.append(f"{start}..{prev}" if prev != start else f"{start}")
            start = prev = d
    if start is not None:
        out.append(f"{start}..{prev}" if prev != start else f"{start}")
    return out


COMPLETE_DAY = "COMPLETE_DAY"
PARTIAL_DAY = "PARTIAL_DAY"
UNVERIFIED_DAY = "UNVERIFIED"


def day_coverage() -> dict[dt.date, dict]:
    """Classify every statement day. Never treats a partial day as a daily total.

    COMPLETE_DAY  proven by ledger continuity (flows.proven_complete_dates).
    PARTIAL_DAY   the newest export of that day was taken ON that day and the day is not
                  proven complete: a day-to-date snapshot. Its coverage runs from the start
                  of the day (the statement's opening balance) up to AT MOST the export
                  timestamp -- ledger rows carry dates only, so the true last-transaction
                  time is not known.
    UNVERIFIED    rows exist but neither proof applies (e.g. exported after the day ended
                  but the next statement is missing, so completeness cannot be shown).
    """
    complete = flows.proven_complete_dates()
    info: dict[dt.date, dict] = {}
    for f in StatementFile.objects.filter(
        status=StatementFile.Status.IMPORTED, first_row_date__isnull=False
    ).order_by("id"):
        if f.last_row_date is None or f.first_row_date is None:
            continue
        export_local = timezone.localtime(f.file_modified_at) if f.file_modified_at else None
        day = f.first_row_date
        while day <= f.last_row_date:
            e = info.setdefault(day, {"files": [], "latest_export": None})
            e["files"].append(f.filename)
            if day == f.last_row_date and export_local is not None:
                if e["latest_export"] is None or export_local > e["latest_export"]:
                    e["latest_export"] = export_local
            day += dt.timedelta(days=1)
    row_counts = {
        r["txn_date"]: r["n"]
        for r in StatementTransaction.objects.values("txn_date").annotate(n=Count("id"))
    }
    tz = timezone.get_current_timezone()
    for day, e in info.items():
        exp = e["latest_export"]
        e["rows"] = row_counts.get(day, 0)
        if day in complete:
            e["class"], e["reason"] = COMPLETE_DAY, "ledger continuity proven"
        elif exp is not None and exp.date() == day:
            e["class"] = PARTIAL_DAY
            e["reason"] = "day-to-date snapshot: exported on the same day"
            e["coverage_start"] = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
            e["coverage_end_at_most"] = exp
            e["coverage_hours"] = round((exp - e["coverage_start"]).total_seconds() / 3600, 1)
        else:
            e["class"] = UNVERIFIED_DAY
            e["reason"] = (
                "no export timestamp"
                if exp is None
                else "exported after the day ended; no continuity proof (next statement missing)"
            )
    return info


def partial_snapshot_report() -> list[dict]:
    """Day-to-date snapshots analysed on their own. Only quantities the data supports
    are reported (rows, pool credits/debits, attribution), each with its coverage window.
    Snapshots are NOT comparable across days unless their coverage is similar, so each
    carries `cut_hour` for like-with-like grouping; no per-day total is implied."""
    out = []
    for day, e in sorted(day_coverage().items()):
        if e["class"] != PARTIAL_DAY:
            continue
        agg = StatementTransaction.objects.filter(txn_date=day).aggregate(
            credit=Sum("credit"), debit=Sum("debit")
        )
        exact = CodeResolution.objects.filter(transaction__txn_date=day, status="exact").count()
        out.append(
            {
                "date": day,
                "rows": e["rows"],
                "exported_at": e["latest_export"],
                "coverage_start": e["coverage_start"],
                "coverage_end_at_most": e["coverage_end_at_most"],
                "coverage_hours": e["coverage_hours"],
                "cut_hour": e["latest_export"].hour,
                "pool_credit": agg["credit"] or ZERO,
                "pool_debit": agg["debit"] or ZERO,
                "exact_attributed_rows": exact,
                "files": e["files"],
            }
        )
    return out
