"""
Multi-agent CSP Operations architecture — coordinator (agent.py) +
specialists (agents.py) + tool permission boundary (agent_tools.py).

Every test either uses real DailyBalance/Csp fixtures and asserts the
agents only ever report facts the comparison engine actually computed, or
mocks chat_json (the ONLY external call this code makes) so tests never hit
the real Nemotron API — same convention as test_autopilot_services.py.
"""

from __future__ import annotations

import datetime as dt
import decimal
from unittest.mock import patch

import pytest
from autopilot import agent_tools, agents
from autopilot.agent import run_csp_operations_agent
from autopilot.agent_tools import (
    ToolPermissionError,
    call_tool,
    create_message_draft,
    get_at_risk_csps,
    get_csp_context,
    get_network_snapshot,
    get_top_movers,
    verify_action,
)
from autopilot.models import (
    AgentAction,
    AgentFinding,
    AgentMessage,
    AgentTask,
    AgentVerification,
    DraftMessage,
)
from autopilot.nemotron_client import NemotronUnavailable
from autopilot.services import approve_draft_message, reject_draft_message
from csp.models import Csp, DailyBalance, IngestLog
from django.contrib.auth import get_user_model


@pytest.fixture
def declining_csp(db):
    """One CSP with a real >10% overnight drop — a genuine SHARP_DECLINE /
    at-risk case per csp.comparison's own thresholds, not a fabricated one."""
    csp = Csp.objects.create(csp_code="1A850099", name="Declining CSP", account_count=100)
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    DailyBalance.objects.create(
        csp=csp, balance_date=yesterday, daily_avg_balance=decimal.Decimal("5000.00"),
        source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=csp, balance_date=today, daily_avg_balance=decimal.Decimal("3000.00"),
        source=DailyBalance.Source.CALLING_SHEET,
    )
    return csp


@pytest.fixture
def stable_csp(db):
    csp = Csp.objects.create(csp_code="1A850098", name="Stable CSP", account_count=50)
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    DailyBalance.objects.create(
        csp=csp, balance_date=yesterday, daily_avg_balance=decimal.Decimal("6000.00"),
        source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=csp, balance_date=today, daily_avg_balance=decimal.Decimal("6010.00"),
        source=DailyBalance.Source.CALLING_SHEET,
    )
    return csp


def _no_llm():
    return patch("autopilot.agents.chat_json", side_effect=NemotronUnavailable("no key"))


# ======================================================================
# TOOL LAYER — unchanged behaviour, just re-verified (including the new
# current_value/previous_value enrichment on at-risk rows).
# ======================================================================


@pytest.mark.django_db
def test_get_network_snapshot_returns_real_numbers(stable_csp):
    snapshot = get_network_snapshot()
    assert snapshot["csp_total"] >= 1
    assert snapshot["comparison_date"] is not None


@pytest.mark.django_db
def test_get_csp_context_for_real_csp(declining_csp):
    ctx = get_csp_context(declining_csp.csp_code)
    assert ctx["current_value"] == 3000.0
    assert ctx["previous_value"] == 5000.0


@pytest.mark.django_db
def test_get_csp_context_returns_none_for_unknown_csp(db):
    assert get_csp_context("DOES-NOT-EXIST") is None


@pytest.mark.django_db
def test_get_top_movers_decline_finds_real_decliner(declining_csp, stable_csp):
    movers = get_top_movers(direction="decline", limit=10)
    codes = {m["csp_code"] for m in movers}
    assert declining_csp.csp_code in codes
    assert stable_csp.csp_code not in codes


@pytest.mark.django_db
def test_get_at_risk_csps_includes_real_reason_and_values(declining_csp):
    flags = get_at_risk_csps(limit=10)
    assert len(flags) == 1
    assert flags[0]["csp_code"] == declining_csp.csp_code
    assert any("decline" in r.lower() for r in flags[0]["reasons"])
    assert flags[0]["current_value"] == 3000.0
    assert flags[0]["previous_value"] == 5000.0


@pytest.mark.django_db
def test_create_message_draft_refuses_unknown_csp(db):
    assert create_message_draft("DOES-NOT-EXIST", "hello") is None


@pytest.mark.django_db
def test_create_message_draft_refuses_duplicate(declining_csp):
    assert create_message_draft(declining_csp.csp_code, "First nudge") is not None
    assert create_message_draft(declining_csp.csp_code, "Second nudge") is None
    assert DraftMessage.objects.filter(csp=declining_csp).count() == 1


# ======================================================================
# TOOL PERMISSION BOUNDARY — the concrete enforcement behind
# "Balance Agent cannot execute communication tools" etc.
# ======================================================================


@pytest.mark.django_db
def test_balance_agent_cannot_call_create_message_draft(declining_csp):
    with pytest.raises(ToolPermissionError):
        call_tool("balance_agent", agent_tools.create_message_draft, declining_csp.csp_code, "hi")


@pytest.mark.django_db
def test_transaction_agent_cannot_call_create_message_draft(declining_csp):
    with pytest.raises(ToolPermissionError):
        call_tool(
            "transaction_agent", agent_tools.create_message_draft, declining_csp.csp_code, "hi"
        )


@pytest.mark.django_db
def test_transaction_agent_cannot_call_balance_tools(db):
    with pytest.raises(ToolPermissionError):
        call_tool("transaction_agent", agent_tools.get_at_risk_csps)


@pytest.mark.django_db
def test_risk_agent_has_no_tool_access(declining_csp):
    with pytest.raises(ToolPermissionError):
        call_tool("risk_agent", agent_tools.get_csp_context, declining_csp.csp_code)


@pytest.mark.django_db
def test_coordinator_has_no_direct_tool_access(db):
    with pytest.raises(ToolPermissionError):
        call_tool("coordinator", agent_tools.get_network_snapshot)


@pytest.mark.django_db
def test_action_agent_allowed_to_create_drafts(declining_csp):
    draft = call_tool(
        "action_agent", agent_tools.create_message_draft, declining_csp.csp_code, "hi there"
    )
    assert draft is not None


# ======================================================================
# STRUCTURED COMMUNICATION — AgentMessage/AgentFinding, never free chat.
# ======================================================================


@pytest.mark.django_db
def test_coordinator_logs_structured_messages_between_agents(declining_csp):
    with _no_llm():
        task = run_csp_operations_agent()

    messages = list(AgentMessage.objects.filter(task=task))
    assert messages  # real handoffs were logged
    pairs = {(m.from_agent, m.to_agent) for m in messages}
    assert ("coordinator", "data_agent") in pairs
    assert ("coordinator", "balance_agent") in pairs
    assert ("coordinator", "transaction_agent") in pairs
    assert ("coordinator", "risk_agent") in pairs
    assert ("coordinator", "verification_agent") in pairs
    for m in messages:
        assert isinstance(m.payload, dict)  # structured, never a plain string


@pytest.mark.django_db
def test_finding_carries_the_required_structured_fields(declining_csp):
    with _no_llm():
        task = run_csp_operations_agent()

    finding = task.findings.get(finding_type="balance_decline")
    assert finding.task_id == task.pk
    assert finding.source_agent == "balance_agent"
    assert finding.csp_id == declining_csp.csp_code
    assert finding.evidence  # real evidence dict
    assert finding.source == "csp.comparison.at_risk_csps"
    assert finding.data_timestamp is not None
    assert finding.verification_status in dict(AgentFinding.VerificationStatus.choices)


# ======================================================================
# MISSING / STALE / CONFLICTING EVIDENCE — never a guessed conclusion.
# ======================================================================


@pytest.mark.django_db
def test_missing_comparison_baseline_yields_needs_data(db):
    csp = Csp.objects.create(csp_code="1A850097", name="Brand New CSP")
    DailyBalance.objects.create(
        csp=csp, balance_date=dt.date.today(), daily_avg_balance=decimal.Decimal("1000.00"),
        source=DailyBalance.Source.CALLING_SHEET,
    )
    with patch("autopilot.agent_tools.comparison.resolve_comparison_date", return_value=None):
        task = run_csp_operations_agent()
    # data_agent may still flag freshness (no IngestLog fixture here means
    # "missing" is the real, honest status) — what must be zero is anything
    # balance-derived, since there was no comparison baseline to derive from.
    assert task.findings.exclude(finding_type="data_freshness_issue").count() == 0
    assert task.status in (AgentTask.Status.COMPLETED, AgentTask.Status.NEEDS_DATA)


@pytest.mark.django_db
def test_verification_needs_review_when_csp_disappears(declining_csp):
    with _no_llm():
        task = run_csp_operations_agent()
    finding = task.findings.get(finding_type="risk_flag")

    with patch("autopilot.agents.agent_tools.get_csp_context", return_value=None):
        verification = agents.verify_single_finding(finding)

    assert verification.result == AgentVerification.Result.NEEDS_REVIEW
    finding.refresh_from_db()
    assert finding.verification_status == AgentFinding.VerificationStatus.NEEDS_REVIEW


@pytest.mark.django_db
def test_verification_flags_stale_data(declining_csp):
    with _no_llm():
        task = run_csp_operations_agent()
    finding = task.findings.get(finding_type="risk_flag")

    stale_context = {
        "csp_code": declining_csp.csp_code, "current_value": 3000.0, "previous_value": 5000.0,
        "trend": "DECLINE", "current_data_status": "stale",
    }
    with patch("autopilot.agents.agent_tools.get_csp_context", return_value=stale_context):
        verification = agents.verify_single_finding(finding)

    finding.refresh_from_db()
    assert finding.verification_status == AgentFinding.VerificationStatus.STALE_DATA
    assert verification.result == AgentVerification.Result.NEEDS_REVIEW


@pytest.mark.django_db
def test_verification_flags_conflicting_data(declining_csp):
    with _no_llm():
        task = run_csp_operations_agent()
    finding = task.findings.get(finding_type="risk_flag")
    # Original evidence said current_value=3000.0 (declining_csp fixture);
    # a fresh re-check disagreeing by far more than rounding, while still
    # showing decline, is a genuine conflicting-data case.
    conflicting_context = {
        "csp_code": declining_csp.csp_code, "current_value": 1200.0, "previous_value": 5000.0,
        "trend": "DECLINE", "current_data_status": "fresh",
    }
    with patch("autopilot.agents.agent_tools.get_csp_context", return_value=conflicting_context):
        verification = agents.verify_single_finding(finding)

    finding.refresh_from_db()
    assert finding.verification_status == AgentFinding.VerificationStatus.CONFLICTING_DATA
    assert verification.result == AgentVerification.Result.NEEDS_REVIEW


@pytest.mark.django_db
def test_verification_rejects_when_csp_recovered(declining_csp):
    with _no_llm():
        task = run_csp_operations_agent()
    finding = task.findings.get(finding_type="risk_flag")

    recovered_context = {
        "csp_code": declining_csp.csp_code, "current_value": 6000.0, "previous_value": 5000.0,
        "trend": "GROWTH", "current_data_status": "fresh",
    }
    # Recovery means BOTH signals agree: the context looks healthy AND the
    # canonical at_risk_csps() check no longer flags this CSP.
    with (
        patch("autopilot.agents.agent_tools.get_csp_context", return_value=recovered_context),
        patch("autopilot.agents.agent_tools.get_at_risk_csps", return_value=[]),
    ):
        verification = agents.verify_single_finding(finding)

    finding.refresh_from_db()
    assert finding.verification_status == AgentFinding.VerificationStatus.REJECTED
    assert verification.result == AgentVerification.Result.REJECTED


# ======================================================================
# BALANCE / TRANSACTION / RISK investigation chain.
# ======================================================================


@pytest.mark.django_db
def test_balance_agent_creates_finding_for_real_decliner(declining_csp):
    task = AgentTask.objects.create(description="test")
    run, findings = agents.run_balance_agent(task)
    assert run.status == run.Status.COMPLETED
    assert len(findings) == 1
    assert findings[0].csp_id == declining_csp.csp_code
    assert findings[0].target_agent == "transaction_agent"


@pytest.mark.django_db
def test_transaction_agent_enriches_balance_finding(declining_csp):
    task = AgentTask.objects.create(description="test")
    _, balance_findings = agents.run_balance_agent(task)
    run, txn_findings = agents.run_transaction_agent(task, balance_findings)
    assert run.status == run.Status.COMPLETED
    assert len(txn_findings) == 1
    assert txn_findings[0].finding_type == "transaction_context"
    assert txn_findings[0].source == "csp.services.get_csp_daily_activity"


@pytest.mark.django_db
def test_risk_agent_combines_balance_and_transaction_evidence(declining_csp):
    task = AgentTask.objects.create(description="test")
    _, balance_findings = agents.run_balance_agent(task)
    _, txn_findings = agents.run_transaction_agent(task, balance_findings)
    run, risk_findings = agents.run_risk_agent(task, balance_findings, txn_findings)
    assert run.status == run.Status.COMPLETED
    assert len(risk_findings) == 1
    assert "balance_evidence" in risk_findings[0].evidence
    assert "transaction_evidence" in risk_findings[0].evidence


# ======================================================================
# ACTION AGENT — only VERIFIED findings may trigger a real action.
# ======================================================================


@pytest.mark.django_db
def test_unverified_finding_cannot_trigger_an_action(declining_csp):
    task = AgentTask.objects.create(description="test")
    _, balance_findings = agents.run_balance_agent(task)
    _, txn_findings = agents.run_transaction_agent(task, balance_findings)
    _, risk_findings = agents.run_risk_agent(task, balance_findings, txn_findings)
    # Deliberately skip verification — the finding is still PENDING.
    assert risk_findings[0].verification_status == AgentFinding.VerificationStatus.PENDING

    with _no_llm():
        run, actions = agents.run_action_agent(task, risk_findings)

    # run_action_agent defensively refuses to act on anything not VERIFIED,
    # even if a caller forgets to filter first.
    assert actions == []


@pytest.mark.django_db
def test_verified_finding_can_trigger_review_flag_action(declining_csp):
    task = AgentTask.objects.create(description="test")
    _, balance_findings = agents.run_balance_agent(task)
    _, txn_findings = agents.run_transaction_agent(task, balance_findings)
    _, risk_findings = agents.run_risk_agent(task, balance_findings, txn_findings)
    _, verified = agents.run_verification_agent(task, risk_findings)
    assert verified

    with _no_llm():
        run, actions = agents.run_action_agent(task, verified)

    review_flags = [a for a in actions if a.action_type == AgentAction.ActionType.REVIEW_FLAG]
    assert len(review_flags) == 1
    assert review_flags[0].finding_id == verified[0].pk


@pytest.mark.django_db
def test_action_agent_never_drafts_for_a_hallucinated_csp_code(declining_csp):
    task = AgentTask.objects.create(description="test")
    _, balance_findings = agents.run_balance_agent(task)
    _, txn_findings = agents.run_transaction_agent(task, balance_findings)
    _, risk_findings = agents.run_risk_agent(task, balance_findings, txn_findings)
    _, verified = agents.run_verification_agent(task, risk_findings)

    with patch(
        "autopilot.agents.chat_json",
        return_value={"drafts": [{"csp_code": "TOTALLY-MADE-UP", "text": "Hello"}]},
    ):
        run, actions = agents.run_action_agent(task, verified)

    assert not any(a.action_type == AgentAction.ActionType.MESSAGE_DRAFT for a in actions)
    assert DraftMessage.objects.count() == 0


# ======================================================================
# APPROVAL / EXECUTION — reuses the existing Messaging flow untouched.
# ======================================================================


@pytest.mark.django_db
def test_verify_action_reflects_real_approval_after_it_happens(declining_csp):
    draft = create_message_draft(declining_csp.csp_code, "Nudge text")
    from autopilot.models import AgentRun

    run = AgentRun.objects.create(agent_name="action_agent", task="test")
    action = AgentAction.objects.create(
        run=run, csp=declining_csp, action_type=AgentAction.ActionType.MESSAGE_DRAFT,
        description="Nudge text", status=AgentAction.Status.PENDING_APPROVAL, draft_message=draft,
    )

    result = verify_action(action.pk)
    assert result["status"] == AgentAction.Status.PENDING_APPROVAL

    user = get_user_model().objects.create_user(username="approver", password="pw12345")
    assert approve_draft_message(draft, user) is True

    result = verify_action(action.pk)
    action.refresh_from_db()
    assert action.status == AgentAction.Status.APPROVED
    assert result["status"] == AgentAction.Status.APPROVED


@pytest.mark.django_db
def test_verify_action_reflects_real_rejection(declining_csp):
    draft = create_message_draft(declining_csp.csp_code, "Nudge text")
    from autopilot.models import AgentRun

    run = AgentRun.objects.create(agent_name="action_agent", task="test")
    action = AgentAction.objects.create(
        run=run, csp=declining_csp, action_type=AgentAction.ActionType.MESSAGE_DRAFT,
        description="Nudge text", status=AgentAction.Status.PENDING_APPROVAL, draft_message=draft,
    )
    user = get_user_model().objects.create_user(username="rejector", password="pw12345")
    reject_draft_message(draft, user)

    result = verify_action(action.pk)
    action.refresh_from_db()
    assert action.status == AgentAction.Status.REJECTED
    assert result["status"] == AgentAction.Status.REJECTED


# ======================================================================
# REPLAN — a rejected verification doesn't just vanish.
# ======================================================================


def _fake_tool(name, side_effect):
    """A plain function (not a bare MagicMock) so call_tool's permission
    check — which reads tool_fn.__name__ — has a real attribute to read;
    a MagicMock only gets __name__ if explicitly configured. `side_effect`
    is a callable receiving the same args/kwargs the real tool would."""
    def _fn(*args, **kwargs):
        return side_effect(*args, **kwargs)
    _fn.__name__ = name
    return _fn


def _recovery_patches(declining_csp, recovered_context):
    """The balance_agent's FIRST call to get_at_risk_csps must see the real,
    genuinely-declining fixture data (or there's nothing to investigate at
    all) — only the verification_agent's LATER re-check should see the
    CSP as recovered. A blanket empty-list patch would short-circuit the
    whole pipeline before it ever reaches verification."""
    real_at_risk = agent_tools.get_at_risk_csps(limit=1000)
    calls = {"n": 0}

    def _at_risk_side_effect(*args, **kwargs):
        calls["n"] += 1
        return real_at_risk if calls["n"] == 1 else []

    return (
        patch(
            "autopilot.agents.agent_tools.get_csp_context",
            new=_fake_tool("get_csp_context", lambda *a, **k: recovered_context),
        ),
        patch(
            "autopilot.agents.agent_tools.get_at_risk_csps",
            new=_fake_tool("get_at_risk_csps", _at_risk_side_effect),
        ),
    )


@pytest.mark.django_db
def test_replan_creates_child_task_on_rejected_verification(declining_csp):
    """Simulates the CSP recovering between the initial finding and
    verification (e.g. a same-day correction) — a real, plausible scenario,
    not a contrived one — and confirms the coordinator opens a replan.
    Recovery means BOTH re-checks agree: the context looks healthy AND the
    canonical at_risk_csps() check no longer flags this CSP."""
    recovered_context = {
        "csp_code": declining_csp.csp_code, "current_value": 6000.0, "previous_value": 5000.0,
        "trend": "GROWTH", "current_data_status": "fresh",
    }
    context_patch, at_risk_patch = _recovery_patches(declining_csp, recovered_context)
    with _no_llm(), context_patch, at_risk_patch:
        task = run_csp_operations_agent()

    children = list(task.replans.all())
    assert len(children) == 1
    assert children[0].parent_task_id == task.pk
    assert children[0].status == AgentTask.Status.COMPLETED


@pytest.mark.django_db
def test_replan_is_bounded_and_does_not_recurse_forever(declining_csp):
    recovered_context = {
        "csp_code": declining_csp.csp_code, "current_value": 6000.0, "previous_value": 5000.0,
        "trend": "GROWTH", "current_data_status": "fresh",
    }
    context_patch, at_risk_patch = _recovery_patches(declining_csp, recovered_context)
    with _no_llm(), context_patch, at_risk_patch:
        task = run_csp_operations_agent()

    assert task.replans.count() == 1  # the replan actually happened
    # Exactly one replan level — no grandchild replans were spawned.
    for child in task.replans.all():
        assert child.replans.count() == 0


# ======================================================================
# COORDINATOR — end-to-end shape and graceful degradation.
# ======================================================================


@pytest.mark.django_db
def test_coordinator_end_to_end_with_llm_available(declining_csp):
    with patch(
        "autopilot.agents.chat_json",
        return_value={"drafts": [{"csp_code": declining_csp.csp_code, "text": "Please check in."}]},
    ):
        task = run_csp_operations_agent(task="Full pipeline test")

    assert task.status == AgentTask.Status.COMPLETED
    assert task.runs.count() == 6  # data, balance, transaction, risk, verification, action
    agent_names = set(task.runs.values_list("agent_name", flat=True))
    assert agent_names == {
        "data_agent", "balance_agent", "transaction_agent", "risk_agent",
        "verification_agent", "action_agent",
    }
    draft_actions = AgentAction.objects.filter(
        run__task_fk=task, action_type=AgentAction.ActionType.MESSAGE_DRAFT
    )
    assert draft_actions.count() == 1
    assert draft_actions.first().status == AgentAction.Status.PENDING_APPROVAL


@pytest.mark.django_db
def test_coordinator_degrades_honestly_without_llm(declining_csp):
    with _no_llm():
        task = run_csp_operations_agent()

    assert task.status == AgentTask.Status.COMPLETED
    # The deterministic review-flag action still gets created without any LLM.
    assert AgentAction.objects.filter(
        run__task_fk=task, action_type=AgentAction.ActionType.REVIEW_FLAG
    ).exists()
    assert not AgentAction.objects.filter(
        run__task_fk=task, action_type=AgentAction.ActionType.MESSAGE_DRAFT
    ).exists()


@pytest.mark.django_db
def test_coordinator_no_issues_when_network_is_stable(stable_csp):
    with patch("autopilot.agents.chat_json") as mock_chat:
        task = run_csp_operations_agent()
    mock_chat.assert_not_called()  # no LLM call spent when there's nothing to reason about
    assert task.status == AgentTask.Status.COMPLETED
    # Same reasoning as the NEEDS_DATA test above: data_agent's own freshness
    # finding is independent of balance findings, which must be zero here.
    assert task.findings.exclude(finding_type="data_freshness_issue").count() == 0
    assert "no significant" in task.final_status.lower()


@pytest.mark.django_db
def test_coordinator_records_failure_without_crashing(declining_csp):
    with patch("autopilot.agents.run_data_agent", side_effect=RuntimeError("boom")):
        task = run_csp_operations_agent()
    assert task.status == AgentTask.Status.FAILED
    assert "boom" in task.final_status


# ======================================================================
# REGRESSION — get_data_freshness() returns real datetime objects once a
# real successful IngestLog exists; every fixture above has none, which
# masked a JSONField serialization bug (only ever surfaced against the
# real Postgres database, which raises loudly where SQLite stayed silent).
# ======================================================================


@pytest.mark.django_db
def test_data_agent_survives_a_real_timestamped_ingest_log(declining_csp):
    IngestLog.objects.create(source="calling_sheet", status=IngestLog.Status.SUCCESS)
    task = AgentTask.objects.create(description="test")

    run, findings = agents.run_data_agent(task)

    assert run.status == run.Status.COMPLETED
    assert run.observation["freshness"]["calling_sheet_last_success"]  # a real ISO string
    assert isinstance(run.observation["freshness"]["calling_sheet_last_success"], str)
    assert findings == []  # freshly ingested — no freshness issue to flag


@pytest.mark.django_db
def test_coordinator_end_to_end_with_a_real_ingest_log(declining_csp):
    """The exact scenario that broke in production: a real IngestLog with a
    real datetime, running the FULL coordinator pipeline end to end."""
    IngestLog.objects.create(source="calling_sheet", status=IngestLog.Status.SUCCESS)
    with _no_llm():
        task = run_csp_operations_agent()
    assert task.status == AgentTask.Status.COMPLETED
    assert not task.runs.filter(status=AgentTask.Status.FAILED).exists()
