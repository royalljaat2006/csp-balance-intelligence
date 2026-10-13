"""Daily estimates, hypothesis runs, 06:00 baseline snapshot, API + dashboard."""

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


def test_no_hypothesis_evidence_gives_carry_forward_insufficient():
    _csp("A")
    _bal("A", d(2026, 9, 22), "1000", 350)
    _bal("A", TODAY, "1000", 360)
    estimates.compute_daily_estimates(TODAY)
    e = _est("A")
    assert e.status == "INSUFFICIENT_EVIDENCE" and e.method == "carry_forward"
    assert e.reason_code == "COUNT_RULE_NOT_VALIDATED"
    assert e.estimated_value == D("1000.00") and e.estimated_delta == D("0.00")
    assert (e.baseline_account_count, e.current_account_count, e.account_count_change) == (
        350, 360, 10,
    )
    assert e.baseline_date == d(2026, 9, 22)
    assert e.expected_error_pct is None
    # the unvalidated candidate is information only, never the estimate
    assert e.details["unvalidated_count_scaled_value"] == "1028.57"


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
    assert _est("SAME").reason_code == "COUNT_UNCHANGED_CARRY_FORWARD"
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


def test_missing_account_count_keeps_carry_forward():
    _csp("A", n=300)
    _bal("A", d(2026, 9, 1), "800", None)
    _bal("A", TODAY, "800", 300)
    estimates.compute_daily_estimates(TODAY)
    assert _est("A").reason_code == "MISSING_ACCOUNT_COUNT"


def test_rerun_is_idempotent_and_keeps_history():
    _csp("A")
    _bal("A", TODAY, "1000", 300)
    estimates.compute_daily_estimates(TODAY)
    estimates.compute_daily_estimates(TODAY)
    estimates.compute_daily_estimates(TODAY - dt.timedelta(days=1))
    assert DailyEstimate.objects.filter(csp_id="A").count() == 2


def _seed_history(events=10, csps=4):
    """`events` monthly refreshes where the value really tracks the account count."""
    for i in range(csps):
        _csp(f"P{i}")
    for e in range(events + 1):
        day = d(2026, 1, 1) + dt.timedelta(days=e * 30)
        n = 350 + e * 10
        for i in range(csps):
            _bal(f"P{i}", day, str((D("1000") * D(n) / D(350)).quantize(D("0.01"))), n)
    return d(2026, 1, 1) + dt.timedelta(days=events * 30)


def test_validated_rule_applies_proportion_and_reports_error_only_then():
    last_day = _seed_history()
    _, status, _, run = run_hypothesis_test()
    assert status == "VALIDATED" and run is not None
    nxt = last_day + dt.timedelta(days=1)  # count rises again, value not yet refreshed
    last = DailyBalance.objects.get(csp_id="P0", balance_date=last_day)
    _bal("P0", nxt, str(last.daily_avg_balance), last.account_count + 10)
    estimates.compute_daily_estimates(nxt)
    e = DailyEstimate.objects.get(csp_id="P0", estimate_date=nxt)
    assert e.status == "VALIDATED" and e.method == "growth_only"
    assert e.estimated_value > e.baseline_value
    assert e.expected_error_pct is not None and e.hypothesis_run_id == run.pk


def test_insufficient_history_never_invents_error_or_validation():
    _seed_history(events=1, csps=2)
    _, status, _, _ = run_hypothesis_test()
    assert status == "INSUFFICIENT_EVIDENCE"
    estimates.compute_daily_estimates(d(2026, 1, 8))
    assert not DailyEstimate.objects.exclude(expected_error_pct=None).exists()
    assert not DailyEstimate.objects.filter(status__in=["VALIDATED", "PROVISIONAL"]).exists()


def test_hypothesis_uses_only_data_up_to_cutoff():
    _seed_history(events=10, csps=4)
    _, full, _, _ = run_hypothesis_test(persist=False)
    _, early, _, _ = run_hypothesis_test(persist=False, until=d(2026, 1, 15))
    assert full == "VALIDATED" and early == "INSUFFICIENT_EVIDENCE"


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
    assert row["estimate"]["method"] == "carry_forward"
    assert row["status"] == "INSUFFICIENT_EVIDENCE" and row["expected_error_pct"] is None
    assert client.get(url + "?status=BOGUS", **h).status_code == 422
    summ = client.get(url + "/summary", **h).json()["data"]
    assert summ["by_status"] == {"INSUFFICIENT_EVIDENCE": 1}
    hyp = client.get("/api/v1/bank-flow/account-count-hypothesis", **h).json()["data"]
    assert hyp["status"] == "NOT_RUN"


def test_dashboard_shows_estimates_distinct_from_observed(client):
    get_user_model().objects.create_user("staff", password="x", is_staff=True)
    client.login(username="staff", password="x")
    assert "No estimates computed yet" in client.get("/dashboard/bank-flow/").content.decode()
    _csp("A")
    _bal("A", TODAY, "1000", 300)
    estimates.compute_daily_estimates(TODAY)
    page = client.get("/dashboard/bank-flow/").content.decode()
    assert "Observed (SBI)" in page and "(est.)" in page and "INSUFFICIENT_EVIDENCE" in page
