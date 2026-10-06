"""
Shared response-size bound for every /api/v1 list endpoint (scalability
foundation, 2026-09-21). Before this, `limit` was an unbounded client-
supplied int — a consumer passing `?limit=5000000` got exactly that many
rows serialized in one response. `clamp_limit` is the one place that cap
lives, so every endpoint enforces the same ceiling rather than each
picking (or forgetting to pick) its own.
"""

from __future__ import annotations

import datetime as dt

from ninja.errors import HttpError

MAX_LIMIT = 500

# P0 production-hardening fast-pass (2026-09-21): bounds an expensive
# date-range scan the same way clamp_limit bounds a response — a consumer
# could otherwise pass date_from=2000-01-01&date_to=2099-01-01 on a
# per-CSP history endpoint. One CSP's own row count is naturally bounded
# (indexed on csp+date), but there's no reason to let the *query itself*
# be unbounded when the caller-visible contract doesn't need it to be.
MAX_DATE_RANGE_DAYS = 366
MAX_LOOKBACK_DAYS = 3650  # 10 years — generous, still a real ceiling


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def validate_date_range(date_from: dt.date | None, date_to: dt.date | None) -> None:
    if date_from and date_to and (date_to - date_from).days > MAX_DATE_RANGE_DAYS:
        raise HttpError(422, f"date_from/date_to span cannot exceed {MAX_DATE_RANGE_DAYS} days.")
