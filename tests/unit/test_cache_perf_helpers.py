"""
common/cache.py -- the helpers behind the fast-loading work: peeking at a cached value without
computing it, invalidating by decorator, and the "did this poll change anything" fingerprint.
"""

from common.cache import (
    bump_generation,
    bumps_generation,
    cache_aside,
    cache_peek,
    fingerprint_unchanged,
    get_generation,
    remember_fingerprint,
)


def test_peek_never_computes_and_sees_what_cache_aside_stored():
    assert cache_peek("p1") == (False, None)
    cache_aside("p1", ttl=30, fn=lambda: {"n": 5})
    assert cache_peek("p1") == (True, {"n": 5})


def test_peek_does_not_see_a_retired_generation():
    cache_aside("p2", ttl=30, fn=lambda: 1)
    bump_generation()
    assert cache_peek("p2") == (False, None)


def test_a_stored_falsy_value_is_still_a_hit_only_when_it_is_not_none():
    cache_aside("p3", ttl=30, fn=lambda: 0)
    assert cache_peek("p3") == (True, 0)


def test_bumps_generation_invalidates_after_the_write_even_if_it_fails():
    @bumps_generation
    def write(ok: bool) -> str:
        if not ok:
            raise RuntimeError("half written")
        return "done"

    before = get_generation()
    assert write(True) == "done"
    assert get_generation() == before + 1
    try:
        write(False)
    except RuntimeError:
        pass
    assert get_generation() == before + 2  # a partial write may have changed data too


def test_fingerprint_is_unchanged_only_for_the_same_digest():
    assert fingerprint_unchanged("sheet", "abc") is False  # nothing remembered yet
    remember_fingerprint("sheet", "abc")
    assert fingerprint_unchanged("sheet", "abc") is True
    assert fingerprint_unchanged("sheet", "abd") is False
    assert fingerprint_unchanged("other", "abc") is False
