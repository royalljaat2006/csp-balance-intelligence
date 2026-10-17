"""
Hardening tests for the bank-flow pipeline: variant-flag safety, privacy,
ledger-continuity completeness, rollback/idempotency/concurrency, bounded
queries, evaluation leakage guards, historical readiness, and the Calling Sheet
backfill's protection of live observations.
"""

import datetime as dt
import decimal
import os
import runpy
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey
from bankflow import flows, ingest, services
from bankflow.evaluation import EvalConfig, build_datasets, evaluate
from bankflow.ingest import build_resolver, run_import, variant_rule_confirmed
from bankflow.models import (
    CodeResolution,
    EvaluationRun,
    PredictionRecord,
    StatementFile,
    StatementTransaction,
)
from bankflow.normalize import normalize_row
from bankflow.targets import RefreshEvent, detect_refresh_events
from csp.models import Csp, DailyBalance
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from tests.bankflow_fixtures import desc_withdrawal, make_statement, sample_rows

D = decimal.Decimal


def _write(tmp_path, name, text, age=60):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    old = time.time() - age
    os.utime(p, (old, old))
    return p


@pytest.fixture
def csps(db):
    for code in ("1A852455", "1A850295", "1A850949", "1A850946", "1A850943"):
        Csp.objects.create(csp_code=code, name=f"CSP {code}", account_count=250)


# ------------------------------------------------------------ code-format rule + other formats
@pytest.fixture
def other_format_csp(db):
    Csp.objects.create(csp_code="ZZ123456", name="Other-format CSP", account_count=250)


def test_1a85_codes_are_trimmed_to_eight_characters_without_any_flag(csps):
    """Owner-confirmed rule: '1A85' + 4 digits, max 8 characters; the rest is dropped."""
    r = build_resolver()
    for raw in ("1A8502957", "1A850295N", "1A85029510"):
        res = r.resolve(raw)
        assert res.status == "exact" and res.resolved_code == "1A850295"
        assert res.method == "first_8_chars"
    assert r.resolve("1A85029").status == "ambiguous_truncated"  # cut-off: never guessed


@override_settings(
    BANKFLOW_CONFIRM_TRAILING_VARIANTS=True, BANKFLOW_VARIANT_RULE_CONFIRMATION_REF=""
)
def test_variant_flag_alone_does_not_enable_guessing_for_other_formats(other_format_csp):
    assert variant_rule_confirmed() is False
    res = build_resolver().resolve("ZZ1234567")
    assert res.status == "variant_provisional" and res.resolved_code is None


@override_settings(
    BANKFLOW_CONFIRM_TRAILING_VARIANTS=True, BANKFLOW_VARIANT_RULE_CONFIRMATION_REF="OPS-123"
)
def test_other_format_rule_needs_flag_and_confirmation_reference(other_format_csp):
    assert variant_rule_confirmed() is True
    assert build_resolver().resolve("ZZ1234567").resolved_code == "ZZ123456"


@override_settings(
    BANKFLOW_CONFIRM_TRAILING_VARIANTS=False, BANKFLOW_VARIANT_RULE_CONFIRMATION_REF="OPS-123"
)
def test_reference_without_flag_is_still_off(csps):
    assert variant_rule_confirmed() is False


def test_turning_the_other_format_rule_back_off_unassigns_on_reresolve(tmp_path, other_format_csp):
    rows = [(desc_withdrawal("62850720372300012013", "ZZ1234567"), "", "1000.00")]
    _write(tmp_path, "a.xls", make_statement(rows))
    with override_settings(
        BANKFLOW_CONFIRM_TRAILING_VARIANTS=True, BANKFLOW_VARIANT_RULE_CONFIRMATION_REF="OPS-1"
    ):
        run_import(tmp_path)
        assert CodeResolution.objects.filter(
            status="variant_provisional", resolved_csp_id="ZZ123456"
        ).exists()
    ingest.reresolve_all()  # settings back to defaults: flag off
    assert (
        not CodeResolution.objects.filter(resolved_csp__isnull=False)
        .exclude(status="exact")
        .exists()
    )


# ------------------------------------------------------------ privacy
CUSTOMER_ACCT = "36471609123"
CUSTOMER_NAME = "Test  Customer"


def test_no_customer_account_or_name_is_stored_anywhere(tmp_path, csps):
    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    run_import(tmp_path)
    blob = " ".join(
        str(v) for row in StatementTransaction.objects.values() for v in row.values()
    ) + " ".join(str(f.header) for f in StatementFile.objects.all())
    assert CUSTOMER_ACCT not in blob and "Customer" not in blob and "Mr. Test" not in blob


def test_unexpected_description_layout_cannot_leak_free_text():
    n = normalize_row(
        "BY TRANSFER-NEFT SOMEBODY SECRET ACCT 9988776655 Mrs. Private Person-",
        "ref col with 12345678901",
        debit=D(0),
        credit=D(5),
    )
    assert "SECRET" not in n.description_redacted and "Private" not in n.description_redacted
    assert "9988776655" not in n.description_redacted
    assert n.category == "unclassified" and n.description_hash


def test_free_text_in_channel_position_is_dropped():
    n = normalize_row(
        "BY TRANSFER-62850709168200095256Pay to Mr Secret Name 9988776655 TXN@KO1A852455-X",
        "x",
        debit=D(0),
        credit=D(5),
    )
    assert "Secret" not in n.description_redacted
    assert n.channel_raw == ""  # contains digits -> not an accepted channel label


def test_api_and_dashboard_never_expose_customer_data(client, tmp_path, csps):
    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    run_import(tmp_path)
    evaluate(EvalConfig())
    consumer = ApiConsumer.objects.create(name="pii")
    raw = generate_raw_key()
    ApiKey.objects.create(
        consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw), scopes="report:read"
    )
    bodies = []
    for ep in ("summary", "code-resolution", "flows?limit=50", "evaluations", "target"):
        r = client.get(f"/api/v1/bank-flow/{ep}", HTTP_X_API_KEY=raw)
        assert r.status_code == 200
        bodies.append(r.content.decode())
    get_user_model().objects.create_user("staff", password="x", is_staff=True)
    client.login(username="staff", password="x")
    bodies.append(client.get("/dashboard/bank-flow/").content.decode())
    joined = " ".join(bodies)
    for secret in (
        CUSTOMER_ACCT,
        "Test Customer",  # the planted name; the UI's own "Customer accounts" label is fine
        "Test  Customer",
        "Mr. Test",
        "30707699536",
        "TRANSFER FROM",
        "source_path",
    ):
        assert secret not in joined
    assert "/tmp" not in joined and str(tmp_path) not in joined  # no server paths either


# ------------------------------------------------------------ completeness
def _two_days(tmp_path, *, continuity: bool):
    day1 = [(desc_withdrawal("62850709168200095256", "1A852455"), "", "4000.00")]
    d1 = make_statement(day1, opening="1000.00", start="12 Oct 2026", txn_date="12/10/2026")
    closing = D("5000.00")
    opening2 = closing if continuity else closing + D("7.00")
    day2 = [(desc_withdrawal("62850709168200099999", "1A852455"), "", "100.00")]
    d2 = make_statement(day2, opening=str(opening2), start="13 Oct 2026", txn_date="13/10/2026")
    _write(tmp_path, "d1.xls", d1)
    _write(tmp_path, "d2.xls", d2)


def test_proven_complete_dates_requires_ledger_continuity(tmp_path, csps):
    _two_days(tmp_path, continuity=True)
    run_import(tmp_path)
    complete = flows.proven_complete_dates()
    assert complete == {dt.date(2026, 10, 12)}  # day 2 has no later statement => not proven


def test_broken_continuity_does_not_prove_completeness(tmp_path, csps):
    _two_days(tmp_path, continuity=False)
    run_import(tmp_path)
    assert flows.proven_complete_dates() == set()


def test_single_intraday_statement_is_never_complete(tmp_path, csps):
    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    run_import(tmp_path)
    assert flows.proven_complete_dates() == set()


def test_window_flows_exclude_incomplete_days_and_report_coverage(tmp_path, csps):
    _two_days(tmp_path, continuity=True)
    run_import(tmp_path)
    start, end = dt.date(2026, 10, 11), dt.date(2026, 10, 13)
    got, cov = flows.window_flows(["1A852455"], start, end)
    assert got["1A852455"] == D("-4000.00") and cov == 0.5  # only 12 Oct counted; 13 Oct is partial
    got_all, cov_all = flows.window_flows(["1A852455"], start, end, complete_only=False)
    assert got_all["1A852455"] == D("-4100.00")


# ------------------------------------------------------------ robustness
def test_non_statement_files_in_a_folder_are_ignored(tmp_path, csps):
    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    (tmp_path / "notes.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / ".hidden.xls").write_text("x")
    (tmp_path / "half.xls.crdownload").write_text("x")
    run = run_import(tmp_path)
    assert run.files_seen == 1 and StatementFile.objects.count() == 1


def test_oversize_file_is_refused_with_an_audit_record(tmp_path, csps, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_FILE_BYTES", 100)
    _write(tmp_path, "big.xls", make_statement(sample_rows()))
    run = run_import(tmp_path)
    assert run.files_failed == 1
    sf = StatementFile.objects.get()
    assert sf.status == "failed" and "safety limit" in sf.status_reason


def test_failure_mid_import_rolls_back_everything_and_leaves_an_audit_record(
    tmp_path, csps, monkeypatch
):
    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    real = ingest._insert_rows

    def boom(sf, rows, keys, normalized, resolver):
        real(sf, rows[:3], keys, normalized, resolver)  # partial work, then fail
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(ingest, "_insert_rows", boom)
    run = run_import(tmp_path)
    assert run.files_failed == 1 and run.files_imported == 0
    assert StatementTransaction.objects.count() == 0  # no partial import
    assert CodeResolution.objects.count() == 0
    sf = StatementFile.objects.get()
    assert sf.status == "failed" and "rolled back" in sf.status_reason

    monkeypatch.setattr(ingest, "_insert_rows", real)
    assert run_import(tmp_path).files_duplicate == 1  # failed records are not silently retried...
    retry = run_import(tmp_path, retry_quarantined=True)  # ...unless the operator asks
    assert retry.files_imported == 1
    assert StatementTransaction.objects.count() == len(sample_rows())
    assert StatementFile.objects.count() == 1


def test_import_run_counters_are_durable_per_file(tmp_path, csps):
    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    _write(tmp_path, "b.xls", make_statement(sample_rows(), opening="2000.00"))
    run = run_import(tmp_path)
    run.refresh_from_db()
    assert (run.files_seen, run.files_imported + run.files_quarantined) == (2, 2)
    assert run.finished_at is not None


def test_query_count_is_bounded_not_per_row(tmp_path, csps):
    rows = [
        (desc_withdrawal(f"62850709168{i:09d}", "1A852455"), "", "10.00") for i in range(12_000)
    ]
    _write(tmp_path, "big.xls", make_statement(rows))
    with CaptureQueriesContext(connection) as ctx:
        run = run_import(tmp_path)
    assert run.files_imported == 1
    assert StatementTransaction.objects.count() == 12_000
    # never one query per row (SQLite splits bulk inserts by its 999-variable limit, so only
    # the loose bound applies there); PostgreSQL batches properly and must stay small.
    assert len(ctx) < 12_000 // 20, len(ctx)
    if connection.vendor == "postgresql":
        assert len(ctx) < 150, len(ctx)


def test_small_chunk_size_changes_nothing(tmp_path, csps, monkeypatch):
    monkeypatch.setattr(ingest, "QUERY_CHUNK", 7)
    _write(tmp_path, "h1.xls", make_statement(sample_rows(0)))
    run_import(tmp_path)
    _write(tmp_path, "h2.xls", make_statement(sample_rows(5)))
    run_import(tmp_path)
    assert StatementTransaction.objects.count() == len(sample_rows(5))


@pytest.mark.django_db(transaction=True)
def test_concurrent_importers_never_duplicate_or_lose_rows(tmp_path, csps):
    if connection.vendor != "postgresql":
        pytest.skip("concurrency/advisory-lock behaviour is PostgreSQL-specific")
    f1 = _write(tmp_path, "h1.xls", make_statement(sample_rows(0)))
    f2 = _write(tmp_path, "h2.xls", make_statement(sample_rows(25)))
    barrier = threading.Barrier(4)

    def job(path):
        try:
            barrier.wait(timeout=10)
            return run_import(path, min_age_seconds=0).files_imported
        finally:
            connection.close()

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(job, [f1, f2, f1, f2]))

    keys = list(StatementTransaction.objects.values_list("row_key", flat=True))
    assert len(keys) == len(set(keys)) == len(sample_rows(25))
    assert StatementFile.objects.filter(status="imported").count() == 2
    assert StatementFile.objects.count() == 2  # same hash never stored twice


# ------------------------------------------------------------ evaluation guards
def test_baseline_origin_is_previous_refresh_value_not_a_later_observation(db, csps):
    codes = [f"C{i:03d}" for i in range(40)]
    Csp.objects.bulk_create([Csp(csp_code=c, account_count=250) for c in codes])
    e0, small, e1 = dt.date(2026, 1, 1), dt.date(2026, 1, 5), dt.date(2026, 1, 8)
    for d, v in ((e0, 4000), (small, 4010), (dt.date(2026, 1, 7), 4010), (e1, 4500)):
        DailyBalance.objects.bulk_create(
            DailyBalance(
                csp_id=c,
                balance_date=d,
                daily_avg_balance=D(v),
                account_count=250,
                source="calling_sheet",
            )
            for c in codes
        )
    ev = RefreshEvent(date=e1, previous_date=dt.date(2026, 1, 7), compared=40, changed=40)
    obs = {}
    for d, c, v in DailyBalance.objects.values_list("balance_date", "csp_id", "daily_avg_balance"):
        obs.setdefault(d, {})[c] = (v, 250)
    cfg = EvalConfig(horizon_days=1)
    [data] = build_datasets(obs, [ev], cfg, complete_days=set())
    # origin is the value on the *earliest* observation (no earlier event), not the 4010
    # that appeared on 5 Jan, i.e. before the forecast point but after the previous refresh.
    assert data.base_date == e0
    assert {r["prev"] for r in data.rows} == {4000.0}
    assert {r["d"] for r in data.rows} == {500.0}


def test_refresh_events_carry_account_count_evidence(db):
    codes = [f"C{i:03d}" for i in range(40)]
    Csp.objects.bulk_create([Csp(csp_code=c, account_count=250) for c in codes])
    for d, v, acct in ((dt.date(2026, 1, 1), 4000, 250), (dt.date(2026, 1, 9), 4400, 260)):
        DailyBalance.objects.bulk_create(
            DailyBalance(
                csp_id=c,
                balance_date=d,
                daily_avg_balance=D(v),
                account_count=acct,
                source="calling_sheet",
            )
            for c in codes
        )
    obs = {}
    for d, c, v, a in DailyBalance.objects.values_list(
        "balance_date", "csp_id", "daily_avg_balance", "account_count"
    ):
        obs.setdefault(d, {})[c] = (v, a)
    [ev] = detect_refresh_events(obs)
    assert ev.gap_days == 8
    assert (
        ev.compared_acct_unchanged == 0
    )  # every change here coincides with an account-count change


def test_multiple_configurations_carry_a_selection_warning(db):
    r = evaluate(EvalConfig(batch_configs=3), persist=False)[0]
    assert any("NOT a valid validation" in n for n in r.notes)


def test_forced_dates_that_do_not_match_are_reported(db):
    r = evaluate(EvalConfig(forced_event_dates=["2030-01-01"]), persist=False)[0]
    assert r.status == "INSUFFICIENT_EVIDENCE"


@pytest.fixture
def complete_days(monkeypatch):
    days = {dt.date(2025, 12, 1) + dt.timedelta(days=i) for i in range(400)}
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: days)
    return days


def test_insufficient_evidence_publishes_no_performance_numbers_or_predictions(db, complete_days):
    from tests.integration.test_bankflow import _seed_events

    _seed_events(n_events=6, beta=1.0, noise=0.1)  # a perfect signal, but too few events
    run = EvaluationRun.objects.none()
    runs = evaluate(EvalConfig(horizon_days=1))
    avg = next(r for r in runs if r.target == "avg_change")
    assert avg.status == "INSUFFICIENT_EVIDENCE"
    assert "mae_model" not in avg.metrics and "mae_nochange" not in avg.metrics
    assert PredictionRecord.objects.count() == 0 and run is not None
    assert avg.n_test_events == 0


def test_failed_run_predictions_are_marked_audit_only_on_the_dashboard(client, db, complete_days):
    from tests.integration.test_bankflow import _seed_events

    _seed_events(n_events=12, beta=0.0, noise=25.0, seed=11)
    runs = evaluate(EvalConfig(horizon_days=1))
    assert next(r for r in runs if r.target == "avg_change").status == "EVALUATION_FAILED"
    get_user_model().objects.create_user("staff", password="x", is_staff=True)
    client.login(username="staff", password="x")
    html = client.get("/dashboard/sbi-comparison/").content.decode()
    assert "EVALUATION FAILED" in html
    assert "Model NOT validated" in html
    assert "VALIDATION PASSED" not in html


def test_ingest_never_changes_balances_or_eligibility(tmp_path, csps):
    from csp.rules import is_eligible, slab_for

    DailyBalance.objects.create(
        csp_id="1A852455",
        balance_date=dt.date(2026, 10, 1),
        daily_avg_balance=D("4000"),
        account_count=250,
        source="calling_sheet",
    )
    snapshot = (
        list(
            DailyBalance.objects.values_list("id", "daily_avg_balance", "account_count", "source")
        ),
        list(Csp.objects.values_list("csp_code", "account_count", "status", "name")),
    )
    _write(tmp_path, "a.xls", make_statement(sample_rows()))
    run_import(tmp_path)
    evaluate(EvalConfig())
    assert snapshot == (
        list(
            DailyBalance.objects.values_list("id", "daily_avg_balance", "account_count", "source")
        ),
        list(Csp.objects.values_list("csp_code", "account_count", "status", "name")),
    )
    assert not is_eligible(199) and is_eligible(200)
    assert slab_for(D("12000"), account_count=199) == "NIL"
    assert slab_for(D("12000"), account_count=200) == "S4"


# ------------------------------------------------------------ readiness + backfill
def test_coverage_report_measures_gaps_and_readiness(tmp_path, csps):
    _write(
        tmp_path, "a.xls", make_statement(sample_rows(), start="10 Oct 2026", txn_date="10/10/2026")
    )
    _write(
        tmp_path, "b.xls", make_statement(sample_rows(), start="13 Oct 2026", txn_date="13/10/2026")
    )
    run_import(tmp_path)
    rep = services.coverage_report()
    assert (rep["statement_files"], rep["days_covered"]) == (2, 2)
    assert rep["missing_date_ranges"] == ["2026-10-11..2026-10-12"]
    assert rep["ready_for_evaluation"] is False and rep["events_still_needed"] == 8
    assert rep["attribution_exact_share_by_amount"] is not None


def test_coverage_command_runs_read_only(tmp_path, csps, capsys):
    from django.core.management import call_command

    before = (DailyBalance.objects.count(), StatementFile.objects.count())
    call_command("bank_flow_coverage")
    assert "NOT ready" in capsys.readouterr().out
    assert before == (DailyBalance.objects.count(), StatementFile.objects.count())


def test_backfill_script_never_overwrites_live_calling_sheet_rows(db, tmp_path, monkeypatch):
    import openpyxl

    day = dt.date(2020, 1, 5)
    Csp.objects.create(csp_code="1A850001", account_count=250)
    DailyBalance.objects.create(
        csp_id="1A850001",
        balance_date=day,
        daily_avg_balance=D("1111"),
        account_count=250,
        source="calling_sheet",
    )
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Calling Sheet New"
    ws.append(["section"])
    ws.append(
        [
            "CSP ID",
            "CSP Name",
            "CSP Mail ID",
            "CSP Mobile number",
            "Total Accounts",
            "Total Balance",
            "Avg Balance",
            "Amount to be deposited (for eligiblity/higher slab)",
        ]
    )
    ws.append(["1A850001", "n", "a@b.co", "9999999999", "250", "10,00,000", "9,999", ""])
    ws.append(["1A850002", "n", "c@d.co", "9999999998", "300", "12,00,000", "4,000", ""])
    wb.save(tmp_path / "Calling Sheet_20200105_120000.xlsx")
    monkeypatch.setenv("CS_BACKFILL_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))

    script = os.path.join(
        os.path.dirname(__file__), "..", "..", "scripts", "backfill_calling_sheet_history.py"
    )
    for _ in range(2):  # second pass proves idempotency
        runpy.run_path(script, run_name="__main__")

    live = DailyBalance.objects.get(csp_id="1A850001", balance_date=day)
    assert (live.daily_avg_balance, live.source) == (D("1111.00"), "calling_sheet")
    hist = DailyBalance.objects.get(csp_id="1A850002", balance_date=day)
    assert (hist.daily_avg_balance, hist.source) == (D("4000.00"), "history")
    assert DailyBalance.objects.filter(balance_date=day).count() == 2
    assert Csp.objects.get(csp_code="1A850001").status == ""  # master data untouched
    assert Csp.objects.get(csp_code="1A850002").status == "historical_only"
