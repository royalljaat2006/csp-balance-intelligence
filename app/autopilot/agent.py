"""
CSP Operations Agent — multi-agent coordinator.

    COORDINATOR
        v
    DATA AGENT -> BALANCE AGENT -> TRANSACTION AGENT -> RISK AGENT
        v
    VERIFICATION AGENT (independently re-checks every risk finding)
        v
    ACTION AGENT (only ever acts on VERIFIED findings)
        v
    human approval (existing Messaging flow) -> execution -> re-verify
        v
    REJECTED verification -> coordinator replans (child AgentTask)

TRUST BOUNDARY — read this before changing anything below:

  * Every FACT any agent reasons over comes from agent_tools.py, which
    itself only calls csp.services/csp.comparison — the same canonical
    engine every other consumer uses. No agent computes a balance, a
    trend, or a "risk" classification itself.

  * Structured handoffs only: agents pass AgentFinding rows (evidence +
    source + verification_status) and AgentMessage rows (payload dicts)
    between each other — never free-form text "chat." See autopilot/agents.py.

  * A finding is PENDING until the Verification Agent independently
    re-checks it against a fresh canonical-service call. Nothing downstream
    (especially the Action Agent) may treat a PENDING/REJECTED/
    NEEDS_REVIEW finding as true — see agents.run_action_agent's docstring.

  * Each specialist may only call the tools agent_tools.AGENT_PERMISSIONS
    grants it (enforced by agent_tools.call_tool, not just documented).

  * Actions that create real state (a DraftMessage) go through the exact
    same human-approval flow Messaging already has — nothing here ever
    marks anything "sent" or bypasses approval.

  * If there isn't enough data to say anything meaningful, the task is
    marked NEEDS_DATA — it never guesses.
"""

from __future__ import annotations

from django.utils import timezone

from . import agents
from .models import AgentFinding, AgentMessage, AgentTask

_MAX_REPLAN_DEPTH = 1


def _send(task: AgentTask, from_agent: str, to_agent: str, payload: dict) -> None:
    AgentMessage.objects.create(
        task=task, from_agent=from_agent, to_agent=to_agent, payload=payload
    )


def _investigate(task: AgentTask) -> tuple[list, list]:
    """DATA -> BALANCE -> TRANSACTION -> RISK -> VERIFICATION. Returns
    (verified_findings, all_risk_findings) for the coordinator to act on
    and report."""
    _send(task, "coordinator", "data_agent", {"instruction": "check_ingestion_health"})
    data_run, data_findings = agents.run_data_agent(task)
    _send(
        task, "data_agent", "coordinator",
        {"status": data_run.status, "findings": len(data_findings)},
    )

    _send(task, "coordinator", "balance_agent", {"instruction": "find_at_risk_csps"})
    balance_run, balance_findings = agents.run_balance_agent(task)
    _send(
        task, "balance_agent", "coordinator",
        {"status": balance_run.status, "findings": len(balance_findings)},
    )
    if balance_run.status == balance_run.Status.NEEDS_DATA:
        return [], []
    if not balance_findings:
        return [], []

    _send(
        task, "coordinator", "transaction_agent",
        {"instruction": "investigate_csps", "csp_codes": [f.csp_id for f in balance_findings]},
    )
    txn_run, txn_findings = agents.run_transaction_agent(task, balance_findings)
    _send(
        task, "transaction_agent", "coordinator",
        {"status": txn_run.status, "findings": len(txn_findings)},
    )

    _send(task, "coordinator", "risk_agent", {"instruction": "combine_evidence"})
    risk_run, risk_findings = agents.run_risk_agent(task, balance_findings, txn_findings)
    _send(
        task, "risk_agent", "coordinator",
        {"status": risk_run.status, "findings": len(risk_findings)},
    )

    _send(
        task, "coordinator", "verification_agent",
        {"instruction": "verify_findings", "count": len(risk_findings)},
    )
    verify_run, verified = agents.run_verification_agent(task, risk_findings)
    _send(
        task, "verification_agent", "coordinator",
        {"status": verify_run.status, "verified": len(verified), "total": len(risk_findings)},
    )
    return verified, risk_findings


def _replan(task: AgentTask, rejected_findings: list[AgentFinding], depth: int) -> AgentTask | None:
    """A REJECTED finding isn't just dropped — the coordinator opens a child
    task, re-checks the CSP's current state, and records what actually
    happened (recovered, or still an open issue needing a human look).
    Bounded to one level (`_MAX_REPLAN_DEPTH`) so a persistently-flapping
    CSP can't spin the agent forever."""
    if depth >= _MAX_REPLAN_DEPTH or not rejected_findings:
        return None
    child = AgentTask.objects.create(
        description=f"Replan: re-investigate {len(rejected_findings)} rejected finding(s)",
        parent_task=task,
    )
    _send(task, "coordinator", "coordinator", {"instruction": "replan", "child_task_id": child.pk})
    outcomes = []
    for finding in rejected_findings:
        verification = agents.verify_single_finding(finding)
        outcomes.append(
            f"{finding.csp_id}: {verification.get_result_display()} — {verification.reasoning}"
        )
    child.status = AgentTask.Status.COMPLETED
    child.final_status = "; ".join(outcomes)[:255]
    child.finished_at = timezone.now()
    child.save()
    return child


def run_csp_operations_agent(task: str = "Daily network check") -> AgentTask:
    """Top-level entry point — callable on demand (AI Operations' "Run
    Agent Now") or on the daily autopilot schedule (run_autopilot.py).
    Always returns the AgentTask, whatever the outcome; callers render
    task.status/task.final_status rather than assuming success."""
    agent_task = AgentTask.objects.create(description=task)
    try:
        verified, all_risk = _investigate(agent_task)

        if not all_risk:
            agent_task.status = AgentTask.Status.COMPLETED
            agent_task.final_status = "No significant CSP issues detected today."
            agent_task.finished_at = timezone.now()
            agent_task.save()
            return agent_task

        _send(
            agent_task, "coordinator", "action_agent",
            {"instruction": "act_on_verified", "count": len(verified)},
        )
        action_run, action_list = agents.run_action_agent(agent_task, verified)
        _send(
            agent_task, "action_agent", "coordinator",
            {"status": action_run.status, "actions": len(action_list)},
        )

        rejected = [
            f for f in all_risk if f.verification_status == AgentFinding.VerificationStatus.REJECTED
        ]
        _replan(agent_task, rejected, depth=0)

        pending = sum(1 for a in action_list if a.status == a.Status.PENDING_APPROVAL)
        agent_task.status = AgentTask.Status.COMPLETED
        agent_task.final_status = (
            f"{len(all_risk)} finding(s), {len(verified)} verified, {len(action_list)} action(s) "
            f"created, {pending} awaiting approval."
        )
    except Exception as exc:  # noqa: BLE001 — a task must record its own failure, never crash the caller
        agent_task.status = AgentTask.Status.FAILED
        agent_task.final_status = f"Coordinator failed: {exc}"[:255]
    agent_task.finished_at = timezone.now()
    agent_task.save()
    return agent_task
