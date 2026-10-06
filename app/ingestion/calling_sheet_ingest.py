"""
Parses the CALLING SHEET's "Calling Sheet New" worksheet — PRD §7.1, FR1.

Confirmed live against the real sheet (2026-09-13): row 1 is merged section
labels, row 2 has the actual column headers, data starts row 3. We match
required columns **by header text** (stripped), not fixed index — the sheet
has 320+ columns (calling logs, monthly targets, ...) that shift over time;
matching by name survives that as long as these 8 headers don't change.

Currency/count cells are Indian-formatted text, e.g. "₹ 34,00,000", "1,781"
— parsed defensively; a cell that doesn't parse is treated as absent, never
as zero (a CSP with no data yet is not the same as a CSP with ₹0 balance).
"""

from __future__ import annotations

import decimal
import re
from dataclasses import dataclass

HEADER_ROW = 2
DATA_START_ROW = 3
DEFAULT_WORKSHEET_NAME = "Calling Sheet New"

REQUIRED_COLUMNS = [
    "CSP ID",
    "CSP Name",
    "CSP Mail ID",
    "CSP Mobile number",
    "Total Accounts",
    "Total Balance",
    "Avg Balance",
    "Amount to be deposited (for eligiblity/higher slab)",
]

_CURRENCY_STRIP_RE = re.compile(r"[^\d.]")
_WHITESPACE_RE = re.compile(r"\s+")
_MOBILE_DIGITS_RE = re.compile(r"\d{10}")


class CallingSheetValidationError(ValueError):
    """Raised when the header row doesn't contain every required column."""


def find_columns(header_row: list[str]) -> dict[str, int]:
    normalized = [h.strip() for h in header_row]
    index_by_name: dict[str, int] = {}
    missing = []
    for name in REQUIRED_COLUMNS:
        try:
            index_by_name[name] = normalized.index(name)
        except ValueError:
            missing.append(name)
    if missing:
        raise CallingSheetValidationError(f"Required column(s) not found in header row: {missing}")
    return index_by_name


def parse_currency(raw: str | None) -> decimal.Decimal | None:
    """'₹ 34,00,000' -> Decimal('3400000'). Blank/unparseable -> None (not 0)."""
    if not raw or not raw.strip():
        return None
    cleaned = _CURRENCY_STRIP_RE.sub("", raw)
    if not cleaned:
        return None
    try:
        return decimal.Decimal(cleaned)
    except decimal.InvalidOperation:
        return None


def parse_int(raw: str | None) -> int | None:
    """'1,781' -> 1781. Blank/unparseable -> None (not 0)."""
    if not raw or not raw.strip():
        return None
    cleaned = raw.replace(",", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        return None


def clean_mobile(raw: str | None) -> str:
    """Extracts the first 10-digit mobile number in the cell. A handful of
    real rows contain more than one number separated by '/' or ',' (e.g.
    '9905985993/ 7563993807') — this takes the first; the untouched cell
    text is kept in Csp.raw_attrs['sheet_mobile_raw'] so nothing is lost.
    Falls back to a whitespace-stripped, length-capped copy of the cell if
    no clean 10-digit run is found, rather than raising on garbage input."""
    if not raw:
        return ""
    match = _MOBILE_DIGITS_RE.search(raw)
    if match:
        return match.group(0)
    return _WHITESPACE_RE.sub("", raw)[:20]


@dataclass(frozen=True)
class CallingSheetRow:
    csp_code: str
    name: str
    email: str
    mobile: str
    mobile_raw: str
    account_count: int | None
    total_balance: decimal.Decimal | None
    avg_balance: decimal.Decimal | None
    amount_to_deposit_reported: decimal.Decimal | None


@dataclass
class ParseResult:
    rows: list[CallingSheetRow]
    rows_read: int  # data rows with a non-blank CSP ID
    rows_skipped: int  # data rows with a blank/missing CSP ID


def parse_sheet_values(all_values: list[list[str]]) -> ParseResult:
    """`all_values` is `Worksheet.get_all_values()` — a list of rows, each a
    list of cell strings, 1:1 with the sheet's actual rows/columns."""
    if len(all_values) < HEADER_ROW:
        raise CallingSheetValidationError(f"Sheet has fewer than {HEADER_ROW} rows")

    cols = find_columns(all_values[HEADER_ROW - 1])
    max_col = max(cols.values())

    rows: list[CallingSheetRow] = []
    skipped = 0
    for raw in all_values[DATA_START_ROW - 1 :]:
        if len(raw) <= max_col:
            skipped += 1
            continue
        csp_code = raw[cols["CSP ID"]].strip()
        if not csp_code:
            skipped += 1
            continue
        mobile_cell = raw[cols["CSP Mobile number"]]
        rows.append(
            CallingSheetRow(
                csp_code=csp_code,
                name=raw[cols["CSP Name"]].strip(),
                email=raw[cols["CSP Mail ID"]].strip(),
                mobile=clean_mobile(mobile_cell),
                mobile_raw=mobile_cell.strip(),
                account_count=parse_int(raw[cols["Total Accounts"]]),
                total_balance=parse_currency(raw[cols["Total Balance"]]),
                avg_balance=parse_currency(raw[cols["Avg Balance"]]),
                amount_to_deposit_reported=parse_currency(
                    raw[cols["Amount to be deposited (for eligiblity/higher slab)"]]
                ),
            )
        )
    return ParseResult(rows=rows, rows_read=len(rows), rows_skipped=skipped)
