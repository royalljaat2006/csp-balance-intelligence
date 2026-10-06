"""
Per-consumer API-key auth — Phase 8. Replaces the old flat env-var key list.

Design notes:
- Keys are high-entropy random tokens (`secrets.token_urlsafe`), not
  low-entropy human passwords — SHA-256 is the right hash here, not
  bcrypt/PBKDF2 (those exist to slow down guessing a *weak* secret; a 32-byte
  random token has nothing to guess).
- The hash comparison is still done with `hmac.compare_digest` against the
  computed hash, even though a DB index lookup already narrows to one row —
  belt and suspenders against timing side-channels.
- The raw key is never logged, never re-stored, and never re-displayed after
  creation (see `manage.py create_api_key`).
- `authenticate()` stamps `request.api_consumer` / `request.api_key` so
  downstream code (endpoints, the logging middleware) can identify the
  caller without re-parsing the header.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from django.http import HttpRequest
from django.utils import timezone
from ninja.errors import AuthorizationError
from ninja.security import APIKeyHeader

from .models import ApiKey

KEY_PREFIX_LENGTH = 12


def generate_raw_key() -> str:
    return secrets.token_urlsafe(32)


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class ApiKeyAuth(APIKeyHeader):
    """Base auth: valid, active, unrevoked key. Use `RequireScope` to also
    enforce a specific scope for a given endpoint."""

    param_name = "X-API-Key"

    def authenticate(self, request: HttpRequest, key: str | None) -> str | None:
        request.api_consumer = None  # type: ignore[attr-defined]
        request.api_key = None  # type: ignore[attr-defined]
        if not key:
            return None

        candidate_hash = hash_key(key)
        # key_prefix narrows the query to a handful of rows (indexed); the
        # actual security decision is still the constant-time hash compare.
        candidates = ApiKey.objects.filter(
            is_active=True,
            consumer__is_active=True,
            key_prefix=key[:KEY_PREFIX_LENGTH],
        ).select_related("consumer")
        for api_key in candidates:
            if hmac.compare_digest(candidate_hash, api_key.key_hash):
                api_key.last_used_at = timezone.now()
                api_key.save(update_fields=["last_used_at"])
                request.api_consumer = api_key.consumer  # type: ignore[attr-defined]
                request.api_key = api_key  # type: ignore[attr-defined]
                return key
        return None


class RequireScope(ApiKeyAuth):
    """Same as ApiKeyAuth, plus the resolved key must carry `scope`."""

    def __init__(self, scope: str) -> None:
        super().__init__()
        self.scope = scope

    def authenticate(self, request: HttpRequest, key: str | None) -> str | None:
        result = super().authenticate(request, key)
        if result is None:
            return None  # missing/unknown/revoked key -> 401, handled by the base class
        api_key: ApiKey | None = getattr(request, "api_key", None)
        if api_key is None or not api_key.has_scope(self.scope):
            # A *valid* key without the required scope is 403 (authenticated,
            # not authorized), not 401 (not authenticated at all).
            raise AuthorizationError(message=f"This key does not have the '{self.scope}' scope.")
        return result
