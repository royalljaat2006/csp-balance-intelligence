"""
/dashboard/transactions/ — transaction intelligence + ingestion health.
Reads through csp.services (list_transactions, get_active_transaction_csp_
count, get_transaction_type_distribution, get_network_daily_trend) and
csp.models.IngestLog directly; these tests are about the view's own
param handling (pagination, ONUS filter, date range) and that real ledger
rows flow through correctly — not the underlying aggregation math, which
test_services_transactions.py already covers.
"""

import datetime as dt
import decimal

import pytest
from csp.models import Csp, IngestLog, Transaction
from django.contrib.auth import get_user_model
from django.db import connection


@pytest.fixture
def staff_user(db):
    return get_user_model().objects.create_user(username="staff", password="pw12345", is_staff=True)


@pytest.fixture
def logged_in_client(client, staff_user):
    client.login(username="staff", password="pw12345")
    return client


@pytest.fixture
def csp(db):
    return Csp.objects.create(csp_code="1A850800", name="Txn Test CSP")


def _txn(csp, ref, txn_type, amount, txn_date, category="withdrawal", direction="in_pool"):
    return Transaction.objects.create(
        ref_number=ref, csp=csp, txn_datetime=dt.datetime.combine(txn_date, dt.time(10, 0)),
        txn_date=txn_date, txn_type=txn_type, category=category, direction=direction,
        amount=decimal.Decimal(str(amount)),
    )


@pytest.mark.django_db
def test_requires_login(client):
    response = client.get("/dashboard/transactions/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_renders_with_no_transactions(logged_in_client, db):
    response = logged_in_client.get("/dashboard/transactions/")
    assert response.status_code == 200
    assert response.context["txn_total"] == 0
    assert response.context["txn_rows"] == []


@pytest.mark.django_db
def test_lists_real_transactions_paginated(logged_in_client, csp):
    today = dt.date.today()
    for i in range(3):
        _txn(csp, f"R{i}", "Deposit", 100 + i, today)

    response = logged_in_client.get("/dashboard/transactions/")
    assert response.context["txn_total"] == 3
    assert len(response.context["txn_rows"]) == 3
    assert response.context["page"] == 1


@pytest.mark.django_db
def test_onus_only_filter(logged_in_client, csp):
    today = dt.date.today()
    _txn(csp, "R1", "AEPS ONUS Withdrawal", 500, today)
    _txn(csp, "R2", "Deposit", 700, today)

    response = logged_in_client.get("/dashboard/transactions/?mode=onus")
    assert response.context["txn_total"] == 1
    assert response.context["txn_rows"][0].ref_number == "R1"
    assert response.context["onus_only"] is True


@pytest.mark.django_db
def test_invalid_page_falls_back_to_one(logged_in_client, db):
    response = logged_in_client.get("/dashboard/transactions/?page=bogus")
    assert response.context["page"] == 1


@pytest.mark.django_db
def test_ingestion_health_shows_real_ingest_logs(logged_in_client, db):
    IngestLog.objects.create(
        source="telegram", file_name="Sep.xlsx", status=IngestLog.Status.SUCCESS,
        rows_read=100, rows_valid=95, rows_rejected=5,
    )
    response = logged_in_client.get("/dashboard/transactions/")
    body = response.content.decode()
    assert "Sep.xlsx" in body
    assert len(response.context["recent_logs"]) == 1


@pytest.fixture
def stale_daily_activity_mart(db):
    """A daily_activity mart whose latest row is several days in the past —
    stands in for a real end-of-day batch ledger where dbt hasn't (and,
    realistically, never will) catch up to the wall-clock date."""
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
        latest = dt.date.today() - dt.timedelta(days=3)
        previous = latest - dt.timedelta(days=1)
        onus_zeros = (0, 0, 0, 0, 0, 0, 0, 0, 0)
        rows = [
            ("1A850800", str(previous), 4, 400, 2, 200, 2, 200, 200, 200, 0, *onus_zeros),
            ("1A850800", str(latest), 6, 600, 3, 300, 3, 300, 300, 300, 0, *onus_zeros),
        ]
        cursor.executemany(
            """
            insert into daily_activity
                (csp_code, activity_date, txn_count, txn_amount, withdrawal_count,
                 withdrawal_amount, deposit_count, deposit_amount, cash_in_pool,
                 cash_out_pool, net_flow, onus_txn_count, onus_txn_amount,
                 onus_withdrawal_count, onus_withdrawal_amount, onus_deposit_count,
                 onus_deposit_amount, onus_cash_in_pool, onus_cash_out_pool, onus_net_flow)
            values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
    return latest, previous


@pytest.mark.django_db
def test_kpi_cards_anchor_on_latest_mart_date_not_wall_clock(
    logged_in_client, stale_daily_activity_mart
):
    latest, previous = stale_daily_activity_mart
    response = logged_in_client.get("/dashboard/transactions/")

    assert response.context["today"] == latest
    assert response.context["yesterday"] == previous
    assert response.context["is_wall_clock_today"] is False
    assert response.context["txn_count_today"] == 6  # the latest row's real count, not 0

    body = response.content.decode()
    # The label must show the real date, never silently claim "today".
    assert latest.strftime("%d %b") in body
    assert "no data yet" not in body


@pytest.mark.django_db
def test_kpi_cards_say_today_when_mart_is_genuinely_current(logged_in_client, csp):
    today = dt.date.today()
    _txn(csp, "R1", "Deposit", 500, today)
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
            insert into daily_activity
                (csp_code, activity_date, txn_count, txn_amount, withdrawal_count,
                 withdrawal_amount, deposit_count, deposit_amount, cash_in_pool,
                 cash_out_pool, net_flow, onus_txn_count, onus_txn_amount,
                 onus_withdrawal_count, onus_withdrawal_amount, onus_deposit_count,
                 onus_deposit_amount, onus_cash_in_pool, onus_cash_out_pool, onus_net_flow)
            values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("1A850800", str(today), 1, 500, 0, 0, 1, 500, 500, 0, 500, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        )

    response = logged_in_client.get("/dashboard/transactions/")
    assert response.context["is_wall_clock_today"] is True
    body = response.content.decode()
    assert "Transactions today" in body


@pytest.mark.django_db
def test_type_distribution_json_is_valid_json_serializable(logged_in_client, csp):
    import json

    today = dt.date.today()
    _txn(csp, "R1", "Deposit", 500, today)
    response = logged_in_client.get("/dashboard/transactions/")
    body = response.content.decode()
    # The json_script block must actually be valid JSON, not a Python repr
    # leaking Decimal(...) syntax into the page (would break the chart JS).
    start = body.index('id="type-distribution-data"')
    start = body.index(">", start) + 1
    end = body.index("</script>", start)
    parsed = json.loads(body[start:end])
    assert parsed[0]["txn_type"] == "Deposit"
    assert parsed[0]["amount"] == 500.0
