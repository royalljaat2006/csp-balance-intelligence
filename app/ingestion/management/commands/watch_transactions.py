"""
manage.py watch_transactions — the `worker` service's long-running process
(PRD §7.2 Option A, Phase 7). Despite the name, it's grown into the one
scheduler for everything the worker container runs on a timer — one
process, one place startup/scheduling behaviour lives, rather than a
second worker process per job.

Five independent schedules, each tracked with its own last-run timestamp
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
  - poll_telegram         — every --telegram-interval seconds (default 60,
    same cadence as ingest_calling_sheet). Non-blocking per poll (see
    telegram_client.get_updates's default timeout=0) so it can't stall the
    other schedules sharing this loop; degrades to a no-op if
    TELEGRAM_BOT_TOKEN isn't configured (same pattern as run_autopilot
    below), so enabling/disabling it needs no code change here either.
  - run_autopilot         — every --autopilot-interval seconds (default
    daily). Nemotron insights/priority-calls/anomaly-review/draft-nudges;
    degrades to a no-op if NEMOTRON_API_KEY isn't configured (see
    autopilot/nemotron_client.py), so enabling/disabling it needs no code
    change here — just the env var.

The loop wakes up every min(interval, calling_sheet_interval,
snapshot_interval, telegram_interval, autopilot_interval) seconds and runs
whichever schedules are due.
"""

from __future__ import annotations

import time

import structlog
from common import metrics
from common.cache import release_lock, try_acquire_lock
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

logger = structlog.get_logger("ingestion")

# Crash safety net only — the lock is normally released right after the job
# finishes (see _run_locked_job), not held for this long. This just bounds
# how long a *crashed* replica's stale lock can block every other replica
# from running that job.
_LOCK_TTL_SECONDS = 600

_ALL_JOBS = (
    "ingest_transactions",
    "ingest_calling_sheet",
    "sync_daily_snapshots",
    "poll_telegram",
    "run_autopilot",
)


class Command(BaseCommand):
    help = (
        "Poll transactions, CALLING SHEET edits, daily snapshots, Telegram, and "
        "autopilot — each on its own schedule."
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
            "--telegram-interval",
            type=int,
            default=60,
            help="Seconds between poll_telegram passes (default: 60 = near-live).",
        )
        parser.add_argument(
            "--autopilot-interval",
            type=int,
            default=86400,
            help="Seconds between run_autopilot passes (default: 86400 = daily).",
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
                "(default) runs all five, exactly as before this flag existed. Lets compose.yml "
                "run separately-scalable worker roles from the same image/command — see "
                "docs/DEPLOYMENT.md's worker topology.".format(", ".join(_ALL_JOBS))
            ),
        )

    def _run_locked_job(self, job_name: str) -> None:
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
            call_command(job_name)
            metrics.record_job_run(job=job_name, outcome="ok")
        except Exception:  # noqa: BLE001 — a bad file/run must not kill the worker loop
            logger.exception("poll_failed", stage="SCHEDULER", job=job_name)
            metrics.record_job_run(job=job_name, outcome="failed")
        finally:
            release_lock(f"watch:{job_name}")

    def handle(self, *args, **options):
        interval = options["interval"]
        calling_sheet_interval = options["calling_sheet_interval"]
        snapshot_interval = options["snapshot_interval"]
        telegram_interval = options["telegram_interval"]
        autopilot_interval = options["autopilot_interval"]
        run_once = options["once"]
        jobs_arg = options["jobs"].strip()
        active_jobs = (
            {j.strip() for j in jobs_arg.split(",") if j.strip()} if jobs_arg else set(_ALL_JOBS)
        )
        unknown = active_jobs - set(_ALL_JOBS)
        if unknown:
            raise CommandError(
                f"--jobs: unknown job(s) {sorted(unknown)}; expected a subset of {_ALL_JOBS}"
            )
        tick = min(
            interval, calling_sheet_interval, snapshot_interval, telegram_interval,
            autopilot_interval,
        )
        logger.info(
            "started",
            stage="SCHEDULER",
            interval_seconds=interval,
            calling_sheet_interval_seconds=calling_sheet_interval,
            snapshot_interval_seconds=snapshot_interval,
            telegram_interval_seconds=telegram_interval,
            autopilot_interval_seconds=autopilot_interval,
            once=run_once,
            active_jobs=sorted(active_jobs),
        )
        self.stdout.write(
            f"watch_transactions starting (interval={interval}s, "
            f"calling_sheet_interval={calling_sheet_interval}s, "
            f"snapshot_interval={snapshot_interval}s, "
            f"telegram_interval={telegram_interval}s, once={run_once}, "
            f"jobs={sorted(active_jobs)})"
        )

        last_txn_run = 0.0
        last_calling_sheet_run = 0.0
        last_snapshot_run = 0.0
        last_telegram_run = 0.0
        last_autopilot_run = 0.0
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

            if "poll_telegram" in active_jobs and (
                run_once or now - last_telegram_run >= telegram_interval
            ):
                self._run_locked_job("poll_telegram")
                last_telegram_run = time.time()

            if "run_autopilot" in active_jobs and (
                run_once or now - last_autopilot_run >= autopilot_interval
            ):
                self._run_locked_job("run_autopilot")
                last_autopilot_run = time.time()

            if run_once:
                logger.info("stopped", stage="SCHEDULER", reason="once")
                return
            time.sleep(tick)
