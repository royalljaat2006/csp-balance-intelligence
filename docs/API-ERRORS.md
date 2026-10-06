# API errors

All `/api/v1/*` errors use the RFC 9457 *Problem Details* shape, served as
`Content-Type: application/problem+json` (implemented in `app/api/errors.py`):

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "detail": "No Csp matches the given query.",
  "instance": "/api/v1/csps/UNKNOWN",
  "request_id": "6f1c2a9e-…"
}
```

`request_id` matches the `X-Request-ID` response header — quote it when reporting a problem;
it's on the matching server log line (docs/OPERATIONS.md).

Validation errors (422) add one extension member, `errors`, with django-ninja's per-field detail:

```json
{ "...": "...", "status": 422, "detail": "The request did not pass validation.",
  "errors": [{"type": "date_from_parsing", "loc": ["query", "date_from"], "msg": "…"}] }
```

## Statuses the service actually produces

| Status | When | `detail` | Fix on the caller side |
|---|---|---|---|
| **401** Unauthorized | `X-API-Key` header missing, unknown, revoked, or its consumer is inactive | `Unauthorized` | Send a valid, active key (docs/API-CONSUMERS.md) |
| **403** Forbidden | Key is valid but lacks the scope the endpoint requires | `This key does not have the '<scope>' scope.` | Request that scope for the consumer, or use the right key |
| **404** Not Found | Unknown `csp_code`; or `/balance` / `/incentive` / `/projection` when that CSP has no data yet | endpoint-specific message | Check the code; for "no data yet" this resolves once ingestion runs |
| **422** Unprocessable Entity | A query/path parameter failed validation (bad date, out-of-range enum, non-integer `limit`) | `The request did not pass validation.` + `errors[]` | Fix the parameter named in `errors[].loc` |
| **500** Internal Server Error | Unhandled exception | `An unexpected error occurred.` — **never** the exception text or a trace | Report the `request_id`; it's logged server-side with the real error |
| **503** Service Unavailable | `/health` only — database unreachable | `{"status": "db_unreachable", …}` (plain JSON, not Problem Details — infra endpoint) | Operational; see docs/OPERATIONS.md |

## Statuses the service does NOT produce (deliberately not documented as behaviour)

- **400** — django-ninja routes all input-shape problems to 422, so a bare 400 never occurs.
- **409** — the API is read-only; nothing can conflict.
- **429** — no rate limiting is configured yet. `errors.py` already maps ninja's `Throttled` to
  the same Problem Details shape, so enabling throttling later needs no contract change — but
  until then a consumer will never see one.

## What is never in an error body

Stack traces, SQL, exception messages (for 500s), API keys, database credentials, file paths
beyond the request's own `instance`, or the contents of any secret file. If you see any of these,
that's a bug — report it.
