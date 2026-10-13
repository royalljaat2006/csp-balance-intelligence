"""Decision dashboard: summary, movement, growth explorer, exceptions, API permissions,
page states (empty / partial / stale), classic-overview preservation, responsive guards."""

import datetime as dt
import decimal

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey
from bankflow import estimates, intelligence, pmjdy
from bankflow.models import (
    AccountCountObservation,
    DailyEstimate,
    FlowAggregate,
    UnresolvedFlowDaily,
)
from csp.models import Csp, DailyBalance
from django.contrib.auth import get_user_model
from django.utils import timezone

D = decimal.Decimal
d = dt.date
pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()


def _csp(code, n=300, **kw):
    return Csp.objects.create(csp_code=code, name=f"Name {code}", account_count=n, **kw)


def _bal(code, day, value, n):
    DailyBalance.objects.create(csp_id=code, balance_date=day, daily_avg_balance=D(value),
                                account_count=n, source="calling_sheet")


def _obs(code, day, n, hour=6):
    at = dt.datetime.combine(day, dt.time(hour), tzinfo=timezone.get_current_timezone())
    AccountCountObservation.objects.create(csp_id=code, source="pmjdy", account_count=n,
                                           observed_at=at, snapshot_date=day)


def _flow(code, day, credit=0, debit=0):
    FlowAggregate.objects.create(
        csp_id=code, flow_date=day, resolution_mode="exact_only", txn_count=2,
        credit_amount=D(credit), debit_amount=D(debit), recon_status="RECONCILED",
        by_category={"withdrawal": {"count": 1, "credit": str(credit), "debit": "0"},
                     "deposit": {"count": 1, "credit": "0", "debit": str(debit)}})


@pytest.fixture
def world():
    """A: PMJDY 3000 -> 3090 with flows; B: no PMJDY (Calling Sheet fallback), shrinking flows."""
    _csp("A", 3000)
    _csp("B", 250)
    start = TODAY - dt.timedelta(days=10)
    _obs("A", start - dt.timedelta(days=1), 3000)
    _obs("A", TODAY, 3090)
    _bal("A", start - dt.timedelta(days=20), "1000", 3000)
    _bal("A", TODAY, "1000", 3090)
    _bal("B", start - dt.timedelta(days=20), "2000", 250)
    _bal("B", TODAY, "2000", 250)
    for i in range(10):
        _flow("A", start + dt.timedelta(days=i), credit=1000, debit=400)
        _flow("B", start + dt.timedelta(days=i), credit=100, debit=900)
    return start


RANGE = 30


@pytest.fixture(autouse=True)
def _permissive_alert_thresholds(settings):
    """These tests exercise the alert plumbing with tiny fixture amounts; the real materiality
    thresholds are tested in test_alert_materiality.py."""
    settings.ALERT_MIN_OUTFLOW = 0
    settings.ALERT_MIN_IMBALANCE = 0
    settings.ALERT_HIGH_OUTFLOW = 0
    settings.ALERT_HIGH_IMBALANCE = 0
    settings.ALERT_MIN_NETWORK_COVERAGE = 0


def _range():
    return intelligence.resolve_range(RANGE)


def test_current_count_is_latest_pmjdy_and_growth_is_vs_previous_snapshot(world):
    f, t = _range()
    counts = intelligence.account_counts(["A", "B"], f, t)
    assert counts["A"]["current"] == 3090 and counts["A"]["previous"] == 3000
    assert counts["A"]["source"] == "pmjdy"
    assert counts["B"]["source"] == "calling_sheet" and counts["B"]["current"] == 250


def test_summary_flows_are_credits_debits_net_gross_and_exclude_unattributed(world):
    UnresolvedFlowDaily.objects.create(flow_date=TODAY, status="unmatched", txn_count=5,
                                       credit_amount=D(500), debit_amount=D(500))
    s = intelligence.dashboard_summary(*_range())
    fl = s["flows"]
    assert fl["pool_credits"] == D(11000) and fl["pool_debits"] == D(13000)
    assert fl["net_pool_flow"] == D(-2000) and fl["gross_flow"] == D(24000)
    assert fl["unattributed_amount"] == D(1000)  # reported, never added to any CSP
    assert 0 < fl["unattributed_share"] < 0.1
    assert s["accounts"]["current_total"] == 3090 + 250
    assert s["accounts"]["growth"] == 90  # only CSPs with a comparable previous reading
    assert s["accounts"]["by_source"] == {"pmjdy": 1, "calling_sheet": 1}
    assert s["estimates"]["estimator"]["status"] == "NOT_RUN"


def test_repeated_capture_and_summary_never_double_count(world):
    f, t = _range()
    before = intelligence.dashboard_summary(f, t)["flows"]
    pmjdy.capture_counts({"A": 3090})
    pmjdy.capture_counts({"A": 3090})
    after = intelligence.dashboard_summary(f, t)
    assert after["flows"]["net_pool_flow"] == before["net_pool_flow"]  # counts never touch flows
    assert after["accounts"]["current_total"] == 3090 + 250


def test_new_account_growth_does_not_alter_attributed_flows(world):
    f, t = _range()
    rows_before = {r["csp_code"]: r["net_flow"] for r in intelligence.csp_growth(f, t, limit=9)[0]}
    _obs("A", TODAY, 9999, hour=12)
    rows_after = {r["csp_code"]: r["net_flow"] for r in intelligence.csp_growth(f, t, limit=9)[0]}
    assert rows_before == rows_after


def test_movement_is_daily_and_marks_missing_days_not_zero(world):
    m = intelligence.network_movement(*_range())
    assert m["granularity"] == "daily" and len(m["series"]) == RANGE
    by = {p["date"]: p for p in m["series"]}
    some_day = world + dt.timedelta(days=3)
    assert by[some_day]["pool_credits"] == D(1100) and by[some_day]["net_pool_flow"] == D(-200)
    gap = world - dt.timedelta(days=5)
    assert by[gap]["pool_credits"] is None and by[gap]["flow_status"] == "no_data"
    assert m["account_source"] == "pmjdy" and by[TODAY]["total_accounts"] == 3090


def test_growth_explorer_filters_sorts_and_paginates(world):
    f, t = _range()
    rows, total, _ = intelligence.csp_growth(f, t, sort="net_flow", descending=True, limit=1)
    assert total == 2 and rows[0]["csp_code"] == "A" and rows[0]["net_flow"] == D(6000)
    rows, total, _ = intelligence.csp_growth(f, t, search="name b")
    assert [r["csp_code"] for r in rows] == ["B"] and total == 1
    rows, total, _ = intelligence.csp_growth(f, t, flag="NEGATIVE_NET_FLOW")
    assert [r["csp_code"] for r in rows] == ["B"]
    rows, total, _ = intelligence.csp_growth(f, t, sort="count_change", limit=1, offset=1)
    assert rows[0]["csp_code"] == "B" and total == 2
    a = intelligence.csp_growth(f, t, search="A")[0][0]
    assert a["previous_count"] == 3000 and a["current_count"] == 3090
    assert a["growth_pct"] == pytest.approx(3.0) and a["estimate_status"] == "NOT_COMPUTED"


def test_exceptions_have_reason_evidence_and_next_action(world):
    estimates.compute_daily_estimates(TODAY)
    ex = intelligence.exceptions(*_range())
    types = {i["type"] for i in ex["items"]}
    assert "NEGATIVE_NET_FLOW" in types and "INSUFFICIENT_HISTORY" in types
    neg = next(i for i in ex["items"] if i["type"] == "NEGATIVE_NET_FLOW")
    assert neg["csp_code"] == "B" and neg["next_action"]
    assert "not a measured change" in neg["reason"]
    assert neg["metrics"]["net_flow"] == -8000 and "evidence_coverage" in neg
    assert not any(i["csp_code"] == "A" and i["type"] == "NEGATIVE_NET_FLOW" for i in ex["items"])


def test_growth_without_transaction_coverage_is_flagged():
    _csp("G", 500)
    _obs("G", TODAY - dt.timedelta(days=11), 400)
    _obs("G", TODAY, 500)  # +25 %, no statements at all
    ex = intelligence.exceptions(*_range())
    g = [i for i in ex["items"] if i["csp_code"] == "G"]
    assert any(i["type"] == "GROWTH_WITHOUT_FLOW_EVIDENCE" for i in g)


def test_decrease_and_stale_pmjdy_are_flagged():
    _csp("S", 500)
    _obs("S", TODAY - dt.timedelta(days=11), 520)
    at = timezone.now() - dt.timedelta(days=3)
    AccountCountObservation.objects.create(
        csp_id="S", source="pmjdy", account_count=500, observed_at=at,
        snapshot_date=timezone.localtime(at).date())
    ex = intelligence.exceptions(*_range())
    types = {i["type"] for i in ex["items"] if i["csp_code"] == "S"}
    assert {"ACCOUNT_COUNT_DECREASED", "STALE_OR_MISSING_SOURCE"} <= types


def test_unvalidated_estimates_never_raise_a_decline_alert(world):
    estimates.compute_daily_estimates(TODAY)
    ex = intelligence.exceptions(*_range())
    assert not any(i["type"] == "POSSIBLE_ESTIMATED_DECLINE" for i in ex["items"])
    est = DailyEstimate.objects.filter(estimate_date=TODAY, status="INSUFFICIENT_EVIDENCE")
    assert est.exists()


def test_historical_only_stubs_are_not_tracked():
    _csp("OLD", 300, status="historical_only")
    _csp("LIVE", 300)
    assert intelligence.dashboard_summary(*_range())["csps_tracked"] == 1


def test_200_account_gate_is_still_applied_to_estimates(world):
    _csp("TINY", 150)
    _bal("TINY", TODAY, "900", 150)
    estimates.compute_daily_estimates(TODAY)
    e = DailyEstimate.objects.get(csp_id="TINY", estimate_date=TODAY)
    assert e.status == "UNAVAILABLE" and e.reason_code == "BELOW_200_ACCOUNT_GATE"
    assert intelligence.dashboard_summary(*_range())["estimates"]["below_200_account_gate"] == 1


# ---------------------------------------------------------------- API
@pytest.fixture
def api_key(db):
    def make(scopes):
        consumer = ApiConsumer.objects.create(name=f"t-{scopes}")
        raw = generate_raw_key()
        ApiKey.objects.create(consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw),
                              scopes=scopes)
        return raw

    return make


ENDPOINTS = ["dashboard-summary", "network-movement", "csp-growth", "exceptions"]


@pytest.mark.parametrize("name", ENDPOINTS)
def test_new_endpoints_require_report_scope(client, api_key, name):
    url = f"/api/v1/bank-flow/{name}"
    assert client.get(url).status_code == 401
    assert client.get(url, HTTP_X_API_KEY=api_key("csp:read")).status_code == 403
    assert client.get(url, HTTP_X_API_KEY=api_key("report:read")).status_code == 200


def test_api_payloads_label_sources_and_never_expose_customer_data(client, api_key, world):
    h = {"HTTP_X_API_KEY": api_key("report:read")}
    s = client.get("/api/v1/bank-flow/dashboard-summary?days=30", **h).json()["data"]
    assert s["flows"]["attribution"] == "exact KO match only"
    assert s["accounts"]["source"].startswith("PMJDY") or "Calling Sheet" in s["accounts"]["source"]
    g = client.get("/api/v1/bank-flow/csp-growth?sort=net_flow&limit=1&flag=NEGATIVE_NET_FLOW", **h)
    body = g.json()
    assert body["pagination"]["total"] == 1 and body["data"][0]["csp_code"] == "B"
    allowed_keys = set(body["data"][0])
    assert not {"mobile", "email", "customer_account", "customer_name"} & allowed_keys
    assert client.get("/api/v1/bank-flow/csp-growth?sort=bogus", **h).status_code == 422
    assert client.get("/api/v1/bank-flow/network-movement?granularity=hourly", **h
                      ).json()["data"]["granularity"] == "hourly"


# ---------------------------------------------------------------- dashboard page
@pytest.fixture
def staff(client, db):
    get_user_model().objects.create_user("staff", password="x", is_staff=True)
    client.login(username="staff", password="x")
    return client


def test_home_requires_login_and_renders_empty_state(client, staff):
    page = staff.get("/dashboard/")
    assert page.status_code == 200
    html = page.content.decode()
    assert "Nothing needs attention right now" in html
    assert "New customer accounts for" in html and "have not been captured yet" in html
    client.logout()
    assert client.get("/dashboard/").status_code == 302


def test_home_keeps_sbi_reported_and_estimated_values_apart(staff, world):
    estimates.compute_daily_estimates(TODAY)
    html = staff.get("/dashboard/?range=30").content.decode()
    for needle in ("Customer accounts", "Money received into the pool",
                   "Money sent out of the pool", "SBI-reported average balance",
                   "Reliable estimate not available yet", "Today in short", "Your CSPs",
                   "Advanced details", "Incentive summary", "Classic overview"):
        assert needle in html, needle
    assert "+90 new in the last 30 days" in html  # latest PMJDY count is the current one
    assert "/dashboard/csp/A/" in html  # rows link to the existing CSP detail page
    assert "X-API-Key" not in html


def test_home_flags_stale_sources_and_filters(staff, world):
    from bankflow.models import PmjdyMonthly

    old = timezone.now() - dt.timedelta(days=4)
    PmjdyMonthly.objects.create(csp_id="A", month=f"{TODAY:%Y-%m}", mtd=3, ftd=0, target=7,
                                observed_at=old, snapshot_date=timezone.localtime(old).date())
    url = "/dashboard/?show=attention&sort=name&order=asc&page=1"
    html = staff.get(url).content.decode()
    assert "New customer account numbers were last updated" in html  # plain-words stale notice
    assert 'class="fh-src stale"' in html  # advanced sources table
    table = html.split('id="my-csps"')[1]
    assert "Name B" in table and "Name A</strong>" not in table


def test_home_bad_params_fall_back_safely(staff, world):
    assert staff.get("/dashboard/?range=abc&sort=drop&show=y&order=z&page=-4").status_code == 200


def test_classic_overview_is_still_served_at_its_own_route(staff, world):
    from django.urls import reverse

    assert reverse("dashboard:overview_classic") == "/dashboard/overview/"
    assert reverse("dashboard:bank_flow") == "/dashboard/bank-flow/"


def test_viewer_sees_home_without_staff_only_links(client, world):
    get_user_model().objects.create_user("viewer", password="x", is_staff=False)
    client.login(username="viewer", password="x")
    html = client.get("/dashboard/").content.decode()
    assert "Customer accounts" in html and "Technical validation details" not in html
    assert ">Administration<" not in html


# ---------------------------------------------------------------- only the selected types count
def _mixed_flow(code, day):
    """One CSP-day with every kind of statement row the real data contains."""
    cats = {
        "withdrawal": {"count": 3, "credit": "9000", "debit": "0"},
        "deposit": {"count": 2, "credit": "0", "debit": "2000"},
        "transfer": {"count": 5, "credit": "0", "debit": "50000"},
        "offus": {"count": 1, "credit": "700", "debit": "0"},
        "other_product": {"count": 1, "credit": "0", "debit": "300"},
        "unclassified": {"count": 1, "credit": "100", "debit": "0"},
    }
    FlowAggregate.objects.create(
        csp_id=code, flow_date=day, resolution_mode="exact_only", txn_count=13,
        credit_amount=D(9800), debit_amount=D(52300), recon_status="RECONCILED",
        by_category=cats)


def test_headline_money_counts_only_withdrawals_and_deposits_and_reports_the_rest_apart():
    _csp("M")
    _mixed_flow("M", TODAY)
    fl = intelligence.dashboard_summary(*_range())["flows"]
    # only withdrawal + deposit (the types the estimate uses) are in the headline ...
    assert fl["counted_types"] == ["withdrawal", "deposit"]
    assert fl["pool_credits"] == D(9000) and fl["pool_debits"] == D(2000)
    assert fl["net_pool_flow"] == D(7000) and fl["txn_count"] == 5
    # ... the money transfer debit (50,000) does NOT inflate "money out" ...
    assert fl["pool_debits"] < D(50000)
    # ... but nothing is hidden: the other types are reported on their own
    assert fl["other_types"]["credit"] == D(800) and fl["other_types"]["debit"] == D(50300)
    assert fl["other_types"]["txn_count"] == 8


def test_unattributed_share_compares_all_types_with_all_types():
    _csp("M")
    _mixed_flow("M", TODAY)  # attributed, all types: 9,800 + 52,300 = 62,100
    UnresolvedFlowDaily.objects.create(flow_date=TODAY, status="unmatched", txn_count=1,
                                       credit_amount=D(0), debit_amount=D(62100))
    assert intelligence.dashboard_summary(*_range())["flows"]["unattributed_share"] == 0.5


def test_per_csp_flow_and_movement_use_the_same_selected_types():
    _csp("M")
    _mixed_flow("M", TODAY)
    f = intelligence.flow_by_csp(["M"], *_range())["M"]
    assert (f["credits"], f["debits"], f["txn_count"]) == (D(9000), D(2000), 5)
    day = next(r for r in intelligence.network_movement(*_range())["series"]
               if r["date"] == TODAY)
    assert (day["pool_credits"], day["pool_debits"]) == (D(9000), D(2000))
