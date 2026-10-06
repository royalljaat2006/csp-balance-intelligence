"""
Phase 11 productization — the four new read-only AI-operations pages
(Risk, Agent Findings, Verification, Actions) and CSP 360°'s new
findings/actions panel. No new business logic anywhere here: every view
just filters existing models the same way Agent Monitoring/AI Operations
already do — these tests are about access control, empty states, and
that real rows actually render, not about any new calculation.
"""

import datetime as dt

import pytest
from autopilot.models import AgentAction, AgentFinding, AgentRun, AgentTask, AgentVerification
from csp.models import Csp
from django.contrib.auth import get_user_model


@pytest.fixture
def staff_user(db):
    return get_user_model().objects.create_user(username="staff11", password="pw12345", is_staff=True)


@pytest.fixture
def logged_in_client(client, staff_user):
    client.login(username="staff11", password="pw12345")
    return client


@pytest.fixture
def viewer_client(client, db):
    get_user_model().objects.create_user(username="viewer11", password="pw12345", is_staff=False)
    client.login(username="viewer11", password="pw12345")
    return client


@pytest.fixture
def csp(db):
    return Csp.objects.create(csp_code="1A850900", name="Phase11 CSP")


@pytest.fixture
def task(db):
    return AgentTask.objects.create(description="test task")


@pytest.mark.parametrize("url", ["/dashboard/risk/", "/dashboard/agent-findings/", "/dashboard/verification/", "/dashboard/actions/"])
@pytest.mark.django_db
def test_new_pages_require_login(client, url):
    response = client.get(url)
    assert response.status_code == 302


@pytest.mark.parametrize("url", ["/dashboard/risk/", "/dashboard/agent-findings/", "/dashboard/verification/", "/dashboard/actions/"])
@pytest.mark.django_db
def test_new_pages_forbidden_for_non_staff(viewer_client, url):
    response = viewer_client.get(url)
    assert response.status_code == 403


@pytest.mark.django_db
def test_risk_page_renders_empty_state_with_no_data(logged_in_client):
    response = logged_in_client.get("/dashboard/risk/")
    assert response.status_code == 200
    assert response.context["total_flagged"] == 0


@pytest.mark.django_db
def test_agent_findings_lists_real_rows_and_filters_by_status(logged_in_client, csp, task):
    AgentFinding.objects.create(
        task=task, source_agent="balance_agent", csp=csp, finding_type="risk_flag",
        source="get_at_risk_csps", verification_status=AgentFinding.VerificationStatus.VERIFIED,
    )
    AgentFinding.objects.create(
        task=task, source_agent="balance_agent", csp=csp, finding_type="risk_flag",
        source="get_at_risk_csps", verification_status=AgentFinding.VerificationStatus.REJECTED,
    )
    response = logged_in_client.get("/dashboard/agent-findings/")
    assert response.status_code == 200
    assert response.context["total"] == 2

    filtered = logged_in_client.get("/dashboard/agent-findings/?status=verified")
    assert filtered.context["total"] == 1


@pytest.mark.django_db
def test_verification_page_lists_real_rows(logged_in_client, csp, task):
    finding = AgentFinding.objects.create(
        task=task, source_agent="balance_agent", csp=csp, finding_type="risk_flag", source="x",
    )
    AgentVerification.objects.create(finding=finding, result=AgentVerification.Result.VERIFIED)
    response = logged_in_client.get("/dashboard/verification/")
    assert response.status_code == 200
    assert response.context["total"] == 1


@pytest.mark.django_db
def test_actions_page_lists_real_rows(logged_in_client, csp, task):
    run = AgentRun.objects.create(task=task, agent_name="action_agent")
    AgentAction.objects.create(
        run=run, csp=csp, action_type=AgentAction.ActionType.REVIEW_FLAG,
        description="test", status=AgentAction.Status.PENDING_APPROVAL,
    )
    response = logged_in_client.get("/dashboard/actions/")
    assert response.status_code == 200
    assert response.context["total"] == 1
    assert "Pending approval" in response.content.decode() or "pending_approval" in response.content.decode()


@pytest.mark.django_db
def test_csp_detail_shows_this_csps_agent_findings(logged_in_client, csp, task):
    AgentFinding.objects.create(
        task=task, source_agent="balance_agent", csp=csp, finding_type="risk_flag", source="x",
    )
    response = logged_in_client.get(f"/dashboard/csp/{csp.csp_code}/")
    assert response.status_code == 200
    assert len(response.context["agent_findings_for_csp"]) == 1


@pytest.mark.django_db
def test_csp_detail_shows_account_level_not_available_state(logged_in_client, csp):
    response = logged_in_client.get(f"/dashboard/csp/{csp.csp_code}/")
    assert "Not available" in response.content.decode()
