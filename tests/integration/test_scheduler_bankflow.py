"""The bank-flow / estimate pipeline runs on its own, in order, without touching the original five
jobs: hourly PMJDY + estimates, a once-a-day 06:00 IST snapshot, daily re-attribution/verdict."""

import datetime as dt
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bankflow.models import (
    BaselineSnapshot,
    DailyEstimate,
    HypothesisRun,
    PmjdyMonthly,
    StatementFile,
)
from csp.models import Csp, DailyBalance, IngestLog
from django.core.management import call_command
from django.utils import timezone
from ingestion.management.commands.watch_transactions import snapshot_due

from tests.bankflow_fixtures import desc_withdrawal, make_statement
from tests.unit.test_pmjdy_parse import make_grid

pytestmark = pytest.mark.django_db
SCHED = "ingestion.management.commands.watch_transactions.call_command"
PIPELINE = ["ingest_bank_statements", "build_bank_flows", "ingest_pmjdy",
            "snapshot_calling_sheet_baseline", "evaluate_account_count_hypothesis",
            "compute_daily_estimates"]
TODAY = timezone.localdate()


# ---------------------------------------------------------------- wiring
def test_a_plain_worker_never_runs_the_bankflow_jobs():
    with patch(SCHED) as mock_call:
        call_command("watch_transactions", once=True)
    assert not set(PIPELINE) & {c.args[0] for c in mock_call.call_args_list}


def test_the_pipeline_runs_in_dependency_order_with_the_statement_folder_read_only(
        settings, tmp_path):
    settings.BANKFLOW_WATCH_DIR = str(tmp_path)
    with patch(SCHED) as mock_call:
        call_command("watch_transactions", once=True, jobs=",".join(reversed(PIPELINE)))
    # the order is fixed by dependency, not by how the jobs were listed
    assert [c.args[0] for c in mock_call.call_args_list] == PIPELINE
    stmt = mock_call.call_args_list[0].kwargs
    assert stmt == {"path": str(tmp_path), "recursive": True, "pattern": "statement_1556_*",
                    "max_file_mb": 100}
    snap = next(c for c in mock_call.call_args_list if c.args[0] == PIPELINE[3])
    assert snap.kwargs == {"expected_at": "06:00"}


def test_a_missing_statement_folder_is_skipped_and_the_rest_still_runs(settings):
    settings.BANKFLOW_WATCH_DIR = "/definitely/not/mounted"
    with patch(SCHED) as mock_call:
        call_command("watch_transactions", once=True, jobs=",".join(PIPELINE))
    assert [c.args[0] for c in mock_call.call_args_list] == PIPELINE[1:]


def test_one_failing_job_does_not_stop_the_pass(settings, tmp_path):
    settings.BANKFLOW_WATCH_DIR = str(tmp_path)
    with patch(SCHED) as mock_call:
        mock_call.side_effect = [None, RuntimeError("sheets down"), None, None, None, None][:6]
        call_command("watch_transactions", once=True, jobs=",".join(PIPELINE))
    assert [c.args[0] for c in mock_call.call_args_list] == PIPELINE


def test_unknown_jobs_are_still_rejected():
    with pytest.raises(Exception, match="unknown job"):
        call_command("watch_transactions", once=True, jobs="ingest_pmjdy,rm_rf")


# ---------------------------------------------------------------- the agreed cadence
def test_defaults_are_hourly_for_pmjdy_and_estimates_and_06_00_for_the_snapshot(settings):
    assert settings.PMJDY_INTERVAL_SECONDS == 3600
    assert settings.ESTIMATE_INTERVAL_SECONDS == 3600
    assert settings.BASELINE_SNAPSHOT_AT == "06:00"
    assert settings.BANKFLOW_STATEMENT_INTERVAL_SECONDS == 3600
    # the PMJDY reader's own throttle must be shorter than its schedule, or hourly ticks get skipped
    assert settings.PMJDY_MIN_INTERVAL_SECONDS < settings.PMJDY_INTERVAL_SECONDS


@pytest.mark.parametrize("hour, minute, last, due", [
    (5, 59, None, False),          # before 06:00 IST
    (6, 0, None, True),            # at 06:00
    (9, 30, None, True),           # worker was down at 06:00: takes it when it comes back
    (6, 5, dt.date(2026, 10, 14), False),   # already taken today
    (0, 10, dt.date(2026, 10, 13), False),  # a new day, but not 06:00 yet
    (6, 0, dt.date(2026, 10, 13), True),    # next morning
])
def test_snapshot_is_due_once_per_day_from_06_00(hour, minute, last, due):
    now = dt.datetime(2026, 10, 14, hour, minute)
    assert snapshot_due(now, "06:00", last) is due


# ---------------------------------------------------------------- end to end, real commands
class _Sheets:
    def __init__(self, grid):
        self.grid = grid

    def service_account(self, filename):
        grid = self.grid

        class _Client:
            def set_timeout(self, t):
                pass

            def open_by_key(self, key):
                return SimpleNamespace(worksheet=lambda name: SimpleNamespace(
                    get_all_values=lambda: grid))

        return _Client()


def test_one_pass_takes_real_inputs_to_stored_estimates(settings, tmp_path, monkeypatch):
    """No mocks of our own code: statement file -> flows, sheet block -> PMJDY, 06:00 snapshot,
    estimator verdict, daily estimates, and a log row for every step."""
    for code in ("1A852455", "1A850295"):
        Csp.objects.create(csp_code=code, name=code, account_count=300)
        DailyBalance.objects.create(csp_id=code, balance_date=TODAY, daily_avg_balance=4000,
                                    account_count=300, source="calling_sheet")
    stmt_dir = tmp_path / "2026-10-12"
    stmt_dir.mkdir()
    f = stmt_dir / "statement_1556_12-10-2026.xls"
    f.write_text(make_statement([
        (desc_withdrawal("62850709168200095256", "1A852455"), "", "4000.00"),
        (desc_withdrawal("62850720372300012013", "1A8502957"), "", "1000.00"),  # -> 1A850295
    ]), encoding="utf-8")
    old = time.time() - 600
    os.utime(f, (old, old))

    mon, yr = TODAY.strftime("%B"), TODAY.strftime("%y")
    grid = make_grid([(f"MTD Achievement {mon}'{yr}", ["PMJDY"])],
                     {"1A852455": [[9]], "1A850295": [[4]]})
    settings.GOOGLE_SERVICE_ACCOUNT_FILE, settings.CALLING_SHEET_ID = "k.json", "sheet"
    settings.BANKFLOW_WATCH_DIR = str(tmp_path)
    monkeypatch.setattr("bankflow.management.commands.ingest_pmjdy.gspread", _Sheets(grid))

    call_command("watch_transactions", once=True, jobs=",".join(PIPELINE))

    assert StatementFile.objects.count() == 1
    assert dict(PmjdyMonthly.objects.values_list("csp_id", "mtd")) == {"1A852455": 9, "1A850295": 4}
    assert BaselineSnapshot.objects.filter(snapshot_date=TODAY).count() == 2
    assert HypothesisRun.objects.count() == 1  # verdict recorded (insufficient history, honestly)
    assert HypothesisRun.objects.get().status == "INSUFFICIENT_EVIDENCE"
    assert DailyEstimate.objects.filter(estimate_date=TODAY).count() == 2
    assert {e.status for e in DailyEstimate.objects.all()} == {"INSUFFICIENT_EVIDENCE"}
    sources = set(IngestLog.objects.values_list("source", flat=True))
    assert {"pmjdy", "baseline_snapshot"} <= sources
    assert not IngestLog.objects.filter(source__in=["pmjdy", "baseline_snapshot"],
                                        status="failed").exists()

    # a second pass the same hour changes nothing: every step is idempotent
    before = (PmjdyMonthly.objects.count(), BaselineSnapshot.objects.count(),
              StatementFile.objects.count(), DailyEstimate.objects.count())
    call_command("watch_transactions", once=True, jobs=",".join(PIPELINE))
    assert before == (PmjdyMonthly.objects.count(), BaselineSnapshot.objects.count(),
                      StatementFile.objects.count(), DailyEstimate.objects.count())
