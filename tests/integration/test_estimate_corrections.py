"""Historical reconciliation of the shadow estimates: ordinary late data versus an explicit,
authorised, audited correction. Synthetic data only."""

import datetime as dt
import decimal
import os
import time
from io import StringIO

import pytest
from bankflow import corrections, estimates, flows, outlook
from bankflow.ingest import run_import
from bankflow.models import DailyEstimate, EstimateCorrection, FlowAggregate, StatementTransaction
from bankflow.review import review_queue
from csp.models import Csp, DailyBalance, MonthlySummary
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError

from tests.bankflow_fixtures import desc_deposit, make_statement

D = decimal.Decimal
d = dt.date
pytestmark = pytest.mark.django_db
CSP = "1A850001"


def day(n: int) -> dt.date:
    return d(2026, 10, 1) + dt.timedelta(days=n - 1)


@pytest.fixture
def complete(monkeypatch):
    days = {day(n) for n in range(1, 13)}
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: days)
    return days


@pytest.fixture
def root(db):
    return get_user_model().objects.create_superuser("root", password="x")


def _seed(code=CSP, n=300, last=12):
    Csp.objects.create(csp_code=code, name=f"CSP {code}", account_count=n)
    for i in range(1, last + 1):
        DailyBalance.objects.create(csp_id=code, balance_date=day(i), daily_avg_balance=D(1000),
                                    account_count=n, source="calling_sheet")


def _flow(when, deposit=0, withdrawal=0, code=CSP):
    cats = {}
    if deposit:
        cats["deposit"] = {"count": 1, "credit": "0", "debit": str(deposit)}
    if withdrawal:
        cats["withdrawal"] = {"count": 1, "credit": str(withdrawal), "debit": "0"}
    FlowAggregate.objects.update_or_create(
        csp_id=code, flow_date=when, resolution_mode=flows.MODE_EXACT,
        defaults={"txn_count": len(cats), "by_category": cats, "credit_amount": withdrawal,
                  "debit_amount": deposit, "recon_status": "RECONCILED"})


def _row(n, code=CSP):
    return DailyEstimate.objects.get(csp_id=code, estimate_date=day(n))


def _drifted_world():
    """Days 1-4 finalised, then day 3 gets a late 5,000 deposit and day 5 a 500 withdrawal."""
    _seed()
    _flow(day(2), deposit=1000)
    estimates.compute_daily_estimates(day(4))
    _flow(day(3), deposit=5000)       # the day was already final
    _flow(day(5), withdrawal=500)
    estimates.compute_daily_estimates(day(6))


# ------------------------------------------------------------------ ordinary late data
def test_backfill_into_a_provisional_day_is_applied_by_the_normal_run(complete, monkeypatch):
    _seed()
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: {day(2), day(4)})
    estimates.compute_daily_estimates(day(4))
    assert not _row(3).is_final and _row(3).estimated_combined == D("300000.00")
    _flow(day(3), deposit=700)
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: {day(2), day(3), day(4)})
    estimates.compute_daily_estimates(day(4))
    assert _row(3).estimated_combined == D("300700.00") and _row(3).is_final
    assert not EstimateCorrection.objects.exists()  # an open day needs no correction


def test_backfill_after_a_final_day_is_not_applied_but_is_visible(complete):
    _drifted_world()
    assert _row(3).is_final and _row(3).estimated_combined == D("301000.00")  # untouched
    assert _row(6).estimated_combined == D("300500.00")  # builds on the stored (old) day 4
    drift = corrections.estimate_drift(today=day(12))
    assert [(i["csp_code"], i["date"]) for i in drift] == [(CSP, day(3))]
    assert drift[0]["current_deposits"] == D("5000.00") and drift[0]["stored_deposits"] == 0
    assert drift[0]["net_difference"] == D("5000.00")
    assert not EstimateCorrection.objects.exists()
    cats = {c["key"]: c for c in review_queue(30, today=day(12))["categories"]}
    assert cats["drift"]["status"] == "open" and cats["drift"]["count"] == 1
    assert "finalised" in cats["drift"]["what"]


def test_drift_is_empty_when_nothing_changed(complete):
    _seed()
    _flow(day(2), deposit=1000)
    estimates.compute_daily_estimates(day(5))
    assert corrections.estimate_drift(today=day(12)) == []


# ------------------------------------------------------------------ authorised correction
def test_an_authorised_correction_recomputes_the_cycle_forward_and_keeps_the_old_values(
    complete, root,
):
    _drifted_world()
    before_outlook = outlook.month_end_outlook("2026-10", today=day(6))["rows"][0]
    untouched = {n: (_row(n).estimated_combined, _row(n).calculated_at) for n in (1, 2)}
    rec = corrections.apply_correction(
        CSP, day(3), reason="Re-exported statement for 3 Oct added a deposit", actor=root)
    assert rec.outcome == "APPLIED" and rec.actor == "root"
    assert rec.reason.startswith("Re-exported") and rec.requested_at is not None
    assert rec.affected_dates == [day(n).isoformat() for n in (3, 4, 5, 6)]
    # the numbers: 300,000 + 1,000 (day 2) + 5,000 (day 3), then - 500 on day 5
    assert [_row(n).estimated_combined for n in (3, 4, 5, 6)] == [
        D("306000.00"), D("306000.00"), D("305500.00"), D("305500.00")]
    assert _row(6).estimated_value == D("1018.33")
    assert all(_row(n).is_final for n in (3, 4, 5))
    # the days before the correction were not touched
    assert {n: (_row(n).estimated_combined, _row(n).calculated_at) for n in (1, 2)} == untouched
    # the old versions are preserved
    old = {s["date"]: s for s in rec.superseded}
    assert old[day(3).isoformat()]["estimated_combined"] == "301000.00"
    assert old[day(6).isoformat()]["estimated_combined"] == "300500.00"
    assert old[day(3).isoformat()]["is_final"] is True
    assert rec.summary["combined_before"] == "300500.00"
    assert rec.summary["combined_after"] == "305500.00"
    # each rewritten row says why
    assert _row(3).details["correction"]["id"] == rec.pk
    assert "correction" not in _row(2).details
    # the month-to-date average and projection follow the stored rows
    after = outlook.month_end_outlook("2026-10", today=day(6))["rows"][0]
    assert after["mtd_average"] > before_outlook["mtd_average"]
    assert after["latest_estimate"] == D("1018.33")
    # drift is gone, and a later normal run builds on the corrected rows
    assert corrections.estimate_drift(today=day(12)) == []
    estimates.compute_daily_estimates(day(7))
    assert _row(7).estimated_combined == D("305500.00")


def test_a_correction_only_touches_that_csp_and_its_current_cycle(complete, root):
    _drifted_world()
    _seed("1A850002")
    _flow(day(2), deposit=1000, code="1A850002")
    estimates.compute_daily_estimates(day(6))
    other = {n: (_row(n, "1A850002").estimated_combined,
                 _row(n, "1A850002").calculated_at) for n in range(1, 7)}
    corrections.apply_correction(CSP, day(3), reason="Reviewed and approved by finance", actor=root)
    assert other == {n: (_row(n, "1A850002").estimated_combined,
                        _row(n, "1A850002").calculated_at) for n in range(1, 7)}


def test_the_baseline_day_and_earlier_cycles_cannot_be_corrected(complete, root):
    _drifted_world()
    for bad in (day(1), d(2026, 9, 20)):
        with pytest.raises(corrections.CorrectionError):
            corrections.apply_correction(CSP, bad, reason="Trying to move the baseline", actor=root)
    assert not EstimateCorrection.objects.exists()


# ------------------------------------------------------------------ authorisation and validation
def test_only_an_active_superuser_may_correct(complete, root):
    _drifted_world()
    staff = get_user_model().objects.create_user("staff", password="x", is_staff=True)
    viewer = get_user_model().objects.create_user("viewer", password="x")
    gone = get_user_model().objects.create_superuser("gone", password="x")
    gone.is_active = False
    gone.save()
    snapshot = list(DailyEstimate.objects.order_by("id").values_list(
        "id", "estimated_combined", "is_final", "calculated_at"))
    for actor in (staff, viewer, gone, None):
        with pytest.raises(PermissionDenied):
            corrections.apply_correction(CSP, day(3), reason="Not allowed to do this", actor=actor)
    assert not EstimateCorrection.objects.exists()
    assert snapshot == list(DailyEstimate.objects.order_by("id").values_list(
        "id", "estimated_combined", "is_final", "calculated_at"))
    assert corrections.is_authorised(root)


def test_a_missing_or_short_reason_and_bad_dates_are_rejected_and_write_nothing(complete, root):
    _drifted_world()
    for reason in ("", "   ", "too short"):
        with pytest.raises(corrections.CorrectionError):
            corrections.apply_correction(CSP, day(3), reason=reason, actor=root)
    with pytest.raises(corrections.CorrectionError):
        corrections.apply_correction(CSP, day(30), reason="A day after the latest", actor=root)
    with pytest.raises(corrections.CorrectionError):
        corrections.apply_correction("NOPE", day(3), reason="An unknown CSP code", actor=root)
    assert not EstimateCorrection.objects.exists()
    assert _row(3).estimated_combined == D("301000.00")


# ------------------------------------------------------------------ idempotency and failure
def test_repeating_a_correction_changes_nothing_the_second_time(complete, root):
    _drifted_world()
    first = corrections.apply_correction(
        CSP, day(3), reason="First reviewed correction", actor=root)
    rows = list(DailyEstimate.objects.order_by("id").values_list(
        "id", "estimated_combined", "calculated_at", "details"))
    again = corrections.apply_correction(CSP, day(3), reason="Second identical request", actor=root)
    assert first.outcome == "APPLIED" and again.outcome == "NO_CHANGE"
    assert again.superseded == [] and again.affected_dates == []
    assert rows == list(DailyEstimate.objects.order_by("id").values_list(
        "id", "estimated_combined", "calculated_at", "details"))
    assert EstimateCorrection.objects.count() == 2  # both attempts are on the record


def test_a_correction_with_no_drift_is_logged_but_changes_nothing(complete, root):
    _seed()
    _flow(day(2), deposit=1000)
    estimates.compute_daily_estimates(day(5))
    rec = corrections.apply_correction(CSP, day(2), reason="Checked: nothing differed", actor=root)
    assert rec.outcome == "NO_CHANGE" and _row(5).estimated_combined == D("301000.00")


def test_a_failed_correction_rolls_back_completely_and_can_be_retried(complete, root, monkeypatch):
    _drifted_world()
    before = list(DailyEstimate.objects.order_by("id").values_list(
        "id", "estimated_combined", "is_final", "calculated_at"))

    def boom(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(estimates, "write_rows", boom)
    with pytest.raises(RuntimeError):
        corrections.apply_correction(CSP, day(3), reason="Interrupted correction run", actor=root)
    assert not EstimateCorrection.objects.exists()  # the audit row rolled back with the rows
    assert before == list(DailyEstimate.objects.order_by("id").values_list(
        "id", "estimated_combined", "is_final", "calculated_at"))
    monkeypatch.undo()
    rec = corrections.apply_correction(CSP, day(3), reason="Retried correction run", actor=root)
    assert rec.outcome == "APPLIED" and EstimateCorrection.objects.count() == 1
    assert DailyEstimate.objects.filter(csp_id=CSP).count() == 6  # no duplicate rows


def test_a_failed_daily_run_leaves_the_chain_intact_and_the_retry_adds_no_duplicates(
    complete, monkeypatch,
):
    _seed()
    _flow(day(2), deposit=1000)
    estimates.compute_daily_estimates(day(3))
    rows_before = list(DailyEstimate.objects.order_by("id").values_list(
        "id", "estimated_combined", "is_final"))
    real = DailyEstimate.objects.bulk_create
    calls = {"n": 0}

    def flaky(objs, **kw):
        calls["n"] += 1
        real(objs, **kw)
        raise RuntimeError("connection reset after the insert")

    monkeypatch.setattr(DailyEstimate.objects, "bulk_create", flaky)
    with pytest.raises(RuntimeError):
        estimates.compute_daily_estimates(day(6))
    monkeypatch.undo()
    assert rows_before == list(DailyEstimate.objects.order_by("id").values_list(
        "id", "estimated_combined", "is_final"))  # the whole write was rolled back
    estimates.compute_daily_estimates(day(6))
    estimates.compute_daily_estimates(day(6))
    assert DailyEstimate.objects.filter(csp_id=CSP).count() == 6
    assert _row(6).estimated_combined == D("301000.00")


def test_importing_the_same_statement_twice_leaves_stored_estimates_and_drift_unchanged(
    tmp_path, complete,
):
    _seed()
    rows = [(desc_deposit("62850709168200095002", CSP), "1500.00", "")]
    f = tmp_path / "a.xls"
    f.write_text(make_statement(rows, txn_date="02/10/2026", start="2 Oct 2026"), encoding="utf-8")
    old = time.time() - 60
    os.utime(f, (old, old))
    run_import(tmp_path)
    estimates.compute_daily_estimates(day(4))
    n_txn = StatementTransaction.objects.count()
    stored = list(DailyEstimate.objects.order_by("id").values_list(
        "id", "estimated_combined", "calculated_at"))
    (tmp_path / "b.xls").write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(tmp_path / "b.xls", (old, old))
    run_import(tmp_path)
    assert StatementTransaction.objects.count() == n_txn
    assert corrections.estimate_drift(today=day(12)) == []
    estimates.compute_daily_estimates(day(4))
    assert stored == list(DailyEstimate.objects.order_by("id").values_list(
        "id", "estimated_combined", "calculated_at"))


# ------------------------------------------------------------------ isolation
def test_a_correction_never_writes_production_tables(complete, root):
    csp = Csp.objects.create(csp_code="9Z999999", name="Other", account_count=300)
    MonthlySummary.objects.create(csp=csp, month="2026-10", days_in_month=31, days_with_data=7,
                                  mtd_mab=D("1000"), projected_mab=D("1000"), slab="S1")
    _drifted_world()
    daily = list(DailyBalance.objects.order_by("id").values_list(
        "id", "daily_avg_balance", "account_count", "source"))
    monthly = list(MonthlySummary.objects.values_list("id", "mtd_mab", "projected_mab", "slab"))
    corrections.apply_correction(CSP, day(3), reason="Isolation check correction", actor=root)
    assert daily == list(DailyBalance.objects.order_by("id").values_list(
        "id", "daily_avg_balance", "account_count", "source"))
    assert monthly == list(MonthlySummary.objects.values_list(
        "id", "mtd_mab", "projected_mab", "slab"))


# ------------------------------------------------------------------ the operator command
def test_the_command_lists_drift_and_enforces_the_actor_rule(complete, root):
    _drifted_world()
    out = StringIO()
    call_command("correct_estimate", "--list-drift", stdout=out)
    text = out.getvalue()
    assert f"{CSP} {day(3)}" in text and f"correct from {day(3)}" in text
    assert not EstimateCorrection.objects.exists()  # listing is read only
    get_user_model().objects.create_user("staff", password="x", is_staff=True)
    args = ["--csp", CSP, "--from-date", day(3).isoformat(), "--reason", "Approved by finance team"]
    with pytest.raises(CommandError):
        call_command("correct_estimate", *args, "--actor", "staff")
    with pytest.raises(CommandError):
        call_command("correct_estimate", *args, "--actor", "nobody")
    with pytest.raises(CommandError):
        call_command("correct_estimate", "--csp", CSP)
    dry = StringIO()
    call_command("correct_estimate", *args, "--actor", "root", "--dry-run", stdout=dry)
    assert "4 day(s) would change" in dry.getvalue()
    assert not EstimateCorrection.objects.exists() and _row(3).estimated_combined == D("301000.00")
    done = StringIO()
    call_command("correct_estimate", *args, "--actor", "root", stdout=done)
    assert "APPLIED" in done.getvalue() and _row(3).estimated_combined == D("306000.00")


def test_there_is_no_web_or_api_route_that_can_correct(client):
    from django.urls import get_resolver

    names = " ".join(str(p.pattern) for p in get_resolver().url_patterns)
    assert "correct" not in names.lower()
    for url in ("/dashboard/correct-estimate/", "/api/v1/bank-flow/correct-estimate"):
        assert client.post(url).status_code in (404, 405, 401, 403, 302)


def test_a_row_inserted_by_the_previous_release_is_valid_during_a_rolling_deploy(db):
    """The previous release's INSERT knows nothing of the new columns; is_final has a database
    default, so it keeps working between `migrate` and the restart of the old worker."""
    from django.db import connection

    Csp.objects.create(csp_code=CSP, name="A", account_count=300)
    with connection.cursor() as cur:
        cur.execute(
            "insert into bankflow_dailyestimate (csp_id, estimate_date, method, status, "
            "reason_code, error_basis, details, calculated_at) values "
            "(%s, '2026-10-01', 'carry_forward', 'INSUFFICIENT_EVIDENCE', 'X', '', '{}'::jsonb, "
            "now())", [CSP])
    row = DailyEstimate.objects.get(csp_id=CSP)
    assert row.is_final is False and row.estimated_combined is None


def test_the_review_page_shows_drift_read_only_with_no_correction_control(complete, client):
    _drifted_world()
    get_user_model().objects.create_user("s", password="x", is_staff=True)
    client.login(username="s", password="x")
    page = client.get("/dashboard/needs-attention/", {"days": 90}).content.decode()
    assert "Finished estimates built on older statement data" in page
    assert "correct_estimate" in page and "Read only here" in page
    assert "<button" not in page.split('id="cat-drift"')[1].split("</details>")[0]
