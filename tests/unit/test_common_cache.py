"""
common/cache.py — the Redis-backed cache-aside/lock helpers behind the
scalability foundation (2026-09-21). These tests exercise the module in
isolation (no view/service involved): fail-open behaviour when the cache
backend errors, generation-based invalidation, and the distributed lock.
"""

from unittest.mock import patch

from common import cache as cache_module
from common.cache import (
    bump_generation,
    cache_aside,
    clear_job_progress,
    get_job_progress,
    set_job_progress,
    try_acquire_lock,
)


def test_cache_aside_calls_fn_on_miss_and_returns_its_value():
    calls = []

    def fn():
        calls.append(1)
        return "computed"

    assert cache_aside("k1", ttl=30, fn=fn) == "computed"
    assert len(calls) == 1


def test_cache_aside_returns_cached_value_without_recalling_fn():
    calls = []

    def fn():
        calls.append(1)
        return "computed"

    cache_aside("k2", ttl=30, fn=fn)
    result = cache_aside("k2", ttl=30, fn=fn)
    assert result == "computed"
    assert len(calls) == 1  # second call was a cache hit, fn not called again


def test_bump_generation_invalidates_previously_cached_value():
    calls = []

    def fn():
        calls.append(1)
        return f"computed-{len(calls)}"

    first = cache_aside("k3", ttl=30, fn=fn)
    bump_generation()
    second = cache_aside("k3", ttl=30, fn=fn)

    assert first != second
    assert len(calls) == 2  # generation bump forced a real recompute


def test_cache_aside_fails_open_when_backend_get_raises():
    """A broken cache backend must degrade to "always compute", never
    crash the caller — see the module's fail-open contract."""
    calls = []

    def fn():
        calls.append(1)
        return "computed"

    with patch.object(cache_module.cache, "get", side_effect=ConnectionError("redis down")):
        result = cache_aside("k4", ttl=30, fn=fn)

    assert result == "computed"
    assert len(calls) == 1


def test_cache_aside_fails_open_when_backend_set_raises():
    def fn():
        return "computed"

    with patch.object(cache_module.cache, "set", side_effect=ConnectionError("redis down")):
        result = cache_aside("k5", ttl=30, fn=fn)

    assert result == "computed"


def test_try_acquire_lock_true_then_false_until_released_or_expired():
    assert try_acquire_lock("job-x", ttl=60) is True
    # Same key, still within TTL -> another instance must not also proceed.
    assert try_acquire_lock("job-x", ttl=60) is False


def test_try_acquire_lock_fails_open_when_backend_unavailable():
    """Every job this guards is independently idempotent, so a broken lock
    backend must let the caller proceed (fail open), not skip a real run."""
    with patch.object(cache_module.cache, "add", side_effect=ConnectionError("redis down")):
        assert try_acquire_lock("job-y", ttl=60) is True


def test_get_generation_defaults_to_zero_and_survives_backend_errors():
    with patch.object(cache_module.cache, "get", side_effect=ConnectionError("redis down")):
        assert cache_module.get_generation() == 0


def test_cache_aside_stampede_guard_only_one_concurrent_caller_computes():
    """P1 fast-pass, 2026-09-21: a second caller arriving mid-compute must
    not also run fn() — it should wait for and reuse the first caller's
    result once the lock is released."""
    calls = []

    def fn():
        calls.append(1)
        return "computed"

    # Simulate "another request is already computing this key" by holding
    # the compute lock ourselves before calling cache_aside.
    key = "csp-tracker:v0:stampede-key"
    assert try_acquire_lock(f"compute:{key}", ttl=30) is True
    try:
        # cache_aside will fail to acquire the lock, wait briefly, find
        # nothing cached (we never populated it), and fall back to
        # computing directly itself — proving it never hangs even when the
        # "holder" never finishes.
        result = cache_aside("stampede-key", ttl=30, fn=fn)
    finally:
        cache_module.release_lock(f"compute:{key}")

    assert result == "computed"
    assert len(calls) == 1


# ---- job progress (Phase: real-time agentic pipeline, 2026-10-07) --------


def test_get_job_progress_is_none_when_nothing_reported():
    assert get_job_progress("never-reported") is None


def test_set_then_get_job_progress_round_trips_real_values():
    set_job_progress("job-p1", processed=4000, total=18420)
    assert get_job_progress("job-p1") == {"processed": 4000, "total": 18420}


def test_set_job_progress_accepts_none_total_when_genuinely_unknown():
    set_job_progress("job-p2", processed=10, total=None)
    assert get_job_progress("job-p2") == {"processed": 10, "total": None}


def test_clear_job_progress_removes_it():
    set_job_progress("job-p3", processed=5, total=10)
    clear_job_progress("job-p3")
    assert get_job_progress("job-p3") is None


def test_set_job_progress_fails_open_when_backend_unavailable():
    with patch.object(cache_module.cache, "set", side_effect=ConnectionError("redis down")):
        set_job_progress("job-p4", processed=1, total=2)  # must not raise


def test_get_job_progress_fails_open_to_none_when_backend_unavailable():
    with patch.object(cache_module.cache, "get", side_effect=ConnectionError("redis down")):
        assert get_job_progress("job-p5") is None
