"""
API rate limiting (scalability foundation, 2026-09-21) — api/v1/router.py
wires ninja's AuthRateThrottle/AnonRateThrottle onto the NinjaAPI instance,
backed by CACHES["default"]. The throttle objects are constructed once at
import time from settings.API_*_THROTTLE_RATE, so a behavioral test can't
use override_settings after the fact — it patches the already-parsed
num_requests/duration on the live throttle instances instead, which is
exactly what actually gates a request.

Note: ninja runs auth *before* throttling (see ninja/operation.py
_run_checks), and a missing/invalid API key fails auth immediately with
401 — so AnonRateThrottle never actually engages on this API (every
endpoint requires RequireScope). The real protection here is
AuthRateThrottle, gating a *valid* key's request rate — tested below with
a real ApiKey, same fixture pattern as test_api_v1.py.
"""

from contextlib import contextmanager

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey
from api.v1.router import api
from django.core.cache import cache


@pytest.fixture
def api_key(db):
    consumer = ApiConsumer.objects.create(name="test-suite-throttle")
    raw = generate_raw_key()
    ApiKey.objects.create(
        consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw), scopes="csp:read"
    )
    return raw


@contextmanager
def _tight_throttle(num_requests: int):
    originals = [(t, t.num_requests, t.duration) for t in api.throttle]
    for t, _, _ in originals:
        t.num_requests = num_requests
        t.duration = 60
    cache.clear()
    try:
        yield
    finally:
        for t, num, duration in originals:
            t.num_requests = num
            t.duration = duration
        cache.clear()


@pytest.mark.django_db
def test_valid_key_is_throttled_after_the_configured_rate(client, api_key):
    with _tight_throttle(num_requests=1):
        first = client.get("/api/v1/csps", HTTP_X_API_KEY=api_key)
        second = client.get("/api/v1/csps", HTTP_X_API_KEY=api_key)

    assert first.status_code == 200
    assert second.status_code == 429


@pytest.mark.django_db
def test_throttled_response_includes_retry_after_header(client, api_key):
    with _tight_throttle(num_requests=1):
        client.get("/api/v1/csps", HTTP_X_API_KEY=api_key)
        second = client.get("/api/v1/csps", HTTP_X_API_KEY=api_key)

    assert second.status_code == 429
    assert "Retry-After" in second
    assert int(second["Retry-After"]) >= 1


@pytest.mark.django_db
def test_missing_key_fails_auth_before_throttling_engages(client):
    """Documents the real behaviour: no key -> 401 from auth, not 429 from
    AnonRateThrottle, because ninja checks auth first."""
    with _tight_throttle(num_requests=1):
        first = client.get("/api/v1/csps")
        second = client.get("/api/v1/csps")

    assert first.status_code == 401
    assert second.status_code == 401


def test_router_has_throttle_configured():
    from ninja.throttling import AnonRateThrottle, AuthRateThrottle

    assert any(isinstance(t, AuthRateThrottle) for t in api.throttle)
    assert any(isinstance(t, AnonRateThrottle) for t in api.throttle)
