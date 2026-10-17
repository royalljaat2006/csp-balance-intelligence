"""The read-only services behind the redesigned pages: month-end outlook, estimate explanation,
SBI comparison, review workspace and data readiness. They only read; none of them may write to the
production tables or turn a prediction into a balance."""

import datetime as dt
import decimal
import os
import time

import pytest
from bankflow import explain, flows, outlook, readiness, review, sbi_comparison
from bankflow.account_hypothesis import run_hypothesis_test
from bankflow.ingest import run_import
from bankflow.models import DailyEstimate, FlowAggregate
from csp.models import Csp, DailyBalance, MonthlySummary

from tests.bankflow_fixtures import desc_deposit, desc_withdrawal, make_statement

D = decimal.Decimal
d = dt.date
pytestmark = pytest.mark.django_db
TODAY = d(2026, 10, 14)


def _csp(code="1A850001", n=300, **kw):
    return Csp.objects.create(csp_code=code, name=f"CSP {code}", account_count=n, **kw)


def _bal(code, day, value, n):
    DailyBalance.objects.create(csp_id=code, balance_date=day, daily_avg_balance=D(str(value)),
                                account_count=n, source="calling_sheet")


def _est(csp, day, value, status="INSUFFICIENT_EVIDENCE", method="carry_forward", **kw):
    return DailyEstimate.objects.create(
        csp=csp, estimate_date=day, method=method, status=status, reason_code="NO_VALIDATED_MODEL",
        baseline_value=D(str(value)), baseline_date=kw.pop("baseline_date", d(2026, 10, 1)),
        last_seen_date=day, estimated_value=D(str(value)), estimated_delta=D(0),
        baseline_account_count=300, current_account_count=300, account_count_change=0,
        calculated_at=kw.pop("calculated_at", dt.datetime(2026, 10, 14, 6, tzinfo=dt.UTC)), **kw)


# ------------------------------------------------------------------ month-end outlook
def test_outlook_carries_forward_missing_days_and_projects_the_latest_estimate():
    r = outlook.csp_outlook({d(2026, 10, 10): D(100), d(2026, 10, 12): D(200)},
                            d(2026, 10, 1), d(2026, 10, 31), d(2026, 10, 14))
    # days 10..14 = 100, 100 (carried), 200, 200 (carried), 200 (carried)
    assert r["days_counted"] == 5 and r["days_carried_forward"] == 3
    assert r["mtd_average"] == D("160.00")
    assert r["days_remaining"] == 17 and r["latest_estimate"] == D("200.00")
    assert r["projected_average"] == D("190.91")  # (800 + 200 * 17) / 22
    assert r["days_before_first_estimate"] == 9
    assert outlook.csp_outlook({}, d(2026, 10, 1), d(2026, 10, 31), TODAY) is None


def test_outlook_is_provisional_labelled_and_never_touches_production_tables():
    c = _csp()
    _est(c, d(2026, 10, 13), 3000)
    _est(c, d(2026, 10, 14), 3000)
    MonthlySummary.objects.create(csp=c, month="2026-10", days_in_month=31, days_with_data=14,
                                  mtd_mab=D("3100"), projected_mab=D("3150"), slab="S1")
    before = list(DailyBalance.objects.values_list("id"))
    o = outlook.month_end_outlook("2026-10", today=TODAY)
    assert o["csps_with_outlook"] == 1 and o["network"]["projected_average"] == D("3000.00")
    assert o["rows"][0]["provisional"] is True
    assert o["production_reference"]["projected_average"] == D("3150.00")  # reference only
    assert any("not been validated" in w for w in o["warnings"])
    assert list(DailyBalance.objects.values_list("id")) == before
    empty = outlook.month_end_outlook("2026-09", today=TODAY)
    assert empty["csps_with_outlook"] == 0 and empty["warnings"]


# ------------------------------------------------------------------ explanation
def test_explanation_shows_stored_figures_and_does_not_apply_movement():
    c = _csp(raw_attrs={"sheet_total_balance": "900000"})
    _bal(c.csp_code, d(2026, 10, 14), 3000, 300)
    _est(c, d(2026, 10, 14), 3000, flow_net=D("300"), flow_coverage=0.5)
    FlowAggregate.objects.create(
        csp=c, flow_date=d(2026, 10, 12), resolution_mode=flows.MODE_EXACT,
        by_category={"deposit": {"count": 2, "credit": "0", "debit": "500"},
                     "withdrawal": {"count": 1, "credit": "200", "debit": "0"},
                     "transfer": {"count": 1, "credit": "0", "debit": "999"}})
    x = explain.explain_estimate(c.csp_code, today=TODAY)
    assert x["baseline"]["per_account_average"] == D("3000")
    assert x["combined_baseline"] == D("900000")
    assert x["check"]["state"] == "consistent"
    m = x["movement"]
    assert (m["deposits"], m["withdrawals"], m["net"]) == (D(500), D(200), D(300))  # transfer excluded
    assert x["estimate"]["per_account_average"] == D("3000")   # movement NOT added
    assert "does not add it" in x["not_applied"]
    assert any("Provisional" in f["text"] for f in x["flags"])
    assert explain.explain_estimate("NOPE") is None


def test_baseline_check_flags_a_total_that_does_not_match_average_times_count():
    c = _csp(raw_attrs={"sheet_total_balance": "500000"})
    assert explain.baseline_check(c, D(3000))["state"] == "inconsistent"
    assert explain.baseline_check(_csp("1A850002"), D(3000))["state"] == "cannot_check"


def test_explanation_for_a_csp_without_an_estimate_says_so():
    c = _csp()
    x = explain.explain_estimate(c.csp_code, today=TODAY)
    assert x["has_estimate"] is False and "No estimate" in x["message"]


# ------------------------------------------------------------------ SBI comparison
def test_comparison_percentages_aggregates_and_zero_reported_handling():
    a, b = _csp("1A850001"), _csp("1A850002")
    _bal(a.csp_code, d(2026, 8, 1), 1000, 300)
    _bal(a.csp_code, d(2026, 9, 5), 1100, 330)     # predicted 1000*330/300 = 1100: exact
    _bal(b.csp_code, d(2026, 8, 1), 1000, 300)
    _bal(b.csp_code, d(2026, 9, 5), 0, 330)        # reported zero -> % is N/A
    r = sbi_comparison.sbi_comparison()
    rows = {x["csp_code"]: x for x in r["rows"]}
    assert rows["1A850001"]["pct_variance"] == 0 and rows["1A850001"]["difference"] == D("0.00")
    assert rows["1A850002"]["pct_variance"] is None
    s = r["summary"]
    assert s["valid_comparisons"] == 2 and s["excluded_zero_reported"] == 1
    assert s["simple_avg_pct_variance"] == 0.0
    assert s["aggregate"]["reported_total"] == D(1100) * 330 + 0
    assert r["gate"]["has_run"] is False and r["gate"]["satisfied"] is False


def test_the_gate_is_never_satisfied_by_a_thin_history():
    a = _csp()
    _bal(a.csp_code, d(2026, 8, 1), 1000, 300)
    _bal(a.csp_code, d(2026, 9, 5), 1100, 330)
    run_hypothesis_test(persist=True)
    g = sbi_comparison.sbi_comparison()["gate"]
    assert g["has_run"] and g["satisfied"] is False and g["approval"] == "Not granted"
    assert any(c["met"] is False for c in g["criteria"])


# ------------------------------------------------------------------ review + readiness
def _import(tmp_path, rows):
    f = tmp_path / "s.xls"
    f.write_text(make_statement(rows), encoding="utf-8")
    old = time.time() - 60
    os.utime(f, (old, old))
    run_import(tmp_path)


def _row(desc, debit="", credit=""):
    return (desc, debit, credit)


def test_review_lists_unmatched_codes_and_holds_yono_withdrawal_out(tmp_path):
    _csp("1A852455")
    yono = ("BY TRANSFER-62850748548500089299 YONO WITHDRAWAL@KO 1A852455-TRANSFER FROM "
            "4599142105216-")
    _import(tmp_path, [
        _row(desc_withdrawal("62850709168200095256", "1A852455"), credit="4000.00"),
        _row(desc_deposit("62850709168200095257", "1A859999"), debit="1500.00"),  # unknown CSP
        _row(yono, credit="50.00"),
    ])
    q = review.review_queue(30, today=d(2026, 10, 14))
    cats = {c["key"]: c for c in q["categories"]}
    assert set(cats) == {"codes", "labels", "duplicates", "counts", "coverage", "late", "failed"}
    assert cats["codes"]["count"] >= 1
    labels = {i["label"]: i["decision"] for i in cats["labels"]["items"]}
    assert labels.get("yono withdrawal") == "held out until approved"
    assert q["controls"]["can_clear_flags"] is False


def test_readiness_separates_technical_success_from_fitness_for_estimates():
    r = readiness.data_readiness(today=TODAY)
    assert [s["number"] for s in r["stages"]] == [1, 2, 3, 4, 5, 6]
    assert r["verdict"]["reliable"] == "provisional"
    assert any("not been validated" in x for x in r["verdict"]["reasons"])


# ------------------------------------------------------------------ API
@pytest.fixture
def api_key(db):
    from api.auth import generate_raw_key, hash_key
    from api.models import ApiConsumer, ApiKey

    def make(scopes):
        consumer = ApiConsumer.objects.create(name=f"t-{scopes}")
        raw = generate_raw_key()
        ApiKey.objects.create(consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw),
                              scopes=scopes)
        return raw

    return make


NEW_ROUTES = ["data-readiness", "review", "sbi-comparison", "month-end-outlook"]


@pytest.mark.parametrize("name", NEW_ROUTES)
def test_new_routes_need_the_report_scope(client, api_key, name):
    url = f"/api/v1/bank-flow/{name}"
    assert client.get(url).status_code == 401
    assert client.get(url, HTTP_X_API_KEY=api_key("csp:read")).status_code == 403
    assert client.get(url, HTTP_X_API_KEY=api_key("report:read")).status_code == 200


def test_explanation_route_is_scoped_and_404s_for_an_unknown_csp(client, api_key):
    c = _csp()
    key = api_key("report:read")
    ok = client.get(f"/api/v1/bank-flow/estimate-explanation/{c.csp_code}", HTTP_X_API_KEY=key)
    assert ok.status_code == 200 and ok.json()["data"]["csp"]["code"] == c.csp_code
    assert client.get("/api/v1/bank-flow/estimate-explanation/NOPE", HTTP_X_API_KEY=key
                      ).status_code == 404
    assert client.get(f"/api/v1/bank-flow/estimate-explanation/{c.csp_code}",
                      HTTP_X_API_KEY=api_key("csp:read")).status_code == 403
