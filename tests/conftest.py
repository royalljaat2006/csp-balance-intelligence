"""
Shared pytest fixtures for the whole suite.

Autouse cache clearing (scalability foundation, 2026-09-21): once
dashboard views started reading through common.cache.cache_aside
(config/settings/base.py's CACHES -> LocMemCache in dev/test), the cache
backend persists across tests within one pytest process — a value cached
by one test would otherwise leak into the next test's assertions, since
LocMemCache is process-wide, not per-test. Real production code paths
invalidate the cache themselves the moment new data lands (see
common/cache.py's bump_generation() and its call sites in
ingest_calling_sheet/sync_monthly_summary/file_ingest) — but a test that
creates DailyBalance/MonthlySummary rows directly via the ORM, bypassing
those commands, never triggers that invalidation, so it needs the cache
cleared out from under it instead.
"""

import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()
