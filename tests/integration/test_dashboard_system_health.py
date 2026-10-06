"""
/dashboard/system-health/ — real computed status (DB/ingestion/freshness/
config), and quick actions that trigger a *fixed whitelist* of already-
existing, idempotent management commands (never an arbitrary command
string from the request — the same safety property Django admin's own
bulk actions already have). These tests are about that whitelist boundary
and staff-gating; the underlying commands' own behavior is already tested
elsewhere.
"""

from unittest.mock import patch

import pytest
from csp.models import IngestLog
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
    response = client.get("/dashboard/system-health/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_forbidden_for_non_staff(viewer_client):
    response = viewer_client.get("/dashboard/system-health/")
    assert response.status_code == 403


@pytest.mark.django_db
def test_renders_with_healthy_empty_state(logged_in_client, db):
    response = logged_in_client.get("/dashboard/system-health/")
    assert response.status_code == 200
    assert response.context["db_ok"] is True
    assert response.context["balance_bounds"] is None


@pytest.mark.django_db
def test_shows_recent_failed_ingestion_runs(logged_in_client, db):
    IngestLog.objects.create(source="calling_sheet", status=IngestLog.Status.FAILED)
    response = logged_in_client.get("/dashboard/system-health/")
    assert len(response.context["recent_failed"]) == 1


@pytest.mark.django_db
def test_quick_action_runs_whitelisted_command(logged_in_client, db):
    with patch("dashboard.views.call_command") as mock_call:
        response = logged_in_client.post(
            "/dashboard/system-health/", {"action": "sync_daily_snapshots"}
        )
    mock_call.assert_called_once_with("sync_daily_snapshots")
    assert response.status_code == 200
    assert "ran successfully" in response.context["action_result"]


@pytest.mark.django_db
def test_quick_action_rejects_non_whitelisted_command(logged_in_client, db):
    """A request forging an arbitrary `action` value must never reach
    call_command — only the fixed whitelist keys are ever dispatched."""
    with patch("dashboard.views.call_command") as mock_call:
        response = logged_in_client.post(
            "/dashboard/system-health/", {"action": "migrate"}
        )
    mock_call.assert_not_called()
    assert response.status_code == 200
    assert response.context["action_result"] is None


@pytest.mark.django_db
def test_quick_action_failure_is_shown_not_raised(logged_in_client, db):
    with patch("dashboard.views.call_command", side_effect=RuntimeError("boom")):
        response = logged_in_client.post(
            "/dashboard/system-health/", {"action": "run_autopilot"}
        )
    assert response.status_code == 200
    assert "failed" in response.context["action_result"]


@pytest.mark.django_db
def test_config_status_reflects_settings(logged_in_client, db, settings):
    settings.NEMOTRON_API_KEY = "test-key"
    response = logged_in_client.get("/dashboard/system-health/")
    assert response.context["config_status"]["Nemotron AI"] is True
