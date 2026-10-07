"""
sync_monthly_summary reads a raw-SQL mart query that uses Postgres-only
syntax (to_char, LATERAL join) — matching dbt-postgres, our only real
target (PRD §12). Skipped against the SQLite dev fallback; runs for real
against `DATABASE_URL` pointed at Postgres (as this was verified against,
by hand, during development — see the command's docstring for the full
pipeline this exercises just the Python half of).
"""

import datetime as dt
import decimal

import pytest
from csp.models import Csp, DailyBalance, MonthlySummary
from django.core.management import call_command
from django.db import connection

pytestmark = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="sync_monthly_summary's mart query is Postgres-only (to_char, LATERAL)",
)


@pytest.fixture
def fake_dbt_mart(db):
    """Stands in for `dbt run` having already built the monthly_summary mart."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            create table if not exists monthly_summary (
                csp_code varchar(32), month varchar(7), days_in_month int,
                days_with_data int, mtd_mab numeric, slab varchar(4),
                incentive_rate_pa numeric, gap_to_min numeric, gap_to_next_slab numeric,
                prev_month_mab numeric, mom_change_abs numeric, mom_change_pct numeric,
                trend_7d_pct numeric, is_eligible boolean, account_count int
            )
            """
        )
        cursor.execute("truncate table monthly_summary")
    yield
    with connection.cursor() as cursor:
        cursor.execute("drop table if exists monthly_summary")


@pytest.mark.django_db
def test_sync_creates_monthly_summary_with_projection(fake_dbt_mart):
    Csp.objects.create(csp_code="1A850001", account_count=300)
    DailyBalance.objects.create(
        csp_id="1A850001",
        balance_date=dt.date(2026, 8, 31),
        daily_avg_balance=decimal.Decimal("2900"),
        source="demo",
    )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into monthly_summary
                (csp_code, month, days_in_month, days_with_data, mtd_mab, slab,
                 incentive_rate_pa, gap_to_min, gap_to_next_slab, prev_month_mab,
                 mom_change_abs, mom_change_pct, trend_7d_pct, is_eligible, account_count)
            values
                ('1A850001', '2026-08', 31, 10, 2600, 'S1', 1.10, 0, 1400,
                 null, null, null, 3.5, true, 300)
            """
        )

    call_command("sync_monthly_summary")

    row = MonthlySummary.objects.get(csp_id="1A850001", month="2026-08")
    assert row.mtd_mab == decimal.Decimal("2600.00")
    assert row.slab == "S1"
    assert row.is_eligible is True
    assert row.trend_flag == "improving"  # trend_7d_pct=3.5 > 2
    # 10 known days at 2600, 21 remaining assumed at the last known 2900:
    expected = (decimal.Decimal("2600") * 10 + decimal.Decimal("2900") * 21) / 31
    assert float(row.projected_mab) == pytest.approx(float(expected), abs=0.01)
    assert row.projected_incentive_annual is not None


@pytest.mark.django_db
def test_sync_is_idempotent(fake_dbt_mart):
    Csp.objects.create(csp_code="1A850002")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into monthly_summary
                (csp_code, month, days_in_month, days_with_data, mtd_mab, slab,
                 incentive_rate_pa, gap_to_min, gap_to_next_slab, prev_month_mab,
                 mom_change_abs, mom_change_pct, trend_7d_pct, is_eligible, account_count)
            values
                ('1A850002', '2026-08', 31, 31, 2000, 'NIL', 0, 501, 501,
                 null, null, null, null, false, null)
            """
        )

    call_command("sync_monthly_summary")
    call_command("sync_monthly_summary")

    assert MonthlySummary.objects.filter(csp_id="1A850002", month="2026-08").count() == 1


@pytest.mark.django_db
def test_sync_creates_stub_csp_if_missing(fake_dbt_mart):
    # No Csp row exists yet for this code — sync should stub-create it,
    # mirroring ingest_transactions' behaviour.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into monthly_summary
                (csp_code, month, days_in_month, days_with_data, mtd_mab, slab,
                 incentive_rate_pa, gap_to_min, gap_to_next_slab, prev_month_mab,
                 mom_change_abs, mom_change_pct, trend_7d_pct, is_eligible, account_count)
            values
                ('1A850099', '2026-08', 31, 31, 3000, 'S1', 1.10, 0, 1001,
                 null, null, null, null, false, null)
            """
        )

    call_command("sync_monthly_summary")

    assert Csp.objects.filter(csp_code="1A850099").exists()
    assert MonthlySummary.objects.filter(csp_id="1A850099").exists()


@pytest.mark.django_db
def test_sync_removes_stale_rows_no_longer_in_the_mart(fake_dbt_mart):
    """A (csp, month) Django still has a row for but the dbt mart no longer
    does (its source DailyBalance was removed and dbt already re-run) must
    be deleted here -- not left lingering forever showing numbers for data
    that no longer exists anywhere upstream. This is the exact bug class
    that let a bogus "Growth" CSP's old MonthlySummary rows keep showing
    real-looking balances on the dashboard after its backfilled DailyBalance
    had already been deleted."""
    Csp.objects.create(csp_code="1A850001", account_count=300)
    MonthlySummary.objects.create(
        csp_id="1A850001", month="2026-07", days_in_month=31, days_with_data=31,
        mtd_mab=decimal.Decimal("15000"), slab="S4", incentive_rate_pa=decimal.Decimal("1.30"),
        gap_to_min=decimal.Decimal("0"), is_eligible=False,
    )
    # The current mart has a row for a *different* month only -- 2026-07 is
    # not in it, simulating its source data having been removed since.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into monthly_summary
                (csp_code, month, days_in_month, days_with_data, mtd_mab, slab,
                 incentive_rate_pa, gap_to_min, gap_to_next_slab, prev_month_mab,
                 mom_change_abs, mom_change_pct, trend_7d_pct, is_eligible, account_count)
            values
                ('1A850001', '2026-08', 31, 10, 2600, 'S1', 1.10, 0, 1400,
                 null, null, null, null, true, 300)
            """
        )

    call_command("sync_monthly_summary")

    assert not MonthlySummary.objects.filter(csp_id="1A850001", month="2026-07").exists()
    assert MonthlySummary.objects.filter(csp_id="1A850001", month="2026-08").exists()


@pytest.mark.django_db
def test_sync_does_not_wipe_table_when_mart_comes_back_empty(fake_dbt_mart):
    """A transient dbt failure returning zero rows must never look like
    "every CSP's data is gone" -- the early-return path (mart empty) must
    never reach the stale-cleanup logic at all."""
    Csp.objects.create(csp_code="1A850001")
    MonthlySummary.objects.create(
        csp_id="1A850001", month="2026-07", days_in_month=31, days_with_data=31,
        mtd_mab=decimal.Decimal("3000"), slab="S1", incentive_rate_pa=decimal.Decimal("1.10"),
        gap_to_min=decimal.Decimal("0"), is_eligible=True,
    )

    call_command("sync_monthly_summary")  # mart table exists but is empty

    assert MonthlySummary.objects.filter(csp_id="1A850001", month="2026-07").exists()
