"""
Transaction-file validation helpers — PRD §7.2, §8 FR2, and Phase 4 ingestion
safety rules.

These functions encode facts already decided and documented in PRD.md (the
Success-sheet-only / 7-type allow-list, required columns) — nothing here is
a new business rule. They are pure and Django-independent on purpose, so
they're testable without a database or settings (see tests/unit/).
"""

from __future__ import annotations

import datetime as dt
import decimal
from dataclasses import dataclass
from pathlib import Path

# Confirmed against the real sample file via openpyxl (cells are text-
# formatted, not native Excel date/number types) — see the September 2026
# verification run before this module was written.
_DATETIME_FORMAT = "%d-%m-%Y %I:%M:%S %p"  # "01-08-2026 08:05:23 AM"
_DATE_FORMAT = "%d-%m-%Y"  # "01-08-2026"

ALLOWED_EXTENSIONS = {".xlsx"}
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB — a monthly export this size is already anomalous

REQUIRED_SHEET_NAME = "Success"

REQUIRED_COLUMNS = [
    "KO ID",
    "Transaction Date & Time",
    "Reference Number",
    "Type of Transaction",
    "From Account",
    "To Account",
    "Amount",
    "Status",
    "New Date",
    "CSP Code New",
]

# PRD §7.2 — the exact 7-type allow-list, by category. Everything else
# (OFFUS, Money Transfer, IMPS, BBPS, Initial/Loan deposit, ...) is dropped.
# This full 7-type set is "Overall" mode (confirmed 2026-09-18) — not "every
# type in the source file," the already-approved allow-list as a whole.
TRANSACTION_TYPE_CATEGORY: dict[str, str] = {
    "AEPS ONUS Withdrawal": "withdrawal",
    "ATM Onus Withdrawal": "withdrawal",
    "Withdrawal": "withdrawal",
    "YONO WITHDRAWAL": "withdrawal",
    "AEPS ONUS Deposit": "deposit",
    "Deposit": "deposit",
    "AEPS ONUS Fund Transfer": "fund_transfer",
}

# "ONUS" mode — the narrower on-us-channel subset *within* the approved
# allow-list above (confirmed 2026-09-18): exactly the 4 types with
# "ONUS"/"Onus" literally in the name. Mirrored in
# dbt/models/staging/stg_transaction.sql's is_onus column — keep both in
# sync if this set ever changes, same convention as macros/rule_19.sql
# already mirroring csp/rules.py.
ONUS_TXN_TYPES: frozenset[str] = frozenset(
    {
        "AEPS ONUS Withdrawal",
        "ATM Onus Withdrawal",
        "AEPS ONUS Deposit",
        "AEPS ONUS Fund Transfer",
    }
)


def is_onus_transaction_type(txn_type: str) -> bool:
    """The one canonical answer to "is this an ONUS transaction" — every
    ONUS/Overall split in the app (ingestion, comparison engine, dashboard)
    must call this, never re-derive the type list independently."""
    return txn_type.strip() in ONUS_TXN_TYPES

# Which category implies which direction relative to the pool/settlement
# account (PRD §7.2). Fund transfers touch neither side of the pool.
CATEGORY_DIRECTION: dict[str, str] = {
    "withdrawal": "in_pool",
    "deposit": "out_pool",
    "fund_transfer": "other",
}


class IngestionValidationError(ValueError):
    """Raised for any rejected file/row — always carries a human-readable reason."""


def validate_file_extension(path: Path) -> None:
    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise IngestionValidationError(
            f"Rejected {path.name}: extension {path.suffix!r} not in {sorted(ALLOWED_EXTENSIONS)}"
        )


def validate_file_size(path: Path, max_bytes: int = MAX_FILE_SIZE_BYTES) -> None:
    size = path.stat().st_size
    if size == 0:
        raise IngestionValidationError(f"Rejected {path.name}: file is empty")
    if size > max_bytes:
        raise IngestionValidationError(
            f"Rejected {path.name}: {size} bytes exceeds the {max_bytes} byte limit"
        )


def validate_required_sheet(sheet_names: list[str]) -> None:
    if REQUIRED_SHEET_NAME not in sheet_names:
        raise IngestionValidationError(
            f"Rejected: sheet {REQUIRED_SHEET_NAME!r} not found (got {sheet_names})"
        )


def validate_required_columns(columns: list[str]) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise IngestionValidationError(f"Rejected: missing required column(s) {missing}")


def classify_transaction_type(txn_type: str) -> str | None:
    """Returns the category for an allow-listed type, or None to drop the row."""
    return TRANSACTION_TYPE_CATEGORY.get(txn_type.strip())


def direction_for_category(category: str) -> str:
    return CATEGORY_DIRECTION.get(category, "other")


def parse_txn_datetime(raw: object) -> dt.datetime | None:
    """Parses the 'Transaction Date & Time' cell. Returns None (never raises)
    on anything unparseable — the caller counts that as a rejected row."""
    if isinstance(raw, dt.datetime):
        return raw.replace(tzinfo=dt.UTC) if raw.tzinfo is None else raw
    if not isinstance(raw, str):
        return None
    try:
        return dt.datetime.strptime(raw.strip(), _DATETIME_FORMAT).replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def parse_txn_date(raw: object) -> dt.date | None:
    """Parses the 'New Date' cell. Returns None (never raises) on anything
    unparseable."""
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return dt.datetime.strptime(raw.strip(), _DATE_FORMAT).date()
    except ValueError:
        return None


def parse_amount(raw: object) -> decimal.Decimal | None:
    """Parses the 'Amount' cell (observed as plain text, e.g. '2000').
    Returns None (never raises) on anything unparseable or non-positive."""
    if raw is None:
        return None
    try:
        value = decimal.Decimal(str(raw).strip().replace(",", ""))
    except (decimal.InvalidOperation, ValueError):
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class FileValidationResult:
    ok: bool
    reason: str = ""


def validate_file_basics(
    path: Path, *, max_bytes: int = MAX_FILE_SIZE_BYTES
) -> FileValidationResult:
    """Convenience wrapper: extension + size, without raising. Sheet/column
    checks need the parsed workbook and are validated separately once it's
    open (validate_required_sheet / validate_required_columns)."""
    try:
        validate_file_extension(path)
        validate_file_size(path, max_bytes)
    except IngestionValidationError as exc:
        return FileValidationResult(ok=False, reason=str(exc))
    return FileValidationResult(ok=True)
