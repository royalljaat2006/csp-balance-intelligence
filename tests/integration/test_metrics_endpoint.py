"""
GET /metrics — Prometheus text exposition (scalability foundation,
2026-09-21). Exercised through a real request (not just common/metrics.py
directly) so the middleware wiring (api/middleware.py's
RequestLoggingMiddleware -> common.metrics.record_http_request) is
covered too.
"""

import pytest


@pytest.mark.django_db
def test_metrics_endpoint_returns_prometheus_text(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    body = response.content.decode()
    assert "# HELP http_requests_total" in body
    assert "# TYPE http_requests_total counter" in body


@pytest.mark.django_db
def test_metrics_reflects_requests_made_in_this_process(client):
    client.get("/health")
    response = client.get("/metrics")
    body = response.content.decode()
    assert 'route="health"' in body


@pytest.mark.django_db
def test_metrics_endpoint_needs_no_auth(client):
    # Same reasoning as /health — no session, no API key.
    response = client.get("/metrics")
    assert response.status_code == 200
