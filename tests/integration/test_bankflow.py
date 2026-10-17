"""
Integration tests: ingestion, idempotency, quarantine, code resolution,
aggregates, evaluation statuses, API permissions, dashboard access -- and that
the Calling Sheet side (DailyBalance / Csp / eligibility) is left untouched.
"""

import datetime as dt
import decimal
import os
import random
import time

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey
from bankflow import flows, services
from bankflow.evaluation import EvalConfig, evaluate
from bankflow.ingest import reresolve_all, run_import
from bankflow.models import (
    CodeResolution,
    EvaluationRun,
    FlowAggregate,
    PredictionRecord,
    StatementFile,
    StatementTransaction,
    UnresolvedFlowDaily,
)
from csp.models import Csp, DailyBalance
from csp.rules import slab_for
from django.contrib.auth import get_user_model

from tests.bankflow_fixtures import desc_withdrawal, make_statement, sample_rows

D = decimal.Decimal
pytestmark = pytest.mark.django_db


@pytest.fixture
def csps(db):
    for code in ("1A852455", "1A850295", "1A850949", "1A850946", "1A850943"):
        Csp.objects.create(csp_code=code, name=f"CSP {code}", account_count=250)


def _write(tmp_path, name, text, age=60):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    old = time.time() - age
    os.utime(p, (old, old))
    return p


# ---------------------------------------------------------------- ingestion
def test_ingest_imports_resolves_and_never_touches_calling_sheet(tmp_path, csps):
    DailyBalance.objects.create(
        csp_id="1A852455",
        balance_date=dt.date(2026, 10, 1),
        daily_avg_balance=D("4000"),
        account_count=250,
        source="calling_sheet",
    )
    before = (DailyBalance.objects.count(), Csp.objects.count())
    _write(tmp_path, "SBI_1556_12-10-2026.xls", make_statement(sample_rows()))

    run = run_import(tmp_path)
    assert (run.files_seen, run.files_imported, run.files_quarantined) == (1, 1, 0)
    assert StatementTransaction.objects.count() == len(sample_rows())
    # Calling Sheet side untouched; NO placeholder CSPs created for unmatched codes
    assert (DailyBalance.objects.count(), Csp.objects.count()) == before
    assert DailyBalance.objects.get().daily_avg_balance == D("4000.00")

    by_status = {r.status for r in CodeResolution.objects.all()}
    assert {"exact", "ambiguous_truncated", "unmatched", "missing"} <= by_status
    assert "variant_provisional" not in by_status  # 1A85 codes are trimmed to 8 characters instead
    trimmed = CodeResolution.objects.get(method="first_8_chars")  # 1A8502957 -> 1A850295
    assert trimmed.status == "exact" and trimmed.resolved_csp_id == "1A850295"
    # ambiguous / unmatched are NOT attributed to any CSP
    assert (
        CodeResolution.objects.exclude(status="exact").filter(resolved_csp__isnull=False).count()
        == 0
    )


def test_rerun_is_idempotent_and_counts_duplicates(tmp_path, csps):
    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    run_import(tmp_path)
    n = StatementTransaction.objects.count()
    agg = FlowAggregate.objects.count()
    second = run_import(tmp_path)
    assert second.files_duplicate == 1 and second.files_imported == 0
    assert StatementTransaction.objects.count() == n
    assert FlowAggregate.objects.count() == agg
    assert StatementFile.objects.get().seen_count == 2


def test_source_files_are_never_deleted_or_moved(tmp_path, csps):
    p = _write(tmp_path, "a.xls", make_statement(sample_rows()))
    run_import(tmp_path)
    assert p.exists() and list(tmp_path.iterdir()) == [p]


def test_recently_modified_files_are_skipped_as_possibly_incomplete(tmp_path, csps):
    _write(tmp_path, "a.xls", make_statement(sample_rows()), age=0)
    run = run_import(tmp_path, min_age_seconds=30)
    assert run.files_skipped == 1 and StatementFile.objects.count() == 0


def test_overlapping_hourly_export_adds_only_new_rows(tmp_path, csps):
    _write(tmp_path, "h1.xls", make_statement(sample_rows(0)))
    run_import(tmp_path)
    base = StatementTransaction.objects.count()
    _write(tmp_path, "h2.xls", make_statement(sample_rows(5)))  # same day, superset
    run = run_import(tmp_path)
    assert run.files_imported == 1
    assert StatementTransaction.objects.count() == base + 5
    second = StatementFile.objects.get(filename="h2.xls")
    assert second.reconciliation.overlap_rows == base
    assert second.reconciliation.conflicting_rows == 0


def test_conflicting_overlap_is_quarantined_not_merged(tmp_path, csps):
    _write(tmp_path, "h1.xls", make_statement(sample_rows()))
    run_import(tmp_path)
    n = StatementTransaction.objects.count()
    # same rows, different opening balance => every running balance differs
    _write(tmp_path, "h2.xls", make_statement(sample_rows(), opening="2000.00"))
    run = run_import(tmp_path)
    assert run.files_quarantined == 1
    assert StatementTransaction.objects.count() == n
    q = StatementFile.objects.get(filename="h2.xls")
    assert q.status == "quarantined" and "conflict" in q.status_reason.lower()


def test_failed_reconciliation_is_quarantined_with_a_record(tmp_path, csps):
    _write(tmp_path, "bad.xls", make_statement(sample_rows(), corrupt_balance_at=2))
    run = run_import(tmp_path)
    assert run.files_quarantined == 1
    q = StatementFile.objects.get()
    assert q.status == "quarantined" and q.reconciliation.status == "FAILED"
    assert StatementTransaction.objects.count() == 0


def test_unexpected_account_is_quarantined(tmp_path, csps):
    _write(tmp_path, "other.xls", make_statement(sample_rows(), account="_00000011111111111"))
    run = run_import(tmp_path)
    assert run.files_quarantined == 1
    assert "Unexpected account" in StatementFile.objects.get().status_reason


def test_unsupported_binary_xls_is_recorded_as_failed_not_crashed(tmp_path, csps):
    p = tmp_path / "old.xls"
    p.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 200)
    os.utime(p, (time.time() - 60,) * 2)
    run = run_import(tmp_path)
    assert run.files_failed == 1
    assert StatementFile.objects.get().status == "failed"


def test_header_gap_is_surfaced_as_partial_not_reconciled(tmp_path, csps):
    _write(tmp_path, "gap.xls", make_statement(sample_rows(), book="900000.00"))
    run_import(tmp_path)
    rec = StatementFile.objects.get().reconciliation
    assert rec.status == "PARTIAL" and rec.header_gap and rec.header_gap > 0


def test_missing_path_is_reported_not_raised(tmp_path):
    run = run_import(tmp_path / "does-not-exist")
    assert "does not exist" in run.error_summary


def test_duplicate_reference_and_reversal_detection(tmp_path, csps):
    rows = [
        (desc_withdrawal("62850709168200095256", "1A852455"), "", "700.00"),
        # same reference, opposite side, same amount => reversal pair
        (desc_withdrawal("62850709168200095256", "1A852455"), "700.00", ""),
        # same reference AND same side again => duplicate reference
        (desc_withdrawal("62850709168200099999", "1A852455"), "", "50.00"),
        (desc_withdrawal("62850709168200099999", "1A852455"), "", "60.00"),
    ]
    _write(tmp_path, "a.xls", make_statement(rows))
    run_import(tmp_path)
    assert StatementTransaction.objects.filter(is_reversal_candidate=True).count() == 1
    assert StatementTransaction.objects.filter(is_duplicate_ref=True).count() == 2


# ---------------------------------------------------------------- aggregates
def test_aggregates_signs_unresolved_visibility_and_provisional_mode(tmp_path, csps):
    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    run_import(tmp_path)
    day = dt.date(2026, 10, 12)

    a = FlowAggregate.objects.get(csp_id="1A852455", flow_date=day, resolution_mode="exact_only")
    # 2 withdrawals (4000 + 3000 credited to pool) + 1 money transfer (3100 debit)
    assert a.credit_amount == D("7000.00") and a.debit_amount == D("3100.00")
    assert flows.signed_flow(a.by_category, "customer_wd_dep") == D(
        "-7000.00"
    )  # customers' balances fell
    assert flows.signed_flow(a.by_category, "customer_wd_dep_xfer") == D("-3900.00")

    # 1A8502957 is trimmed to the code 1A850295 and counts like any exact match, in both modes
    for mode in ("exact_only", "with_provisional"):
        t = FlowAggregate.objects.get(csp_id="1A850295", resolution_mode=mode, flow_date=day)
        assert t.credit_amount == D("1000.00") and t.debit_amount == D("10000.00")

    unresolved = {u.status: u for u in UnresolvedFlowDaily.objects.filter(flow_date=day)}
    assert {"ambiguous_truncated", "unmatched"} <= set(unresolved)
    assert "variant_provisional" not in unresolved
    assert "pool_operations_no_csp" in unresolved  # CDM funding is not counted as a missing KO code
    assert unresolved["unmatched"].credit_amount == D(
        "350.00"
    )  # 250 + the off-us 100 (KO 1A851803 unknown)


def test_match_rates_report_amount_as_well_as_count(tmp_path, csps):
    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    run_import(tmp_path)
    stats = services.code_resolution_stats()
    assert stats["total_transactions"] == len(sample_rows())
    ex = next(r for r in stats["by_status"] if r["status"] == "exact")
    assert ex["count_share"] != ex["amount_share"]  # count % and amount % are reported separately
    assert stats["variant_rule_confirmed"] is False  # the generic, other-format rule stays off
    assert "1A85" in stats["code_format_rule"] and stats["trimmed_to_8_chars"]["count"] == 1


def test_reresolve_picks_up_new_csp_without_altering_raw_codes(tmp_path, csps):
    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    run_import(tmp_path)
    raw_before = sorted(CodeResolution.objects.values_list("ko_raw", flat=True))
    Csp.objects.create(csp_code="1A859999", name="late arrival", account_count=300)
    out = reresolve_all()
    assert out["resolutions_changed"] >= 1
    assert CodeResolution.objects.get(ko_raw="1A859999").status == "exact"
    assert sorted(CodeResolution.objects.values_list("ko_raw", flat=True)) == raw_before


# ---------------------------------------------------------------- evaluation
def _seed_events(n_events: int, beta: float, noise: float, seed: int = 7, n_csp: int = 40):
    """Calling Sheet observations stepwise at 7-day events + daily flow data
    (net flow per account linearly related to the change, plus noise)."""
    rng = random.Random(seed)
    start = dt.date(2026, 1, 1)
    codes = [f"1A85{i:04d}" for i in range(n_csp)]
    Csp.objects.bulk_create([Csp(csp_code=c, name=c, account_count=250) for c in codes])
    Csp.objects.create(csp_code="DUMMY001", name="dummy", account_count=250)
    value = {c: 4000.0 for c in codes}
    for k in range(n_events):
        ev = start + dt.timedelta(days=7 * (k + 1))
        prev_day = ev - dt.timedelta(days=1)
        xs = {c: rng.uniform(-20, 20) for c in codes}
        DailyBalance.objects.bulk_create(
            DailyBalance(
                csp_id=c,
                balance_date=prev_day,
                daily_avg_balance=D(str(round(value[c], 2))),
                account_count=250,
                source="calling_sheet",
            )
            for c in codes
        )
        new = {c: value[c] + beta * xs[c] + rng.gauss(0, noise) + 1.0 for c in codes}
        DailyBalance.objects.bulk_create(
            DailyBalance(
                csp_id=c,
                balance_date=ev,
                daily_avg_balance=D(str(round(new[c], 2))),
                account_count=250,
                source="calling_sheet",
            )
            for c in codes
        )
        value = new
        # daily coverage for the window (prev refresh, ev-1]; the CSP flow lands on ev-1
        for off in range(1, 8):
            day = ev - dt.timedelta(days=off)
            rows = [
                FlowAggregate(
                    csp_id="DUMMY001",
                    flow_date=day,
                    resolution_mode="exact_only",
                    by_category={},
                    recon_status="RECONCILED",
                )
            ]
            if off == 1:
                for c in codes:
                    deposit = max(xs[c], 0) * 250
                    withdrawal = max(-xs[c], 0) * 250
                    rows.append(
                        FlowAggregate(
                            csp_id=c,
                            flow_date=day,
                            resolution_mode="exact_only",
                            recon_status="RECONCILED",
                            by_category={
                                "deposit": {
                                    "count": 1,
                                    "credit": "0",
                                    "debit": str(round(deposit, 2)),
                                },
                                "withdrawal": {
                                    "count": 1,
                                    "credit": str(round(withdrawal, 2)),
                                    "debit": "0",
                                },
                            },
                        )
                    )
            FlowAggregate.objects.bulk_create(rows)


@pytest.fixture
def complete_days(monkeypatch):
    """The synthetic evaluation fixtures seed FlowAggregate rows directly (no statements),
    so declare every day in range 'proven complete'. Continuity proof itself is tested
    separately with real statements (test_proven_complete_dates_*)."""
    start = dt.date(2025, 12, 1)
    days = {start + dt.timedelta(days=i) for i in range(400)}
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: days)
    return days


def _by_target(runs):
    return {r.target: r for r in runs}


def test_with_no_data_every_target_is_insufficient_evidence_and_nothing_breaks():
    runs = _by_target(evaluate(EvalConfig(horizon_days=1)))
    assert runs["avg_change"].status == "INSUFFICIENT_EVIDENCE"
    assert runs["direction"].status == "INSUFFICIENT_EVIDENCE"
    assert runs["month_end_mab"].status == "INSUFFICIENT_EVIDENCE"
    assert "unavailable" in runs["month_end_mab"].outcome_detail
    assert DailyBalance.objects.count() == 0


def test_few_events_stay_insufficient_even_when_relationship_is_perfect(complete_days):
    _seed_events(n_events=5, beta=1.0, noise=0.0)
    runs = _by_target(evaluate(EvalConfig(horizon_days=1)))
    r = runs["avg_change"]
    assert r.status == "INSUFFICIENT_EVIDENCE"
    # the first event has no earlier refresh to open its flow window, so N events -> N-1 usable
    assert r.n_events == 4 and "need >= 8" in r.outcome_detail


def test_enough_events_with_real_signal_passes_on_heldout_only(complete_days):
    _seed_events(n_events=12, beta=1.0, noise=0.3)
    runs = _by_target(evaluate(EvalConfig(horizon_days=1)))
    r = runs["avg_change"]
    assert r.status == "VALIDATION_PASSED", (r.outcome_detail, r.metrics.get("per_event"))
    assert r.n_events == 11 and r.n_test_events == 8  # first of 12 events has no flow window
    assert r.metrics["mae_model"] < r.metrics["mae_nochange"]
    assert runs["direction"].status in ("VALIDATION_PASSED", "EVALUATION_FAILED")
    # stored predictions are held-out events only (never the 3 training events)
    first_test = min(PredictionRecord.objects.filter(run=r).values_list("event_date", flat=True))
    assert first_test > dt.date(2026, 1, 1) + dt.timedelta(days=7 * 4)


def test_enough_events_without_signal_fails_not_passes(complete_days):
    _seed_events(n_events=12, beta=0.0, noise=25.0, seed=11)
    r = _by_target(evaluate(EvalConfig(horizon_days=1)))["avg_change"]
    assert r.status == "EVALUATION_FAILED"
    assert "criteria_not_met" in r.outcome_detail


def test_horizon_zero_is_labelled_association(complete_days):
    _seed_events(n_events=5, beta=1.0, noise=0.1)
    r = _by_target(evaluate(EvalConfig(horizon_days=0)))["avg_change"]
    assert any("ASSOCIATION" in n for n in r.notes)


def test_account_count_changes_are_flagged_and_subsettable(complete_days):
    _seed_events(n_events=5, beta=1.0, noise=0.0)
    # change account counts on the last event for half the CSPs
    last = DailyBalance.objects.order_by("-balance_date").first().balance_date
    DailyBalance.objects.filter(balance_date=last, csp_id__lt="1A850020").update(account_count=260)
    runs = evaluate(EvalConfig(horizon_days=1, subset="acct_unchanged", min_rows_per_event=5))
    assert runs  # runs cleanly on the unchanged-account subset


def test_evaluation_never_modifies_calling_sheet_data(complete_days):
    _seed_events(n_events=5, beta=1.0, noise=0.1)
    snapshot = list(
        DailyBalance.objects.values_list("id", "daily_avg_balance", "account_count", "source")
    )
    evaluate(EvalConfig())
    assert (
        list(DailyBalance.objects.values_list("id", "daily_avg_balance", "account_count", "source"))
        == snapshot
    )
    assert EvaluationRun.objects.count() == 3


def test_existing_rule19_and_200_account_gate_are_unchanged():
    assert slab_for(D("9000"), account_count=199) == "NIL"
    assert slab_for(D("9000"), account_count=200) == "S3"


# ---------------------------------------------------------------- API + dashboard
@pytest.fixture
def api_key(db):
    def make(scopes):
        consumer = ApiConsumer.objects.create(name=f"t-{scopes or 'none'}")
        raw = generate_raw_key()
        ApiKey.objects.create(
            consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw), scopes=scopes
        )
        return raw

    return make


def test_api_requires_report_scope(client, api_key, tmp_path, csps):
    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    run_import(tmp_path)
    url = "/api/v1/bank-flow/summary"
    assert client.get(url).status_code == 401
    assert client.get(url, HTTP_X_API_KEY=api_key("csp:read")).status_code == 403
    ok = client.get(url, HTTP_X_API_KEY=api_key("report:read"))
    assert ok.status_code == 200
    body = ok.json()["data"]
    assert body["ingestion"]["files"]["imported"] == 1
    assert body["ingestion"]["latest_statement"]["reconciliation"] == "RECONCILED"


def test_api_endpoints_shape(client, api_key, tmp_path, csps):
    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    run_import(tmp_path)
    evaluate(EvalConfig())
    h = {"HTTP_X_API_KEY": api_key("report:read")}
    res = client.get("/api/v1/bank-flow/code-resolution", **h).json()["data"]
    statuses = {r["status"] for r in res["match_rates"]["by_status"]}
    assert "exact" in statuses and "ambiguous_truncated" in statuses
    flows_res = client.get("/api/v1/bank-flow/flows?limit=5", **h).json()
    assert flows_res["pagination"]["total"] >= 1
    # gross order: 1A850295 (deposit + the trimmed 1A8502957 withdrawal) ahead of 1A852455
    assert [r["csp_code"] for r in flows_res["data"][:2]] == ["1A850295", "1A852455"]
    ev = client.get("/api/v1/bank-flow/evaluations", **h).json()["data"]["evaluations"]
    assert {e["target"] for e in ev} == {"avg_change", "direction", "month_end_mab"}
    assert all(e["status"] == "INSUFFICIENT_EVIDENCE" for e in ev)
    assert client.get("/api/v1/bank-flow/target", **h).status_code == 200
    assert client.get("/api/v1/bank-flow/flows?mode=bogus", **h).status_code == 422


def test_dashboard_is_staff_only(client, db):
    User = get_user_model()
    url = "/dashboard/sbi-comparison/"
    assert client.get(url).status_code == 302  # anonymous -> login
    User.objects.create_user("viewer", password="x", is_staff=False)
    client.login(username="viewer", password="x")
    assert client.get(url).status_code == 403
    client.logout()
    User.objects.create_user("staff", password="x", is_staff=True)
    client.login(username="staff", password="x")
    assert client.get(url).status_code == 200


def test_dashboard_labels_status_and_predictions_honestly(client, db, tmp_path, csps):
    User = get_user_model()
    User.objects.create_user("staff", password="x", is_staff=True)
    client.login(username="staff", password="x")
    empty = client.get("/dashboard/sbi-comparison/").content.decode()
    assert "NOT EVALUATED" in empty and "No validation run yet" in empty

    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    run_import(tmp_path)
    evaluate(EvalConfig())
    html = client.get("/dashboard/sbi-comparison/").content.decode()
    assert "INSUFFICIENT EVIDENCE" in html
    ready = client.get("/dashboard/readiness/").content.decode()
    assert "Newest statement: ledger check" in ready
    assert "nine-character code belongs to its eight-character parent" in ready  # the format rule
