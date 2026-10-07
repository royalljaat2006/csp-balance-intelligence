"""
Rule #19 slab/rate/gap math (PRD §5.1). These exact (mab -> slab) pairs also
appear, independently, as the CASE thresholds in dbt/macros/rule_19.sql —
if you change a boundary here, change it there too; this file's cases are
the tripwire for that drift.
"""

import decimal

import pytest
from csp.rules import (
    annual_cap_for,
    gap_to_min,
    gap_to_next_slab,
    incentive_rate_for,
    is_eligible,
    slab_for,
)

D = decimal.Decimal


@pytest.mark.parametrize(
    ("mab", "expected_slab"),
    [
        (D("0"), "NIL"),
        (D("2500"), "NIL"),
        (D("2501"), "S1"),
        (D("4000"), "S1"),
        (D("4001"), "S2"),
        (D("6000"), "S2"),
        (D("6001"), "S3"),
        (D("10000"), "S3"),
        (D("10001"), "S4"),
        (D("999999"), "S4"),
    ],
)
def test_slab_for_boundaries(mab, expected_slab):
    assert slab_for(mab, account_count=500) == expected_slab


@pytest.mark.parametrize(
    ("slab", "expected_rate", "expected_cap"),
    [
        ("NIL", D("0"), None),
        ("S1", D("1.10"), D("25000")),
        ("S2", D("1.20"), D("35000")),
        ("S3", D("1.25"), D("40000")),
        ("S4", D("1.30"), D("50000")),
    ],
)
def test_incentive_rate_and_cap(slab, expected_rate, expected_cap):
    assert incentive_rate_for(slab) == expected_rate
    assert annual_cap_for(slab) == expected_cap


def test_incentive_rate_for_unknown_slab_raises():
    with pytest.raises(ValueError, match="Unknown slab"):
        incentive_rate_for("BOGUS")


def test_gap_to_min_zero_once_over_threshold():
    assert gap_to_min(D("2500")) == D("1")
    assert gap_to_min(D("2501")) == D("0")
    assert gap_to_min(D("5000")) == D("0")


@pytest.mark.parametrize(
    ("mab", "expected_gap"),
    [
        (D("2000"), D("501")),  # to reach 2501
        (D("2501"), D("1500")),  # to reach 4001
        (D("4000"), D("1")),
        (D("4001"), D("2000")),  # to reach 6001
        (D("10000"), D("1")),
    ],
)
def test_gap_to_next_slab(mab, expected_gap):
    assert gap_to_next_slab(mab, account_count=500) == expected_gap


def test_gap_to_next_slab_is_none_at_top():
    assert gap_to_next_slab(D("15000"), account_count=500) is None


@pytest.mark.parametrize(
    ("account_count", "expected"),
    [(None, False), (0, False), (199, False), (200, True), (500, True)],
)
def test_is_eligible(account_count, expected):
    assert is_eligible(account_count) is expected


# ---- PRD §5.1 "Eligibility gate: minimum 200 BSBD accounts" --------------
# A CSP under 200 accounts is NIL regardless of balance -- the gate
# overrides the balance-based band, it doesn't just sit beside it as a
# separate flag.


@pytest.mark.parametrize("account_count", [None, 0, 1, 199])
def test_slab_for_forces_nil_when_ineligible_even_with_a_high_balance(account_count):
    assert slab_for(D("50000"), account_count=account_count) == "NIL"


@pytest.mark.parametrize("account_count", [200, 201, 10000])
def test_slab_for_uses_real_balance_band_once_eligible(account_count):
    assert slab_for(D("5000"), account_count=account_count) == "S2"


def test_ineligible_nil_slab_has_zero_incentive_rate():
    slab = slab_for(D("50000"), account_count=100)
    assert slab == "NIL"
    assert incentive_rate_for(slab) == D("0")


@pytest.mark.parametrize(
    ("mab", "expected_gap"),
    [
        (D("0"), D("2501")),  # nowhere near the floor
        (D("2500"), D("1")),  # right at the floor's edge
        (D("2501"), D("0")),  # already clears the floor...
        (D("50000"), D("0")),  # ...no matter how far over -- never negative
    ],
)
def test_gap_to_next_slab_for_ineligible_mirrors_nil_floor_gap(mab, expected_gap):
    assert gap_to_next_slab(mab, account_count=50) == expected_gap
