"""
/dashboard/agent-monitoring/ — health/activity/performance over real
AgentRun/AgentAction/AgentToolCall records (autopilot/monitoring.py), and
/dashboard/ai/'s "Run Agent Now" integration. No fake/demo metrics: an
empty system must render "No data" style states, never a fabricated number.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest
from autopilot.models import AgentAction, AgentRun, AgentTask, AgentToolCall
from autopilot.nemotron_client import NemotronUnavailable
from csp.models import Csp
from django.contrib.auth import get_user_model
from django.utils import timezone


@pytest.fixture
def staff_user(db):
    return get_user_model().objects.create_user(username="staff", password="pw12345", is_staff=True)


@pytest.fixture
def logged_in_client(client, staff_user):
    client.login(username="staff", password="pw12345")
    return client


@pytest.fixture
def viewer_client(client, db):
    get_user_model().objects.create_user(username="viewer", password="pw12345", is_staff=False)
    client.login(username="viewer", password="pw12345")
    return client


# ---- access control ---------------------------------------------------


@pytest.mark.django_db
def test_agent_monitoring_requires_login(client):
    response = client.get("/dashboard/agent-monitoring/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_agent_monitoring_forbidden_for_non_staff(viewer_client):
    response = viewer_client.get("/dashboard/agent-monitoring/")
    assert response.status_code == 403


# ---- empty/no-data states -----------------------------------------------


@pytest.mark.django_db
def test_agent_monitoring_empty_state_shows_no_data_not_zero(logged_in_client, db):
    response = logged_in_client.get("/dashboard/agent-monitoring/")
    assert response.status_code == 200
    kpis = response.context["kpis"]
    assert kpis["success_rate_today"] is None
    assert kpis["avg_run_seconds"] is None
    assert kpis["verification_success_rate"] is None
    assert kpis["retry_count"] is None
    assert response.context["heatmap"] == []
    body = response.content.decode()
    assert "No data" in body


# ---- KPI metrics ---------------------------------------------------------


@pytest.mark.django_db
def test_agent_run_metrics_reflect_real_runs(logged_in_client, db):
    AgentRun.objects.create(task="a", status=AgentRun.Status.COMPLETED, finished_at=timezone.now())
    AgentRun.objects.create(task="b", status=AgentRun.Status.FAILED, finished_at=timezone.now())

    response = logged_in_client.get("/dashboard/agent-monitoring/")
    kpis = response.context["kpis"]
    assert kpis["runs_today"] == 2
    assert kpis["successful_runs_today"] == 1
    assert kpis["failed_runs_today"] == 1
    assert kpis["success_rate_today"] == 50.0


@pytest.mark.django_db
def test_tool_usage_reflects_real_tool_calls(logged_in_client, db):
    run = AgentRun.objects.create(task="a")
    AgentToolCall.objects.create(run=run, tool_name="get_network_snapshot", success=True)
    AgentToolCall.objects.create(run=run, tool_name="get_network_snapshot", success=True)
    AgentToolCall.objects.create(run=run, tool_name="get_at_risk_csps", success=False)

    response = logged_in_client.get("/dashboard/agent-monitoring/")
    usage = {row["tool_name"]: row for row in response.context["tool_usage"]}
    assert usage["get_network_snapshot"]["calls"] == 2
    assert usage["get_at_risk_csps"]["failures"] == 1


@pytest.mark.django_db
def test_action_metrics_reflect_real_actions(logged_in_client, db):
    csp = Csp.objects.create(csp_code="1A850900", name="Monitor CSP")
    run = AgentRun.objects.create(task="a")
    AgentAction.objects.create(
        run=run, csp=csp, action_type=AgentAction.ActionType.MESSAGE_DRAFT,
        description="x", status=AgentAction.Status.EXECUTED,
    )
    AgentAction.objects.create(
        run=run, csp=csp, action_type=AgentAction.ActionType.MESSAGE_DRAFT,
        description="y", status=AgentAction.Status.FAILED,
    )
    AgentAction.objects.create(
        run=run, csp=csp, action_type=AgentAction.ActionType.REVIEW_FLAG,
        description="z", status=AgentAction.Status.PENDING_APPROVAL,
    )

    response = logged_in_client.get("/dashboard/agent-monitoring/")
    kpis = response.context["kpis"]
    assert kpis["actions_executed"] == 1
    assert kpis["actions_failed"] == 1
    assert kpis["pending_approvals"] == 1


@pytest.mark.django_db
def test_approval_metrics_connect_to_real_draft_message_flow(logged_in_client, db):
    from autopilot.agent_tools import create_message_draft
    from autopilot.services import approve_draft_message

    csp = Csp.objects.create(csp_code="1A850901", name="Approval CSP")
    draft = create_message_draft(csp.csp_code, "hello")
    run = AgentRun.objects.create(task="a")
    action = AgentAction.objects.create(
        run=run, csp=csp, action_type=AgentAction.ActionType.MESSAGE_DRAFT,
        description="hello", status=AgentAction.Status.PENDING_APPROVAL, draft_message=draft,
    )
    user = get_user_model().objects.create_user(username="approver2", password="pw12345")
    approve_draft_message(draft, user)

    response = logged_in_client.get("/dashboard/agent-monitoring/")
    approval = response.context["approval_metrics"]
    assert approval["approved"] == 1
    assert approval["avg_wait_seconds"] is not None
    assert action.pk  # sanity: action row exists and is queryable


# ---- heatmap --------------------------------------------------------------


@pytest.mark.django_db
def test_heatmap_has_one_row_per_agent_and_one_cell_per_day(logged_in_client, db):
    AgentRun.objects.create(task="a", status=AgentRun.Status.COMPLETED, finished_at=timezone.now())
    response = logged_in_client.get("/dashboard/agent-monitoring/")
    heatmap = response.context["heatmap"]
    assert len(heatmap) == 1
    assert heatmap[0]["agent_name"] == "csp_operations_agent"
    assert len(heatmap[0]["cells"]) == 14
    today_cell = heatmap[0]["cells"][-1]
    assert today_cell["total"] == 1
    assert today_cell["status"] == "healthy"


@pytest.mark.django_db
def test_heatmap_cell_status_failed_when_all_runs_failed(logged_in_client, db):
    AgentRun.objects.create(task="a", status=AgentRun.Status.FAILED, finished_at=timezone.now())
    response = logged_in_client.get("/dashboard/agent-monitoring/")
    today_cell = response.context["heatmap"][0]["cells"][-1]
    assert today_cell["status"] == "failed"


# ---- failed runs ------------------------------------------------------


@pytest.mark.django_db
def test_failed_runs_section_lists_real_failures(logged_in_client, db):
    AgentRun.objects.create(
        task="Daily check", status=AgentRun.Status.FAILED, error_message="boom: RuntimeError",
        finished_at=timezone.now(),
    )
    response = logged_in_client.get("/dashboard/agent-monitoring/")
    body = response.content.decode()
    assert "boom: RuntimeError" in body
    assert len(response.context["failed_runs"]) == 1


# ---- date filtering (14-day window) --------------------------------------


@pytest.mark.django_db
def test_heatmap_excludes_runs_older_than_the_window(logged_in_client, db):
    old_run = AgentRun.objects.create(task="old")
    old_run.started_at = timezone.now() - dt.timedelta(days=30)
    old_run.save(update_fields=["started_at"])

    response = logged_in_client.get("/dashboard/agent-monitoring/")
    heatmap = response.context["heatmap"]
    total_runs_in_window = sum(c["total"] for c in heatmap[0]["cells"])
    assert total_runs_in_window == 0


# ---- AI Operations "Run Agent Now" integration --------------------------


@pytest.mark.django_db
def test_run_agent_now_creates_a_real_agent_run(logged_in_client, db):
    # No REDIS_URL configured in tests -> enqueue_agent_run() runs
    # synchronously (same behaviour the view had before the async-dispatch
    # change) and returns the real AgentTask directly.
    with patch("dashboard.views.enqueue_agent_run") as mock_run:
        mock_run.return_value = AgentTask.objects.create(
            description="Manual run from AI Operations", status=AgentTask.Status.COMPLETED,
            final_status="No significant CSP issues detected today.",
        )
        response = logged_in_client.post("/dashboard/ai/", {"action": "run_agent"})
    assert response.status_code == 200
    mock_run.assert_called_once()
    assert "No significant CSP issues detected today." in response.content.decode()


@pytest.mark.django_db
def test_run_agent_now_shows_queued_message_when_enqueued(logged_in_client, db):
    # enqueue_agent_run() returns None when the run was handed to the
    # queue instead of executed inline — the view must not crash on that
    # and must tell the operator it was queued, not silently show nothing.
    with patch("dashboard.views.enqueue_agent_run", return_value=None) as mock_run:
        response = logged_in_client.post("/dashboard/ai/", {"action": "run_agent"})
    assert response.status_code == 200
    mock_run.assert_called_once()
    assert "Queued" in response.content.decode()


@pytest.mark.django_db
def test_run_agent_now_forbidden_for_non_staff(viewer_client):
    response = viewer_client.post("/dashboard/ai/", {"action": "run_agent"})
    assert response.status_code == 403
    assert AgentTask.objects.count() == 0


@pytest.mark.django_db
def test_ai_operations_shows_latest_agent_run_and_timeline(logged_in_client, db):
    with patch("autopilot.agents.chat_json", side_effect=NemotronUnavailable("no key")):
        from autopilot.agent import run_csp_operations_agent

        run_csp_operations_agent(task="Scheduled check")

    response = logged_in_client.get("/dashboard/ai/")
    assert response.status_code == 200
    assert response.context["latest_agent_task"] is not None
    assert response.context["latest_agent_task"].description == "Scheduled check"
    assert len(response.context["agent_timeline"]) >= 2  # at least start + finish events
