"""Per-consumer API-key auth (Phase 8): missing/invalid key -> 401, valid key
missing the required scope -> 403, valid key with the scope -> 200."""

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey

OVERVIEW_URL = "/api/v1/reports/overview"


def _make_key(scopes: str, *, consumer_active: bool = True, key_active: bool = True) -> str:
    consumer = ApiConsumer.objects.create(name="test-consumer", is_active=consumer_active)
    raw = generate_raw_key()
    ApiKey.objects.create(
        consumer=consumer,
        key_prefix=raw[:12],
        key_hash=hash_key(raw),
        scopes=scopes,
        is_active=key_active,
    )
    return raw


@pytest.mark.django_db
def test_rejects_missing_key(client):
    response = client.get(OVERVIEW_URL)
    assert response.status_code == 401


@pytest.mark.django_db
def test_rejects_unknown_key(client):
    response = client.get(OVERVIEW_URL, headers={"X-API-Key": "not-a-real-key"})
    assert response.status_code == 401


@pytest.mark.django_db
def test_rejects_revoked_key(client):
    raw = _make_key("report:read", key_active=False)
    response = client.get(OVERVIEW_URL, headers={"X-API-Key": raw})
    assert response.status_code == 401


@pytest.mark.django_db
def test_rejects_key_of_inactive_consumer(client):
    raw = _make_key("report:read", consumer_active=False)
    response = client.get(OVERVIEW_URL, headers={"X-API-Key": raw})
    assert response.status_code == 401


@pytest.mark.django_db
def test_valid_key_without_required_scope_is_forbidden(client):
    raw = _make_key("csp:read")  # not report:read
    response = client.get(OVERVIEW_URL, headers={"X-API-Key": raw})
    assert response.status_code == 403


@pytest.mark.django_db
def test_valid_key_with_scope_succeeds(client):
    raw = _make_key("report:read")
    response = client.get(OVERVIEW_URL, headers={"X-API-Key": raw})
    assert response.status_code == 200
    body = response.json()
    assert "data" in body and "request_id" in body


@pytest.mark.django_db
def test_last_used_at_is_stamped(client):
    raw = _make_key("report:read")
    key = ApiKey.objects.get(key_prefix=raw[:12])
    assert key.last_used_at is None
    client.get(OVERVIEW_URL, headers={"X-API-Key": raw})
    key.refresh_from_db()
    assert key.last_used_at is not None
