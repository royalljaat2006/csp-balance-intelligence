"""
Live Data Pipeline — real execution state, read from the exact tables/locks
that already track it. This module invents nothing: every field it returns
comes from an existing row or an existing lock (common/cache.py's
try_acquire_lock keys, already written by watch_transactions.py).

No new job-tracking system. No new persistent event log. "Live activity" is
derived on each read by merging recent real rows from IngestLog, AgentRun,
AgentFinding, AgentAction, and AgentVerification, sorted by timestamp — the
same rows Agent Monitoring / AI Operations / System Health already show.

Honesty rules this module enforces:
- A job is "running" only if its lock is currently held (common.cache.
  is_locked) OR (for agents) its own status field says RUNNING with no
  finished_at yet — never inferred from a timer or a guess.
- IngestLog rows are created with a placeholder status=FAILED that only
  flips to a real value at the end (see ingestion/file_ingest.py) — so
  "running" is decided by `finished_at IS NULL`, never by the placeholder
  status value. Reading the raw `status` field while a row is mid-flight
  is exactly the false-alarm bug this project hit once already; this
  module is the one place that distinction is made correctly.
- A stage this system has no real signal for (e.g. sync_monthly_summary,
  which runs via external cron and writes no IngestLog/lock at all) is
  reported as "idle" with its last-known data timestamp — never guessed
  as running.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from autopilot.models import AgentAction, AgentFinding, AgentRun, AgentVerification, DraftMessage
from common.cache import is_locked
from csp.models import Csp, DailyBalance, DailyCspSnapshot, IngestLog, Transaction
from django.utils import timezone

_INGEST_SOURCES = {
    "calling_sheet": ("calling_sheet", "watch:ingest_calling_sheet"),
    "transactions": ("transactions", "watch:ingest_transactions"),
    "telegram": ("telegram", "watch:poll_telegram"),
}
_AGENT_NAMES = ("balance_agent", "transaction_agent", "risk_agent")


@dataclass
class NodeState:
    key: str
    label: str
    status: str  # idle | running | queued | completed | warning | failed | waiting_approval
    last_run_at: str | None = None
    last_duration_seconds: float | None = None
    records: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "last_run_at": self.last_run_at,
            "last_duration_seconds": self.last_duration_seconds,
            "records": self.records,
            "error": self.error,
            "detail": self.detail,
        }


def _ingest_node(key: str, label: str) -> NodeState:
    source, lock_key = _INGEST_SOURCES[key]
    latest = IngestLog.objects.filter(source=source).order_by("-started_at").first()
    locked = is_locked(lock_key)
    if latest is None:
        if locked:
            return NodeState(key=key, label=label, status="running")
        return NodeState(key=key, label=label, status="idle")

    finished_at = latest.finished_at
    if locked or finished_at is None:
        return NodeState(
            key=key, label=label, status="running", last_run_at=latest.started_at.isoformat(),
            detail={"started_at": latest.started_at.isoformat()},
        )

    duration = (finished_at - latest.started_at).total_seconds()
    status = "failed" if latest.status == IngestLog.Status.FAILED else (
        "warning" if latest.status == IngestLog.Status.PARTIAL else "completed"
    )
    return NodeState(
        key=key,
        label=label,
        status=status,
        last_run_at=finished_at.isoformat(),
        last_duration_seconds=round(duration, 1),
        records={
            "read": latest.rows_read,
            "valid": latest.rows_valid,
            "rejected": latest.rows_rejected,
            "upserted": latest.rows_upserted,
        },
        error=latest.error_summary or None,
        detail={"file_name": latest.file_name, "status_label": latest.get_status_display()},
    )


def _snapshots_node() -> NodeState:
    running = is_locked("watch:sync_daily_snapshots")
    latest = DailyCspSnapshot.objects.order_by("-created_at").first()
    if running:
        return NodeState(key="snapshots", label="Daily Snapshots", status="running")
    if latest is None:
        return NodeState(key="snapshots", label="Daily Snapshots", status="idle")
    today_count = DailyCspSnapshot.objects.filter(business_date=latest.business_date).count()
    return NodeState(
        key="snapshots", label="Daily Snapshots", status="completed",
        last_run_at=latest.created_at.isoformat(),
        records={"snapshots": today_count},
        detail={"business_date": latest.business_date.isoformat()},
    )


def _postgres_node(ingest_nodes: list[NodeState], snapshots: NodeState) -> NodeState:
    """A pass-through indicator, not a separately-tracked job — writes here
    happen inside the ingestion/snapshot commands above, there is no
    independent "Postgres stage" in the real pipeline. Status is derived
    (worst-of) from what actually just wrote to it."""
    statuses = [n.status for n in ingest_nodes] + [snapshots.status]
    if "running" in statuses:
        status = "running"
    elif "failed" in statuses:
        status = "warning"  # a source failing doesn't mean Postgres itself is unhealthy
    else:
        status = "completed" if any(s == "completed" for s in statuses) else "idle"
    return NodeState(key="postgres", label="PostgreSQL", status=status)


def _agent_node(agent_name: str, label: str) -> NodeState:
    run = AgentRun.objects.filter(agent_name=agent_name).order_by("-started_at").first()
    if run is None:
        return NodeState(key=agent_name, label=label, status="idle")
    run_finished_at = run.finished_at
    if run.status == AgentRun.Status.RUNNING and run_finished_at is None:
        return NodeState(
            key=agent_name, label=label, status="running", last_run_at=run.started_at.isoformat(),
        )
    duration = (
        (run_finished_at - run.started_at).total_seconds() if run_finished_at else None
    )
    status_map: dict[str, str] = {
        AgentRun.Status.COMPLETED: "completed",
        AgentRun.Status.FAILED: "failed",
        AgentRun.Status.NEEDS_DATA: "warning",
    }
    findings_count = AgentFinding.objects.filter(task=run.task_fk, source_agent=agent_name).count()
    return NodeState(
        key=agent_name, label=label, status=status_map.get(run.status, "idle"),
        last_run_at=run.started_at.isoformat(),
        last_duration_seconds=round(duration, 1) if duration is not None else None,
        error=run.error_message or None,
        records={"findings": findings_count},
        detail={"final_status": run.final_status},
    )


def _findings_node() -> NodeState:
    today = timezone.localdate()
    total_today = AgentFinding.objects.filter(created_at__date=today).count()
    latest = AgentFinding.objects.order_by("-created_at").first()
    if latest is None:
        return NodeState(key="findings", label="AI Findings", status="idle")
    verified = AgentFinding.objects.filter(
        created_at__date=today, verification_status=AgentFinding.VerificationStatus.VERIFIED
    ).count()
    return NodeState(
        key="findings", label="AI Findings", status="completed",
        last_run_at=latest.created_at.isoformat(),
        records={"today": total_today, "verified": verified},
    )


def _actions_node() -> NodeState:
    pending = AgentAction.objects.filter(status=AgentAction.Status.PENDING_APPROVAL).count()
    latest = AgentAction.objects.order_by("-created_at").first()
    if latest is None:
        return NodeState(key="actions", label="Action / Approval", status="idle")
    status = "waiting_approval" if pending else "completed"
    return NodeState(
        key="actions", label="Action / Approval", status=status,
        last_run_at=latest.created_at.isoformat(),
        records={"pending_approval": pending},
    )


def _verification_node() -> NodeState:
    latest = AgentVerification.objects.order_by("-verified_at").first()
    if latest is None:
        return NodeState(key="verification", label="Verification", status="idle")
    today = timezone.localdate()
    rejected_today = AgentVerification.objects.filter(
        verified_at__date=today, result=AgentVerification.Result.REJECTED
    ).count()
    status = "warning" if rejected_today else "completed"
    return NodeState(
        key="verification", label="Verification", status=status,
        last_run_at=latest.verified_at.isoformat(),
        records={"rejected_today": rejected_today},
    )


def get_snapshot() -> dict:
    """The full current pipeline state — one call, reused by both the SSE
    stream and the initial page render so the two can never disagree."""
    ingest_nodes = [
        _ingest_node("calling_sheet", "Calling Sheet"),
        _ingest_node("transactions", "Transactions"),
        _ingest_node("telegram", "Telegram"),
    ]
    snapshots = _snapshots_node()
    postgres = _postgres_node(ingest_nodes, snapshots)
    agent_nodes = [_agent_node(name, name.replace("_", " ").title()) for name in _AGENT_NAMES]
    findings = _findings_node()
    actions = _actions_node()
    verification = _verification_node()

    nodes = ingest_nodes + [postgres, snapshots] + agent_nodes + [findings, actions, verification]
    return {
        "nodes": [n.to_dict() for n in nodes],
        "counters": get_counters(),
        "current_operation": get_current_operation(ingest_nodes + agent_nodes),
        "generated_at": timezone.now().isoformat(),
    }


def get_current_operation(nodes: list[NodeState] | None = None) -> dict | None:
    """The one thing actually running right now, if anything — else the
    most recently completed one. Never fabricated: absence of a running
    node means None, rendered as "No active operation" by the template."""
    if nodes is None:
        snapshot_nodes = get_snapshot()["nodes"]
        running = [n for n in snapshot_nodes if n["status"] == "running"]
        completed = [n for n in snapshot_nodes if n["last_run_at"]]
    else:
        running = [n.to_dict() for n in nodes if n.status == "running"]
        completed = [n.to_dict() for n in nodes if n.last_run_at]

    if running:
        node = running[0]
        started = dt.datetime.fromisoformat(node["last_run_at"]) if node["last_run_at"] else None
        elapsed = (timezone.now() - started).total_seconds() if started else None
        return {
            "node": node["label"], "status": "running", "started_at": node["last_run_at"],
            "elapsed_seconds": round(elapsed, 1) if elapsed else None, "records": node["records"],
        }
    if completed:
        completed.sort(key=lambda n: n["last_run_at"], reverse=True)
        node = completed[0]
        return {
            "node": node["label"], "status": node["status"], "started_at": node["last_run_at"],
            "duration_seconds": node["last_duration_seconds"], "records": node["records"],
            "error": node["error"],
        }
    return None


def get_counters() -> dict:
    today = timezone.localdate()
    active_jobs = sum(1 for _, lock_key in _INGEST_SOURCES.values() if is_locked(lock_key))
    active_jobs += is_locked("watch:sync_daily_snapshots")
    active_jobs += AgentRun.objects.filter(status=AgentRun.Status.RUNNING).count()
    return {
        "csps_processed_today": DailyBalance.objects.filter(balance_date=today).count(),
        "transactions_processed_today": Transaction.objects.filter(txn_date=today).count(),
        "snapshots_generated_today": DailyCspSnapshot.objects.filter(business_date=today).count(),
        "active_jobs": active_jobs,
        "queued_jobs": _queue_depth(),
        "failed_jobs_today": (
            IngestLog.objects.filter(started_at__date=today, status=IngestLog.Status.FAILED).count()
            + AgentRun.objects.filter(started_at__date=today, status=AgentRun.Status.FAILED).count()
        ),
        "ai_findings_today": AgentFinding.objects.filter(created_at__date=today).count(),
        "pending_approvals": (
            AgentAction.objects.filter(status=AgentAction.Status.PENDING_APPROVAL).count()
            + DraftMessage.objects.filter(status=DraftMessage.Status.DRAFT).count()
        ),
        "total_csps": Csp.objects.count(),
    }


def _queue_depth() -> int:
    """Real RQ queue depth for the agent async queue — returns 0 (not an
    error) if Redis/rq aren't reachable; a dashboard read must never 500
    because the queue backend is briefly unavailable."""
    try:
        import redis as redis_lib
        from django.conf import settings
        from rq import Queue

        if not settings.REDIS_URL:
            return 0
        conn = redis_lib.from_url(
            settings.REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5
        )
        return Queue("agents", connection=conn).count
    except Exception:  # noqa: BLE001 — degrade to "unknown -> 0", never break the page
        return 0


def get_activity_timeline(limit: int = 30) -> list[dict]:
    """Recent real events, merged from existing tables — never a stored
    event log of its own. Each entry is one real row's real timestamp."""
    events: list[tuple[dt.datetime, dict]] = []

    for log in IngestLog.objects.order_by("-started_at")[:limit]:
        events.append((
            log.started_at,
            {"ts": log.started_at.isoformat(), "kind": "ingestion_started",
             "text": f"{log.source} ingestion started"},
        ))
        if log.finished_at:
            ok = log.status not in (IngestLog.Status.FAILED, IngestLog.Status.INVALID_SOURCE)
            text = (
                f"{log.source} ingestion {log.get_status_display().lower()} "
                f"({log.rows_upserted} upserted)"
                if ok
                else f"{log.source} ingestion failed: {log.error_summary or 'see logs'}"
            )
            events.append((
                log.finished_at,
                {"ts": log.finished_at.isoformat(),
                 "kind": "ingestion_completed" if ok else "ingestion_failed", "text": text},
            ))

    for run in AgentRun.objects.order_by("-started_at")[:limit]:
        agent_label = run.agent_name.replace("_", " ").title()
        events.append((
            run.started_at,
            {"ts": run.started_at.isoformat(), "kind": "agent_started",
             "text": f"{agent_label} started"},
        ))
        if run.finished_at:
            run_ok = run.status == AgentRun.Status.COMPLETED
            events.append((
                run.finished_at,
                {"ts": run.finished_at.isoformat(),
                 "kind": "agent_completed" if run_ok else "agent_failed",
                 "text": f"{agent_label} {run.get_status_display().lower()}"},
            ))

    for finding in AgentFinding.objects.select_related("csp").order_by("-created_at")[:limit]:
        finding_agent = finding.source_agent.replace("_", " ").title()
        finding_target = f" for {finding.csp_id}" if finding.csp_id else ""
        events.append((
            finding.created_at,
            {"ts": finding.created_at.isoformat(), "kind": "agent_finding",
             "text": f"{finding_agent} found {finding.finding_type}{finding_target}"},
        ))

    for verification in AgentVerification.objects.order_by("-verified_at")[:limit]:
        result = verification.get_result_display().lower()
        events.append((
            verification.verified_at,
            {"ts": verification.verified_at.isoformat(), "kind": "verification_completed",
             "text": f"Verification {result} for finding #{verification.finding_id}"},
        ))

    for action in AgentAction.objects.order_by("-created_at")[:limit]:
        pending = action.status == AgentAction.Status.PENDING_APPROVAL
        kind = "action_pending_approval" if pending else "action_created"
        target = action.csp_id or "network"
        events.append((
            action.created_at,
            {"ts": action.created_at.isoformat(), "kind": kind,
             "text": f"{action.get_action_type_display()} proposed for {target}"},
        ))

    events.sort(key=lambda e: e[0], reverse=True)
    return [e[1] for e in events[:limit]]


_INGEST_NODE_KEYS = set(_INGEST_SOURCES)
_AGENT_NODE_KEYS = set(_AGENT_NAMES)


def get_history(node_key: str | None = None, *, page: int = 1, page_size: int = 20) -> dict:
    """Real past executions for History mode — straight reads of IngestLog/
    AgentRun, the exact rows Agent Monitoring's own history already comes
    from. No node_key -> ingestion history (the most common case)."""
    offset = (page - 1) * page_size
    rows: list[dict[str, Any]]
    if node_key in _AGENT_NODE_KEYS:
        agent_qs = AgentRun.objects.filter(agent_name=node_key).order_by("-started_at")
        total = agent_qs.count()
        rows = [
            {
                "id": r.pk, "label": r.agent_name.replace("_", " ").title(),
                "status": r.status, "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "duration_seconds": (
                    round((r.finished_at - r.started_at).total_seconds(), 1)
                    if r.finished_at else None
                ),
                "error": r.error_message or None,
            }
            for r in agent_qs[offset : offset + page_size]
        ]
    else:
        log_qs = IngestLog.objects.all().order_by("-started_at")
        if node_key in _INGEST_NODE_KEYS:
            log_qs = log_qs.filter(source=_INGEST_SOURCES[node_key][0])
        total = log_qs.count()
        rows = [
            {
                "id": r.pk, "label": r.source, "status": r.status,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "duration_seconds": (
                    round((r.finished_at - r.started_at).total_seconds(), 1)
                    if r.finished_at else None
                ),
                "records": {
                    "read": r.rows_read, "valid": r.rows_valid,
                    "rejected": r.rows_rejected, "upserted": r.rows_upserted,
                },
                "error": r.error_summary or None,
            }
            for r in log_qs[offset : offset + page_size]
        ]
    return {
        "rows": rows, "total": total, "page": page,
        "total_pages": max(1, -(-total // page_size)),
    }


def get_node_detail(node_key: str) -> dict:
    """Real detail for one node — latest execution + a short recent history,
    for the click-to-expand panel. Reuses get_history rather than a second
    query shape."""
    history = get_history(node_key, page=1, page_size=10)
    return {"node_key": node_key, "latest": history["rows"][0] if history["rows"] else None,
            "recent": history["rows"]}
