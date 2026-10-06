"""
RFC 9457 "Problem Details for HTTP APIs" error responses — Phase 10.

Replaces django-ninja's default error bodies (which are fine for humans
but not a stable machine-readable contract) with a consistent shape:
{type, title, status, detail, instance, request_id}. Never includes a
stack trace, SQL, or any credential — see `_exception_response`'s comment.

Registered on the NinjaAPI instance in v1/router.py via `register(api)`.
"""

from __future__ import annotations

import logging

from django.http import Http404, HttpRequest, JsonResponse
from ninja import NinjaAPI
from ninja.errors import HttpError, ValidationError

logger = logging.getLogger(__name__)

_TITLES = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
}


def _problem(
    request: HttpRequest, status: int, detail: str, *, extra: dict | None = None
) -> JsonResponse:
    body = {
        "type": "about:blank",
        "title": _TITLES.get(status, "Error"),
        "status": status,
        "detail": detail,
        "instance": request.path,
        "request_id": getattr(request, "request_id", None),
    }
    if extra:
        body.update(extra)
    return JsonResponse(body, status=status, content_type="application/problem+json")


def register(api: NinjaAPI) -> None:
    @api.exception_handler(Http404)
    def handle_404(request: HttpRequest, exc: Http404) -> JsonResponse:
        return _problem(request, 404, str(exc) or "The requested resource was not found.")

    @api.exception_handler(HttpError)
    def handle_http_error(request: HttpRequest, exc: HttpError) -> JsonResponse:
        # Covers AuthenticationError (401), AuthorizationError (403), and
        # Throttled (429) — all subclass HttpError. ninja's own
        # _check_throttles() already stamps Retry-After (RFC 9110,
        # math.ceil'd seconds) onto whatever response this handler returns,
        # so nothing extra is needed here for that — confirmed by reading
        # ninja/operation.py rather than assumed.
        return _problem(request, exc.status_code, str(exc))

    @api.exception_handler(ValidationError)
    def handle_validation_error(request: HttpRequest, exc: ValidationError) -> JsonResponse:
        return _problem(
            request,
            422,
            "The request did not pass validation.",
            extra={"errors": exc.errors},
        )

    @api.exception_handler(Exception)
    def handle_unexpected(request: HttpRequest, exc: Exception) -> JsonResponse:
        # Deliberately generic: the real exception is logged server-side
        # (with request_id for correlation) but NEVER put in the response —
        # no stack trace, no exception message, no internal detail.
        logger.exception(
            "unhandled_api_exception",
            extra={"request_id": getattr(request, "request_id", None), "path": request.path},
        )
        return _problem(request, 500, "An unexpected error occurred.")
