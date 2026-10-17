"""Every CSP, today (the present IST day) against yesterday: the list behind the Home page's
"Today vs yesterday" view and GET /api/v1/bank-flow/csp-day-compare. Same definitions as the
network strip, one row per tracked CSP."""

import datetime as dt
import decimal

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey
from bankflow import intelligence
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


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def at(day, hh, mm=0):
    return dt.datetime.combine(day, dt.time(hh, mm), tzinfo=TZ)


def csp(code, name=None, **kw):
    return Csp.objects.create(csp_code=code, name=name or f"Name {code}", account_count=300, **kw)


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


def pm(code, when, mtd, ftd):
    PmjdyMonthly.objects.create(csp_id=code, month=f"{when:%Y-%m}", mtd=mtd, ftd=ftd, target=30,
                                observed_at=when, snapshot_date=timezone.localtime(when).date())


@pytest.fixture
def world():
    """A: full data both days. B: only today. C: nothing at all. D: invalid code, not tracked."""
    csp("1A850001", "Alpha")
    csp("1A850002", "Bravo")
    csp("1A850003", "Charlie")
    csp("zzz", "Junk", status="invalid_code")
    bal("1A850001", YESTERDAY, 1000, 300)
    bal("1A850001", TODAY, 1200, 310)
    bal("1A850002", TODAY, 2000, 120)
    bal("zzz", TODAY, 1, 9999)
    flow("1A850001", YESTERDAY, withdrawal=8000, deposit=3000, transfer=70000)
    flow("1A850001", TODAY, withdrawal=5000, deposit=9000, transfer=1)
    flow("1A850002", TODAY, withdrawal=100, deposit=400)
    pm("1A850001", at(YESTERDAY, 21), 6, 3)
    pm("1A850001", at(TODAY, 10), 9, 2)
    return None


def by_code(rows):
    return {r["csp_code"]: r for r in rows}


# ---------------------------------------------------------------- the figures
def test_a_csp_with_both_days_gets_every_figure_and_its_change(world):
    rows, total, meta = intelligence.csp_day_compare_rows(limit=50)
    a = by_code(rows)["1A850001"]
    assert total == 3 and meta["today"] == TODAY and meta["yesterday"] == YESTERDAY
    assert (a["accounts_today"], a["accounts_yesterday"], a["accounts_change"]) == (310, 300, 10)
    assert (a["new_mtd_today"], a["new_mtd_yesterday"]) == (9, 6)
    assert (a["new_day_today"], a["new_day_yesterday"]) == (2, 3)
    # only withdrawals and deposits count (not the 70,000 transfer); net = deposits - withdrawals
    assert (a["withdrawals_today"], a["withdrawals_yesterday"]) == (D(5000), D(8000))
    assert (a["deposits_today"], a["deposits_yesterday"]) == (D(9000), D(3000))
    assert (a["net_today"], a["net_yesterday"], a["net_change"]) == (D(4000), D(-5000), D(9000))
    assert (a["transactions_today"], a["transactions_yesterday"]) == (3, 3)
    assert (a["observed_today"], a["observed_yesterday"], a["observed_change"]) == (
        D(1200), D(1000), D(200))


def test_missing_days_are_none_and_a_change_needs_both_days(world):
    rows, _t, _m = intelligence.csp_day_compare_rows(limit=50)
    b, c = by_code(rows)["1A850002"], by_code(rows)["1A850003"]
    assert b["accounts_today"] == 120 and b["accounts_yesterday"] is None
    assert b["accounts_change"] is None and b["net_change"] is None
    assert b["net_today"] == D(300) and b["net_yesterday"] is None
    assert all(c[k] is None for k in (
        "accounts_today", "accounts_change", "withdrawals_today", "deposits_today", "net_today",
        "observed_today", "estimate_today", "new_day_today"))   # no data is None, never zero


def test_invalid_code_csps_are_not_listed(world):
    rows, total, _m = intelligence.csp_day_compare_rows(limit=50)
    assert "zzz" not in by_code(rows) and total == 3


def test_only_validated_or_provisional_estimates_appear_and_they_carry_the_status(world):
    for day, status, value in ((TODAY, "PROVISIONAL", 1300), (YESTERDAY, "VALIDATED", 1250)):
        DailyEstimate.objects.create(
            csp_id="1A850001", estimate_date=day, method="growth_only", status=status,
            reason_code="X", estimated_value=D(value), current_account_count=300,
            calculated_at=timezone.now(), details={})
    DailyEstimate.objects.create(
        csp_id="1A850002", estimate_date=TODAY, method="carry_forward",
        status="INSUFFICIENT_EVIDENCE", reason_code="X", estimated_value=D(2000),
        current_account_count=120, calculated_at=timezone.now(), details={})
    rows, _t, _m = intelligence.csp_day_compare_rows(limit=50)
    a, b = by_code(rows)["1A850001"], by_code(rows)["1A850002"]
    assert (a["estimate_today"], a["estimate_yesterday"], a["estimate_change"]) == (
        D(1300), D(1250), D(50)) and a["estimate_status"] == "PROVISIONAL"
    assert b["estimate_today"] is None                      # not enough history: not shown


def test_the_rows_add_up_to_the_network_strip(world):
    """One definition: the list and the strip must never disagree."""
    rows, _t, _m = intelligence.csp_day_compare_rows(limit=50)
    net = {r["key"]: r for r in intelligence.day_compare()["rows"]}
    assert sum(r["withdrawals_today"] or 0 for r in rows) == net["withdrawals"]["today"]
    assert sum(r["deposits_yesterday"] or 0 for r in rows) == net["deposits"]["yesterday"]
    assert sum(r["accounts_today"] or 0 for r in rows) == net["accounts"]["today"]
    assert sum(r["new_mtd_today"] or 0 for r in rows) == net["new_accounts_mtd"]["today"]


# ---------------------------------------------------------------- order, search, pages
def test_sorting_puts_missing_figures_last_in_either_direction(world):
    desc, _t, _m = intelligence.csp_day_compare_rows(sort="accounts_change", limit=50)
    asc, _t, _m = intelligence.csp_day_compare_rows(sort="accounts_change", descending=False,
                                                    limit=50)
    # only Alpha has a change figure: it leads in BOTH directions, the others follow with none
    assert desc[0]["csp_code"] == "1A850001" and asc[0]["csp_code"] == "1A850001"
    assert all(r["accounts_change"] is None for r in desc[1:] + asc[1:])
    named, _t, _m = intelligence.csp_day_compare_rows(sort="name", descending=False, limit=50)
    assert [r["name"] for r in named] == ["Alpha", "Bravo", "Charlie"]
    top_net, _t, _m = intelligence.csp_day_compare_rows(sort="net_today", limit=1)
    assert top_net[0]["csp_code"] == "1A850001"


def test_search_by_name_or_code_and_pagination(world):
    rows, total, _m = intelligence.csp_day_compare_rows(search="brav")
    assert [r["csp_code"] for r in rows] == ["1A850002"] and total == 1
    rows, total, _m = intelligence.csp_day_compare_rows(search="1A85000")
    assert total == 3
    page2, total, _m = intelligence.csp_day_compare_rows(sort="name", descending=False, limit=2,
                                                         offset=2)
    assert total == 3 and [r["name"] for r in page2] == ["Charlie"]
    assert intelligence.csp_day_compare_rows(limit=2, offset=99)[0] == []


def test_money_is_flagged_not_like_for_like_while_today_is_a_day_to_date_cut(world):
    StatementFile.objects.create(
        file_hash="a" * 64, filename="s.xls", status="imported", first_row_date=TODAY,
        last_row_date=TODAY, file_modified_at=at(TODAY, 9, 0))
    _rows, _t, meta = intelligence.csp_day_compare_rows(limit=5)
    assert meta["like_for_like_money"] is False and meta["money_state"]["today"] == "so_far"
    assert "Not a like-for-like" in meta["money_note"] and "9:00 AM" in meta["money_note"]


# ---------------------------------------------------------------- the Home page
@pytest.fixture
def staff(client, world):
    u = get_user_model().objects.create_user("st", password="x", is_staff=True)
    client.force_login(u)
    return client


def test_home_offers_the_two_views_and_defaults_to_the_range_one(staff):
    html = staff.get("/dashboard/csps/").content.decode()
    assert "Today vs yesterday" in html and 'href="?view=day"' in html
    assert "data-csp=" not in html  # the per-CSP money table is only on the day view
    assert "SBI-reported average" in html


def test_the_today_vs_yesterday_view_lists_each_csp_with_both_days(staff):
    html = staff.get("/dashboard/csps/?view=day").content.decode()
    assert 'data-csp="1A850001"' in html
    assert "Alpha" in html and "Bravo" in html and "Charlie" in html and "Junk" not in html
    flat = " ".join(html.split())
    assert "yesterday 300 (+10)" in flat                     # Alpha's customer accounts
    assert "₹9,000" in flat and "yesterday ₹3,000" in flat    # full rupees, Indian grouping
    assert f"{TODAY:%a} {TODAY.day} {TODAY:%b}" in html
    assert f"{YESTERDAY:%a} {YESTERDAY.day} {YESTERDAY:%b}" in html
    table = html.split("<tbody>")[1]
    assert table.index("Alpha") < table.index("Charlie")     # biggest account rise first


def test_the_view_keeps_working_with_search_sort_paging_and_bad_input(staff):
    ok = staff.get("/dashboard/csps/?view=day&q=brav&sort=name&order=asc")
    assert ok.status_code == 200 and "Bravo" in ok.content.decode()
    assert "Alpha" not in ok.content.decode().split("<tbody>")[1]
    for url in ("/dashboard/csps/?view=day&sort=bogus", "/dashboard/csps/?view=day&page=99",
                "/dashboard/csps/?view=nonsense", "/dashboard/csps/?view=day&page=abc"):
        assert staff.get(url).status_code == 200, url


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


def test_api_csp_day_compare_scope_shape_and_validation(client, world, api_key):
    url = "/api/v1/bank-flow/csp-day-compare"
    report = api_key("report:read")
    assert client.get(url).status_code == 401
    assert client.get(url, HTTP_X_API_KEY=api_key("csp:read")).status_code == 403
    body = client.get(url + "?sort=net_today&limit=2", HTTP_X_API_KEY=report).json()["data"]
    assert body["total"] == 3 and len(body["rows"]) == 2 and body["limit"] == 2
    assert body["today"] == TODAY.isoformat() and body["like_for_like_money"] in (True, False)
    assert body["rows"][0]["csp_code"] == "1A850001" and body["rows"][0]["net_today"] == 4000.0
    assert "customer_name" not in body["rows"][0] and "mobile" not in body["rows"][0]
    assert client.get(url + "?sort=bogus", HTTP_X_API_KEY=report).status_code == 422
