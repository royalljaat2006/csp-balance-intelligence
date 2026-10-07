"""
Parses an already-validated transaction workbook and upserts allow-listed
rows into csp.models.Transaction (PRD §7.2, FR2).

Bulk-oriented on purpose: a real monthly export is ~300k rows (confirmed
against the sample file) — one query per row would be ~2 round-trips each,
so this does a single parse pass in Python, then two bulk DB operations
(ensure CSPs exist, upsert transactions) regardless of file size.

CSP resolution: if `CSP Code New` isn't in `csp.models.Csp` yet (the CALLING
SHEET ingester hasn't run, or hasn't seen this CSP), a minimal stub Csp row
is created (just the code) rather than dropping the transaction — the
CALLING SHEET ingester fills in the rest (name, mobile, ...) whenever it
next runs. `new_csp_codes` reports which codes were stubbed this way, per
PRD's "unmatched CSP codes reported."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import openpyxl
from common.cache import set_job_progress
from csp.models import Csp, Transaction
from django.db import transaction as db_transaction

from .xlsx_validation import (
    classify_transaction_type,
    direction_for_category,
    parse_amount,
    parse_txn_date,
    parse_txn_datetime,
)

BATCH_SIZE = 1000
# How often (in rows) to write a progress update during a long parse pass.
# Every row would just add Redis round-trips without the live view polling
# anywhere near that fast (pipeline_sse.py reads it at most once per ~2s).
PROGRESS_REPORT_EVERY = 2000

_TRANSACTION_UPDATE_FIELDS = [
    "csp_id",
    "txn_datetime",
    "txn_date",
    "txn_type",
    "category",
    "direction",
    "from_account",
    "to_account",
    "amount",
    "source_file",
]


@dataclass
class IngestResult:
    rows_read: int = 0
    rows_valid: int = 0
    rows_rejected: int = 0  # in-scope type, but a required field was malformed/missing
    rows_upserted: int = 0
    new_csp_codes: set[str] = field(default_factory=set)


def ingest_workbook(
    file_path: Path, header: list[str], *, progress_key: str | None = None
) -> IngestResult:
    """`header` is the already-validated column list (validate_required_columns
    has already run against it) — avoids re-reading row 1 here.

    `progress_key`, when given, reports real row-by-row progress to
    common.cache.set_job_progress every PROGRESS_REPORT_EVERY rows — the
    live pipeline view's only source of real mid-ingestion progress (see
    dashboard/pipeline_state.py). `total` is sheet.max_row (minus the
    header row), the file's own real dimensions, never an estimate. Only
    used here: it's the one ingestion path where a single file can run
    tens of thousands of rows and "running, no further detail for several
    minutes" would otherwise be the honest-but-unhelpful alternative."""
    result = IngestResult()
    col = {name: header.index(name) for name in header}

    pending: list[Transaction] = []
    csp_codes: set[str] = set()

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    try:
        sheet = wb["Success"]
        total_rows = max((sheet.max_row or 1) - 1, 0)
        for i, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=1):
            if progress_key and i % PROGRESS_REPORT_EVERY == 0:
                set_job_progress(progress_key, processed=i, total=total_rows)
            txn_type_raw = row[col["Type of Transaction"]]
            if txn_type_raw is None:
                continue
            result.rows_read += 1

            category = classify_transaction_type(str(txn_type_raw))
            if category is None:
                continue
            result.rows_valid += 1

            ref_number = str(row[col["Reference Number"]] or "").strip()
            csp_code = str(row[col["CSP Code New"]] or "").strip()
            amount = parse_amount(row[col["Amount"]])
            txn_dt = parse_txn_datetime(row[col["Transaction Date & Time"]])
            txn_date = parse_txn_date(row[col["New Date"]])

            if (
                not ref_number
                or not csp_code
                or amount is None
                or txn_dt is None
                or txn_date is None
            ):
                result.rows_rejected += 1
                continue

            csp_codes.add(csp_code)
            pending.append(
                Transaction(
                    ref_number=ref_number,
                    csp_id=csp_code,
                    txn_datetime=txn_dt,
                    txn_date=txn_date,
                    txn_type=str(txn_type_raw).strip(),
                    category=category,
                    direction=direction_for_category(category),
                    from_account=str(row[col["From Account"]] or ""),
                    to_account=str(row[col["To Account"]] or ""),
                    amount=amount,
                    source_file=file_path.name,
                )
            )
    finally:
        wb.close()

    if not pending:
        return result

    with db_transaction.atomic():
        existing_qs = Csp.objects.filter(csp_code__in=csp_codes).values_list("csp_code", flat=True)
        existing = set(existing_qs)
        new_codes = csp_codes - existing
        if new_codes:
            # status="historical_only" keeps a transaction-only stub (no
            # Calling Sheet presence yet) out of "CSPs tracked" (see
            # csp/services.get_overview). ingest_calling_sheet clears this
            # the moment a live poll actually sees the CSP.
            Csp.objects.bulk_create(
                [Csp(csp_code=code, status="historical_only") for code in sorted(new_codes)],
                ignore_conflicts=True,
            )
            result.new_csp_codes = new_codes

        Transaction.objects.bulk_create(
            pending,
            update_conflicts=True,
            unique_fields=["ref_number"],
            update_fields=_TRANSACTION_UPDATE_FIELDS,
            batch_size=BATCH_SIZE,
        )
    result.rows_upserted = len(pending)
    return result
