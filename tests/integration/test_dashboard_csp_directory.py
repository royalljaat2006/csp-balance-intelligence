"""
/dashboard/csps/ — browse every CSP, whether or not it has reported yet.
Merges Csp identity + comparison.bulk_compare_csps() + MonthlySummary;
these tests are about that merge (a CSP with no data still gets a row,
reporting_status reflects reality) and that the page renders.
"""

import datetime as dt
import decimal

import pytest
from csp.models import Csp, DailyBalance
from django.contrib.auth import get_user_model


@pytest.fixture
def staff_user(db):
    return get_user_model().objects.create_user(username="staff", password="pw12345", is_staff=True)


@pytest.fixture
def logged_in_client(client, staff_user):
    client.login(username="staff", password="pw12345")
    return client


@pytest.mark.django_db
def test_requires_login(client):
    response = client.get("/dashboard/csps/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_renders_with_no_csps(logged_in_client, db):
    response = logged_in_client.get("/dashboard/csps/")
    assert response.status_code == 200
    assert response.context["rows"] == []


@pytest.mark.django_db
def test_csp_with_no_data_still_gets_a_row(logged_in_client, db):
    Csp.objects.create(csp_code="1A850700", name="Never Reported")
    response = logged_in_client.get("/dashboard/csps/")
    rows = {r["code"]: r for r in response.context["rows"]}
    assert "1A850700" in rows
    assert rows["1A850700"]["status"] == "NOT_COMPUTED"  # no estimate, said so
    assert rows["1A850700"]["sbi_avg"] is None and rows["1A850700"]["est"] is None
    assert "No estimate" in response.content.decode()


@pytest.mark.django_db
def test_csp_reporting_today_shows_reporting_status(logged_in_client, db):
    today = dt.date.today()
    csp = Csp.objects.create(csp_code="1A850701", name="Reporting CSP")
    DailyBalance.objects.create(
        csp=csp, balance_date=today, daily_avg_balance=decimal.Decimal("3000"),
        source=DailyBalance.Source.CALLING_SHEET,
    )
    response = logged_in_client.get("/dashboard/csps/")
    rows = {r["code"]: r for r in response.context["rows"]}
    assert rows["1A850701"]["sbi_avg"] == decimal.Decimal("3000")
    assert rows["1A850701"]["sbi_date"] == today


@pytest.mark.django_db
def test_invalid_filters_fall_back_to_defaults(logged_in_client, db):
    response = logged_in_client.get("/dashboard/csps/?show=bogus&sort=bogus&order=sideways")
    assert response.context["show"] == "all" and response.context["sort"] == "name"
    assert response.status_code == 200
