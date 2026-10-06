"""
Specialized agents — Data, Balance, Transaction, Risk, Verification, Action.

Each function here is one agent's complete execution: it creates its own
AgentRun (so Agent Monitoring's heatmap gets one real row per agent), calls
ONLY the tools agent_tools.AGENT_PERMISSIONS grants it (via agent_tools.call_tool,
which raises ToolPermissionError otherwise — this is what makes "Balance
Agent cannot execute communication tools" a real, enforced boundary, not a
convention), and hands off structured AgentFinding rows rather than
free-form text. The coordinator (agent.py) sequences these calls and logs
the AgentMessage trail between them.

No agent here treats another agent's finding as true. A finding starts
PENDING and is only ever moved to VERIFIED/REJECTED/NEEDS_REVIEW by
run_verification_agent() re-checking it against a fresh canonical-service
call — never by the agent that created it.
"""

from __future__ import annotations

import decimal
import json

import structlog
from django.utils import timezone

from . import agent_tools
from .agent_tools import call_tool
from .models import AgentAction, AgentFinding, AgentRun, AgentTask, AgentVerification
from .nemotron_client import NemotronUnavailable, chat_json

logger = structlog.get_logger("autopilot.agent")

_MAX_MESSAGE_DRAFTS = 5


def _new_run(agent_name: str, task: AgentTask, description: str) -> AgentRun:
    return AgentRun.objects.create(agent_name=agent_name, task_fk=task, task=description)


def _finish(run: AgentRun, *, status: str, final_status: str = "", error: str = "") -> AgentRun:
    run.status = status
    run.final_status = final_status[:255]
    run.error_message = error[:2000]
    run.finished_at = timezone.now()
    run.save()
    return run


# ---------------------------------------------------------------------------
# DATA AGENT — ingestion health only.
# ---------------------------------------------------------------------------
def run_data_agent(task: AgentTask) -> tuple[AgentRun, list[AgentFinding]]:
    run = _new_run("data_agent", task, "Check ingestion health")
    findings: list[AgentFinding] = []
    try:
        freshness = call_tool("data_agent", agent_tools.get_data_freshness_snapshot, run=run)
        run.observation = {"freshness": freshness}
        run.tools_used = ["get_data_freshness_snapshot"]
        status = freshness.get("overall_status")
        if status in ("stale", "missing"):
            findings.append(
                AgentFinding.objects.create(
                    task=task,
                    source_agent="data_agent",
                    target_agent="balance_agent",
                    finding_type="data_freshness_issue",
                    evidence=freshness,
                    source="csp.services.get_data_freshness",
                    data_timestamp=timezone.now(),
                )
            )
        _finish(run, status=AgentRun.Status.COMPLETED, final_status=f"Freshness: {status}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("data_agent_failed", run_id=run.pk)
        _finish(run, status=AgentRun.Status.FAILED, error=str(exc))
    return run, findings


# ---------------------------------------------------------------------------
# BALANCE AGENT — real at-risk/decline detection via the canonical engine.
# ---------------------------------------------------------------------------
def run_balance_agent(task: AgentTask) -> tuple[AgentRun, list[AgentFinding]]:
    run = _new_run("balance_agent", task, "Investigate balance movement")
    findings: list[AgentFinding] = []
    try:
        snapshot = call_tool("balance_agent", agent_tools.get_network_snapshot, run=run)
        run.observation = {"snapshot": snapshot}
        tools_used = ["get_network_snapshot"]

        if snapshot.get("comparison_date") is None:
            run.tools_used = tools_used
            _finish(
                run,
                status=AgentRun.Status.NEEDS_DATA,
                final_status="No comparison baseline available yet.",
            )
            return run, findings

        at_risk = call_tool("balance_agent", agent_tools.get_at_risk_csps, limit=10, run=run)
        tools_used.append("get_at_risk_csps")
        run.observation["at_risk_csps"] = at_risk
        run.tools_used = tools_used

        now = timezone.now()
        for flag in at_risk:
            findings.append(
                AgentFinding.objects.create(
                    task=task,
                    source_agent="balance_agent",
                    target_agent="transaction_agent",
                    csp_id=flag["csp_code"],
                    finding_type="balance_decline",
                    evidence=flag,
                    source="csp.comparison.at_risk_csps",
                    data_timestamp=now,
                )
            )
        _finish(
            run,
            status=AgentRun.Status.COMPLETED,
            final_status=f"{len(at_risk)} at-risk CSP(s) found.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("balance_agent_failed", run_id=run.pk)
        _finish(run, status=AgentRun.Status.FAILED, error=str(exc))
    return run, findings


# ---------------------------------------------------------------------------
# TRANSACTION AGENT — enriches balance findings with real transaction context.
# ---------------------------------------------------------------------------
def run_transaction_agent(
    task: AgentTask, balance_findings: list[AgentFinding]
) -> tuple[AgentRun, list[AgentFinding]]:
    run = _new_run("transaction_agent", task, "Investigate transaction activity")
    findings: list[AgentFinding] = []
    try:
        tools_used = []
        now = timezone.now()
        for bf in balance_findings:
            if not bf.csp_id:
                continue
            summary = call_tool(
                "transaction_agent", agent_tools.get_csp_transaction_summary, bf.csp_id, run=run
            )
            tools_used.append("get_csp_transaction_summary")
            if summary is None:
                continue
            findings.append(
                AgentFinding.objects.create(
                    task=task,
                    source_agent="transaction_agent",
                    target_agent="risk_agent",
                    csp_id=bf.csp_id,
                    finding_type="transaction_context",
                    evidence=summary,
                    source="csp.services.get_csp_daily_activity",
                    data_timestamp=now,
                )
            )
        run.tools_used = list(dict.fromkeys(tools_used))
        run.observation = {"csps_checked": [bf.csp_id for bf in balance_findings if bf.csp_id]}
        _finish(
            run,
            status=AgentRun.Status.COMPLETED,
            final_status=f"Transaction context gathered for {len(findings)} CSP(s).",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("transaction_agent_failed", run_id=run.pk)
        _finish(run, status=AgentRun.Status.FAILED, error=str(exc))
    return run, findings


# ---------------------------------------------------------------------------
# RISK AGENT — combines already-real balance + transaction evidence into one
# finding per CSP. No tools of its own; reads findings only, invents nothing.
# ---------------------------------------------------------------------------
def run_risk_agent(
    task: AgentTask,
    balance_findings: list[AgentFinding],
    transaction_findings: list[AgentFinding],
) -> tuple[AgentRun, list[AgentFinding]]:
    run = _new_run("risk_agent", task, "Combine evidence into risk findings")
    findings: list[AgentFinding] = []
    try:
        txn_by_csp = {tf.csp_id: tf for tf in transaction_findings}
        now = timezone.now()
        for bf in balance_findings:
            evidence = {
                "balance_evidence": bf.evidence,
                "transaction_evidence": txn_by_csp[bf.csp_id].evidence
                if bf.csp_id in txn_by_csp
                else None,
            }
            findings.append(
                AgentFinding.objects.create(
                    task=task,
                    source_agent="risk_agent",
                    target_agent="verification_agent",
                    csp_id=bf.csp_id,
                    finding_type="risk_flag",
                    evidence=evidence,
                    source=(
                        "autopilot.agents.run_risk_agent "
                        "(combines balance_agent + transaction_agent findings)"
                    ),
                    data_timestamp=now,
                )
            )
        run.observation = {"risk_flags_created": len(findings)}
        _finish(
            run, status=AgentRun.Status.COMPLETED, final_status=f"{len(findings)} risk flag(s)."
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("risk_agent_failed", run_id=run.pk)
        _finish(run, status=AgentRun.Status.FAILED, error=str(exc))
    return run, findings


# Amounts are real rupee balances — a few paise of float rounding must never
# read as a genuine data conflict.
_CONFLICTING_VALUE_THRESHOLD = decimal.Decimal("1.00")


def _evaluate_finding(
    finding: AgentFinding, fresh: dict | None, fresh_at_risk_codes: set[str]
) -> tuple[str, str, str]:
    """The one place a finding's fresh re-check is turned into a verdict —
    shared by run_verification_agent (bulk) and verify_single_finding
    (replan) so the two paths can never silently diverge.

    at_risk_csps() flags a CSP for any of THREE independent reasons (sharp
    decline, slab downgrade, or persistently near the eligibility minimum)
    — a CSP flagged only for the third reason can be perfectly NO_CHANGE on
    trend and still be a completely real, still-open risk. Checking "is
    trend still DECLINE" would wrongly reject that category every time (a
    real bug this comment exists to prevent reintroducing — caught against
    the real 540-CSP network, where most at-risk flags are exactly this
    category, not a decline). The correct, non-duplicating verification is
    to re-run the EXACT SAME canonical check that created the finding
    (csp.comparison.at_risk_csps, via agent_tools.get_at_risk_csps) and see
    if it still flags this CSP — never a narrower proxy for that rule.

    Returns (AgentVerification.Result, AgentFinding.VerificationStatus, reasoning).
    """
    if fresh is None:
        return (
            AgentVerification.Result.NEEDS_REVIEW,
            AgentFinding.VerificationStatus.NEEDS_REVIEW,
            f"{finding.csp_id} no longer resolves to a real CSP on re-check.",
        )
    if fresh["current_data_status"] != "fresh":
        return (
            AgentVerification.Result.NEEDS_REVIEW,
            AgentFinding.VerificationStatus.STALE_DATA,
            f"Data status is now {fresh['current_data_status']!r}, not fresh enough to confirm.",
        )
    if finding.csp_id not in fresh_at_risk_codes:
        return (
            AgentVerification.Result.REJECTED,
            AgentFinding.VerificationStatus.REJECTED,
            "Re-checked via csp.comparison.at_risk_csps: no longer flagged at-risk.",
        )
    original = finding.evidence.get("balance_evidence", finding.evidence).get("current_value")
    if (
        original is not None
        and fresh["current_value"] is not None
        and abs(decimal.Decimal(str(fresh["current_value"])) - decimal.Decimal(str(original)))
        > _CONFLICTING_VALUE_THRESHOLD
    ):
        return (
            AgentVerification.Result.NEEDS_REVIEW,
            AgentFinding.VerificationStatus.CONFLICTING_DATA,
            f"Original evidence said current_value={original}, but a fresh re-check now says "
            f"{fresh['current_value']} — still flagged at-risk, but the numbers disagree.",
        )
    return (
        AgentVerification.Result.VERIFIED,
        AgentFinding.VerificationStatus.VERIFIED,
        "Re-checked via csp.comparison.at_risk_csps: still flagged at-risk.",
    )


# ---------------------------------------------------------------------------
# VERIFICATION AGENT — independently re-checks each risk finding against a
# FRESH call to the canonical comparison engine. Never trusts the original
# evidence at face value.
# ---------------------------------------------------------------------------
def run_verification_agent(
    task: AgentTask, risk_findings: list[AgentFinding]
) -> tuple[AgentRun, list[AgentFinding]]:
    run = _new_run("verification_agent", task, "Independently verify risk findings")
    verified: list[AgentFinding] = []
    try:
        tools_used = ["get_at_risk_csps"]
        # One fresh call to the SAME canonical check that created these
        # findings, shared across all of them — not one re-derivation per
        # finding, and never a narrower proxy for what at_risk_csps() means.
        fresh_at_risk = call_tool(
            "verification_agent", agent_tools.get_at_risk_csps, limit=1000, run=run
        )
        fresh_at_risk_codes = {row["csp_code"] for row in fresh_at_risk}

        for finding in risk_findings:
            fresh = (
                call_tool(
                    "verification_agent", agent_tools.get_csp_context, finding.csp_id, run=run
                )
                if finding.csp_id
                else None
            )
            tools_used.append("get_csp_context")

            result, status, reasoning = _evaluate_finding(finding, fresh, fresh_at_risk_codes)
            AgentVerification.objects.create(finding=finding, result=result, reasoning=reasoning)
            finding.verification_status = status
            finding.save(update_fields=["verification_status"])
            if status == AgentFinding.VerificationStatus.VERIFIED:
                verified.append(finding)

        run.tools_used = list(dict.fromkeys(tools_used))
        run.observation = {"checked": len(risk_findings), "verified": len(verified)}
        _finish(
            run,
            status=AgentRun.Status.COMPLETED,
            final_status=f"{len(verified)} of {len(risk_findings)} finding(s) verified.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("verification_agent_failed", run_id=run.pk)
        _finish(run, status=AgentRun.Status.FAILED, error=str(exc))
    return run, verified


def verify_single_finding(finding: AgentFinding) -> AgentVerification:
    """Re-runs the exact same independent check as run_verification_agent
    for one finding — used by the coordinator's replan path (re-verifying
    after a rejected finding), without needing a whole AgentRun for one row."""
    fresh = agent_tools.get_csp_context(finding.csp_id) if finding.csp_id else None
    fresh_at_risk_codes = {row["csp_code"] for row in agent_tools.get_at_risk_csps(limit=1000)}
    result, status, reasoning = _evaluate_finding(finding, fresh, fresh_at_risk_codes)
    verification = AgentVerification.objects.create(
        finding=finding, result=result, reasoning=reasoning
    )
    finding.verification_status = status
    finding.save(update_fields=["verification_status"])
    return verification


# ---------------------------------------------------------------------------
# ACTION AGENT — only ever acts on VERIFIED findings. Reuses the exact
# create_message_draft() tool (DRAFT status, existing Messaging approval
# flow) — never a new send path.
# ---------------------------------------------------------------------------
def _reason_about_verified_findings(verified_findings: list[AgentFinding]) -> dict:
    valid_codes = {f.csp_id for f in verified_findings}
    payload = [
        {"csp_code": f.csp_id, "evidence": f.evidence} for f in verified_findings if f.csp_id
    ]
    system_prompt = (
        "You are a CSP (kiosk banking agent) operations analyst. You will be given a list of "
        "INDEPENDENTLY VERIFIED at-risk CSPs, each with real evidence (balance and, where "
        "available, transaction context) — never invented by you. For AT MOST 5 of them that "
        "most warrant a proactive nudge, draft a short SMS (under 300 characters) referencing "
        "the actual evidence. These are DRAFTS a human will review before sending; never imply "
        "one has already been sent. You may ONLY use a csp_code that appears in the input; "
        'never invent one. Respond with ONLY a JSON object, no other text: {"drafts": '
        '[{"csp_code": "...", "text": "..."}, ...]}'
    )
    try:
        result = chat_json(system_prompt, json.dumps(payload, default=str), max_tokens=4096)
    except NemotronUnavailable as exc:
        logger.warning("action_agent_reasoning_skipped", reason=str(exc))
        return {"drafts": [], "note": f"AI drafting unavailable this run: {exc}"}

    entries = result.get("drafts")
    drafts: list[dict] = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("csp_code") or "")
            if code not in valid_codes:
                continue  # never trust a hallucinated CSP code
            text = str(entry.get("text") or "").strip()
            if text:
                drafts.append({"csp_code": code, "text": text[:2000]})
    return {"drafts": drafts[:_MAX_MESSAGE_DRAFTS], "note": ""}


def run_action_agent(
    task: AgentTask, verified_findings: list[AgentFinding]
) -> tuple[AgentRun, list[AgentAction]]:
    """CRITICAL invariant: only findings with verification_status == VERIFIED
    ever reach this function's action-creating logic — see agent.py's
    coordinator, which filters before calling this. A finding that is
    PENDING/REJECTED/NEEDS_REVIEW/etc. must never trigger a real action."""
    run = _new_run("action_agent", task, "Plan and create actions for verified findings")
    actions: list[AgentAction] = []
    try:
        for finding in verified_findings:
            if finding.verification_status != AgentFinding.VerificationStatus.VERIFIED:
                # Defensive — the coordinator already filters this, but an
                # action must never be created for anything unverified even
                # if a future caller forgets to filter.
                continue
            reasons = finding.evidence.get("balance_evidence", {}).get("reasons", [])
            actions.append(
                AgentAction.objects.create(
                    run=run,
                    finding=finding,
                    csp_id=finding.csp_id,
                    action_type=AgentAction.ActionType.REVIEW_FLAG,
                    description="; ".join(reasons) or "Verified risk finding.",
                )
            )

        reasoning = _reason_about_verified_findings(verified_findings)
        run.reasoning_summary = reasoning.get("note", "") or (
            f"Drafted {len(reasoning['drafts'])} message(s) for verified at-risk CSPs."
        )
        for draft_rec in reasoning["drafts"]:
            matched_finding = next(
                (f for f in verified_findings if f.csp_id == draft_rec["csp_code"]), None
            )
            draft = call_tool(
                "action_agent",
                agent_tools.create_message_draft,
                draft_rec["csp_code"],
                draft_rec["text"],
                run=run,
            )
            actions.append(
                AgentAction.objects.create(
                    run=run,
                    finding=matched_finding,
                    csp_id=draft_rec["csp_code"],
                    action_type=AgentAction.ActionType.MESSAGE_DRAFT,
                    description=draft_rec["text"],
                    status=(
                        AgentAction.Status.PENDING_APPROVAL
                        if draft
                        else AgentAction.Status.FAILED
                    ),
                    draft_message=draft,
                )
            )

        run.tools_used = ["create_message_draft"] if reasoning["drafts"] else []
        _finish(
            run,
            status=AgentRun.Status.COMPLETED,
            final_status=f"{len(actions)} action(s) created.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("action_agent_failed", run_id=run.pk)
        _finish(run, status=AgentRun.Status.FAILED, error=str(exc))
    return run, actions
