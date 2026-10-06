"""api/pagination.py — the shared response-size/date-range caps."""

import datetime as dt

import pytest
from api.pagination import MAX_LIMIT, clamp_limit, validate_date_range
from ninja.errors import HttpError


def test_clamp_limit_passes_through_a_reasonable_value():
    assert clamp_limit(50) == 50


def test_clamp_limit_caps_an_oversized_request():
    assert clamp_limit(5_000_000) == MAX_LIMIT


def test_clamp_limit_floors_a_non_positive_value():
    assert clamp_limit(0) == 1
    assert clamp_limit(-10) == 1


def test_validate_date_range_allows_a_reasonable_span():
    validate_date_range(dt.date(2026, 1, 1), dt.date(2026, 6, 1))  # must not raise


def test_validate_date_range_allows_missing_bounds():
    validate_date_range(None, None)
    validate_date_range(dt.date(2026, 1, 1), None)


def test_validate_date_range_rejects_an_oversized_span():
    with pytest.raises(HttpError):
        validate_date_range(dt.date(2000, 1, 1), dt.date(2026, 1, 1))
