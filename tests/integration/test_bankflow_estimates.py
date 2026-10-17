"""Daily estimates (basics), validation runs, 06:00 baseline snapshot, API + dashboard.

The movement arithmetic itself is covered in test_movement_estimator.py."""

import datetime as dt
import decimal

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey
from bankflow import estimates
from bankflow.account_hypothesis import run_hypothesis_test
from bankflow.models import BaselineSnapshot, DailyEstimate, HypothesisRun
from csp.models import Csp, DailyBalance, IngestLog
from django.contrib.auth import get_user_model
from django.core.management import call_command

D = decimal.Decimal
d = dt.date
pytestmark = pytest.mark.django_db
TODAY = d(2026, 10, 13)


def _csp(code, n=300, status=""):
    return Csp.objects.create(csp_code=code, name=code, account_count=n, status=status)


def _bal(code, day, value, n, source="calling_sheet"):
    DailyBalance.objects.create(
        csp_id=code, balance_date=day, daily_avg_balance=D(value), account_count=n, source=source
    )


def _est(code, estimate_date=TODAY):
    return DailyEstimate.objects.get(csp_id=code, estimate_date=estimate_date)


def test_unvalidated_estimate_is_a_labelled_shadow_value_not_a_scaled_average():
    _csp("A")
    _bal("A", d(2026, 9, 22), "1000", 350)
    _bal("A", TODAY, "1000", 360)
    estimates.compute_daily_estimates(TODAY)
    e = _est("A")
    assert e.status == "INSUFFICIENT_EVIDENCE" and e.method == "movement"
    assert (e.baseline_account_count, e.current_account_count, e.account_count_change) == (
        350, 360, 10,
    )
    assert e.baseline_date == d(2026, 9, 22)
    assert e.expected_error_pct is None
    # the combined balance is untouched by the count change: 1000 x 350 = 350,000; the per-account
    # figure only changes its divisor (350,000 / 360), it is NOT scaled up
    assert e.estimated_combined == D("350000.00")
    assert e.estimated_value == D("972.22") and e.estimated_value < e.baseline_value


def test_never_writes_to_daily_balance():
    _csp("A")
    _bal("A", TODAY, "1000", 300)
    q = DailyBalance.objects.values_list("csp_id", "balance_date", "daily_avg_balance", "source")
    before = list(q)
    estimates.compute_daily_estimates(TODAY)
    estimates.compute_daily_estimates(TODAY)
    assert before == list(q)


def test_unchanged_count_missing_baseline_gate_and_untracked():
    _csp("SAME")
    _bal("SAME", d(2026, 9, 1), "800", 300)
    _bal("SAME", TODAY, "800", 300)
    _csp("NONE")  # tracked, no balance rows
    _csp("SMALL", n=150)
    _bal("SMALL", TODAY, "900", 150)
    _csp("OLD", status="historical_only")  # not tracked: no row
    _bal("OLD", TODAY, "100", 300)
    estimates.compute_daily_estimates(TODAY)
    assert _est("SAME").estimated_value == D("800.00") and _est("SAME").method == "movement"
    assert _est("NONE").status == "UNAVAILABLE" and _est("NONE").estimated_value is None
    small = _est("SMALL")
    assert small.status == "UNAVAILABLE" and small.reason_code == "BELOW_200_ACCOUNT_GATE"
    assert small.estimated_value is None
    assert not DailyEstimate.objects.filter(csp_id="OLD").exists()


def test_demo_and_carry_forward_rows_are_not_baselines():
    _csp("A")
    _bal("A", d(2026, 9, 1), "700", 300, source="demo")
    _bal("A", TODAY, "700", 300, source="carry_forward")
    estimates.compute_daily_estimates(TODAY)
    assert _est("A").status == "UNAVAILABLE" and _est("A").reason_code == "NO_BASELINE"


def test_a_baseline_with_no_valid_account_count_is_unavailable():
    _csp("A", n=300)
    _bal("A", d(2026, 9, 1), "800", None)
    _bal("A", d(2026, 9, 5), "900", 0)  # a zero count is invalid too
    _bal("A", TODAY, "900", 0)
    estimates.compute_daily_estimates(TODAY)
    assert _est("A").status == "UNAVAILABLE"
    assert _est("A").reason_code == "MISSING_ACCOUNT_COUNT" and _est("A").estimated_value is None


def test_rerun_is_idempotent_and_keeps_history():
    _csp("A")
    _bal("A", TODAY, "1000", 300)
    estimates.compute_daily_estimates(TODAY)
    estimates.compute_daily_estimates(TODAY)
    estimates.compute_daily_estimates(TODAY - dt.timedelta(days=1))
    assert DailyEstimate.objects.filter(csp_id="A").count() == 2  # today, and yesterday: no baseline yet


def _seed_history(events=10, csps=4):
    """`events` monthly refreshes of a value that does NOT follow the statements."""
    for i in range(csps):
        _csp(f"P{i}")
    for e in range(events + 1):
        day = d(2026, 1, 1) + dt.timedelta(days=e * 30)
        n = 350 + e * 10
        for i in range(csps):
            _bal(f"P{i}", day, str(D("1000") + e), n)
    return d(2026, 1, 1) + dt.timedelta(days=events * 30)


def test_without_statement_evidence_the_estimator_is_never_validated():
    _seed_history()
    _, status, _, run = run_hypothesis_test()
    assert status == "INSUFFICIENT_EVIDENCE" and run is not None
    assert run.selected_method == "carry_forward"
    estimates.compute_daily_estimates(d(2026, 1, 8))
    assert not DailyEstimate.objects.exclude(expected_error_pct=None).exists()
    assert not DailyEstimate.objects.filter(status__in=["VALIDATED", "PROVISIONAL"]).exists()


def test_validation_uses_only_data_up_to_cutoff():
    _seed_history(events=10, csps=4)
    _, full, _, _ = run_hypothesis_test(persist=False)
    _, early, _, _ = run_hypothesis_test(persist=False, until=d(2026, 1, 15))
    assert early == "INSUFFICIENT_EVIDENCE" and full == "INSUFFICIENT_EVIDENCE"


def test_hypothesis_run_is_insert_only_history():
    _seed_history(events=2, csps=2)
    run_hypothesis_test()
    run_hypothesis_test()
    assert HypothesisRun.objects.count() == 2


# ---------------------------------------------------------------- 06:00 snapshot
def test_snapshot_is_idempotent_insert_only_and_logged():
    _csp("A")
    _csp("B")
    _bal("A", TODAY, "1000", 300)
    call_command("snapshot_calling_sheet_baseline", date=TODAY.isoformat())
    _bal("B", TODAY, "500", 250)
    DailyBalance.objects.filter(csp_id="A").update(daily_avg_balance=D("9999"))  # later poll
    call_command("snapshot_calling_sheet_baseline", date=TODAY.isoformat())
    call_command("snapshot_calling_sheet_baseline", date=TODAY.isoformat())
    snap = {s.csp_id: s for s in BaselineSnapshot.objects.all()}
    assert set(snap) == {"A", "B"}
    assert snap["A"].avg_balance == D("1000.00")  # first capture preserved
    logs = IngestLog.objects.filter(source="baseline_snapshot").order_by("id")
    assert [log.rows_upserted for log in logs] == [1, 1, 0]
    assert all(log.status == "success" for log in logs)


def test_snapshot_with_no_readings_is_partial_not_silent():
    _csp("A")
    call_command("snapshot_calling_sheet_baseline", date=TODAY.isoformat())
    log = IngestLog.objects.get(source="baseline_snapshot")
    assert log.status == "partial" and "no Calling Sheet readings" in log.error_summary


def test_snapshot_failure_is_recorded(monkeypatch):
    _csp("A")
    _bal("A", TODAY, "1000", 300)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(BaselineSnapshot.objects, "bulk_create", boom)
    with pytest.raises(RuntimeError):
        call_command("snapshot_calling_sheet_baseline", date=TODAY.isoformat())
    log = IngestLog.objects.get(source="baseline_snapshot")
    assert log.status == "failed" and "db down" in log.error_summary
    assert not BaselineSnapshot.objects.exists()


# ---------------------------------------------------------------- API + dashboard
@pytest.fixture
def api_key(db):
    def make(scopes):
        consumer = ApiConsumer.objects.create(name=f"t-{scopes}")
        raw = generate_raw_key()
        ApiKey.objects.create(
            consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw), scopes=scopes
        )
        return raw

    return make


def test_estimates_api_scope_and_labelling(client, api_key):
    _csp("A")
    _bal("A", d(2026, 9, 22), "1000", 350)
    _bal("A", TODAY, "1000", 360)
    estimates.compute_daily_estimates(TODAY)
    url = "/api/v1/bank-flow/estimates"
    assert client.get(url).status_code == 401
    assert client.get(url, HTTP_X_API_KEY=api_key("csp:read")).status_code == 403
    h = {"HTTP_X_API_KEY": api_key("report:read")}
    res = client.get(url + "?limit=5", **h)
    assert res.status_code == 200
    row = res.json()["data"][0]
    assert row["observed"]["avg_balance"] == 1000.0
    assert row["estimate"]["method"] == "movement"
    assert row["estimate"]["combined_balance"] == 350000.0
    assert row["movement"] == {"eligible_deposits": 0.0, "eligible_withdrawals": 0.0,
                               "net_eligible": 0.0}
    assert row["status"] == "INSUFFICIENT_EVIDENCE" and row["expected_error_pct"] is None
    assert client.get(url + "?status=BOGUS", **h).status_code == 422
    summ = client.get(url + "/summary", **h).json()["data"]
    assert summ["by_status"] == {"INSUFFICIENT_EVIDENCE": 1}
    hyp = client.get("/api/v1/bank-flow/account-count-hypothesis", **h).json()["data"]
    assert hyp["status"] == "NOT_RUN"


def test_dashboard_shows_estimates_distinct_from_observed(client):
    get_user_model().objects.create_user("staff", password="x", is_staff=True)
    client.login(username="staff", password="x")
    assert "No outlook for this month" in client.get("/dashboard/month-end/").content.decode()
    _csp("A")
    _bal("A", TODAY, "1000", 300)
    estimates.compute_daily_estimates(TODAY)
    page = client.get("/dashboard/csp/A/").content.decode()
    assert "SBI-reported average" in page and "Our estimate" in page
    assert "Shadow estimate, not validated" in page  # the estimate is labelled, never a balance
