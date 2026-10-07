"""
manage.py ingest_calling_sheet — PRD §8 FR1.

Pulls CSP master data + today's average balance from the CALLING SHEET
(ingestion/calling_sheet_ingest.py does the parsing) and upserts:
  - csp.models.Csp — name/email/mobile/account_count. A blank cell means
    "not reported today," not "cleared" — it never overwrites a
    previously-known value, only a fresh one does.
  - csp.models.DailyBalance — one row for today (IST), source=calling_sheet,
    only for CSPs that have a parseable Avg Balance this run.

Fails loudly (CommandError, no partial state) if credentials are missing,
per Phase 4's "access must fail clearly if credentials are missing."
"""

from __future__ import annotations

import time

import gspread
import structlog
from common.cache import bump_generation
from csp.models import Csp, DailyBalance, IngestLog
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ingestion.calling_sheet_ingest import CallingSheetValidationError, parse_sheet_values

logger = structlog.get_logger("ingestion")

_CSP_UPDATE_FIELDS = [
    "name",
    "email",
    "mobile",
    "account_count",
    "raw_attrs",
    "last_seen_date",
    "status",
]
_SHEETS_TIMEOUT_SECONDS = 30
# Read-only fetch, safe to retry: bounded so a permanently-broken sheet
# (bad credentials, deleted sheet) still fails within a few seconds rather
# than hanging the calling-sheet worker for its whole interval.
_SHEETS_FETCH_RETRIES = 2
_SHEETS_FETCH_BACKOFF_SECONDS = 2


class Command(BaseCommand):
    help = "Pull CSP master data + daily average balance from the CALLING SHEET."

    def handle(self, *args, **options):
        missing = [
            name
            for name in ("GOOGLE_SERVICE_ACCOUNT_FILE", "CALLING_SHEET_ID")
            if not getattr(settings, name)
        ]
        if missing:
            logger.error("credentials_missing", stage="AUTH", missing=missing)
            raise CommandError(
                f"Cannot run ingest_calling_sheet: {', '.join(missing)} not configured. "
                "Set them in .env (see .env.example) before scheduling this command."
            )

        log = IngestLog.objects.create(source="calling_sheet", status=IngestLog.Status.FAILED)
        job = logger.bind(job_id=log.id)
        job.info("started", stage="INGESTION")

        try:
            all_values = self._fetch_sheet_values(job)
        except Exception as exc:  # noqa: BLE001 — any Sheets API failure is a rejection, not a crash
            job.error("fetch_failed", stage="INGESTION", error_type=type(exc).__name__)
            self._fail(log, f"Could not read the CALLING SHEET: {type(exc).__name__}: {exc}")
            return

        try:
            result = parse_sheet_values(all_values)
        except CallingSheetValidationError as exc:
            job.error("validation_failed", stage="VALIDATION", reason=str(exc))
            self._fail(log, str(exc))
            return

        job.info(
            "completed",
            stage="VALIDATION",
            rows_read=result.rows_read,
            rows_skipped=result.rows_skipped,
        )

        today = timezone.localdate()  # PRD §13: Asia/Kolkata
        existing = {
            c.csp_code: c
            for c in Csp.objects.filter(csp_code__in=[r.csp_code for r in result.rows])
        }

        csp_objs = []
        balance_objs = []
        for row in result.rows:
            prev = existing.get(row.csp_code)
            csp_objs.append(
                Csp(
                    csp_code=row.csp_code,
                    # Appearing in a live poll means this CSP is genuinely
                    # active again — clears any "historical_only" stub flag
                    # (see ingestion/transaction_ingest.py,
                    # sync_monthly_summary.py) so it's correctly counted in
                    # "CSPs tracked" (csp/services.get_overview) from here on.
                    status="",
                    name=row.name or (prev.name if prev else ""),
                    email=row.email or (prev.email if prev else ""),
                    mobile=row.mobile or (prev.mobile if prev else ""),
                    account_count=(
                        row.account_count
                        if row.account_count is not None
                        else (prev.account_count if prev else None)
                    ),
                    raw_attrs={
                        "sheet_total_balance": (
                            str(row.total_balance) if row.total_balance is not None else None
                        ),
                        "sheet_amount_to_deposit": (
                            str(row.amount_to_deposit_reported)
                            if row.amount_to_deposit_reported is not None
                            else None
                        ),
                        # Only set when the cell had more than one number —
                        # see clean_mobile()'s docstring.
                        "sheet_mobile_raw": (
                            row.mobile_raw if row.mobile_raw != row.mobile else None
                        ),
                    },
                    last_seen_date=today,
                )
            )
            if row.avg_balance is not None:
                balance_objs.append(
                    DailyBalance(
                        csp_id=row.csp_code,
                        balance_date=today,
                        daily_avg_balance=row.avg_balance,
                        account_count=row.account_count,
                        source=DailyBalance.Source.CALLING_SHEET,
                    )
                )

        Csp.objects.bulk_create(
            csp_objs,
            update_conflicts=True,
            unique_fields=["csp_code"],
            update_fields=_CSP_UPDATE_FIELDS,
            batch_size=1000,
        )
        DailyBalance.objects.bulk_create(
            balance_objs,
            update_conflicts=True,
            unique_fields=["csp", "balance_date"],
            update_fields=["daily_avg_balance", "account_count", "source"],
            batch_size=1000,
        )

        log.status = IngestLog.Status.SUCCESS
        log.rows_read = result.rows_read
        log.rows_valid = len(result.rows)
        log.rows_rejected = result.rows_skipped
        log.rows_upserted = len(csp_objs)
        log.finished_at = timezone.now()
        no_balance = len(csp_objs) - len(balance_objs)
        if no_balance:
            log.error_summary = (
                f"{no_balance} CSP(s) had a blank Avg Balance (no accounts/data yet) — "
                "master record updated, no balance row written for today."
            )
        log.save(
            update_fields=[
                "status",
                "rows_read",
                "rows_valid",
                "rows_rejected",
                "rows_upserted",
                "finished_at",
                "error_summary",
            ]
        )

        job.info("upserted", stage="DATABASE", csps=len(csp_objs), balances=len(balance_objs))
        # New balances/account counts just landed — invalidate the cached
        # dashboard rollups (Overview, Trends) immediately rather than
        # waiting out their TTL. See common/cache.py.
        bump_generation()
        job.info("finished", stage="INGESTION", status=log.status)
        self.stdout.write(
            self.style.SUCCESS(
                f"Synced {len(csp_objs)} CSPs, {len(balance_objs)} balance rows for {today}."
            )
        )

    def _fetch_sheet_values(self, job) -> list[list[str]]:
        """Bounded timeout + bounded retry (fast-pass, 2026-09-21) — a
        read-only fetch, safe to retry. Was previously unbounded (gspread's
        default HTTPClient sets no timeout), so one hung Sheets API call
        could block this worker's calling-sheet schedule indefinitely."""
        last_exc: Exception | None = None
        for attempt in range(1, _SHEETS_FETCH_RETRIES + 2):
            try:
                gc = gspread.service_account(filename=settings.GOOGLE_SERVICE_ACCOUNT_FILE)
                gc.set_timeout(_SHEETS_TIMEOUT_SECONDS)
                sheet = gc.open_by_key(settings.CALLING_SHEET_ID)
                worksheet = sheet.worksheet(settings.CALLING_SHEET_WORKSHEET_NAME)
                return worksheet.get_all_values()
            except Exception as exc:  # noqa: BLE001 — retried generically, then re-raised as-is
                last_exc = exc
                if attempt <= _SHEETS_FETCH_RETRIES:
                    job.warning(
                        "fetch_retry",
                        stage="INGESTION",
                        attempt=attempt,
                        error_type=type(exc).__name__,
                    )
                    time.sleep(_SHEETS_FETCH_BACKOFF_SECONDS * attempt)
        # loop always runs >=1 time and sets this on any failure path above
        assert last_exc is not None  # noqa: S101
        raise last_exc

    def _fail(self, log: IngestLog, message: str) -> None:
        log.error_summary = message
        log.finished_at = timezone.now()
        log.save(update_fields=["error_summary", "finished_at"])
        self.stderr.write(message)
