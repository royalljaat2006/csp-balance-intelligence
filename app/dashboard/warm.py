"""
Pre-computes the figures every page needs, as soon as the data behind them has changed.

The dashboard caches by "data generation" (common/cache.py): any ingest or calculation that really
changes data bumps the generation, which retires every cached figure at once. Without this module
the first visitor after each change pays for recomputing them (seconds on a large dataset); with it,
the worker does that work in the background right after the change and visitors always hit a warm
cache. It is safe to skip or fail: a cold page still computes what it needs itself.
"""

from __future__ import annotations

import time

import structlog
from bankflow import flows as bankflow_flows
from bankflow import intelligence, services
from common.cache import get_generation
from django.core.cache import cache
from django.utils import timezone

from . import shell

logger = structlog.get_logger(__name__)

_MARKER_KEY = "csp-tracker:warm:last"
# the ranges the Home page offers (views._RANGES)
RANGES = (7, 30, 90)


def _steps() -> list[tuple[str, object]]:
    today = timezone.localdate()
    steps: list[tuple[str, object]] = [
        ("attention_count", shell.attention_count),
        ("day_compare", shell.day_compare),
        ("day_rows", lambda: intelligence.csp_day_compare_rows(today, limit=1)),
        ("proven_complete", bankflow_flows.proven_complete_dates),
        ("day_coverage", services.day_coverage),
        ("partial_snapshots", services.partial_snapshot_report),
        ("resolution_stats", services.code_resolution_stats),
    ]
    for days in RANGES:
        date_from, date_to = intelligence.resolve_range(days)
        steps += [
            (f"summary_{days}d",
             lambda f=date_from, t=date_to: intelligence.dashboard_summary(f, t)),
            (f"movement_{days}d",
             lambda f=date_from, t=date_to: intelligence.network_movement(f, t)),
            (f"csp_rows_{days}d", lambda f=date_from, t=date_to: intelligence.csp_growth(
                f, t, sort="net_flow", descending=True, limit=1, offset=0)),
        ]
    return steps


def warm_dashboard_cache(*, force: bool = False) -> dict:
    """Compute the shared figures for the current data generation and today's date.

    Does nothing if this generation/date has already been warmed (unless `force`). Each step is
    isolated: one failing figure never stops the others. Returns what happened, for the log."""
    state = f"{get_generation()}:{timezone.localdate()}"
    if not force and cache.get(_MARKER_KEY) == state:
        return {"warmed": False, "reason": "already_warm", "state": state, "steps": {}}
    started = time.monotonic()
    results: dict[str, str] = {}
    for name, fn in _steps():
        t0 = time.monotonic()
        try:
            fn()  # type: ignore[operator]
            results[name] = f"{(time.monotonic() - t0) * 1000:.0f}ms"
        except Exception as exc:  # noqa: BLE001 -- a figure that cannot be computed stays lazy
            logger.warning("warm_step_failed", step=name, error=str(exc)[:200])
            results[name] = f"failed: {type(exc).__name__}"
    # a generation bump while we ran means what we computed is already retired: do not mark it warm
    if get_generation() == int(state.split(":", 1)[0]):
        cache.set(_MARKER_KEY, state, timeout=None)
    return {"warmed": True, "state": state, "seconds": round(time.monotonic() - started, 2),
            "steps": results}
