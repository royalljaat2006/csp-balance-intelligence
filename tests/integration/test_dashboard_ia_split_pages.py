"""
Dedicated Approvals/AI Recommendations/Audit pages (2026-10-06 IA split —
these previously just linked into Messaging/AI Operations/Administration).
No new business logic: Approvals reuses the exact same
approve_draft_message/reject_draft_message calls Messaging already uses.
"""

import pytest
from autopilot.models import AgentAction, AgentRun, AgentTask, DraftMessage
from csp.models import Csp, IngestLog
from django.contrib.auth import get_user_model


@pytest.fixture
def staff_user(db):
    return get_user_model().objects.create_user(username="ia_staff", password="pw12345", is_staff=True)


@pytest.fixture
def logged_in_client(client, staff_user):
    client.login(username="ia_staff", password="pw12345")
    return client


@pytest.fixture
def viewer_client(client, db):
    get_user_model().objects.create_user(username="ia_viewer", password="pw12345", is_staff=False)
    client.login(username="ia_viewer", password="pw12345")
    return client


@pytest.fixture
def csp(db):
    return Csp.objects.create(csp_code="1A850970", name="IA Split Test CSP")


@pytest.mark.parametrize("url", ["/dashboard/approvals/", "/dashboard/ai-recommendations/", "/dashboard/audit/"])
@pytest.mark.django_db
def test_new_pages_require_login(client, url):
    assert client.get(url).status_code == 302


@pytest.mark.parametrize("url", ["/dashboard/approvals/", "/dashboard/ai-recommendations/", "/dashboard/audit/"])
@pytest.mark.django_db
def test_new_pages_forbidden_for_non_staff(viewer_client, url):
    assert viewer_client.get(url).status_code == 403


@pytest.mark.django_db
def test_approvals_lists_pending_drafts(logged_in_client, csp):
    DraftMessage.objects.create(
        csp=csp, text="hi", channel=DraftMessage.Channel.SMS, status=DraftMessage.Status.DRAFT,
    )
    response = logged_in_client.get("/dashboard/approvals/")
    assert response.status_code == 200
    assert len(response.context["pending_drafts"]) == 1


@pytest.mark.django_db
def test_approvals_approve_action_reuses_the_real_service_call(logged_in_client, csp):
    draft = DraftMessage.objects.create(
        csp=csp, text="hi", channel=DraftMessage.Channel.SMS, status=DraftMessage.Status.DRAFT,
    )
    response = logged_in_client.post(
        "/dashboard/approvals/", {"message_id": draft.pk, "action": "approve"}
    )
    assert response.status_code == 302
    draft.refresh_from_db()
    assert draft.status == DraftMessage.Status.APPROVED
    assert draft.reviewed_by is not None


@pytest.mark.django_db
def test_approvals_shows_other_pending_actions_read_only(logged_in_client, csp):
    task = AgentTask.objects.create(description="t")
    run = AgentRun.objects.create(task_fk=task, agent_name="action_agent", task="t")
    AgentAction.objects.create(
        run=run, csp=csp, action_type=AgentAction.ActionType.REVIEW_FLAG,
        description="flagged", status=AgentAction.Status.PENDING_APPROVAL,
    )
    response = logged_in_client.get("/dashboard/approvals/")
    assert len(response.context["other_pending_actions"]) == 1
    # read-only: no approve/reject form targets a non-DraftMessage action
    assert "action_id" not in response.content.decode()


@pytest.mark.django_db
def test_ai_recommendations_renders_empty_state(logged_in_client):
    response = logged_in_client.get("/dashboard/ai-recommendations/")
    assert response.status_code == 200
    assert response.context["latest_insight"] is None


@pytest.mark.django_db
def test_audit_lists_real_ingest_logs_and_filters(logged_in_client):
    IngestLog.objects.create(source="calling_sheet", status=IngestLog.Status.SUCCESS)
    IngestLog.objects.create(source="transactions", status=IngestLog.Status.FAILED)

    response = logged_in_client.get("/dashboard/audit/")
    assert response.context["total"] == 2

    filtered = logged_in_client.get("/dashboard/audit/?source=calling_sheet")
    assert filtered.context["total"] == 1
