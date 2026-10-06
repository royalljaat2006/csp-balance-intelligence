"""
/dashboard/comparisons/ — the CSP Daily Comparison tab. Reads through
csp/comparison.py exactly like the API does (test_api_comparisons.py covers
the comparison math itself); these tests are about the view/template
wiring: login-gating, query-param handling, and that seeded data actually
renders into the page (KPI counts, top movers, at-risk reasons, embedded
row JSON).
"""

import datetime as dt
import decimal
import json
import re

import pytest
from csp import comparison as cmp
from csp.models import Csp, DailyBalance
from django.contrib.auth import get_user_model


@pytest.fixture
def staff_user(db):
    return get_user_model().objects.create_user(username="staff", password="pw12345", is_staff=True)


@pytest.fixture
def logged_in_client(client, staff_user):
    client.login(username="staff", password="pw12345")
    return client


def _balance(csp, date, amount):
    DailyBalance.objects.create(
        csp=csp, balance_date=date, daily_avg_balance=decimal.Decimal(str(amount)),
        source=DailyBalance.Source.CALLING_SHEET,
    )


def _extract_rows(html: str) -> list[dict]:
    match = re.search(r'<script id="comparison-data"[^>]*>(.*?)</script>', html, re.S)
    assert match, "comparison-data json_script block not found in rendered page"
    return json.loads(match.group(1))


@pytest.mark.django_db
def test_requires_login(client):
    response = client.get("/dashboard/comparisons/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_renders_with_no_csps(logged_in_client, db):
    response = logged_in_client.get("/dashboard/comparisons/")
    assert response.status_code == 200
    assert _extract_rows(response.content.decode()) == []


@pytest.mark.django_db
def test_renders_growth_and_decline_kpis(logged_in_client, db):
    grower = Csp.objects.create(csp_code="1A850801", name="Grower")
    decliner = Csp.objects.create(csp_code="1A850802", name="Decliner")
    _balance(grower, dt.date(2026, 9, 26), 3000)
    _balance(grower, dt.date(2026, 9, 27), 4000)
    _balance(decliner, dt.date(2026, 9, 26), 4000)
    _balance(decliner, dt.date(2026, 9, 27), 3000)

    response = logged_in_client.get("/dashboard/comparisons/?current_date=2026-09-27")
    assert response.status_code == 200
    html = response.content.decode()

    rows = _extract_rows(html)
    by_code = {r["csp_code"]: r for r in rows}
    assert by_code["1A850801"]["trend"] == "GROWTH"
    assert by_code["1A850802"]["trend"] == "DECLINE"
    assert "Grower" in html  # top-growers section
    assert "Decliner" in html  # top-decliners section


@pytest.mark.django_db
def test_invalid_mode_falls_back_to_overall(logged_in_client, db):
    response = logged_in_client.get("/dashboard/comparisons/?mode=bogus")
    assert response.status_code == 200
    assert response.context["mode"] == "overall"


@pytest.mark.django_db
def test_invalid_comparison_type_falls_back_to_yesterday(logged_in_client, db):
    response = logged_in_client.get("/dashboard/comparisons/?comparison_type=bogus")
    assert response.status_code == 200
    assert response.context["comparison_type"] == cmp.COMPARISON_TYPE_YESTERDAY


@pytest.mark.django_db
def test_same_date_previous_month_resolves_comparison_date(logged_in_client, db):
    response = logged_in_client.get(
        "/dashboard/comparisons/?current_date=2026-09-27"
        "&comparison_type=same_date_previous_month"
    )
    assert response.status_code == 200
    assert response.context["comparison_date"] == "2026-08-27"


@pytest.mark.django_db
def test_at_risk_section_shows_explainable_reason(logged_in_client, db):
    risky = Csp.objects.create(csp_code="1A850803", name="Risky CSP", account_count=250)
    _balance(risky, dt.date(2026, 9, 26), 2540)
    _balance(risky, dt.date(2026, 9, 27), 2510)
    cmp.build_daily_snapshot(risky.csp_code, dt.date(2026, 9, 27))

    response = logged_in_client.get(
        "/dashboard/comparisons/?current_date=2026-09-27&comparison_type=yesterday"
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert "Risky CSP" in html
    assert "eligibility minimum" in html


@pytest.mark.django_db
def test_onus_mode_is_passed_through(logged_in_client, db):
    response = logged_in_client.get("/dashboard/comparisons/?mode=onus")
    assert response.status_code == 200
    assert response.context["mode"] == "onus"
