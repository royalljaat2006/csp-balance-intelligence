"""
CSP Operations Agent — tool layer.

TRUST BOUNDARY (see agent.py's module docstring for the full picture):
every function in this file returns facts computed by the existing,
already-trusted domain layer (csp.services / csp.comparison) or performs a
narrowly-scoped write the same way the dashboard/admin already does. No
function here asks an LLM anything and no function here computes a
business number itself — it only reads/writes through the same services
every other consumer (API, dashboard, autopilot) already uses.

Each tool optionally takes `run: AgentRun | None` and logs an
AgentToolCall when given one — this is the audit trail behind "which
tools did the agent use" and the Agent Monitoring page's tool-usage table.
A tool's return value is the trusted fact; nothing downstream (including
Nemotron) is allowed to overwrite it.
"""

from __future__ import annotations

import datetime as dt
import decimal

import structlog
from csp import comparison
from csp.models import Csp
from csp.services import current_month, get_csp_daily_activity, get_data_freshness, get_overview
from django.utils import timezone

from .models import AgentAction, AgentRun, AgentToolCall, DraftMessage

logger = structlog.get_logger("autopilot.agent")


def _log(run: AgentRun | None, tool_name: str, success: bool, summary: str = "") -> None:
    if run is None:
        return
    AgentToolCall.objects.create(
        run=run, tool_name=tool_name, success=success, summary=summary[:255]
    )


def _to_float(value: decimal.Decimal | float | int | None) -> float | None:
    return float(value) if value is not None else None


def _row(c: comparison.CspComparison) -> dict:
    """Flattens a CspComparison the same way dashboard/views.py's
    _comparison_row() does — field extraction only, no computation. Kept as
    a small local copy rather than importing from the dashboard app, so the
    agent's tool layer has no dependency on the presentation layer."""
    return {
        "csp_code": c.csp_code,
        "csp_name": c.csp_name,
        "current_value": _to_float(c.balance.current_value),
        "previous_value": _to_float(c.balance.previous_value),
        "abs_change": _to_float(c.balance.abs_change),
        "pct_change": _to_float(c.balance.pct_change),
        "trend": c.balance.trend,
        "movement": c.balance.movement,
        "current_slab": c.current_slab,
        "previous_slab": c.previous_slab,
        "slab_downgraded": c.slab_movement.downgraded,
        "current_data_status": c.current_data_status,
    }


def get_network_snapshot(run: AgentRun | None = None) -> dict:
    """Real, already-computed network-wide numbers — the agent's starting
    point for every run. Never recomputes anything get_overview()/
    get_data_freshness() don't already provide."""
    try:
        month = current_month()
        overview = get_overview(month)
        freshness = get_data_freshness()
        today = timezone.localdate()
        comparison_date = comparison.resolve_comparison_date(today)
        comparisons = comparison.bulk_compare_csps(today, comparison_date, mode="overall")
        trend_counts: dict[str, int] = {}
        for c in comparisons:
            trend_counts[c.balance.trend] = trend_counts.get(c.balance.trend, 0) + 1
        result = {
            "month": month,
            "current_date": str(today),
            "comparison_date": str(comparison_date) if comparison_date else None,
            "csp_total": overview["csp_total"],
            "csp_with_data": overview["csp_with_data"],
            "avg_mab": overview["avg_mab"],
            "slab_distribution": overview["slab_distribution"],
            "in_nil_count": overview["in_nil_count"],
            "freshness_status": freshness["overall_status"],
            "trend_counts": trend_counts,
        }
        _log(run, "get_network_snapshot", True, f"{overview['csp_with_data']} CSPs with data")
        return result
    except Exception as exc:  # noqa: BLE001 — a tool failure is data, never a crash
        logger.exception("tool_failed", tool="get_network_snapshot")
        _log(run, "get_network_snapshot", False, str(exc)[:255])
        raise


def get_top_movers(
    *, direction: str = "decline", limit: int = 10, run: AgentRun | None = None
) -> list[dict]:
    """Real day-over-day movers, straight from csp.comparison.top_movers() —
    the same function Balance Intelligence uses. `direction` is "growth" or
    "decline"."""
    try:
        today = timezone.localdate()
        comparison_date = comparison.resolve_comparison_date(today)
        comparisons = comparison.bulk_compare_csps(today, comparison_date, mode="overall")
        movers = comparison.top_movers(comparisons, direction=direction, limit=limit)
        rows = [_row(c) for c in movers]
        _log(run, "get_top_movers", True, f"{len(rows)} {direction} movers")
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.exception("tool_failed", tool="get_top_movers")
        _log(run, "get_top_movers", False, str(exc)[:255])
        raise


def get_at_risk_csps(*, limit: int = 10, run: AgentRun | None = None) -> list[dict]:
    """Real, explainable at-risk flags from csp.comparison.at_risk_csps() —
    every entry names the exact reason (sharp decline / slab downgrade /
    near the eligibility minimum); never an opaque score. Also carries the
    same CspComparison's current/previous balance alongside the reasons
    (already computed in the same bulk_compare_csps call, not a second
    query) — the Verification Agent needs a real number to re-check
    against, not just the reason text."""
    try:
        today = timezone.localdate()
        comparison_date = comparison.resolve_comparison_date(today)
        comparisons = comparison.bulk_compare_csps(today, comparison_date, mode="overall")
        by_code = {c.csp_code: c for c in comparisons}
        flags = comparison.at_risk_csps(comparisons)[:limit]
        rows = []
        for f in flags:
            c = by_code.get(f.csp_code)
            rows.append(
                {
                    "csp_code": f.csp_code,
                    "csp_name": f.csp_name,
                    "reasons": f.reasons,
                    "current_value": _to_float(c.balance.current_value) if c else None,
                    "previous_value": _to_float(c.balance.previous_value) if c else None,
                }
            )
        _log(run, "get_at_risk_csps", True, f"{len(rows)} at-risk CSPs")
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.exception("tool_failed", tool="get_at_risk_csps")
        _log(run, "get_at_risk_csps", False, str(exc)[:255])
        raise


def get_csp_context(csp_code: str, run: AgentRun | None = None) -> dict | None:
    """One CSP's identity + today-vs-yesterday comparison — the same
    csp.comparison.compare_csp_metrics() the CSP profile page uses. Returns
    None (never a guess) if the CSP doesn't exist."""
    try:
        if not Csp.objects.filter(pk=csp_code).exists():
            _log(run, "get_csp_context", False, f"{csp_code} not found")
            return None
        today = timezone.localdate()
        comparison_date = comparison.resolve_comparison_date(today)
        c = comparison.compare_csp_metrics(csp_code, today, comparison_date, mode="overall")
        _log(run, "get_csp_context", True, csp_code)
        return _row(c)
    except Exception as exc:  # noqa: BLE001
        logger.exception("tool_failed", tool="get_csp_context", csp_code=csp_code)
        _log(run, "get_csp_context", False, str(exc)[:255])
        raise


def get_data_freshness_snapshot(run: AgentRun | None = None) -> dict:
    """Thin pass-through to csp.services.get_data_freshness() — kept as its
    own tool (rather than folded silently into get_network_snapshot) so a
    run that only needs freshness doesn't pull the whole network snapshot,
    and so it shows up distinctly in the tool-usage audit trail. Datetimes
    are converted to ISO strings — this dict gets stored straight into an
    AgentRun.observation JSONField, which can't serialize a raw datetime
    (only surfaced against real IngestLog history; every dev/test fixture
    without one masked it by returning None for every timestamp)."""
    result = get_data_freshness()
    result = {
        key: (value.isoformat() if isinstance(value, dt.datetime) else value)
        for key, value in result.items()
    }
    _log(run, "get_data_freshness", True, result.get("overall_status", ""))
    return result


def create_message_draft(
    csp_code: str, text: str, *, run: AgentRun | None = None
) -> DraftMessage | None:
    """Creates a DraftMessage exactly the way autopilot.services.draft_csp_nudges
    already does — DRAFT status, no send integration, human approval
    required via the existing Messaging page. Validates the target exists
    and skips creating a duplicate if this CSP already has an unresolved
    draft, per the "check for duplicate actions" safety rule. Returns None
    (never fabricates a DraftMessage) if validation fails."""
    if not text or not text.strip():
        _log(run, "create_message_draft", False, "empty text")
        return None
    if not Csp.objects.filter(pk=csp_code).exists():
        _log(run, "create_message_draft", False, f"{csp_code} not found")
        return None
    existing = DraftMessage.objects.filter(
        csp_id=csp_code, status=DraftMessage.Status.DRAFT
    ).first()
    if existing is not None:
        _log(run, "create_message_draft", False, f"duplicate skipped, draft #{existing.pk} pending")
        return None

    draft = DraftMessage.objects.create(
        csp_id=csp_code, channel=DraftMessage.Channel.SMS, text=text.strip()[:2000]
    )
    # Verify the write actually landed before trusting it happened — never
    # assume success merely because .create() didn't raise.
    confirmed = DraftMessage.objects.filter(pk=draft.pk).first()
    if confirmed is None:
        _log(run, "create_message_draft", False, "create() returned but row not found on re-read")
        return None
    _log(run, "create_message_draft", True, f"draft #{confirmed.pk} for {csp_code}")
    return confirmed


def get_pending_actions(run: AgentRun | None = None) -> list[AgentAction]:
    """Every agent action still waiting on a human — the same PENDING_APPROVAL
    state the Messaging page's own DRAFT filter reflects for message_draft
    actions."""
    rows = list(
        AgentAction.objects.filter(status=AgentAction.Status.PENDING_APPROVAL)
        .select_related("csp", "draft_message")
        .order_by("-created_at")
    )
    _log(run, "get_pending_actions", True, f"{len(rows)} pending")
    return rows


def get_action_status(action_id: int, run: AgentRun | None = None) -> dict | None:
    """Current, freshly-read status of one action — never a cached belief."""
    action = AgentAction.objects.select_related("csp", "draft_message").filter(pk=action_id).first()
    if action is None:
        _log(run, "get_action_status", False, f"action #{action_id} not found")
        return None
    _log(run, "get_action_status", True, f"action #{action_id}: {action.status}")
    return {
        "id": action.pk,
        "action_type": action.action_type,
        "status": action.status,
        "csp_code": action.csp_id,
        "draft_message_status": action.draft_message.status if action.draft_message else None,
    }


def verify_action(action_id: int, run: AgentRun | None = None) -> dict:
    """Re-reads the action's linked DraftMessage (if any) fresh from the
    database and reconciles AgentAction.status to match it — the single
    source of truth is always the DraftMessage row, never a value this
    function assumes. An action with no linked DraftMessage (a review flag
    or investigation task) has no external state to check, so it's marked
    verified with a note rather than left ambiguous. Per the agentic safety
    rule: an action is never marked successful just because a tool call
    for it didn't raise — this always re-reads real, current data."""
    action = AgentAction.objects.select_related("draft_message").filter(pk=action_id).first()
    if action is None:
        _log(run, "verify_action", False, f"action #{action_id} not found")
        return {"id": action_id, "verified": False, "result": "Action not found."}

    if action.draft_message_id is None:
        action.verification_result = "No external state to verify (informational action only)."
        action.verified_at = timezone.now()
        action.save(update_fields=["verification_result", "verified_at"])
        _log(run, "verify_action", True, f"action #{action_id}: informational, no-op verify")
        return {"id": action_id, "verified": True, "result": action.verification_result}

    draft = DraftMessage.objects.filter(pk=action.draft_message_id).first()
    if draft is None:
        action.status = AgentAction.Status.NEEDS_REVIEW
        action.verification_result = "Linked draft message no longer exists."
    elif draft.status == DraftMessage.Status.DRAFT:
        action.status = AgentAction.Status.PENDING_APPROVAL
        action.verification_result = "Still pending human approval."
    elif draft.status == DraftMessage.Status.APPROVED:
        action.status = AgentAction.Status.APPROVED
        action.verification_result = "Approved by a human reviewer."
    elif draft.status == DraftMessage.Status.REJECTED:
        action.status = AgentAction.Status.REJECTED
        action.verification_result = "Rejected by a human reviewer."
    elif draft.status == DraftMessage.Status.SENT:
        action.status = AgentAction.Status.EXECUTED
        action.verification_result = "Message marked sent."
    else:
        action.status = AgentAction.Status.NEEDS_REVIEW
        action.verification_result = f"Unrecognized draft status: {draft.status!r}."

    action.verified_at = timezone.now()
    action.save(update_fields=["status", "verification_result", "verified_at"])
    _log(run, "verify_action", True, f"action #{action_id}: {action.status}")
    return {
        "id": action_id,
        "verified": True,
        "result": action.verification_result,
        "status": action.status,
    }


def get_csp_transaction_summary(
    csp_code: str, *, days: int = 7, run: AgentRun | None = None
) -> dict | None:
    """One CSP's recent transaction activity (Overall + ONUS), straight from
    the same dbt-materialized daily_activity mart the Transactions page and
    csp.services.get_csp_daily_activity() already read — the Transaction
    Agent's only tool. Returns None (never a guess) if the CSP doesn't
    exist; an empty `days` list is a real, honest "no transactions ingested
    for this CSP in the window," not a failure."""
    if not Csp.objects.filter(pk=csp_code).exists():
        _log(run, "get_csp_transaction_summary", False, f"{csp_code} not found")
        return None
    rows = get_csp_daily_activity(csp_code)[-days:]
    result = {
        "csp_code": csp_code,
        "days": [
            {
                "date": str(r["activity_date"]),
                "txn_count": r["txn_count"],
                "txn_amount": _to_float(r["txn_amount"]),
                "onus_txn_count": r["onus_txn_count"] or 0,
                "onus_txn_amount": _to_float(r["onus_txn_amount"]) or 0.0,
                "net_flow": _to_float(r["net_flow"]),
            }
            for r in rows
        ],
    }
    _log(run, "get_csp_transaction_summary", True, f"{csp_code}: {len(rows)} day(s)")
    return result


# ---------------------------------------------------------------------------
# Multi-agent permission boundary — a specialist may only call the tools
# listed for its own agent_name. This is the concrete enforcement behind
# "Balance Agent cannot execute communication tools," etc.: call_tool()
# raises PermissionError for anything not explicitly granted, it never just
# trusts the caller. Read-only/analytical agents (risk_agent, coordinator)
# intentionally get no direct tool access — they only read AgentFinding
# rows other specialists already wrote.
# ---------------------------------------------------------------------------
AGENT_PERMISSIONS: dict[str, frozenset[str]] = {
    "data_agent": frozenset({"get_data_freshness_snapshot"}),
    "balance_agent": frozenset(
        {"get_network_snapshot", "get_at_risk_csps", "get_top_movers", "get_csp_context"}
    ),
    "transaction_agent": frozenset({"get_csp_transaction_summary"}),
    "action_agent": frozenset({"create_message_draft", "get_pending_actions"}),
    "verification_agent": frozenset(
        {
            "get_csp_context",
            "get_at_risk_csps",
            "get_action_status",
            "verify_action",
            "get_data_freshness_snapshot",
        }
    ),
    "risk_agent": frozenset(),  # reads findings only, no tools of its own
    "performance_agent": frozenset(),  # reads autopilot.monitoring aggregates only
    "coordinator": frozenset(),  # routes; never touches a business/action tool directly
}


class ToolPermissionError(PermissionError):
    """Raised when an agent calls a tool outside its granted permission set."""


def call_tool(agent_name: str, tool_fn, *args, **kwargs):
    """The only sanctioned way a specialist invokes a tool function above —
    enforces AGENT_PERMISSIONS before the call ever reaches the real
    service layer. `tool_fn` is one of this module's own functions (passed
    directly, e.g. `call_tool("balance_agent", get_at_risk_csps, limit=5)`)."""
    tool_name = tool_fn.__name__
    allowed = AGENT_PERMISSIONS.get(agent_name, frozenset())
    if tool_name not in allowed:
        raise ToolPermissionError(f"{agent_name!r} is not permitted to call tool {tool_name!r}.")
    return tool_fn(*args, **kwargs)
