"""Today vs yesterday, everywhere: today is the PRESENT IST day, yesterday the day before. One
function (intelligence.day_compare) feeds the strip on every data page, the per-CSP panel and
GET /api/v1/bank-flow/day-compare."""

import datetime as dt
import decimal
import pathlib

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey
from bankflow import flows, intelligence
from bankflow.models import DailyEstimate, FlowAggregate, PmjdyMonthly, StatementFile
from csp.models import Csp, DailyBalance
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

D = decimal.Decimal
pytestmark = pytest.mark.django_db
TZ = timezone.get_current_timezone()
TODAY = timezone.localdate()
YESTERDAY = TODAY - dt.timedelta(days=1)
EARLIER = TODAY - dt.timedelta(days=5)


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def at(day, hh, mm=0):
    return dt.datetime.combine(day, dt.time(hh, mm), tzinfo=TZ)


def csp(code, n=300, **kw):
    return Csp.objects.create(csp_code=code, name=f"Name {code}", account_count=n, **kw)


def bal(code, day, avg, n):
    DailyBalance.objects.create(csp_id=code, balance_date=day, daily_avg_balance=D(avg),
                                account_count=n, source="calling_sheet")


def flow(code, day, *, withdrawal=0, deposit=0, transfer=0):
    cats = {
        "withdrawal": {"count": 2, "credit": str(withdrawal), "debit": "0"},
        "deposit": {"count": 1, "credit": "0", "debit": str(deposit)},
        "transfer": {"count": 4, "credit": "0", "debit": str(transfer)},
    }
    FlowAggregate.objects.create(
        csp_id=code, flow_date=day, resolution_mode="exact_only", txn_count=7,
        credit_amount=D(withdrawal), debit_amount=D(deposit + transfer),
        recon_status="RECONCILED", by_category=cats)


def pm(code, observed_at, mtd, ftd):
    PmjdyMonthly.objects.create(csp_id=code, month=f"{observed_at:%Y-%m}", mtd=mtd, ftd=ftd,
                                target=30, observed_at=observed_at,
                                snapshot_date=timezone.localtime(observed_at).date())


def rows(result):
    return {r["key"]: r for r in result["rows"]}


# ---------------------------------------------------------------- the dates
def test_today_is_the_present_day_and_yesterday_the_day_before_whatever_data_exists():
    csp("A")
    bal("A", EARLIER, 1000, 300)  # the newest data is five days old: today is still today
    r = intelligence.day_compare()
    assert r["today"] == timezone.localdate() and r["yesterday"] == TODAY - dt.timedelta(days=1)


# ---------------------------------------------------------------- accounts
def test_accounts_are_as_of_each_day_and_the_change_counts_only_csps_with_both_readings():
    csp("A")
    csp("B")
    csp("C")
    bal("A", YESTERDAY, 1000, 300)
    bal("A", TODAY, 1000, 310)
    bal("B", EARLIER, 1000, 250)  # unchanged since five days ago: still its count on both days
    bal("C", TODAY, 1000, 100)    # first seen today: no yesterday reading
    a = rows(intelligence.day_compare())["accounts"]
    assert (a["today"], a["yesterday"]) == (660, 550)
    assert a["change"] == 10          # only A and B have both readings: +10, not +110
    assert a["direction"] == "up" and a["good_when_up"] is True
    assert "2 CSPs reporting" not in a["note"] and "3 CSPs reporting today" in a["note"]
    assert round(a["change_pct"], 2) == round(10 / 550 * 100, 2)


def test_a_reading_taken_today_never_leaks_into_yesterday():
    csp("A")
    bal("A", YESTERDAY, 1000, 300)
    bal("A", TODAY, 1000, 999)
    a = rows(intelligence.day_compare())["accounts"]
    assert a["yesterday"] == 300 and a["today"] == 999


def test_no_data_is_a_dash_not_a_zero():
    csp("A")
    r = rows(intelligence.day_compare())
    for key in ("accounts", "new_accounts_mtd", "withdrawals", "deposits", "observed_avg",
                "estimated_avg"):
        assert r[key]["today"] is None and r[key]["yesterday"] is None, key
        assert r[key]["change"] is None and r[key]["direction"] == "unknown", key


# ---------------------------------------------------------------- new accounts
def test_new_accounts_use_the_newest_reading_on_or_before_each_day():
    csp("A")
    pm("A", at(YESTERDAY, 9), 5, 2)
    pm("A", at(YESTERDAY, 21), 6, 3)   # yesterday's last reading
    pm("A", at(TODAY, 10), 9, 1)
    r = rows(intelligence.day_compare())
    assert (r["new_accounts_mtd"]["today"], r["new_accounts_mtd"]["yesterday"]) == (9, 6)
    assert r["new_accounts_mtd"]["change"] == 3
    assert (r["new_accounts_day"]["today"], r["new_accounts_day"]["yesterday"]) == (1, 3)


# ---------------------------------------------------------------- money movement
def test_money_counts_only_withdrawals_and_deposits_and_reports_other_types_apart():
    csp("A")
    flow("A", TODAY, withdrawal=9000, deposit=2000, transfer=50000)
    flow("A", YESTERDAY, withdrawal=8000, deposit=1000, transfer=40000)
    r = intelligence.day_compare()
    m = rows(r)
    assert (m["withdrawals"]["today"], m["withdrawals"]["yesterday"]) == (D(9000), D(8000))
    assert (m["deposits"]["today"], m["deposits"]["yesterday"]) == (D(2000), D(1000))
    assert m["transactions"]["today"] == 3 and r["other_types"]["today"]["debit"] == D(50000)


def test_money_is_marked_not_like_for_like_while_today_is_only_a_day_to_date_cut():
    csp("A")
    flow("A", TODAY, withdrawal=100)
    flow("A", YESTERDAY, withdrawal=900)
    f = StatementFile.objects.create(
        file_hash="d" * 64, filename="s.xls", status="imported", first_row_date=TODAY,
        last_row_date=TODAY, file_modified_at=at(TODAY, 11, 0))
    assert f.pk
    r = intelligence.day_compare()
    assert r["like_for_like_money"] is False and r["money_state"]["today"] == "so_far"
    m = rows(r)
    assert m["withdrawals"]["like_for_like"] is False
    assert "Not a like-for-like" in m["withdrawals"]["note"]
    assert "11:00 AM" in m["withdrawals"]["note"]
    assert m["withdrawals"]["change"] == D(-800)  # still shown, but flagged: never hidden


def test_two_complete_days_are_like_for_like(monkeypatch):
    csp("A")
    flow("A", TODAY, withdrawal=100)
    flow("A", YESTERDAY, withdrawal=900)
    StatementFile.objects.create(
        file_hash="e" * 64, filename="s.xls", status="imported", first_row_date=YESTERDAY,
        last_row_date=TODAY, file_modified_at=at(TODAY, 1, 0))
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: {YESTERDAY, TODAY})
    r = intelligence.day_compare()
    assert r["like_for_like_money"] is True
    assert rows(r)["withdrawals"]["note"] == ""
    assert rows(r)["withdrawals"]["like_for_like"] is True


# ---------------------------------------------------------------- balances and estimates
def test_observed_average_is_the_newest_reading_on_or_before_each_day_weighted_by_accounts():
    csp("A")
    csp("B")
    bal("A", EARLIER, 1000, 100)
    bal("B", EARLIER, 2000, 300)
    bal("A", TODAY, 1400, 100)   # A refreshed today
    r = rows(intelligence.day_compare())["observed_avg"]
    assert r["yesterday"] == D(1750)                      # (1000*100 + 2000*300) / 400
    assert r["today"] == D(1850)                          # (1400*100 + 2000*300) / 400
    assert r["change"] == D(100)


def test_only_validated_or_provisional_estimates_are_shown():
    csp("A")
    for day, status, value in ((TODAY, "INSUFFICIENT_EVIDENCE", 1000),
                               (YESTERDAY, "PROVISIONAL", 1100)):
        DailyEstimate.objects.create(
            csp_id="A", estimate_date=day, method="growth_only", status=status,
            reason_code="X", estimated_value=D(value), current_account_count=300,
            calculated_at=timezone.now(), details={})
    e = rows(intelligence.day_compare())["estimated_avg"]
    assert e["today"] is None and e["yesterday"] == D(1100) and e["change"] is None


# ---------------------------------------------------------------- one CSP
def test_a_single_csp_gets_its_own_comparison_and_an_unknown_one_gets_dashes():
    csp("A")
    csp("B")
    bal("A", YESTERDAY, 1000, 300)
    bal("A", TODAY, 1000, 310)
    bal("B", YESTERDAY, 1000, 200)
    bal("B", TODAY, 1000, 205)
    one = intelligence.day_compare(csp_codes=["A"])
    assert one["scope"] == "csp" and one["csps"] == 1
    assert rows(one)["accounts"]["today"] == 310
    none = intelligence.day_compare(csp_codes=["NOPE"])
    assert rows(none)["accounts"]["today"] is None and none["csps"] == 0


def test_invalid_code_csps_are_not_part_of_the_comparison():
    csp("1A850001")
    csp("f", status="invalid_code")
    bal("1A850001", TODAY, 1000, 100)
    bal("f", TODAY, 1000, 5000)
    assert rows(intelligence.day_compare())["accounts"]["today"] == 100


# ---------------------------------------------------------------- the pages
@pytest.fixture
def staff(client):
    u = get_user_model().objects.create_user("st", password="x", is_staff=True)
    client.force_login(u)
    csp("1A850001")
    bal("1A850001", YESTERDAY, 1000, 300)
    bal("1A850001", TODAY, 1000, 310)
    return client


@pytest.mark.parametrize("url", [
    "/dashboard/", "/dashboard/money/", "/dashboard/needs-attention/",
])
def test_the_strip_is_on_every_data_page(staff, url):
    html = staff.get(url).content.decode()
    assert "Today vs yesterday" in html and 'data-dc-key="accounts"' in html, url
    assert f"{TODAY:%a %d %b}" in html and f"{YESTERDAY:%a %d %b}" in html


@pytest.mark.parametrize("url", ["/dashboard/progress/", "/dashboard/audit/"])
def test_the_strip_is_not_on_pages_without_network_data(staff, url):
    assert 'data-dc-key="accounts"' not in staff.get(url).content.decode(), url


def test_the_csp_page_has_its_own_strip_for_that_csp(staff):
    """The legacy CSP page needs the dbt marts (absent in the test database), so check the pieces:
    the view helper, the shared component rendered for one CSP, and the include in the page."""
    from dashboard import views
    from django.template.loader import render_to_string

    dc = views._csp_day_compare("1A850001")
    assert dc["scope"] == "csp" and dc["csps"] == 1
    html = render_to_string("dashboard/components/day_compare.html", {"dc": dc})
    assert "Today vs yesterday &middot; this CSP" in html and 'data-dc-key="accounts"' in html
    assert ">310<" in "".join(html.split())
    page = (pathlib.Path(views.__file__).parent / "templates/dashboard/csp_detail.html").read_text(
        encoding="utf-8")
    assert 'include "dashboard/components/day_compare.html" with dc=csp_day_compare' in page


def test_the_strip_numbers_match_the_api(staff, client):
    html = staff.get("/dashboard/money/").content.decode()
    assert ">310<" in html.replace(" ", "")  # today's customer accounts


# ---------------------------------------------------------------- the API
@pytest.fixture
def api_key(db):
    def make(scopes):
        consumer = ApiConsumer.objects.create(name=f"t-{scopes}")
        raw = generate_raw_key()
        ApiKey.objects.create(consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw),
                              scopes=scopes)
        return raw

    return make


def test_api_day_compare_scope_and_csp_filter(client, api_key):
    csp("A")
    csp("B")
    bal("A", YESTERDAY, 1000, 300)
    bal("A", TODAY, 1000, 310)
    bal("B", TODAY, 1000, 50)
    url = "/api/v1/bank-flow/day-compare"
    report = api_key("report:read")
    assert client.get(url).status_code == 401
    assert client.get(url, HTTP_X_API_KEY=api_key("csp:read")).status_code == 403
    net = client.get(url, HTTP_X_API_KEY=report).json()["data"]
    assert net["today"] == TODAY.isoformat() and net["yesterday"] == YESTERDAY.isoformat()
    assert {r["key"]: r for r in net["rows"]}["accounts"]["today"] == 360
    one = client.get(url + "?csp=A", HTTP_X_API_KEY=report).json()["data"]
    assert one["scope"] == "csp" and {r["key"]: r for r in one["rows"]}["accounts"]["today"] == 310
