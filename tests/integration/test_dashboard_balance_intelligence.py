"""
/dashboard/balance-intelligence/ — deep balance analytics. Reads through
csp.comparison exactly like Daily Comparison does (test_comparison_engine.py
covers the underlying math); these tests are about the view's own
aggregation/filtering (mode/window validation, near-next-slab threshold,
below-minimum listing) and that it renders without error.
"""

import datetime as dt
import decimal

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


@pytest.mark.django_db
def test_requires_login(client):
    response = client.get("/dashboard/balance-intelligence/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_renders_with_no_csps(logged_in_client, db):
    response = logged_in_client.get("/dashboard/balance-intelligence/")
    assert response.status_code == 200
    assert response.context["window_days"] == 30
    assert response.context["mode"] == "overall"


@pytest.mark.django_db
def test_invalid_mode_and_window_fall_back_to_defaults(logged_in_client, db):
    response = logged_in_client.get("/dashboard/balance-intelligence/?mode=bogus&window=999")
    assert response.status_code == 200
    assert response.context["mode"] == "overall"
    assert response.context["window_days"] == 30


@pytest.mark.django_db
def test_window_param_accepts_valid_choices(logged_in_client, db):
    response = logged_in_client.get("/dashboard/balance-intelligence/?window=7")
    assert response.context["window_days"] == 7


@pytest.mark.django_db
def test_near_next_slab_lists_csps_within_threshold(logged_in_client, db):
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    close = Csp.objects.create(csp_code="1A850901", name="Close To S1")
    far = Csp.objects.create(csp_code="1A850902", name="Far From S1")
    # NIL upper bound is 2500 — 2490 is within Rs.500 of crossing into S1.
    _balance(close, yesterday, 2480)
    _balance(close, today, 2490)
    _balance(far, yesterday, 1000)
    _balance(far, today, 1000)
    cmp.build_daily_snapshot(close.csp_code, today)
    cmp.build_daily_snapshot(far.csp_code, today)

    response = logged_in_client.get("/dashboard/balance-intelligence/")
    codes = {r["csp_code"] for r in response.context["near_next_slab"]}
    assert "1A850901" in codes
    assert "1A850902" not in codes


@pytest.mark.django_db
def test_below_minimum_lists_nil_csps(logged_in_client, db):
    today = dt.date.today()
    nil_csp = Csp.objects.create(csp_code="1A850903", name="In NIL")
    _balance(nil_csp, today, 1500)
    cmp.build_daily_snapshot(nil_csp.csp_code, today)

    response = logged_in_client.get("/dashboard/balance-intelligence/")
    codes = {r["csp_code"] for r in response.context["below_minimum"]}
    assert "1A850903" in codes


@pytest.mark.django_db
def test_slab_summaries_present_for_every_slab(logged_in_client, db):
    response = logged_in_client.get("/dashboard/balance-intelligence/")
    slabs = [s.slab for s in response.context["slab_summaries"]]
    assert slabs == ["NIL", "S1", "S2", "S3", "S4"]
