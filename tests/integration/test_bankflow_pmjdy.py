"""Total-account-count snapshots (AccountCountObservation): the newest reading is current."""

import datetime as dt

import pytest
from bankflow import pmjdy
from bankflow.models import AccountCountObservation
from csp.models import Csp
from django.utils import timezone

pytestmark = pytest.mark.django_db


def at(day, hour=6):
    return dt.datetime(2026, 10, day, hour, 0, tzinfo=timezone.get_current_timezone())


@pytest.fixture
def csps():
    for c in ("A", "B"):
        Csp.objects.create(csp_code=c, name=c, account_count=300)


def test_latest_count_is_current_not_the_old_snapshot(csps):
    pmjdy.capture_counts({"A": 3000}, at(10))
    pmjdy.capture_counts({"A": 3090}, at(11))
    cur, _ = pmjdy.latest_counts(["A"])["A"]
    assert cur == 3090  # growth +90 vs the previous snapshot, never "still 3000"
    assert pmjdy.counts_as_of(dt.date(2026, 10, 10), ["A"])["A"][0] == 3000


def test_capture_is_idempotent_within_a_day_and_snapshots_daily(csps):
    pmjdy.capture_counts({"A": 3000, "B": 10}, at(10, 6))
    again = pmjdy.capture_counts({"A": 3000, "B": 10}, at(10, 9))
    assert again["inserted"] == 0
    nxt = pmjdy.capture_counts({"A": 3000, "B": 10}, at(11, 6))  # unchanged, but a new day
    assert nxt["inserted"] == 2 and nxt["changed"] == 0
    assert AccountCountObservation.objects.count() == 4


def test_intraday_change_is_recorded_and_decrease_is_flagged(csps):
    pmjdy.capture_counts({"A": 3000}, at(10, 6))
    r = pmjdy.capture_counts({"A": 2990}, at(10, 15))
    assert r["changed"] == 1 and r["decreased"] == 1
    assert pmjdy.latest_counts(["A"])["A"][0] == 2990  # an observation, stored as read


def test_unknown_codes_are_reported_never_created(csps):
    r = pmjdy.capture_counts({"A": 5, "ZZZ": 9}, at(10))
    assert r["unknown_codes"] == ["ZZZ"] and r["n_unknown_codes"] == 1
    assert not Csp.objects.filter(csp_code="ZZZ").exists()
    assert not AccountCountObservation.objects.filter(csp_id="ZZZ").exists()


def test_daily_network_counts_carry_forward_without_zero_fill(csps):
    pmjdy.capture_counts({"A": 100}, at(10))
    pmjdy.capture_counts({"A": 110, "B": 50}, at(12))
    rows = pmjdy.daily_network_counts(dt.date(2026, 10, 9), dt.date(2026, 10, 13))
    totals = {r["date"].day: (r["total_accounts"], r["growth"]) for r in rows}
    assert totals[9] == (None, None)  # before any reading: not zero
    assert totals[10] == (100, None) and totals[11] == (100, 0)
    assert totals[12] == (160, 60) and totals[13] == (160, 0)
