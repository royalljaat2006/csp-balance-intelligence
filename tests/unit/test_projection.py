import decimal

import pytest
from csp.projection import (
    build_projection,
    project_incentive_annual,
    project_month_end_mab,
    trend_flag_for,
)

D = decimal.Decimal


def test_project_month_end_mab_extrapolates_last_known_balance():
    # 10 days in at 2600 avg, last known day was 2700, 20 days left in a 30-day month.
    result = project_month_end_mab(
        mtd_mab=D("2600"), days_with_data=10, days_in_month=30, last_known_balance=D("2700")
    )
    expected = (D("2600") * 10 + D("2700") * 20) / 30
    assert result == expected


def test_project_month_end_mab_month_already_complete_returns_mtd():
    result = project_month_end_mab(
        mtd_mab=D("3000"), days_with_data=31, days_in_month=31, last_known_balance=D("9999")
    )
    assert result == D("3000")


@pytest.mark.parametrize(("days_with_data", "days_in_month"), [(0, 30), (30, 0), (-1, 30)])
def test_project_month_end_mab_no_data_returns_none(days_with_data, days_in_month):
    assert (
        project_month_end_mab(
            mtd_mab=D("2600"),
            days_with_data=days_with_data,
            days_in_month=days_in_month,
            last_known_balance=D("2700"),
        )
        is None
    )


@pytest.mark.parametrize(
    ("pct", "expected"),
    [
        (None, "stable"),
        (D("5"), "improving"),
        (D("-5"), "declining"),
        (D("1"), "stable"),
        (D("2"), "stable"),
    ],
)
def test_trend_flag_for(pct, expected):
    assert trend_flag_for(pct) == expected


def test_project_incentive_annual_none_without_account_count():
    assert project_incentive_annual(projected_mab=D("3000"), account_count=None) is None


def test_project_incentive_annual_nil_slab_is_zero():
    assert project_incentive_annual(projected_mab=D("2000"), account_count=300) == D("0")


def test_project_incentive_annual_computed_and_uncapped():
    # slab S1 (2501-4000): rate 1.10%, cap 25000
    result = project_incentive_annual(projected_mab=D("3000"), account_count=100)
    expected = D("3000") * 100 * D("1.10") / D("100")  # = 330000 * 1.10% = 3300
    assert result == expected
    assert result < D("25000")  # under cap in this case


def test_project_incentive_annual_capped():
    # Large portfolio that would exceed the S4 cap of 50000.
    result = project_incentive_annual(projected_mab=D("15000"), account_count=1000)
    assert result == D("50000")


def test_build_projection_bundles_everything():
    result = build_projection(
        mtd_mab=D("2600"),
        days_with_data=10,
        days_in_month=30,
        last_known_balance=D("2700"),
        trend_7d_pct=D("5"),
        account_count=300,
    )
    assert result["projected_slab"] == "S1"
    assert result["trend_flag"] == "improving"
    assert result["projected_mab"] is not None
    assert result["projected_incentive_annual"] is not None


def test_build_projection_no_data_yields_nulls():
    result = build_projection(
        mtd_mab=D("0"),
        days_with_data=0,
        days_in_month=30,
        last_known_balance=D("0"),
        trend_7d_pct=None,
        account_count=None,
    )
    assert result["projected_mab"] is None
    assert result["projected_slab"] == ""
    assert result["projected_incentive_annual"] is None
    assert result["trend_flag"] == "stable"
