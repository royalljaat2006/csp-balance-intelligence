"""
Server-rendered dashboard views — staff-login-gated (Phase: "full web app").

Deliberately reads through the exact same `csp.services` functions the
public API uses (api/v1/*.py) rather than duplicating query logic — the API
and this UI are two consumers of one domain layer, per the architecture
established in docs/ARCHITECTURE.md.

No API key involved anywhere here: this UI renders server-side with the
request's own Django session, so no credential ever reaches the browser.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json

from autopilot import monitoring as agent_monitoring_service
from autopilot.models import (
    AgentAction,
    AgentFinding,
    AgentTask,
    AgentVerification,
    AnomalyFlag,
    DraftMessage,
    Insight,
    PriorityCall,
)
from autopilot.services import approve_draft_message, reject_draft_message
from autopilot.tasks import enqueue_agent_run
from common.cache import cache_aside
from csp import comparison, services
from csp.models import Csp, DailyBalance, IngestLog, MonthlySummary
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.management import call_command
from django.db import connection
from django.db.models import Max, Min
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import pipeline_state
from .pipeline_sse import stream_pipeline_events


def _decimal_to_float(value):
    return float(value) if isinstance(value, decimal.Decimal) else value


def _parse_date(value: str | None) -> dt.date | None:
    """Best-effort ISO date parse for query params — bad/missing input just
    means "no filter" rather than a 500 or a raw-SQL surprise."""
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _paginate(request, *, page_size: int = 25) -> tuple[int, int, int]:
    """Shared ?page= handling (Phase 11 productization) — same convention
    `transactions()` already used, extracted so the new list pages below
    don't each reimplement it. Returns (page, offset, page_size)."""
    try:
        page = max(1, int(request.GET.get("page", "1")))
    except ValueError:
        page = 1
    return page, (page - 1) * page_size, page_size


@login_required
def api_docs(request):
    """Embeds django-ninja's own Swagger UI (/api/v1/docs) in an iframe so
    it reads as part of the app instead of a disconnected page — see
    config/settings/base.py X_FRAME_OPTIONS (SAMEORIGIN, deliberately).

    Staff-only: this is an operational/integration surface (how to call the
    API), not a monitoring view — non-staff accounts get the read-only
    dashboard (Overview/Trends/CSP detail) without it. See admin_embed
    below for the same access-control pattern."""
    if not request.user.is_staff:
        return HttpResponseForbidden("API docs are restricted to staff accounts.")
    return render(request, "dashboard/api_docs.html")


@login_required
def admin_embed(request):
    """Embeds Django's own /admin/ in an iframe — same treatment as
    api_docs above — so staff never leave the app's own chrome/nav.
    Gated on is_staff: Django's admin already enforces its own permission
    checks inside the iframe, but a non-staff user landing on a blank
    "you don't have permission" frame is a worse experience than just
    not offering the link/route at all."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Admin access is restricted to staff accounts.")
    return render(request, "dashboard/admin_embed.html")


_QUICK_ACTION_COMMANDS = {
    "ingest_calling_sheet": "ingest_calling_sheet",
    "ingest_transactions": "ingest_transactions",
    "sync_daily_snapshots": "sync_daily_snapshots",
    "sync_monthly_summary": "sync_monthly_summary",
    "run_autopilot": "run_autopilot",
}


@login_required
def system_health(request):
    """Admin / System Health landing page — real, computed status only
    (no fake "all green" dashboard). Quick actions are a fixed whitelist
    of already-existing, idempotent management commands (never an
    arbitrary command string from the request) — same safety property
    Django admin's own bulk actions already have. Staff-only: these are
    privileged operational triggers, not monitoring."""
    if not request.user.is_staff:
        return HttpResponseForbidden("System Health is restricted to staff accounts.")

    action_result = None
    if request.method == "POST":
        action = request.POST.get("action")
        command = _QUICK_ACTION_COMMANDS.get(action)
        if command:
            try:
                call_command(command)
                action_result = f"'{command}' ran successfully."
            except Exception as exc:  # noqa: BLE001 — surface any failure to the operator, don't crash the page
                action_result = f"'{command}' failed: {exc}"
        return render(
            request,
            "dashboard/system_health.html",
            _system_health_context(action_result=action_result),
        )

    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        db_ok = False

    return render(
        request, "dashboard/system_health.html", _system_health_context(db_ok=db_ok)
    )


def _system_health_context(*, db_ok: bool = True, action_result: str | None = None) -> dict:
    freshness = services.get_data_freshness()
    bounds = services.get_daily_activity_date_bounds()
    balance_dates = DailyBalance.objects.aggregate(
        earliest=Min("balance_date"), latest=Max("balance_date")
    )
    balance_bounds = (
        (balance_dates["earliest"], balance_dates["latest"]) if balance_dates["earliest"] else None
    )

    recent_failed = list(
        IngestLog.objects.filter(status=IngestLog.Status.FAILED).order_by("-started_at")[:10]
    )
    recent_logs = list(IngestLog.objects.order_by("-started_at")[:15])

    config_status = {
        "Google Sheets (CALLING SHEET)": bool(
            settings.GOOGLE_SERVICE_ACCOUNT_FILE and settings.CALLING_SHEET_ID
        ),
        "Nemotron AI": bool(settings.NEMOTRON_API_KEY),
        "Telegram ingestion": bool(
            settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_ALLOWED_CHAT_ID
        ),
    }

    return {
        "db_ok": db_ok,
        "freshness": freshness,
        "activity_bounds": bounds,
        "balance_bounds": balance_bounds,
        "recent_failed": recent_failed,
        "recent_logs": recent_logs,
        "config_status": config_status,
        "action_result": action_result,
        "quick_actions": list(_QUICK_ACTION_COMMANDS.keys()),
    }


@login_required
def pipeline(request):
    """Data Pipeline — live visualization of real ingestion/agent execution
    state (Phase: live agentic pipeline, 2026-09-21). The page itself just
    renders the current snapshot for the first paint; everything after that
    updates via the SSE stream (pipeline_events) without a reload. No new
    job-tracking system — pipeline_state.py reads the exact same
    IngestLog/AgentRun/AgentFinding/AgentAction/AgentVerification rows and
    lock keys the rest of the app already writes."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Data Pipeline is restricted to staff accounts.")

    snapshot = pipeline_state.get_snapshot()
    return render(
        request,
        "dashboard/pipeline.html",
        {
            "initial_snapshot_json": json.dumps(snapshot),
            "initial_timeline_json": json.dumps(pipeline_state.get_activity_timeline()),
        },
    )


@login_required
def pipeline_events(request):
    """GET /dashboard/pipeline/events — the SSE stream. See
    dashboard/pipeline_sse.py for why it's a short, auto-reconnecting
    stream rather than one held open indefinitely."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Data Pipeline is restricted to staff accounts.")
    return stream_pipeline_events(request)


@login_required
def pipeline_history(request):
    """GET /dashboard/pipeline/history — real past executions (History
    mode). JSON only; the page's own JS renders it."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Data Pipeline is restricted to staff accounts.")
    node_key = request.GET.get("node") or None
    page, _, page_size = _paginate(request)
    return JsonResponse(pipeline_state.get_history(node_key, page=page, page_size=page_size))


@login_required
def pipeline_node_detail(request, node_key: str):
    """GET /dashboard/pipeline/node/<key> — real detail for one node,
    for the click-to-expand panel."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Data Pipeline is restricted to staff accounts.")
    return JsonResponse(pipeline_state.get_node_detail(node_key))


@login_required
def messaging(request):
    """Nemotron-drafted CSP nudges (autopilot.models.DraftMessage), with an
    Approve/Reject action per message — the dashboard-native counterpart to
    DraftMessageAdmin's bulk actions, same underlying service functions
    (autopilot.services.approve_draft_message/reject_draft_message) so the
    two surfaces can never enforce different rules about what "approved"
    means. Staff-only: same reasoning as api_docs/admin_embed above — this
    is an operational surface, not read-only monitoring."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Messaging is restricted to staff accounts.")

    if request.method == "POST":
        message = get_object_or_404(DraftMessage, pk=request.POST.get("message_id"))
        action = request.POST.get("action")
        if action == "approve":
            approve_draft_message(message, request.user)
        elif action == "reject":
            reject_draft_message(message, request.user)
        return redirect(request.get_full_path())

    status_filter = request.GET.get("status", "")
    messages_qs = DraftMessage.objects.select_related("csp", "reviewed_by").order_by(
        "-generated_at"
    )
    if status_filter:
        messages_qs = messages_qs.filter(status=status_filter)

    status_rows = [
        (value, label, DraftMessage.objects.filter(status=value).count())
        for value, label in DraftMessage.Status.choices
    ]

    return render(
        request,
        "dashboard/messaging.html",
        {
            "messages": messages_qs[:200],
            "status_filter": status_filter,
            "status_rows": status_rows,
            "total_count": DraftMessage.objects.count(),
        },
    )


@login_required
def ai_operations(request):
    """AI Operations — a dedicated read-only view over Nemotron's four
    existing outputs (Insight, PriorityCall, AnomalyFlag, DraftMessage).
    Consolidates what was previously scattered across Overview's AI
    briefing panel and the Admin console; reads the exact same tables,
    never a second computation of any of them. Staff-only, same reasoning
    as Messaging: this is an operational surface over AI-generated content,
    not read-only monitoring for everyone."""
    if not request.user.is_staff:
        return HttpResponseForbidden("AI Operations is restricted to staff accounts.")

    agent_run_result = None
    if request.method == "POST" and request.POST.get("action") == "run_agent":
        agent_task = enqueue_agent_run("Manual run from AI Operations")
        if agent_task is not None:
            # No queue configured — ran synchronously, same as before.
            agent_run_result = (
                f"Task #{agent_task.pk}: {agent_task.get_status_display()} — "
                f"{agent_task.final_status}"
            )
        else:
            agent_run_result = (
                "Queued — the agent-worker process will pick this run up shortly. "
                "Refresh this page or check Agent Monitoring for progress."
            )

    month = services.current_month()
    latest_insight = Insight.objects.filter(month=month).first()
    priority_calls = list(
        PriorityCall.objects.filter(month=month).select_related("csp").order_by("rank")[:15]
    )
    unreviewed_anomalies = list(
        AnomalyFlag.objects.filter(reviewed_at__isnull=True)
        .select_related("ingest_log")
        .order_by("-generated_at")[:15]
    )
    recent_drafts = list(
        DraftMessage.objects.select_related("csp").order_by("-generated_at")[:10]
    )

    # "Last run" / "model" — the most recent generation across all four
    # outputs; only Insight records model_used today (PriorityCall/
    # AnomalyFlag/DraftMessage don't have that field), so that's the one
    # source for "model information" — never fabricated for the others.
    last_run_candidates = [
        latest_insight.generated_at if latest_insight else None,
        priority_calls[0].generated_at if priority_calls else None,
        unreviewed_anomalies[0].generated_at if unreviewed_anomalies else None,
        recent_drafts[0].generated_at if recent_drafts else None,
    ]
    last_run_at = max((t for t in last_run_candidates if t is not None), default=None)

    # CSP Operations Agent (multi-agent) — real execution records only, see
    # autopilot/agent.py (coordinator) and autopilot/agents.py (specialists).
    latest_agent_task = AgentTask.objects.order_by("-created_at").first()
    recent_agent_tasks = list(AgentTask.objects.order_by("-created_at")[:10])
    pending_agent_actions = list(
        AgentAction.objects.filter(status=AgentAction.Status.PENDING_APPROVAL)
        .select_related("csp", "run")
        .order_by("-created_at")[:15]
    )
    agent_timeline = _agent_timeline(latest_agent_task) if latest_agent_task else []
    latest_action_agent_run = (
        latest_agent_task.runs.filter(agent_name="action_agent").first()
        if latest_agent_task
        else None
    )

    return render(
        request,
        "dashboard/ai_operations.html",
        {
            "month": month,
            "latest_insight": latest_insight,
            "priority_calls": priority_calls,
            "unreviewed_anomalies": unreviewed_anomalies,
            "unreviewed_anomaly_total": AnomalyFlag.objects.filter(
                reviewed_at__isnull=True
            ).count(),
            "recent_drafts": recent_drafts,
            "draft_total": DraftMessage.objects.filter(status=DraftMessage.Status.DRAFT).count(),
            "last_run_at": last_run_at,
            "model_used": latest_insight.model_used if latest_insight else None,
            "agent_run_result": agent_run_result,
            "latest_agent_task": latest_agent_task,
            "recent_agent_tasks": recent_agent_tasks,
            "pending_agent_actions": pending_agent_actions,
            "agent_timeline": agent_timeline,
            "latest_action_agent_run": latest_action_agent_run,
        },
    )


def _agent_timeline(task: AgentTask) -> list[dict]:
    """Builds a chronological activity timeline from one AgentTask's real
    child records (specialist runs + findings + verifications + actions +
    start/finish) — every entry here is a real timestamped database row,
    never a fabricated narrative."""
    events: list[tuple[dt.datetime, str]] = [
        (task.created_at, f"Coordinator started: {task.description}")
    ]
    for run in task.runs.select_related().order_by("started_at"):
        events.append((run.started_at, f"{run.agent_name} started"))
        if run.finished_at:
            events.append(
                (
                    run.finished_at,
                    f"{run.agent_name} {run.get_status_display()}: {run.final_status}",
                )
            )
    for finding in task.findings.select_related("csp").order_by("created_at"):
        events.append(
            (
                finding.created_at,
                f"{finding.source_agent} created finding: {finding.finding_type} "
                f"for {finding.csp_id or 'network'}",
            )
        )
    verifications = AgentVerification.objects.filter(finding__task=task).order_by("verified_at")
    for verification in verifications:
        events.append(
            (
                verification.verified_at,
                f"Verification Agent: {verification.get_result_display()} "
                f"({verification.finding.csp_id})",
            )
        )
    for action in AgentAction.objects.filter(run__task_fk=task).order_by("created_at"):
        events.append(
            (
                action.created_at,
                f"Action Agent: {action.get_action_type_display()} for {action.csp_id}",
            )
        )
    if task.finished_at:
        events.append((task.finished_at, f"Task {task.get_status_display()}: {task.final_status}"))
    events.sort(key=lambda e: e[0])
    return [{"time": t, "text": text} for t, text in events]


@login_required
def agent_monitoring(request):
    """Agent Monitoring — health/activity/performance/failures for the CSP
    Operations Agent, built entirely from autopilot.monitoring's real
    aggregates over AgentRun/AgentAction/AgentToolCall. A first-class page,
    not a section bolted onto AI Operations: AI Operations shows what the
    agent produced, this page shows whether the agent itself is working.
    Staff-only, same reasoning as AI Operations/Messaging."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Agent Monitoring is restricted to staff accounts.")

    kpis = agent_monitoring_service.get_agent_kpis()
    heatmap = agent_monitoring_service.get_agent_heatmap(days=14)
    tool_usage = agent_monitoring_service.get_tool_usage()
    failed_runs = agent_monitoring_service.get_failed_runs()
    approval_metrics = agent_monitoring_service.get_approval_metrics()

    return render(
        request,
        "dashboard/agent_monitoring.html",
        {
            "kpis": kpis,
            "heatmap": heatmap,
            "tool_usage": tool_usage,
            "failed_runs": failed_runs,
            "approval_metrics": approval_metrics,
        },
    )


@login_required
def risk(request):
    """Risk — network-wide at-risk CSPs (Phase 11 productization). Reuses
    comparison.bulk_compare_csps()/at_risk_csps(), the exact same canonical
    check the API's /comparisons/at-risk endpoint and the multi-agent Risk
    Agent both use — never a second "is this CSP at risk" computation."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Risk is restricted to staff accounts.")

    mode = request.GET.get("mode", "overall")
    if mode not in ("overall", "onus"):
        mode = "overall"
    today = timezone.localdate()
    yesterday = comparison.resolve_comparison_date(today)
    comparisons = comparison.bulk_compare_csps(today, yesterday, mode=mode)
    comparison_by_code = {c.csp_code: c for c in comparisons}
    flags = comparison.at_risk_csps(comparisons)

    rows = [
        {
            "csp_code": f.csp_code,
            "csp_name": f.csp_name,
            "reasons": f.reasons,
            "comparison": _comparison_row(comparison_by_code[f.csp_code])
            if f.csp_code in comparison_by_code
            else None,
        }
        for f in flags
    ]
    return render(
        request,
        "dashboard/risk.html",
        {"mode": mode, "rows": rows, "total_flagged": len(rows), "total_csps": len(comparisons)},
    )


@login_required
def agent_findings(request):
    """Agent Findings — every structured claim a specialist agent has
    handed off, with its verification outcome (Phase 11 productization).
    Read-only: findings are never editable here, only inspectable — the
    same AgentFinding rows Agent Monitoring's timeline already draws from."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Agent Findings is restricted to staff accounts.")

    status_filter = request.GET.get("status", "")
    qs = AgentFinding.objects.select_related("csp", "task").order_by("-created_at")
    if status_filter:
        qs = qs.filter(verification_status=status_filter)
    page, offset, page_size = _paginate(request)
    total = qs.count()
    findings = list(qs[offset : offset + page_size])

    return render(
        request,
        "dashboard/agent_findings.html",
        {
            "findings": findings,
            "status_filter": status_filter,
            "status_choices": AgentFinding.VerificationStatus.choices,
            "page": page,
            "total_pages": max(1, -(-total // page_size)),
            "total": total,
        },
    )


@login_required
def verification(request):
    """Verification — the Verification Agent's independent re-checks of
    every finding (Phase 11 productization). This is what makes a finding
    trustworthy: it was re-derived from the same canonical service call,
    not just asserted by the agent that first produced it."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Verification is restricted to staff accounts.")

    result_filter = request.GET.get("result", "")
    qs = AgentVerification.objects.select_related("finding__csp", "finding__task").order_by(
        "-verified_at"
    )
    if result_filter:
        qs = qs.filter(result=result_filter)
    page, offset, page_size = _paginate(request)
    total = qs.count()
    verifications = list(qs[offset : offset + page_size])

    return render(
        request,
        "dashboard/verification.html",
        {
            "verifications": verifications,
            "result_filter": result_filter,
            "result_choices": AgentVerification.Result.choices,
            "page": page,
            "total_pages": max(1, -(-total // page_size)),
            "total": total,
        },
    )


@login_required
def actions(request):
    """Actions — every AgentAction an agent has proposed/executed (Phase 11
    productization). Read-only inspection; the one existing, real approval
    control (DraftMessage approve/reject) still lives only on Messaging —
    this page never adds a second, competing approval path."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Actions is restricted to staff accounts.")

    status_filter = request.GET.get("status", "")
    qs = AgentAction.objects.select_related("csp", "run", "finding", "draft_message").order_by(
        "-created_at"
    )
    if status_filter:
        qs = qs.filter(status=status_filter)
    page, offset, page_size = _paginate(request)
    total = qs.count()
    action_rows = list(qs[offset : offset + page_size])

    return render(
        request,
        "dashboard/actions.html",
        {
            "action_rows": action_rows,
            "status_filter": status_filter,
            "status_choices": AgentAction.Status.choices,
            "page": page,
            "total_pages": max(1, -(-total // page_size)),
            "total": total,
        },
    )


@login_required
def approvals(request):
    """Approvals — one consolidated queue of everything awaiting human
    sign-off (2026-10-06 IA split, was: a link into Messaging). The ONLY
    real approve/reject control in the system is DraftMessage's — reused
    verbatim (same autopilot.services calls Messaging itself uses, so the
    two surfaces can never enforce different rules). Other pending
    AgentActions (review_flag/escalation/followup_task/investigation) are
    shown read-only: no approve/reject mechanism exists for them anywhere
    in this codebase, and this page does not invent one — that would be a
    new, unreviewed capability, not a UI consolidation."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Approvals is restricted to staff accounts.")

    if request.method == "POST":
        message = get_object_or_404(DraftMessage, pk=request.POST.get("message_id"))
        action = request.POST.get("action")
        if action == "approve":
            approve_draft_message(message, request.user)
        elif action == "reject":
            reject_draft_message(message, request.user)
        return redirect(request.get_full_path())

    pending_drafts = list(
        DraftMessage.objects.filter(status=DraftMessage.Status.DRAFT)
        .select_related("csp")
        .order_by("-generated_at")
    )
    other_pending_actions = list(
        AgentAction.objects.filter(status=AgentAction.Status.PENDING_APPROVAL)
        .exclude(action_type=AgentAction.ActionType.MESSAGE_DRAFT)
        .select_related("csp", "run")
        .order_by("-created_at")
    )
    return render(
        request,
        "dashboard/approvals.html",
        {
            "pending_drafts": pending_drafts,
            "other_pending_actions": other_pending_actions,
        },
    )


@login_required
def ai_recommendations(request):
    """AI Recommendations — Nemotron's narrative outputs on their own page
    (2026-10-06 IA split from the broader AI Operations landing, which
    still shows the same data alongside the multi-agent task view — kept
    there too, per "don't remove functionality without evidence"). Every
    row here is real: Insight/PriorityCall/DraftMessage, never re-derived."""
    if not request.user.is_staff:
        return HttpResponseForbidden("AI Recommendations is restricted to staff accounts.")

    month = services.current_month()
    latest_insight = Insight.objects.filter(month=month).first()
    priority_calls = list(
        PriorityCall.objects.filter(month=month).select_related("csp").order_by("rank")[:20]
    )
    recent_drafts = list(
        DraftMessage.objects.select_related("csp").order_by("-generated_at")[:20]
    )
    return render(
        request,
        "dashboard/ai_recommendations.html",
        {
            "month": month,
            "latest_insight": latest_insight,
            "priority_calls": priority_calls,
            "recent_drafts": recent_drafts,
        },
    )


@login_required
def audit(request):
    """Audit — the real ingestion audit trail as its own page (2026-10-06
    IA split, was: a link into the Django admin). Straight read of
    IngestLog, the exact table every ingestion command already writes to;
    agent-side audit trails (findings/actions/verification) have their own
    dedicated pages, linked below rather than duplicated here."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Audit is restricted to staff accounts.")

    source_filter = request.GET.get("source", "")
    status_filter = request.GET.get("status", "")
    qs = IngestLog.objects.order_by("-started_at")
    if source_filter:
        qs = qs.filter(source=source_filter)
    if status_filter:
        qs = qs.filter(status=status_filter)
    page, offset, page_size = _paginate(request)
    total = qs.count()
    rows = list(qs[offset : offset + page_size])

    return render(
        request,
        "dashboard/audit.html",
        {
            "rows": rows,
            "source_filter": source_filter,
            "status_filter": status_filter,
            "status_choices": IngestLog.Status.choices,
            "sources": list(
                IngestLog.objects.values_list("source", flat=True).distinct().order_by("source")
            ),
            "page": page,
            "total_pages": max(1, -(-total // page_size)),
            "total": total,
        },
    )


def _csp_row(ms: MonthlySummary, growth_by_csp: dict[str, dict] | None = None) -> dict:
    csp = ms.csp
    accounts = csp.account_count or 0
    per_account_gap: decimal.Decimal | None
    if ms.slab == MonthlySummary.Slab.NIL:
        per_account_gap = ms.gap_to_min
    else:
        per_account_gap = ms.gap_to_next_slab
    amount_to_deposit = (
        float(per_account_gap) * accounts if per_account_gap is not None and accounts else None
    )
    growth = (growth_by_csp or {}).get(csp.csp_code)
    return {
        "csp_code": csp.csp_code,
        "name": csp.name,
        "mobile": csp.mobile,
        "account_count": accounts,
        "mtd_mab": _decimal_to_float(ms.mtd_mab),
        "slab": ms.slab,
        "incentive_rate_pa": _decimal_to_float(ms.incentive_rate_pa),
        "gap_to_min": _decimal_to_float(ms.gap_to_min),
        "gap_to_next_slab": _decimal_to_float(ms.gap_to_next_slab),
        "amount_to_deposit": amount_to_deposit,
        "projected_mab": _decimal_to_float(ms.projected_mab),
        "trend_flag": ms.trend_flag,
        "trend_7d_pct": _decimal_to_float(ms.trend_7d_pct),
        "mom_change_pct": _decimal_to_float(ms.mom_change_pct),
        "is_eligible": ms.is_eligible,
        "net_new_accounts": growth["net_new"] if growth else None,
    }


def _comparison_row(c: comparison.CspComparison) -> dict:
    """Flattens a CspComparison (csp/comparison.py's own dataclass) into the
    flat dict the CSP Daily Comparison table/JS need — field extraction
    only, no computation: every value here was already computed by the
    comparison engine, never re-derived in the view or in JS."""
    return {
        "csp_code": c.csp_code,
        "csp_name": c.csp_name,
        "current_value": _decimal_to_float(c.balance.current_value),
        "previous_value": _decimal_to_float(c.balance.previous_value),
        "abs_change": _decimal_to_float(c.balance.abs_change),
        "pct_change": _decimal_to_float(c.balance.pct_change),
        "trend": c.balance.trend,
        "movement": c.balance.movement,
        "current_slab": c.current_slab,
        "previous_slab": c.previous_slab,
        "slab_moved": c.slab_movement.moved,
        "slab_upgraded": c.slab_movement.upgraded,
        "slab_downgraded": c.slab_movement.downgraded,
        "mtd_avg_so_far": _decimal_to_float(c.mtd_avg_so_far),
        "gap_to_min": _decimal_to_float(c.gap_to_min),
        "gap_to_next_slab": _decimal_to_float(c.gap_to_next_slab),
        "txn_count_current": _decimal_to_float(c.txn_count.current_value),
        "txn_amount_current": _decimal_to_float(c.txn_amount.current_value),
        "current_data_status": c.current_data_status,
        "comparison_data_status": c.comparison_data_status,
    }


@login_required
def csp_comparisons(request):
    """The CSP Daily Comparison tab — csp/comparison.py's bulk_compare_csps()
    is the ONLY place these numbers are computed; this view only resolves
    query params into a (mode, current_date, comparison_date) triple (via
    comparison.resolve_comparison_date(), the same function the API uses)
    and flattens the result for the template/client-side table."""
    mode = request.GET.get("mode", "overall")
    if mode not in ("overall", "onus"):
        mode = "overall"

    current_date = _parse_date(request.GET.get("current_date")) or timezone.localdate()

    comparison_type = request.GET.get("comparison_type", comparison.COMPARISON_TYPE_YESTERDAY)
    if comparison_type not in (
        comparison.COMPARISON_TYPE_YESTERDAY,
        comparison.COMPARISON_TYPE_SAME_DATE_PREVIOUS_MONTH,
        comparison.COMPARISON_TYPE_CUSTOM,
    ):
        comparison_type = comparison.COMPARISON_TYPE_YESTERDAY

    explicit_comparison_date = None
    if comparison_type == comparison.COMPARISON_TYPE_CUSTOM:
        explicit_comparison_date = _parse_date(request.GET.get("comparison_date"))

    resolved_comparison_date = comparison.resolve_comparison_date(
        current_date,
        comparison_type=comparison_type,
        explicit_comparison_date=explicit_comparison_date,
    )

    comparisons = comparison.bulk_compare_csps(current_date, resolved_comparison_date, mode=mode)

    trend_counts = {"GROWTH": 0, "DECLINE": 0, "NO_CHANGE": 0, "NO_DATA": 0}
    for c in comparisons:
        trend_counts[c.balance.trend] = trend_counts.get(c.balance.trend, 0) + 1

    # Summary KPIs — aggregating an already-computed comparison set, same
    # pattern home()/balance_intelligence() already use (never a second
    # comparison computation).
    pct_changes = [c.balance.pct_change for c in comparisons if c.balance.pct_change is not None]
    abs_changes = [c.balance.abs_change for c in comparisons if c.balance.abs_change is not None]
    avg_network_change_pct = (
        (sum(pct_changes, decimal.Decimal("0")) / len(pct_changes)).quantize(
            decimal.Decimal("0.01")
        )
        if pct_changes
        else None
    )
    total_balance_change = sum(abs_changes, decimal.Decimal("0")) if abs_changes else None
    largest_decline = min(abs_changes, default=None) if abs_changes else None

    top_growth = comparison.top_movers(comparisons, by="pct", direction="growth", limit=8)
    top_decline = comparison.top_movers(comparisons, by="pct", direction="decline", limit=8)
    at_risk_flags = comparison.at_risk_csps(comparisons)

    return render(
        request,
        "dashboard/csp_comparisons.html",
        {
            "mode": mode,
            "current_date": current_date.isoformat(),
            "comparison_type": comparison_type,
            "comparison_date": (
                resolved_comparison_date.isoformat() if resolved_comparison_date else ""
            ),
            "custom_comparison_date": request.GET.get("comparison_date", ""),
            "rows": [_comparison_row(c) for c in comparisons],
            "trend_counts": trend_counts,
            "total_csps": len(comparisons),
            "avg_network_change_pct": avg_network_change_pct,
            "total_balance_change": _decimal_to_float(total_balance_change),
            "largest_decline": _decimal_to_float(largest_decline),
            "top_growth": [_comparison_row(c) for c in top_growth],
            "top_decline": [_comparison_row(c) for c in top_decline],
            "at_risk_flags": [
                {"csp_code": f.csp_code, "csp_name": f.csp_name, "reasons": f.reasons}
                for f in at_risk_flags
            ],
        },
    )


@login_required
def balance_intelligence(request):
    """Deep balance analytics — same comparison engine as the Daily
    Comparison tab (csp.comparison.bulk_compare_csps/summarize_by_slab/
    top_movers), same network trend service as Overview
    (services.get_network_balance_trend), just sliced differently: by
    window (7/30/60D) and grouped by slab/movement rather than listed
    per-CSP. No new business logic — this view only aggregates what the
    domain layer already computed, the same pattern home() already uses."""
    mode = request.GET.get("mode", "overall")
    if mode not in ("overall", "onus"):
        mode = "overall"

    try:
        window_days = int(request.GET.get("window", "30"))
    except ValueError:
        window_days = 30
    if window_days not in (7, 30, 60):
        window_days = 30

    month = services.current_month()
    today = timezone.localdate()
    yesterday = comparison.resolve_comparison_date(today)
    daily_comparisons = comparison.bulk_compare_csps(today, yesterday, mode=mode)

    slab_summaries = comparison.summarize_by_slab(daily_comparisons)

    movement_counts = dict.fromkeys(
        [
            comparison.MovementBucket.STRONG_GROWTH,
            comparison.MovementBucket.MODERATE_GROWTH,
            comparison.MovementBucket.STABLE,
            comparison.MovementBucket.MODERATE_DECLINE,
            comparison.MovementBucket.SHARP_DECLINE,
            comparison.MovementBucket.NO_DATA,
        ],
        0,
    )
    for c in daily_comparisons:
        movement_counts[c.balance.movement] = movement_counts.get(c.balance.movement, 0) + 1

    slab_upgrades = sum(1 for c in daily_comparisons if c.slab_movement.upgraded)
    slab_downgrades = sum(1 for c in daily_comparisons if c.slab_movement.downgraded)

    near_next_slab_threshold = decimal.Decimal("500")
    near_next_slab = sorted(
        (
            c
            for c in daily_comparisons
            if c.gap_to_next_slab is not None and 0 < c.gap_to_next_slab <= near_next_slab_threshold
        ),
        key=lambda c: c.gap_to_next_slab or decimal.Decimal("0"),
    )[:15]
    below_minimum = [c for c in daily_comparisons if c.current_slab == "NIL"][:15]

    largest_increases = comparison.top_movers(
        daily_comparisons, by="abs", direction="growth", limit=10
    )
    largest_decreases = comparison.top_movers(
        daily_comparisons, by="abs", direction="decline", limit=10
    )

    # Month-over-month — aggregates MonthlySummary's own already-computed
    # mom_change_pct (dbt mart output), never recomputed here.
    summaries, _ = services.list_monthly_summaries(month=month, limit=100000)
    mom_values = [s.mom_change_pct for s in summaries if s.mom_change_pct is not None]
    avg_mom_pct = (
        (sum(mom_values, decimal.Decimal("0")) / len(mom_values)).quantize(decimal.Decimal("0.01"))
        if mom_values
        else None
    )

    network_balance_series = [
        {"date": str(r["date"]), "value": r["avg_balance"]}
        for r in cache_aside(
            f"network_balance_trend:{window_days}",
            ttl=30,
            fn=lambda: services.get_network_balance_trend(days=window_days),
        )
    ]
    overview = cache_aside(f"overview:{month}", ttl=30, fn=lambda: services.get_overview(month))

    return render(
        request,
        "dashboard/balance_intelligence.html",
        {
            "mode": mode,
            "window_days": window_days,
            "window_hint": f"trailing {window_days} days",
            "mode_hint": f"today, {mode}",
            "near_slab_hint": f"within Rs.{near_next_slab_threshold} of the next slab",
            "overview": overview,
            "network_balance_series": network_balance_series,
            "daily_trend_counts": {
                trend: sum(1 for c in daily_comparisons if c.balance.trend == trend)
                for trend in ("GROWTH", "DECLINE", "NO_CHANGE", "NO_DATA")
            },
            "movement_counts": movement_counts,
            "slab_summaries": slab_summaries,
            "slab_upgrades": slab_upgrades,
            "slab_downgrades": slab_downgrades,
            "avg_mom_pct": avg_mom_pct,
            "near_next_slab": [_comparison_row(c) for c in near_next_slab],
            "near_next_slab_threshold": near_next_slab_threshold,
            "below_minimum": [_comparison_row(c) for c in below_minimum],
            "largest_increases": [_comparison_row(c) for c in largest_increases],
            "largest_decreases": [_comparison_row(c) for c in largest_decreases],
        },
    )


@login_required
def home(request):
    month = request.GET.get("month") or services.current_month()
    overview = cache_aside(f"overview:{month}", ttl=30, fn=lambda: services.get_overview(month))
    summaries, _ = services.list_monthly_summaries(month=month, limit=100000)
    growth_by_csp = cache_aside(
        "all_csps_account_growth", ttl=30, fn=services.get_all_csps_account_growth
    )
    csp_rows = [_csp_row(ms, growth_by_csp) for ms in summaries]

    quick_wins = sorted(
        (r for r in csp_rows if r["slab"] != "S4" and (r["amount_to_deposit"] or 0) > 0),
        key=lambda r: r["amount_to_deposit"],
    )[:8]

    # Autopilot (Nemotron) output — all read-only narration, none of it
    # required for the page to work: empty/missing just means autopilot
    # hasn't run yet or NEMOTRON_API_KEY isn't configured (see
    # autopilot/nemotron_client.py's degrade-gracefully contract).
    latest_insight = Insight.objects.filter(month=month).first()
    priority_calls = list(
        PriorityCall.objects.filter(month=month).select_related("csp").order_by("rank")[:10]
    )
    # unreviewed_anomaly_count comes from dashboard.context_processors.dashboard_shell
    # now (every page's notification bell needs it, not just Overview).

    # Today-vs-yesterday, network-wide — same comparison engine as the Daily
    # Comparison tab/API, one bulk_compare_csps() call (N+1-free across
    # every CSP), scoped down to what an Overview KPI strip needs.
    today = timezone.localdate()
    yesterday = comparison.resolve_comparison_date(today)
    daily_comparisons = comparison.bulk_compare_csps(today, yesterday, mode="overall")
    daily_trend_counts = {"GROWTH": 0, "DECLINE": 0, "NO_CHANGE": 0, "NO_DATA": 0}
    for c in daily_comparisons:
        daily_trend_counts[c.balance.trend] = daily_trend_counts.get(c.balance.trend, 0) + 1
    reporting_today = sum(1 for c in daily_comparisons if c.balance.current_value is not None)
    top_growth_today = comparison.top_movers(
        daily_comparisons, by="pct", direction="growth", limit=5
    )
    top_decline_today = comparison.top_movers(
        daily_comparisons, by="pct", direction="decline", limit=5
    )
    slab_summaries = comparison.summarize_by_slab(daily_comparisons)

    # Network health — real counts only, every threshold named so a reader
    # can see exactly what "near next slab" means rather than an opaque flag.
    near_next_slab_threshold = decimal.Decimal("500")
    near_next_slab_count = sum(
        1
        for r in csp_rows
        if r["gap_to_next_slab"] is not None
        and 0 < r["gap_to_next_slab"] <= near_next_slab_threshold
    )
    significant_decline_count = sum(
        1
        for c in daily_comparisons
        if c.balance.movement == comparison.MovementBucket.SHARP_DECLINE
    )
    reporting_coverage_pct = (
        round(reporting_today / overview["csp_total"] * 100, 1) if overview["csp_total"] else None
    )

    network_balance_series = [
        {"date": str(r["date"]), "value": r["avg_balance"]}
        for r in cache_aside(
            "network_balance_trend:30",
            ttl=30,
            fn=lambda: services.get_network_balance_trend(days=30),
        )
    ]

    return render(
        request,
        "dashboard/home.html",
        {
            "month": month,
            "overview": overview,
            "csp_rows": csp_rows,
            "quick_wins": quick_wins,
            "reporting_today": reporting_today,
            "daily_trend_counts": daily_trend_counts,
            "top_growth_today": [_comparison_row(c) for c in top_growth_today],
            "top_decline_today": [_comparison_row(c) for c in top_decline_today],
            "slab_summaries": slab_summaries,
            "near_next_slab_count": near_next_slab_count,
            "near_next_slab_threshold": near_next_slab_threshold,
            "significant_decline_count": significant_decline_count,
            "reporting_coverage_pct": reporting_coverage_pct,
            "network_balance_series": network_balance_series,
            "total_accounts": sum(r["account_count"] for r in csp_rows),
            "total_balance": sum(
                r["mtd_mab"] * r["account_count"] for r in csp_rows if r["account_count"]
            ),
            "latest_insight": latest_insight,
            "priority_calls": priority_calls,
        },
    )


@login_required
def csp_directory(request):
    """Browse every CSP — merges three already-computed sources (never a
    fourth computation of its own): `Csp` for identity,
    `comparison.bulk_compare_csps()` for today-vs-yesterday balance/slab
    (the same call Daily Comparison and Overview make), and
    `MonthlySummary` for the monthly average. A CSP with no comparison
    data yet (never ingested) still gets a row — reporting_status makes
    that visible rather than hiding it."""
    mode = request.GET.get("mode", "overall")
    if mode not in ("overall", "onus"):
        mode = "overall"

    month = services.current_month()
    today = timezone.localdate()
    yesterday = comparison.resolve_comparison_date(today)
    daily_comparisons = comparison.bulk_compare_csps(today, yesterday, mode=mode)
    comparison_by_code = {c.csp_code: c for c in daily_comparisons}

    summaries = MonthlySummary.objects.filter(month=month).select_related("csp")
    summary_by_code = {s.csp_id: s for s in summaries}

    rows = []
    for csp in Csp.objects.tracked():
        c = comparison_by_code.get(csp.csp_code)
        summary = summary_by_code.get(csp.csp_code)
        rows.append(
            {
                "csp_code": csp.csp_code,
                "name": csp.name,
                "mobile": csp.mobile,
                "account_count": csp.account_count,
                "current_balance": _decimal_to_float(c.balance.current_value) if c else None,
                "previous_balance": _decimal_to_float(c.balance.previous_value) if c else None,
                "mtd_mab": _decimal_to_float(summary.mtd_mab) if summary else None,
                "pct_change": _decimal_to_float(c.balance.pct_change) if c else None,
                "trend": c.balance.trend if c else "NO_DATA",
                "slab": (c.current_slab if c and c.current_slab else None)
                or (summary.slab if summary else None),
                "txn_count_today": _decimal_to_float(c.txn_count.current_value) if c else None,
                "reporting_status": (
                    "reporting"
                    if c and c.balance.current_value is not None
                    else "not_reporting"
                ),
                "data_status": c.current_data_status if c else "missing",
                "last_sync": csp.last_seen_date.isoformat() if csp.last_seen_date else None,
            }
        )

    return render(
        request,
        "dashboard/csp_directory.html",
        {
            "mode": mode,
            "rows": rows,
            "total_csps": len(rows),
        },
    )


@login_required
def csp_detail(request, csp_code: str):
    try:
        csp = Csp.objects.get(pk=csp_code)
    except Csp.DoesNotExist as exc:
        raise Http404(f"No CSP with code {csp_code}") from exc

    month = request.GET.get("month") or services.current_month()
    mode = request.GET.get("mode", "overall")
    if mode not in ("overall", "onus"):
        mode = "overall"
    summary = services.get_monthly_summary(csp_code, month)
    current_balance = services.get_current_balance(csp)
    balance_history = list(services.get_balance_history(csp))
    activity = services.get_csp_daily_activity(csp_code)[-60:]
    account_growth = services.get_csp_account_growth(csp)
    recent_txns, txn_total = services.list_transactions(csp, limit=15)

    # Daily comparison + trend intelligence — same csp.comparison engine the
    # Daily Comparison tab and the API use, scoped to just this one CSP
    # (today vs yesterday is the fixed, always-useful default here; the
    # dashboard tab is where a different date pair can be chosen). `mode`
    # is a query param so the same ONUS/Overall toggle used everywhere
    # else works here too — always visible, never silently assumed.
    today = timezone.localdate()
    yesterday = comparison.resolve_comparison_date(today)
    daily_comparison = comparison.compare_csp_metrics(csp_code, today, yesterday, mode=mode)
    trend_intel = comparison.get_trend_intelligence(csp_code, today)
    slab_history = comparison.get_csp_slab_history(csp_code, days=60)
    slab_history_series = [
        {"date": str(s.business_date), "value": float(s.mtd_avg_so_far)} for s in slab_history
    ]

    activity_series = [{"date": str(a["activity_date"]), "value": a["txn_count"]} for a in activity]
    balance_series = [
        {"date": str(b.balance_date), "value": float(b.daily_avg_balance)} for b in balance_history
    ]
    account_growth_series = [
        {"date": str(g["date"]), "value": g["account_count"]} for g in account_growth
    ]
    latest_net_new = account_growth[-1]["net_new"] if account_growth else None

    row = _csp_row(summary) if summary else None

    # CSP 360°: this CSP's own agent findings/actions/verification history
    # (Phase 11 productization) — same AgentFinding/AgentAction/
    # AgentVerification rows Agent Findings/Actions/Verification already
    # list network-wide, just filtered to this csp_code. No new business
    # logic: a straight filtered read of existing models.
    agent_findings_for_csp = list(
        AgentFinding.objects.filter(csp=csp).order_by("-created_at")[:10]
    )
    agent_actions_for_csp = list(
        AgentAction.objects.filter(csp=csp)
        .select_related("finding", "run")
        .order_by("-created_at")[:10]
    )

    return render(
        request,
        "dashboard/csp_detail.html",
        {
            "csp": csp,
            "row": row,
            "summary": summary,
            "current_balance": current_balance,
            "activity_series": activity_series,
            "balance_series": balance_series,
            "account_growth_series": account_growth_series,
            "latest_net_new": latest_net_new,
            "recent_txns": recent_txns,
            "txn_total": txn_total,
            "month": month,
            "mode": mode,
            "daily_comparison": daily_comparison,
            "trend_intel": trend_intel,
            "slab_history": slab_history,
            "slab_history_series": slab_history_series,
            "agent_findings_for_csp": agent_findings_for_csp,
            "agent_actions_for_csp": agent_actions_for_csp,
        },
    )


@login_required
def transactions(request):
    """Transaction Intelligence + Telegram/ingestion monitoring — every
    number here reads real ledger/audit tables (Transaction, IngestLog)
    via csp.services, never a second transaction-counting engine. The
    transaction table itself is server-paginated (never all rows loaded
    client-side — a real month's ledger is ~300k rows).

    The headline KPI cards anchor on the LATEST date the daily_activity
    mart actually has, not the wall-clock date — this ledger arrives as an
    end-of-day batch file (Telegram/folder-watch), so "today" would read
    as empty for most of every day until that file lands. Anchoring on the
    latest real date instead means the cards always show the most recent
    complete day's real numbers; the template labels the actual date
    rather than hardcoding the word "today", so this is never a mislabel."""
    bounds = services.get_daily_activity_date_bounds()
    latest_date = bounds[1] if bounds else timezone.localdate()
    previous_date = latest_date - dt.timedelta(days=1)

    activity_by_date = {
        r["activity_date"]: r
        for r in services.get_network_daily_trend(date_from=previous_date, date_to=latest_date)
    }
    today_activity = activity_by_date.get(latest_date)
    yesterday_activity = activity_by_date.get(previous_date)

    def _metric(row, key):
        return row[key] if row else None

    active_csps_today = services.get_active_transaction_csp_count(latest_date)
    type_distribution = services.get_transaction_type_distribution(
        date_from=previous_date, date_to=latest_date
    )

    activity_series_30d = services.get_network_daily_trend(
        date_from=latest_date - dt.timedelta(days=29), date_to=latest_date
    )

    # Ingestion health — real IngestLog rows, both delivery channels.
    recent_logs = list(
        IngestLog.objects.filter(source__in=("transactions", "telegram")).order_by(
            "-started_at"
        )[:10]
    )
    last_telegram = (
        IngestLog.objects.filter(source="telegram", status=IngestLog.Status.SUCCESS)
        .order_by("-started_at")
        .first()
    )
    last_transactions_run = (
        IngestLog.objects.filter(source__in=("transactions", "telegram"))
        .order_by("-started_at")
        .first()
    )

    # Server-side pagination — never load the full ledger client-side.
    try:
        page = max(1, int(request.GET.get("page", "1")))
    except ValueError:
        page = 1
    page_size = 25
    onus_only = request.GET.get("mode") == "onus"
    txn_rows, txn_total = services.list_transactions(
        date_from=_parse_date(request.GET.get("date_from")),
        date_to=_parse_date(request.GET.get("date_to")),
        onus_only=onus_only,
        limit=page_size,
        offset=(page - 1) * page_size,
    )

    return render(
        request,
        "dashboard/transactions.html",
        {
            "today": latest_date,
            "yesterday": previous_date,
            "is_wall_clock_today": latest_date == timezone.localdate(),
            "type_distribution_hint": (
                "today + yesterday, allow-listed types only"
                if latest_date == timezone.localdate()
                else f"{previous_date:%d %b} + {latest_date:%d %b}, allow-listed types only"
            ),
            "txn_count_today": _metric(today_activity, "txn_count"),
            "txn_count_yesterday": _metric(yesterday_activity, "txn_count"),
            "txn_amount_today": _metric(today_activity, "txn_amount"),
            "txn_amount_yesterday": _metric(yesterday_activity, "txn_amount"),
            "onus_txn_count_today": _metric(today_activity, "onus_txn_count"),
            "active_csps_today": active_csps_today,
            "type_distribution": type_distribution,
            "type_distribution_json": [
                {
                    "txn_type": d["txn_type"],
                    "count": d["count"],
                    "amount": _decimal_to_float(d["amount"]),
                }
                for d in type_distribution
            ],
            "activity_series": [
                {"date": str(r["activity_date"]), "value": r["txn_count"]}
                for r in activity_series_30d
            ],
            "onus_activity_series": [
                {"date": str(r["activity_date"]), "value": r["onus_txn_count"]}
                for r in activity_series_30d
            ],
            "recent_logs": recent_logs,
            "last_telegram": last_telegram,
            "last_transactions_run": last_transactions_run,
            "txn_rows": txn_rows,
            "txn_total": txn_total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, -(-txn_total // page_size)),
            "onus_only": onus_only,
            "date_from": request.GET.get("date_from", ""),
            "date_to": request.GET.get("date_to", ""),
        },
    )


@login_required
def trends(request):
    bounds = services.get_daily_activity_date_bounds()
    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))

    # No explicit range in the URL: default to the full available ledger
    # rather than "today minus N days" — the transaction export is a fixed
    # historical range (e.g. one month), not a rolling live feed.
    if date_from is None and date_to is None and bounds:
        date_from, date_to = bounds

    network_activity = services.get_network_daily_trend(date_from=date_from, date_to=date_to)

    def series(key, cast=float):
        return [{"date": str(a["activity_date"]), "value": cast(a[key])} for a in network_activity]

    net_flow_series = series("net_flow")
    withdrawal_count_series = series("withdrawal_count", int)
    deposit_count_series = series("deposit_count", int)
    withdrawal_amount_series = series("withdrawal_amount")
    deposit_amount_series = series("deposit_amount")
    overall_txn_count_series = series("txn_count", int)
    # onus_txn_count is summed the same way get_network_daily_trend() itself
    # sums it ("row[col] or 0") — a NULL only occurs for pre-ONUS-extension
    # mart rows, never for a real row the same dbt run also populated
    # txn_count for, so treating it as 0 here is consistent, not a
    # fabricated "no activity" claim.
    onus_txn_count_series = [
        {"date": str(a["activity_date"]), "value": int(a["onus_txn_count"] or 0)}
        for a in network_activity
    ]

    kpis = {
        "txn_count": sum(a["txn_count"] for a in network_activity),
        "txn_amount": float(sum(a["txn_amount"] for a in network_activity)),
        "net_flow": float(sum(a["net_flow"] for a in network_activity)),
        "withdrawal_amount": float(sum(a["withdrawal_amount"] for a in network_activity)),
        "deposit_amount": float(sum(a["deposit_amount"] for a in network_activity)),
        "onus_txn_count": sum(a["onus_txn_count"] or 0 for a in network_activity),
    }

    month = services.current_month()
    overview = cache_aside(f"overview:{month}", ttl=30, fn=lambda: services.get_overview(month))
    summaries, _ = services.list_monthly_summaries(month=month, limit=100000)
    trend_counts = {"improving": 0, "declining": 0, "stable": 0}
    for ms in summaries:
        trend_counts[ms.trend_flag or "stable"] = trend_counts.get(ms.trend_flag or "stable", 0) + 1

    account_growth = cache_aside(
        "network_account_growth", ttl=30, fn=services.get_network_account_growth
    )
    account_growth_series = [
        {"date": str(g["date"]), "value": g["total_accounts"]} for g in account_growth
    ]
    latest_account_growth = account_growth[-1] if account_growth else None

    # Today-vs-yesterday transaction comparison — anchored on the latest
    # real date the transaction mart actually has (`bounds`, same anchor
    # views.transactions() already uses for its own headline cards), not
    # wall-clock today. The transaction workbook is downloaded/updated once
    # a day and is a full day behind by the time it's ingested, so
    # wall-clock "today" almost never has any real transaction row yet —
    # this isn't a rolling live feed the way the Calling Sheet is.
    txn_today = bounds[1] if bounds else timezone.localdate()
    txn_yesterday = txn_today - dt.timedelta(days=1)
    today_yesterday = {
        a["activity_date"]: a
        for a in services.get_network_daily_trend(date_from=txn_yesterday, date_to=txn_today)
    }
    today_activity = today_yesterday.get(txn_today)
    yesterday_activity = today_yesterday.get(txn_yesterday)

    def _network_metric_comparison(key):
        current = today_activity[key] if today_activity else None
        previous = yesterday_activity[key] if yesterday_activity else None
        return comparison.ComparisonResult(
            txn_today,
            txn_yesterday,
            decimal.Decimal(str(current)) if current is not None else None,
            decimal.Decimal(str(previous)) if previous is not None else None,
        )

    txn_count_comparison = _network_metric_comparison("txn_count")
    txn_amount_comparison = _network_metric_comparison("txn_amount")

    # CSP heatmap — balance-based (comparison.bulk_compare_csps reads
    # DailyBalance, which the Calling Sheet keeps genuinely live via a
    # ~60s poll), so this one correctly uses real wall-clock today, same
    # canonical comparison home()/csp_directory() already use.
    today = timezone.localdate()
    heatmap_comparison_date = comparison.resolve_comparison_date(today)
    heatmap_comparisons = comparison.bulk_compare_csps(
        today, heatmap_comparison_date, mode="overall"
    )
    heatmap_rows = sorted(
        (_comparison_row(c) for c in heatmap_comparisons), key=lambda r: r["csp_code"]
    )

    return render(
        request,
        "dashboard/trends.html",
        {
            "withdrawal_count_series": withdrawal_count_series,
            "deposit_count_series": deposit_count_series,
            "withdrawal_amount_series": withdrawal_amount_series,
            "deposit_amount_series": deposit_amount_series,
            "overall_txn_count_series": overall_txn_count_series,
            "onus_txn_count_series": onus_txn_count_series,
            "txn_count_comparison": txn_count_comparison,
            "txn_amount_comparison": txn_amount_comparison,
            "net_flow_series": net_flow_series,
            "kpis": kpis,
            "has_activity_data": bool(network_activity),
            "activity_days": len(network_activity),
            "trend_counts": trend_counts,
            "slab_distribution": overview["slab_distribution"],
            "csp_with_data": len(summaries),
            "month": month,
            "min_date": bounds[0].isoformat() if bounds else "",
            "max_date": bounds[1].isoformat() if bounds else "",
            "selected_from": date_from.isoformat() if date_from else "",
            "selected_to": date_to.isoformat() if date_to else "",
            "account_growth_series": account_growth_series,
            "latest_account_growth": latest_account_growth,
            "heatmap_rows": heatmap_rows,
            "heatmap_current_date": today,
            "heatmap_comparison_date": heatmap_comparison_date,
        },
    )


@login_required
def csp_activity_json(request, csp_code: str):
    """Small JSON endpoint for the detail page's chart — session-authenticated,
    not the public API (no X-API-Key involved)."""
    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))
    activity = services.get_csp_daily_activity(csp_code, date_from=date_from, date_to=date_to)
    if date_from is None and date_to is None:
        activity = activity[-60:]
    payload = [{"date": str(a["activity_date"]), "txn_count": a["txn_count"]} for a in activity]
    return JsonResponse({"activity": payload})
