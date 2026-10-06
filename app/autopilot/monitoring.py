"""
Agent Monitoring — read-only aggregation over real AgentRun/AgentAction/
AgentToolCall records (autopilot/models.py). This is a separate module from
agent_tools.py on purpose: agent_tools.py is the AGENT's own interface to
the rest of the platform, this module is the MONITORING PAGE's interface to
the agent's own execution history. Neither calls the other.

Every number here is a real aggregate over real rows — nothing is invented,
and a metric with no underlying data returns None (rendered as "No Data" by
the template), never a guessed value. Cell health thresholds are named
constants, documented below, not a hidden formula.
"""

from __future__ import annotations

import datetime as dt

from django.db.models import Avg, F
from django.utils import timezone

from .models import AgentAction, AgentRun, AgentToolCall

# ---- Heatmap cell health thresholds (documented, configuration-based) ----
# A cell aggregates every AgentRun for one agent on one calendar day.
#   NO_ACTIVITY — zero runs that day.
#   HEALTHY     — every run COMPLETED (100% success), zero FAILED.
#   WARNING     — at least half the runs COMPLETED, or any run NEEDS_DATA
#                 (data wasn't available, not a code failure).
#   DEGRADED    — some runs COMPLETED but under half.
#   FAILED      — at least one run and none of them COMPLETED.
CELL_WARNING_SUCCESS_RATE = 0.5


def _cell_status(total: int, completed: int, failed: int, needs_data: int) -> str:
    if total == 0:
        return "no_activity"
    success_rate = completed / total
    if success_rate == 1.0:
        return "healthy"
    if success_rate == 0.0:
        return "failed" if failed > 0 else "warning"
    if success_rate >= CELL_WARNING_SUCCESS_RATE or needs_data > 0:
        return "warning"
    return "degraded"


def get_agent_kpis() -> dict:
    """Real counts/rates only — a metric this Phase doesn't track (e.g.
    retry count: no retry mechanism exists yet) is None, not zero, so the
    template can honestly render "No Data" instead of implying the concept
    was measured and happened to be zero."""
    today = timezone.localdate()
    all_runs = AgentRun.objects.all()
    today_runs = all_runs.filter(started_at__date=today)
    today_total = today_runs.count()
    today_completed = today_runs.filter(status=AgentRun.Status.COMPLETED).count()
    today_failed = today_runs.filter(status=AgentRun.Status.FAILED).count()

    finished = all_runs.exclude(finished_at__isnull=True)
    avg_seconds = None
    if finished.exists():
        avg_delta = finished.annotate(_dur=F("finished_at") - F("started_at")).aggregate(
            avg=Avg("_dur")
        )["avg"]
        if avg_delta is not None:
            avg_seconds = avg_delta.total_seconds()

    verified = AgentAction.objects.exclude(verified_at__isnull=True)
    verified_total = verified.count()
    verified_good = verified.filter(
        status__in=[AgentAction.Status.APPROVED, AgentAction.Status.EXECUTED]
    ).count()

    return {
        "active_agents": all_runs.values_list("agent_name", flat=True).distinct().count(),
        "runs_today": today_total,
        "successful_runs_today": today_completed,
        "failed_runs_today": today_failed,
        "success_rate_today": (
            round(today_completed / today_total * 100, 1) if today_total else None
        ),
        "pending_approvals": AgentAction.objects.filter(
            status=AgentAction.Status.PENDING_APPROVAL
        ).count(),
        "actions_executed": AgentAction.objects.filter(
            status=AgentAction.Status.EXECUTED
        ).count(),
        "actions_failed": AgentAction.objects.filter(status=AgentAction.Status.FAILED).count(),
        "verification_success_rate": (
            round(verified_good / verified_total * 100, 1) if verified_total else None
        ),
        "avg_run_seconds": round(avg_seconds, 1) if avg_seconds is not None else None,
        "tool_calls_total": AgentToolCall.objects.count(),
        "retry_count": None,  # not implemented until Phase 4/5 (replan/retry)
    }


def get_agent_heatmap(days: int = 14) -> list[dict]:
    """One row per distinct agent_name that has ever run (today, only
    "csp_operations_agent" — the architecture doesn't invent extra rows for
    sub-capabilities that aren't separate agents yet), one cell per day."""
    today = timezone.localdate()
    date_range = [today - dt.timedelta(days=i) for i in range(days - 1, -1, -1)]
    agent_names = list(
        AgentRun.objects.values_list("agent_name", flat=True).distinct().order_by("agent_name")
    )
    if not agent_names:
        return []

    rows = []
    for name in agent_names:
        runs = AgentRun.objects.filter(agent_name=name, started_at__date__gte=date_range[0])
        by_date: dict[dt.date, list[AgentRun]] = {d: [] for d in date_range}
        for run in runs:
            d = timezone.localtime(run.started_at).date()
            if d in by_date:
                by_date[d].append(run)

        cells = []
        for d in date_range:
            day_runs = by_date[d]
            total = len(day_runs)
            completed = sum(1 for r in day_runs if r.status == AgentRun.Status.COMPLETED)
            failed = sum(1 for r in day_runs if r.status == AgentRun.Status.FAILED)
            needs_data = sum(1 for r in day_runs if r.status == AgentRun.Status.NEEDS_DATA)
            run_ids = [r.pk for r in day_runs]
            actions = (
                AgentAction.objects.filter(run_id__in=run_ids)
                if run_ids
                else AgentAction.objects.none()
            )
            durations = [
                (r.finished_at - r.started_at).total_seconds()
                for r in day_runs
                if r.finished_at is not None
            ]
            cells.append(
                {
                    "date": d.isoformat(),
                    "total": total,
                    "completed": completed,
                    "failed": failed,
                    "needs_data": needs_data,
                    "actions_total": actions.count(),
                    "actions_pending": actions.filter(
                        status=AgentAction.Status.PENDING_APPROVAL
                    ).count(),
                    "tool_calls_total": AgentToolCall.objects.filter(run_id__in=run_ids).count()
                    if run_ids
                    else 0,
                    "avg_run_seconds": (
                        round(sum(durations) / len(durations), 1) if durations else None
                    ),
                    "status": _cell_status(total, completed, failed, needs_data),
                }
            )
        rows.append({"agent_name": name, "cells": cells})
    return rows


def get_tool_usage(limit: int = 20) -> list[dict]:
    """Real per-tool call/failure counts from the audit trail every tool
    call writes (agent_tools.py's _log helper) — never estimated."""
    calls = AgentToolCall.objects.values_list("tool_name", "success")
    counts: dict[str, dict[str, int]] = {}
    for tool_name, success in calls:
        entry = counts.setdefault(tool_name, {"calls": 0, "failures": 0})
        entry["calls"] += 1
        if not success:
            entry["failures"] += 1
    ordered_names = sorted(counts, key=lambda name: counts[name]["calls"], reverse=True)
    rows = [{"tool_name": name, **counts[name]} for name in ordered_names]
    return rows[:limit]


def get_failed_runs(limit: int = 20) -> list[AgentRun]:
    return list(
        AgentRun.objects.filter(status=AgentRun.Status.FAILED).order_by("-started_at")[:limit]
    )


def get_approval_metrics() -> dict:
    """Connects to the existing Messaging/DraftMessage approval flow —
    never a parallel approval concept. Reads each action's linked
    DraftMessage.status LIVE rather than the action's own (possibly stale
    until verify_action() next runs) cached status — DraftMessage is always
    the single source of truth for approval state, per the "never let two
    records disagree about reality" rule. Wait time is only computable for
    message_draft actions whose linked DraftMessage has actually been
    reviewed (reviewed_at set); anything else contributes no sample rather
    than a guessed duration."""
    from .models import DraftMessage

    actions = (
        AgentAction.objects.filter(action_type=AgentAction.ActionType.MESSAGE_DRAFT)
        .select_related("draft_message")
        .exclude(draft_message__isnull=True)
    )
    pending = approved = rejected = 0
    wait_seconds: list[float] = []
    for action in actions:
        draft = action.draft_message
        if draft is None:  # already excluded above; narrows the type for mypy
            continue
        if draft.status == DraftMessage.Status.DRAFT:
            pending += 1
        elif draft.status == DraftMessage.Status.APPROVED:
            approved += 1
        elif draft.status == DraftMessage.Status.REJECTED:
            rejected += 1
        if draft.reviewed_at:
            wait_seconds.append((draft.reviewed_at - action.created_at).total_seconds())

    return {
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
        "avg_wait_seconds": (
            round(sum(wait_seconds) / len(wait_seconds), 1) if wait_seconds else None
        ),
    }
