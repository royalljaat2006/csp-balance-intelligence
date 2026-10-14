"""
manage.py watch_transactions — the `worker` service's long-running process
(PRD §7.2 Option A, Phase 7). Despite the name, it's grown into the one
scheduler for everything the worker container runs on a timer — one
process, one place startup/scheduling behaviour lives, rather than a
second worker process per job.

Four independent schedules, each tracked with its own last-run timestamp
so a fast one is never starved by a slow one sharing this loop:

  - ingest_transactions   — every --interval seconds (default 5 min). The
    transaction file is dropped once a day (PRD §7.2), so sub-second
    reaction time buys nothing here.
  - ingest_calling_sheet  — every --calling-sheet-interval seconds
    (default 60s). Eko's RMs edit the CALLING SHEET throughout the day, so
    this one deliberately runs far more often than the others — a CSP's
    balance on the dashboard is only as fresh as the last poll. One poll
    is a single small Sheets API read (well under Google's quota even at
    this cadence) plus an upsert keyed on today's date, so re-running it
    every minute just updates today's row in place, never creates
    duplicates.
  - sync_daily_snapshots  — every --snapshot-interval seconds (default 5
    min, same cadence as ingest_transactions). Rebuilds today's
    DailyCspSnapshot (MTD-so-far slab) off whatever DailyBalance rows
    ingest_calling_sheet has landed so far — csp/comparison.py's
    build_daily_snapshots_bulk() does this in a handful of queries for
    every CSP, not one per CSP, so running it this often across 539+ CSPs
    is cheap. Always for today's date; historical-date backfill is a
    separate, not-yet-built concern.
  - run_autopilot         — every --autopilot-interval seconds (default
    daily). Nemotron insights/priority-calls/anomaly-review/draft-nudges;
    degrades to a no-op if NEMOTRON_API_KEY isn't configured (see
    autopilot/nemotron_client.py), so enabling/disabling it needs no code
    change here — just the env var.

The loop wakes up every min(interval, calling_sheet_interval,
snapshot_interval, autopilot_interval) seconds and runs whichever
schedules are due.
"""

from __future__ import annotations

import datetime as dt
import os
import time

import structlog
from common import metrics
from common.cache import release_lock, try_acquire_lock
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

logger = structlog.get_logger("ingestion")

# Crash safety net only — the lock is normally released right after the job
# finishes (see _run_locked_job), not held for this long. This just bounds
# how long a *crashed* replica's stale lock can block every other replica
# from running that job.
_LOCK_TTL_SECONDS = 600

_DEFAULT_JOBS = (
    "ingest_transactions",
    "ingest_calling_sheet",
    "sync_daily_snapshots",
    "run_autopilot",
)
# Bank-flow / estimate pipeline. OPT-IN: runs only when named in --jobs / WATCH_JOBS (the
# worker-bankflow service), so a plain `worker` behaves exactly as before. In a pass they run in
# this order: statements -> re-attribution -> PMJDY -> 06:00 snapshot -> estimator verdict ->
# estimates.
_BANKFLOW_JOBS = (
    "ingest_bank_statements",
    "build_bank_flows",
    "ingest_pmjdy",
    "snapshot_calling_sheet_baseline",
    "evaluate_account_count_hypothesis",
    "compute_daily_estimates",
)
_ALL_JOBS = _DEFAULT_JOBS + _BANKFLOW_JOBS


def snapshot_due(now: dt.datetime, at: str, last_run_date: dt.date | None) -> bool:
    """The once-a-day snapshot is due at/after `at` (HH:MM, same timezone as `now`) if it has not
    already run on `now`'s date. A worker down at the target time still takes it on return."""
    hh, mm = (int(x) for x in at.split(":"))
    return now.time() >= dt.time(hh, mm) and last_run_date != now.date()


class Command(BaseCommand):
    help = (
        "Poll transactions, CALLING SHEET edits, daily snapshots and autopilot — each on "
        "its own schedule."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=300,
            help="Seconds between ingest_transactions polls (default: 300 = 5 minutes).",
        )
        parser.add_argument(
            "--calling-sheet-interval",
            type=int,
            default=60,
            help="Seconds between ingest_calling_sheet polls (default: 60 = near-live).",
        )
        parser.add_argument(
            "--snapshot-interval",
            type=int,
            default=300,
            help="Seconds between sync_daily_snapshots passes (default: 300 = 5 minutes).",
        )
        parser.add_argument(
            "--autopilot-interval",
            type=int,
            default=86400,
            help="Seconds between run_autopilot passes (default: 86400 = daily).",
        )
        for flag, setting, label in (
            ("--statement-interval", "BANKFLOW_STATEMENT_INTERVAL_SECONDS", "statement import"),
            ("--build-flows-interval", "BANKFLOW_BUILD_FLOWS_INTERVAL_SECONDS", "re-attribution"),
            ("--pmjdy-interval", "PMJDY_INTERVAL_SECONDS", "ingest_pmjdy"),
            ("--hypothesis-interval", "HYPOTHESIS_INTERVAL_SECONDS", "estimator verdict"),
            ("--estimate-interval", "ESTIMATE_INTERVAL_SECONDS", "compute_daily_estimates"),
        ):
            parser.add_argument(
                flag, type=int, default=getattr(settings, setting),
                help=f"Seconds between {label} passes (bank-flow worker; default {setting}).",
            )
        parser.add_argument(
            "--snapshot-at", default=settings.BASELINE_SNAPSHOT_AT,
            help="HH:MM IST for the once-a-day snapshot_calling_sheet_baseline (default 06:00).",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run every schedule once and exit (used by tests / manual runs), regardless "
            "of their individual intervals.",
        )
        parser.add_argument(
            "--jobs",
            type=str,
            default="",
            help=(
                "Comma-separated subset of {{{}}} for this instance to run — the rest are "
                "skipped entirely (their interval never ticks, no lock contention). Empty "
                "(default) runs the original four, exactly as before this flag existed; the "
                "bank-flow jobs run only when named here. Lets compose.yml run "
                "separately-scalable worker roles from the same image/command — see "
                "docs/DEPLOYMENT.md's worker topology.".format(", ".join(_ALL_JOBS))
            ),
        )

    def _run_locked_job(self, job_name: str, **job_kwargs) -> None:
        """Cross-replica mutual exclusion for one scheduled tick — see
        common/cache.py's try_acquire_lock docstring for the fail-open/
        idempotent-jobs reasoning. Lock is released immediately after the
        job finishes (success or failure), not left to expire on its own;
        _LOCK_TTL_SECONDS only protects against a replica crashing mid-job."""
        if not try_acquire_lock(f"watch:{job_name}", ttl=_LOCK_TTL_SECONDS):
            logger.info("skipped_locked", stage="SCHEDULER", job=job_name)
            metrics.record_job_run(job=job_name, outcome="skipped_locked")
            return
        try:
            call_command(job_name, **job_kwargs)
            metrics.record_job_run(job=job_name, outcome="ok")
        except Exception:  # noqa: BLE001 — a bad file/run must not kill the worker loop
            logger.exception("poll_failed", stage="SCHEDULER", job=job_name)
            metrics.record_job_run(job=job_name, outcome="failed")
        finally:
            release_lock(f"watch:{job_name}")

    def _run_statement_import(self) -> None:
        """Read-only import of the other team's statement folder. If the folder is not mounted /
        reachable, say so and skip -- never fail the whole worker pass for a missing input."""
        path = settings.BANKFLOW_WATCH_DIR
        if not os.path.isdir(path):
            logger.warning("skipped_no_input", stage="SCHEDULER", job="ingest_bank_statements",
                           path=path)
            metrics.record_job_run(job="ingest_bank_statements", outcome="skipped_no_input")
            return
        self._run_locked_job(
            "ingest_bank_statements", path=path, recursive=True,
            pattern=settings.BANKFLOW_STATEMENT_PATTERN, max_file_mb=settings.BANKFLOW_MAX_FILE_MB,
        )

    def handle(self, *args, **options):
        interval = options["interval"]
        calling_sheet_interval = options["calling_sheet_interval"]
        snapshot_interval = options["snapshot_interval"]
        autopilot_interval = options["autopilot_interval"]
        run_once = options["once"]
        jobs_arg = options["jobs"].strip()
        active_jobs = (
            {j.strip() for j in jobs_arg.split(",") if j.strip()}
            if jobs_arg
            else set(_DEFAULT_JOBS)
        )
        unknown = active_jobs - set(_ALL_JOBS)
        if unknown:
            raise CommandError(
                f"--jobs: unknown job(s) {sorted(unknown)}; expected a subset of {_ALL_JOBS}"
            )
        tick = min(
            interval, calling_sheet_interval, snapshot_interval, autopilot_interval,
        )  # <= 60 s by default; also what the 06:00 time-of-day check needs
        logger.info(
            "started",
            stage="SCHEDULER",
            interval_seconds=interval,
            calling_sheet_interval_seconds=calling_sheet_interval,
            snapshot_interval_seconds=snapshot_interval,
            autopilot_interval_seconds=autopilot_interval,
            once=run_once,
            active_jobs=sorted(active_jobs),
        )
        self.stdout.write(
            f"watch_transactions starting (interval={interval}s, "
            f"calling_sheet_interval={calling_sheet_interval}s, "
            f"snapshot_interval={snapshot_interval}s, "
            f"once={run_once}, "
            f"jobs={sorted(active_jobs)})"
        )

        last_txn_run = 0.0
        last_calling_sheet_run = 0.0
        last_snapshot_run = 0.0
        last_autopilot_run = 0.0
        last_bf = dict.fromkeys(_BANKFLOW_JOBS, 0.0)
        bf_interval = {
            "ingest_bank_statements": options["statement_interval"],
            "build_bank_flows": options["build_flows_interval"],
            "ingest_pmjdy": options["pmjdy_interval"],
            "evaluate_account_count_hypothesis": options["hypothesis_interval"],
            "compute_daily_estimates": options["estimate_interval"],
        }
        last_snapshot_date: dt.date | None = None
        while True:
            now = time.time()

            if "ingest_transactions" in active_jobs and (
                run_once or now - last_txn_run >= interval
            ):
                self._run_locked_job("ingest_transactions")
                last_txn_run = time.time()

            if "ingest_calling_sheet" in active_jobs and (
                run_once or now - last_calling_sheet_run >= calling_sheet_interval
            ):
                self._run_locked_job("ingest_calling_sheet")
                last_calling_sheet_run = time.time()

            if "sync_daily_snapshots" in active_jobs and (
                run_once or now - last_snapshot_run >= snapshot_interval
            ):
                self._run_locked_job("sync_daily_snapshots")
                last_snapshot_run = time.time()

            if "run_autopilot" in active_jobs and (
                run_once or now - last_autopilot_run >= autopilot_interval
            ):
                self._run_locked_job("run_autopilot")
                last_autopilot_run = time.time()

            now = time.time()
            for job in _BANKFLOW_JOBS:
                if job not in active_jobs:
                    continue
                if job == "snapshot_calling_sheet_baseline":
                    local_now = timezone.localtime()
                    due = snapshot_due(local_now, options["snapshot_at"], last_snapshot_date)
                    if run_once or due:
                        self._run_locked_job(job, expected_at=options["snapshot_at"])
                        last_snapshot_date = local_now.date()
                    continue
                if not (run_once or now - last_bf[job] >= bf_interval[job]):
                    continue
                if job == "ingest_bank_statements":
                    self._run_statement_import()
                else:
                    self._run_locked_job(job)
                last_bf[job] = time.time()

            if run_once:
                logger.info("stopped", stage="SCHEDULER", reason="once")
                return
            time.sleep(tick)
