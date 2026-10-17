"""
The two things every page shows around its own content -- the sidebar's "needs attention" count and
the today-vs-yesterday strip -- used to be computed while the page was being built, so a cold cache
made EVERY page wait for them (about 0.3 s each, and more before the queries were fixed).

Now they are computed once per data generation and shared (cache_aside, same keys everywhere), the
page only PEEKS at the cache, and when the value is not there yet the page renders at once with a
placeholder and fetches it afterwards from /dashboard/shell/. The background cache warmer
(`manage.py warm_dashboard_cache`) computes them as soon as the data changes, so users normally
never see the placeholder at all.
"""

from __future__ import annotations

from common.cache import cache_aside, cache_peek
from django.conf import settings
from django.utils import timezone

ATTENTION_KEY = "nav:attention_count"


def day_compare_key() -> str:
    return f"nav:daycompare:{timezone.localdate()}"


def _compute_attention_count() -> int:
    """What the Review page opens with: data categories that are open, plus CSP alerts that are
    high or medium. Same functions the page uses, so the badge and the page cannot disagree."""
    from bankflow import intelligence, review

    frm, to = intelligence.resolve_range(30)
    items = intelligence.exceptions(frm, to, limit=100000)["items"]
    alerts = sum(1 for i in items if i["csp_code"] and i["severity"] in ("high", "medium"))
    return alerts + review.review_queue(30)["open"]


def _compute_day_compare() -> dict:
    from bankflow import intelligence

    return intelligence.day_compare(timezone.localdate())


def attention_count() -> int | None:
    """Items worth checking (high + medium); None -- badge hidden -- if it cannot be computed.
    Never a placeholder number."""
    try:
        return cache_aside(ATTENTION_KEY, ttl=settings.DASHBOARD_CACHE_TTL,
                           fn=_compute_attention_count) or None
    except Exception:  # noqa: BLE001 -- the shell must never fail because a badge could not load
        return None


def day_compare() -> dict | None:
    """Today (the present IST day) against yesterday; None -- strip hidden -- if it cannot be
    computed."""
    try:
        return cache_aside(day_compare_key(), ttl=settings.DASHBOARD_CACHE_TTL,
                           fn=_compute_day_compare)
    except Exception:  # noqa: BLE001
        return None


def peek_attention_count() -> tuple[bool, int | None]:
    """(is it cached, the count) without computing anything."""
    hit, value = cache_peek(ATTENTION_KEY)
    return hit, ((value or None) if hit else None)


def peek_day_compare() -> tuple[bool, dict | None]:
    return cache_peek(day_compare_key())
