"""
Request-ID propagation (Phase 11) + per-request operational logging
(Phase 12). Both are plain Django middleware so they cover every route
(`/health`, `/admin/`, `/api/v1/*`), not just the ninja-routed ones.

Order matters: in MIDDLEWARE (config/settings/base.py), RequestLoggingMiddleware
is listed *before* RequestIdMiddleware, which — because Django middleware
wraps in onion order — makes RequestLoggingMiddleware the outer layer. That
way its timer covers the full request (including RequestIdMiddleware itself),
while its post-response logging still runs *after* RequestIdMiddleware has
already set request.request_id.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import TYPE_CHECKING

import structlog
from common import metrics
from django.http import HttpRequest

if TYPE_CHECKING:
    from api.models import ApiConsumer, ApiKey


class ApiRequest(HttpRequest):
    """
    Type-only: the real request object is a plain Django HttpRequest: these
    attributes are just set on it dynamically (RequestIdMiddleware sets
    request_id; api/auth.py sets api_consumer/api_key). Endpoints type-hint
    with this instead of HttpRequest so mypy knows about them, without
    actually subclassing anything at runtime.
    """

    request_id: str
    api_consumer: ApiConsumer | None
    api_key: ApiKey | None


logger = structlog.get_logger("api.requests")

# Accept a client-supplied X-Request-ID only if it looks like a token, not
# arbitrary/attacker-controlled content that could end up in a log line.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Defence in depth: even though nothing here ever reads these headers, make
# the "never log" invariant impossible to violate by accident later.
_NEVER_LOG_HEADERS = {"x-api-key", "authorization", "cookie"}


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get("X-Request-ID", "")
        request.request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else str(uuid.uuid4())
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id
        return response


class RequestLoggingMiddleware:
    """
    One structured log line per request:
    request_id, consumer, route, method, status, duration_ms, error_category.

    Never logs headers, query params, or body — see _NEVER_LOG_HEADERS above
    and the module docstring. `request.api_consumer` is set by api/auth.py
    (None for unauthenticated routes like /health).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.monotonic()
        response = self.get_response(request)
        duration_seconds = time.monotonic() - started
        duration_ms = round(duration_seconds * 1000, 2)

        consumer = getattr(request, "api_consumer", None)
        route = getattr(getattr(request, "resolver_match", None), "route", request.path)
        status = response.status_code
        if status >= 500:
            error_category = "server_error"
        elif status >= 400:
            error_category = "client_error"
        else:
            error_category = None

        logger.info(
            "request",
            stage="API",
            request_id=getattr(request, "request_id", None),
            consumer=consumer.name if consumer else None,
            route=route,
            method=request.method,
            status=status,
            duration_ms=duration_ms,
            error_category=error_category,
        )
        # Feeds /metrics (common/metrics.py) — see the scalability report's
        # observability section for what this does and doesn't cover
        # (per-process only; real multi-replica RPS needs per-replica
        # scraping or a shared registry, not implemented this round).
        metrics.record_http_request(
            method=request.method, route=str(route), status=status,
            duration_seconds=duration_seconds,
        )
        return response
