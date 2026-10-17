"""The redesigned journey pages render for staff and viewers, show honest states with and without
data, keep staff-only pages staff-only, and never present an estimate as a confirmed balance."""

import datetime as dt
import decimal

import pytest
from bankflow import estimates
from csp.models import Csp, DailyBalance
from django.contrib.auth import get_user_model
from django.utils import timezone

D = decimal.Decimal
pytestmark = pytest.mark.django_db

JOURNEY = [
    "/dashboard/", "/dashboard/readiness/", "/dashboard/csps/", "/dashboard/needs-attention/",
    "/dashboard/month-end/", "/dashboard/money/", "/dashboard/comparisons/",
    "/dashboard/balance-intelligence/", "/dashboard/trends/", "/dashboard/transactions/",
    "/dashboard/more/",
]
STAFF_ONLY = ["/dashboard/sbi-comparison/"]


@pytest.fixture
def staff(client):
    get_user_model().objects.create_user("s", password="x", is_staff=True)
    client.login(username="s", password="x")
    return client


@pytest.fixture
def viewer(client):
    get_user_model().objects.create_user("v", password="x")
    client.login(username="v", password="x")
    return client


@pytest.fixture
def world():
    today = timezone.localdate()
    csp = Csp.objects.create(csp_code="1A850001", name="Alpha Kiosk", account_count=320)
    for back, val in ((40, "3000"), (10, "3100"), (0, "3100")):
        DailyBalance.objects.create(
            csp=csp, balance_date=today - dt.timedelta(days=back), daily_avg_balance=D(val),
            account_count=320, source="calling_sheet")
    estimates.compute_daily_estimates(today)
    return csp


@pytest.mark.parametrize("url", JOURNEY)
def test_every_journey_page_renders_empty(staff, url):
    r = staff.get(url)
    assert r.status_code == 200, url


@pytest.mark.parametrize("url", JOURNEY)
def test_every_journey_page_renders_with_data(staff, world, url):
    r = staff.get(url)
    assert r.status_code == 200, url


def test_staff_only_pages_are_forbidden_to_viewers_and_hidden_from_their_menu(viewer):
    for url in STAFF_ONLY:
        assert viewer.get(url).status_code == 403
    body = viewer.get("/dashboard/").content.decode()
    assert "SBI comparison" not in body and "/dashboard/sbi-comparison/" not in body


def test_staff_see_the_sbi_comparison_and_it_never_claims_validation(staff, world):
    body = staff.get("/dashboard/sbi-comparison/").content.decode()
    assert "SBI comparison" in body and "Not validated" in body or "No validation run" in body
    assert "Validated" not in body.replace("Not validated", "")


def test_csp_pages_label_the_estimate_as_shadow_and_not_a_balance(staff, world):
    detail = staff.get("/dashboard/csp/1A850001/").content.decode()
    assert "Alpha Kiosk" in detail and "SBI-reported" in detail and "Estimate" in detail
    explain = staff.get("/dashboard/csp/1A850001/explain/").content.decode()
    assert "Shadow estimate" in explain and "Movement is shown, not added" not in explain
    assert "Estimated combined balance" in explain and "Estimated average per account" in explain
    assert "NOT scaled" in explain or "unchanged since the baseline" in explain
    assert staff.get("/dashboard/csp/NOPE/explain/").status_code == 404


def test_csp_list_search_and_filters(staff, world):
    ok = staff.get("/dashboard/csps/?q=alpha").content.decode()
    assert "Alpha Kiosk" in ok
    none = staff.get("/dashboard/csps/?q=zzz").content.decode()
    assert "No CSP matches" in none
    assert staff.get("/dashboard/csps/?show=review&sort=month_end&order=desc").status_code == 200
    assert staff.get("/dashboard/csps/?show=bogus&sort=bogus&page=abc").status_code == 200


def test_old_links_still_work(staff):
    for old, new in (("/dashboard/overview/", "/dashboard/"),
                     ("/dashboard/progress/", "/dashboard/readiness/"),
                     ("/dashboard/bank-flow/", "/dashboard/sbi-comparison/")):
        r = staff.get(old)
        assert r.status_code == 302 and r["Location"].endswith(new)


def test_anonymous_users_are_sent_to_sign_in(client):
    for url in JOURNEY + STAFF_ONLY:
        r = client.get(url)
        assert r.status_code == 302 and "/login/" in r["Location"], url
    assert client.get("/dashboard/login/").status_code == 200
