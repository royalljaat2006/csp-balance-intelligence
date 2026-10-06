"""
csp.services' day-wise account-growth readers — net change (opens minus
closes) between consecutive DailyBalance readings, per CSP and network-wide.
See dashboard/views.py's csp_detail/trends for where these feed the UI.
"""

import datetime as dt
import decimal

import pytest
from csp import services
from csp.models import Csp, DailyBalance


@pytest.fixture
def two_csps(db):
    a = Csp.objects.create(csp_code="1A850001", name="CSP A")
    b = Csp.objects.create(csp_code="1A850002", name="CSP B")
    return a, b


@pytest.mark.django_db
def test_get_csp_account_growth_empty_without_readings(two_csps):
    a, _ = two_csps
    assert services.get_csp_account_growth(a) == []


@pytest.mark.django_db
def test_get_csp_account_growth_computes_net_new(two_csps):
    a, _ = two_csps
    DailyBalance.objects.create(
        csp=a, balance_date=dt.date(2026, 8, 1), daily_avg_balance=decimal.Decimal("3000"),
        account_count=100, source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=a, balance_date=dt.date(2026, 8, 3), daily_avg_balance=decimal.Decimal("3100"),
        account_count=108, source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=a, balance_date=dt.date(2026, 8, 5), daily_avg_balance=decimal.Decimal("3050"),
        account_count=104, source=DailyBalance.Source.CALLING_SHEET,
    )

    growth = services.get_csp_account_growth(a)

    assert [g["account_count"] for g in growth] == [100, 108, 104]
    assert growth[0]["net_new"] is None  # no prior reading to diff against
    assert growth[1]["net_new"] == 8
    assert growth[2]["net_new"] == -4  # net decline is a real, valid signal


@pytest.mark.django_db
def test_get_csp_account_growth_skips_readings_without_account_count(two_csps):
    a, _ = two_csps
    DailyBalance.objects.create(
        csp=a, balance_date=dt.date(2026, 8, 1), daily_avg_balance=decimal.Decimal("3000"),
        account_count=None, source=DailyBalance.Source.DEMO,
    )
    DailyBalance.objects.create(
        csp=a, balance_date=dt.date(2026, 8, 2), daily_avg_balance=decimal.Decimal("3000"),
        account_count=50, source=DailyBalance.Source.CALLING_SHEET,
    )
    growth = services.get_csp_account_growth(a)
    assert len(growth) == 1
    assert growth[0]["date"] == dt.date(2026, 8, 2)


@pytest.mark.django_db
def test_get_network_account_growth_sums_across_csps(two_csps):
    a, b = two_csps
    DailyBalance.objects.create(
        csp=a, balance_date=dt.date(2026, 8, 1), daily_avg_balance=decimal.Decimal("3000"),
        account_count=100, source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=b, balance_date=dt.date(2026, 8, 1), daily_avg_balance=decimal.Decimal("2000"),
        account_count=50, source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=a, balance_date=dt.date(2026, 8, 2), daily_avg_balance=decimal.Decimal("3000"),
        account_count=110, source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=b, balance_date=dt.date(2026, 8, 2), daily_avg_balance=decimal.Decimal("2000"),
        account_count=52, source=DailyBalance.Source.CALLING_SHEET,
    )

    growth = services.get_network_account_growth()

    assert [g["total_accounts"] for g in growth] == [150, 162]
    assert [g["csp_count"] for g in growth] == [2, 2]
    assert growth[0]["net_new"] is None
    assert growth[1]["net_new"] == 12


@pytest.mark.django_db
def test_get_network_balance_trend_empty_without_readings(db):
    assert services.get_network_balance_trend() == []


@pytest.mark.django_db
def test_get_network_balance_trend_averages_across_csps(two_csps):
    a, b = two_csps
    DailyBalance.objects.create(
        csp=a, balance_date=dt.date(2026, 8, 1), daily_avg_balance=decimal.Decimal("3000"),
        source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=b, balance_date=dt.date(2026, 8, 1), daily_avg_balance=decimal.Decimal("5000"),
        source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=a, balance_date=dt.date(2026, 8, 2), daily_avg_balance=decimal.Decimal("4000"),
        source=DailyBalance.Source.CALLING_SHEET,
    )

    trend = services.get_network_balance_trend()

    assert trend[0]["date"] == dt.date(2026, 8, 1)
    assert trend[0]["avg_balance"] == 4000.0
    assert trend[0]["reporting_count"] == 2
    assert trend[1]["date"] == dt.date(2026, 8, 2)
    assert trend[1]["avg_balance"] == 4000.0
    assert trend[1]["reporting_count"] == 1


@pytest.mark.django_db
def test_get_network_balance_trend_respects_days_window(two_csps):
    a, _ = two_csps
    for i, val in enumerate([1000, 2000, 3000, 4000]):
        DailyBalance.objects.create(
            csp=a, balance_date=dt.date(2026, 8, 1) + dt.timedelta(days=i),
            daily_avg_balance=decimal.Decimal(val), source=DailyBalance.Source.CALLING_SHEET,
        )

    trend = services.get_network_balance_trend(days=2)

    assert [t["date"] for t in trend] == [dt.date(2026, 8, 3), dt.date(2026, 8, 4)]
