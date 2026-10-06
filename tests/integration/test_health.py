"""
Smoke test for the /health endpoint (PRD FR7). Deliberately the only test in
this scaffolding pass — ingestion and calculation-engine tests land with
FR1-FR4.
"""

import pytest


@pytest.mark.django_db
def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["db_ok"] is True
    # No ingest has run yet in a fresh test DB, so data is expected to be stale.
    assert body["calling_sheet_stale"] is True


@pytest.mark.django_db
def test_health_live_never_checks_the_database(client, monkeypatch):
    """P0 fast-pass, 2026-09-21: liveness must return 200 even if the
    database is unreachable — that's the whole point of splitting it from
    /health/ready."""
    import common.views as views_module

    def _boom(*args, **kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr(views_module, "connection", type("C", (), {"cursor": _boom})())
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_health_ready_matches_health(client):
    assert client.get("/health/ready").json() == client.get("/health").json()
