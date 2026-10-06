"""
csp.services' daily-activity readers query the dbt-materialized
`daily_activity` mart with plain ANSI SQL (no Postgres-only syntax, unlike
sync_monthly_summary's mart query) — so unlike test_sync_monthly_summary.py
these run against the SQLite dev/test fallback too.
"""

import datetime as dt

import pytest
from csp import services
from django.db import connection


@pytest.fixture
def fake_daily_activity_mart(db):
    """Stands in for `dbt run` having already built the daily_activity mart."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            create table if not exists daily_activity (
                csp_code varchar(32), activity_date date, txn_count int,
                txn_amount numeric, withdrawal_count int, withdrawal_amount numeric,
                deposit_count int, deposit_amount numeric, cash_in_pool numeric,
                cash_out_pool numeric, net_flow numeric,
                onus_txn_count int, onus_txn_amount numeric,
                onus_withdrawal_count int, onus_withdrawal_amount numeric,
                onus_deposit_count int, onus_deposit_amount numeric,
                onus_cash_in_pool numeric, onus_cash_out_pool numeric, onus_net_flow numeric
            )
            """
        )
        cursor.execute("delete from daily_activity")
        # These fixtures only exercise Overall-mode aggregation — the
        # trailing zeros are the (unused-by-these-tests) ONUS columns.
        onus_zeros = (0, 0, 0, 0, 0, 0, 0, 0, 0)
        rows = [
            ("1A850001", "2026-08-01", 10, 1000, 6, 600, 4, 400, 600, 400, 200, *onus_zeros),
            ("1A850001", "2026-08-02", 20, 2000, 12, 1200, 8, 800, 1200, 800, 400, *onus_zeros),
            ("1A850001", "2026-08-05", 5, 500, 3, 300, 2, 200, 300, 200, 100, *onus_zeros),
            ("1A850002", "2026-08-02", 7, 700, 4, 400, 3, 300, 400, 300, 100, *onus_zeros),
        ]
        cursor.executemany(
            """
            insert into daily_activity
                (csp_code, activity_date, txn_count, txn_amount, withdrawal_count,
                 withdrawal_amount, deposit_count, deposit_amount, cash_in_pool,
                 cash_out_pool, net_flow, onus_txn_count, onus_txn_amount,
                 onus_withdrawal_count, onus_withdrawal_amount, onus_deposit_count,
                 onus_deposit_amount, onus_cash_in_pool, onus_cash_out_pool, onus_net_flow)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            rows,
        )
    yield
    with connection.cursor() as cursor:
        cursor.execute("drop table if exists daily_activity")


@pytest.mark.django_db
def test_get_daily_activity_date_bounds_empty_without_mart(db):
    assert services.get_daily_activity_date_bounds() is None


@pytest.mark.django_db
def test_get_daily_activity_date_bounds(fake_daily_activity_mart):
    assert services.get_daily_activity_date_bounds() == (
        dt.date(2026, 8, 1),
        dt.date(2026, 8, 5),
    )


@pytest.mark.django_db
def test_get_network_daily_trend_no_filter_returns_everything(fake_daily_activity_mart):
    rows = services.get_network_daily_trend()
    assert [r["activity_date"] for r in rows] == [dt.date(2026, 8, 1), dt.date(2026, 8, 2), dt.date(2026, 8, 5)]
    # 2026-08-02 sums both CSPs
    aug2 = next(r for r in rows if r["activity_date"] == dt.date(2026, 8, 2))
    assert aug2["txn_count"] == 27


@pytest.mark.django_db
def test_get_network_daily_trend_date_range_filters(fake_daily_activity_mart):
    rows = services.get_network_daily_trend(date_from=dt.date(2026, 8, 2), date_to=dt.date(2026, 8, 2))
    assert [r["activity_date"] for r in rows] == [dt.date(2026, 8, 2)]


@pytest.mark.django_db
def test_get_network_daily_trend_includes_onus_columns(db):
    """ONUS-mode columns (dbt's onus_-prefixed counterparts) must flow
    through alongside Overall-mode ones — this is what the Transactions
    page's ONUS/Overall toggle reads."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            create table if not exists daily_activity (
                csp_code varchar(32), activity_date date, txn_count int,
                txn_amount numeric, withdrawal_count int, withdrawal_amount numeric,
                deposit_count int, deposit_amount numeric, cash_in_pool numeric,
                cash_out_pool numeric, net_flow numeric,
                onus_txn_count int, onus_txn_amount numeric,
                onus_withdrawal_count int, onus_withdrawal_amount numeric,
                onus_deposit_count int, onus_deposit_amount numeric,
                onus_cash_in_pool numeric, onus_cash_out_pool numeric, onus_net_flow numeric
            )
            """
        )
        cursor.execute("delete from daily_activity")
        cursor.execute(
            """
            insert into daily_activity values
                ('1A850001', '2026-08-01', 20, 2000, 12, 1200, 8, 800, 1200, 800, 400,
                 5, 500, 3, 300, 2, 200, 300, 200, 100)
            """
        )
    rows = services.get_network_daily_trend()
    assert rows[0]["txn_count"] == 20
    assert rows[0]["onus_txn_count"] == 5
    assert rows[0]["onus_txn_amount"] == 500


@pytest.mark.django_db
def test_get_csp_daily_activity_filters_by_csp_and_date(fake_daily_activity_mart):
    rows = services.get_csp_daily_activity("1A850001", date_from=dt.date(2026, 8, 2))
    assert [r["activity_date"] for r in rows] == [dt.date(2026, 8, 2), dt.date(2026, 8, 5)]
    assert all(r["txn_count"] in (20, 5) for r in rows)
