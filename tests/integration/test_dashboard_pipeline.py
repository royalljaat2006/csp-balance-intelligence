"""
Live Data Pipeline — real-time visualization backed entirely by existing
tables/locks (dashboard/pipeline_state.py, dashboard/pipeline_sse.py). No
new job-tracking system: these tests prove every number/state comes from a
real IngestLog/AgentRun/AgentFinding/AgentAction/AgentVerification row or a
real held lock, never a fabricated timer or status.
"""

from __future__ import annotations

import json

import pytest
from autopilot.models import AgentFinding, AgentRun, AgentTask, AgentVerification
from common.cache import release_lock, try_acquire_lock
from csp.models import Csp, DailyBalance, IngestLog
from dashboard import pipeline_sse, pipeline_state
from django.contrib.auth import get_user_model
from django.utils import timezone


@pytest.fixture
def staff_user(db):
    return get_user_model().objects.create_user(username="pl_staff", password="pw12345", is_staff=True)


@pytest.fixture
def logged_in_client(client, staff_user):
    client.login(username="pl_staff", password="pw12345")
    return client


@pytest.fixture
def viewer_client(client, db):
    get_user_model().objects.create_user(username="pl_viewer", password="pw12345", is_staff=False)
    client.login(username="pl_viewer", password="pw12345")
    return client


@pytest.fixture
def csp(db):
    return Csp.objects.create(csp_code="1A850950", name="Pipeline Test CSP")


# ---- access control --------------------------------------------------


@pytest.mark.parametrize("url", ["/dashboard/pipeline/", "/dashboard/pipeline/events", "/dashboard/pipeline/history"])
@pytest.mark.django_db
def test_pipeline_pages_require_login(client, url):
    assert client.get(url).status_code == 302


@pytest.mark.parametrize("url", ["/dashboard/pipeline/", "/dashboard/pipeline/events", "/dashboard/pipeline/history"])
@pytest.mark.django_db
def test_pipeline_pages_forbidden_for_non_staff(viewer_client, url):
    assert viewer_client.get(url).status_code == 403


# ---- pipeline_state: real signals, never fabricated -------------------


@pytest.mark.django_db
def test_ingest_node_is_idle_with_no_ingest_log():
    snapshot = pipeline_state.get_snapshot()
    node = next(n for n in snapshot["nodes"] if n["key"] == "calling_sheet")
    assert node["status"] == "idle"
    assert node["last_run_at"] is None


@pytest.mark.django_db
def test_ingest_node_is_running_only_while_finished_at_is_null():
    """Regression guard for the exact bug this project hit once already:
    IngestLog rows are created with a placeholder status=FAILED that only
    becomes real at the end — "running" must be decided by finished_at,
    never by that placeholder status value."""
    IngestLog.objects.create(source="calling_sheet", status=IngestLog.Status.FAILED)  # in-flight placeholder
    node = next(n for n in pipeline_state.get_snapshot()["nodes"] if n["key"] == "calling_sheet")
    assert node["status"] == "running"


@pytest.mark.django_db
def test_ingest_node_reports_real_completion_and_records():
    log = IngestLog.objects.create(source="transactions", status=IngestLog.Status.FAILED)
    log.status = IngestLog.Status.SUCCESS
    log.rows_read = 100
    log.rows_valid = 95
    log.rows_upserted = 95
    log.finished_at = timezone.now()
    log.save()

    node = next(n for n in pipeline_state.get_snapshot()["nodes"] if n["key"] == "transactions")
    assert node["status"] == "completed"
    assert node["records"]["upserted"] == 95
    assert node["last_duration_seconds"] is not None


@pytest.mark.django_db
def test_ingest_node_reports_real_failure_with_error():
    log = IngestLog.objects.create(source="telegram", status=IngestLog.Status.FAILED)
    log.error_summary = "corrupt workbook"
    log.finished_at = timezone.now()
    log.save()

    node = next(n for n in pipeline_state.get_snapshot()["nodes"] if n["key"] == "telegram")
    assert node["status"] == "failed"
    assert node["error"] == "corrupt workbook"


@pytest.mark.django_db
def test_lock_held_means_node_reports_running_even_without_ingest_log():
    assert try_acquire_lock("watch:ingest_calling_sheet", ttl=60) is True
    try:
        node = next(n for n in pipeline_state.get_snapshot()["nodes"] if n["key"] == "calling_sheet")
        assert node["status"] == "running"
    finally:
        release_lock("watch:ingest_calling_sheet")


@pytest.mark.django_db
def test_agent_node_running_state_is_real(csp):
    task = AgentTask.objects.create(description="t")
    AgentRun.objects.create(task_fk=task, agent_name="balance_agent", task="t", status=AgentRun.Status.RUNNING)
    node = next(n for n in pipeline_state.get_snapshot()["nodes"] if n["key"] == "balance_agent")
    assert node["status"] == "running"


@pytest.mark.django_db
def test_counters_reflect_real_rows_not_invented_values(csp):
    today = timezone.localdate()
    DailyBalance.objects.create(csp=csp, balance_date=today, daily_avg_balance=1000, source="calling_sheet")
    counters = pipeline_state.get_counters()
    assert counters["csps_processed_today"] == 1
    assert counters["total_csps"] == 1
    assert counters["active_jobs"] == 0
    assert counters["queued_jobs"] == 0  # no Redis configured in tests -> real 0, not fabricated


@pytest.mark.django_db
def test_activity_timeline_only_contains_real_rows(csp):
    task = AgentTask.objects.create(description="t")
    finding = AgentFinding.objects.create(
        task=task, source_agent="risk_agent", csp=csp, finding_type="risk_flag", source="x",
    )
    AgentVerification.objects.create(finding=finding, result=AgentVerification.Result.VERIFIED)
    timeline = pipeline_state.get_activity_timeline()
    kinds = {e["kind"] for e in timeline}
    assert "agent_finding" in kinds
    assert "verification_completed" in kinds
    for entry in timeline:
        assert entry["text"]  # every entry describes a real row, never blank/placeholder


@pytest.mark.django_db
def test_node_detail_and_history_return_real_rows(csp):
    log = IngestLog.objects.create(source="calling_sheet", status=IngestLog.Status.SUCCESS,
                                     finished_at=timezone.now(), rows_upserted=10)
    detail = pipeline_state.get_node_detail("calling_sheet")
    assert detail["latest"]["id"] == log.pk
    history = pipeline_state.get_history("calling_sheet")
    assert history["total"] == 1


@pytest.mark.django_db
def test_node_detail_empty_state_is_honest_not_fabricated():
    detail = pipeline_state.get_node_detail("calling_sheet")
    assert detail["latest"] is None
    assert detail["recent"] == []


# ---- SSE transport ------------------------------------------------------


@pytest.mark.django_db
def test_pipeline_events_is_a_real_event_stream(logged_in_client):
    response = logged_in_client.get("/dashboard/pipeline/events")
    assert response.status_code == 200
    assert response["Content-Type"] == "text/event-stream"
    assert response["X-Accel-Buffering"] == "no"


@pytest.mark.django_db
def test_generate_first_events_carry_real_snapshot_and_timeline(csp):
    """Drives the generator directly with max_seconds=0 so the test doesn't
    wait out the real ~20s stream — proves the sync/timeline events (the
    reconnect-safety mechanism) carry real current state."""
    gen = pipeline_sse._generate(max_seconds=0)
    sync_chunk = next(gen)
    timeline_chunk = next(gen)
    assert sync_chunk.startswith("event: sync\n")
    payload = json.loads(sync_chunk.split("data: ", 1)[1])
    assert payload["counters"]["total_csps"] == 1
    assert timeline_chunk.startswith("event: timeline\n")
    with pytest.raises(StopIteration):
        next(gen)  # max_seconds=0 -> no poll loop iterations


@pytest.mark.django_db
def test_generate_emits_error_event_on_backend_failure(monkeypatch):
    def _boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(pipeline_state, "get_snapshot", _boom)
    gen = pipeline_sse._generate(max_seconds=0)
    chunk = next(gen)
    assert chunk.startswith("event: error\n")
    assert "db unreachable" in chunk


@pytest.mark.django_db
def test_generate_pushes_a_real_state_change_between_polls(csp):
    """The core "no fake activity" proof: a node_update event only appears
    because a real IngestLog row's real state actually changed mid-stream."""
    gen = pipeline_sse._generate(max_seconds=0.05, poll_interval=0.01)
    next(gen)  # sync
    next(gen)  # timeline

    log = IngestLog.objects.create(source="calling_sheet", status=IngestLog.Status.FAILED)

    seen_running = False
    for chunk in gen:
        if chunk.startswith("event: node_update"):
            payload = json.loads(chunk.split("data: ", 1)[1])
            if payload["key"] == "calling_sheet" and payload["status"] == "running":
                seen_running = True
    assert seen_running
    log.refresh_from_db()  # sanity: the row this event described really exists
    assert log.source == "calling_sheet"


# ---- history endpoint ----------------------------------------------------


@pytest.mark.django_db
def test_history_endpoint_returns_real_paginated_rows(logged_in_client):
    for i in range(3):
        IngestLog.objects.create(source="calling_sheet", status=IngestLog.Status.SUCCESS,
                                   finished_at=timezone.now())
    response = logged_in_client.get("/dashboard/pipeline/history?node=calling_sheet")
    body = response.json()
    assert body["total"] == 3


@pytest.mark.django_db
def test_node_detail_endpoint_returns_json(logged_in_client):
    response = logged_in_client.get("/dashboard/pipeline/node/calling_sheet")
    assert response.status_code == 200
    assert response.json()["node_key"] == "calling_sheet"


@pytest.mark.django_db
def test_pipeline_page_renders_with_real_initial_snapshot(logged_in_client, csp):
    response = logged_in_client.get("/dashboard/pipeline/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "PIPELINE_INITIAL_SNAPSHOT" in body
    assert '"total_csps": 1' in body
