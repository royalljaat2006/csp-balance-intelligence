"""
/dashboard/ai/ — consolidated read-only view over the 4 existing autopilot
outputs (Insight, PriorityCall, AnomalyFlag, DraftMessage). Reads the exact
same tables Overview/Messaging/Admin already read; these tests are about
staff-gating, the "last run"/model aggregation, and that real rows render.
"""

import pytest
from autopilot.models import AnomalyFlag, DraftMessage, Insight, PriorityCall
from csp.models import Csp, IngestLog
from django.contrib.auth import get_user_model


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


@pytest.mark.django_db
def test_requires_login(client):
    response = client.get("/dashboard/ai/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_forbidden_for_non_staff(viewer_client):
    response = viewer_client.get("/dashboard/ai/")
    assert response.status_code == 403


@pytest.mark.django_db
def test_renders_with_no_ai_output(logged_in_client, db):
    response = logged_in_client.get("/dashboard/ai/")
    assert response.status_code == 200
    assert response.context["last_run_at"] is None
    assert response.context["model_used"] is None


@pytest.mark.django_db
def test_shows_real_insight_and_priority_calls(logged_in_client, db):
    import datetime as dt

    csp = Csp.objects.create(csp_code="1A850950", name="AI Test CSP")
    month = dt.date.today().strftime("%Y-%m")
    Insight.objects.create(
        month=month, headline="Network balance up 4%", body="Detail here.",
        model_used="nvidia/nemotron-3-super",
    )
    PriorityCall.objects.create(month=month, csp=csp, rank=1, reason="Close to next slab.")

    response = logged_in_client.get("/dashboard/ai/")
    body = response.content.decode()
    assert "Network balance up 4%" in body
    assert "Close to next slab." in body
    assert response.context["model_used"] == "nvidia/nemotron-3-super"


@pytest.mark.django_db
def test_shows_unreviewed_anomalies_only(logged_in_client, db):
    log = IngestLog.objects.create(source="calling_sheet")
    reviewed = AnomalyFlag.objects.create(
        ingest_log=log, description="Reviewed one", severity="low",
    )
    from django.utils import timezone

    reviewed.reviewed_at = timezone.now()
    reviewed.save()
    AnomalyFlag.objects.create(ingest_log=log, description="Unreviewed one", severity="high")

    response = logged_in_client.get("/dashboard/ai/")
    body = response.content.decode()
    assert "Unreviewed one" in body
    assert "Reviewed one" not in body
    assert response.context["unreviewed_anomaly_total"] == 1


@pytest.mark.django_db
def test_shows_draft_messages_with_draft_only_disclaimer(logged_in_client, db):
    csp = Csp.objects.create(csp_code="1A850951", name="Draft CSP")
    DraftMessage.objects.create(csp=csp, text="You're close to S2!", channel="sms")

    response = logged_in_client.get("/dashboard/ai/")
    body = response.content.decode()
    assert "Draft only" in body
    assert response.context["draft_total"] == 1
