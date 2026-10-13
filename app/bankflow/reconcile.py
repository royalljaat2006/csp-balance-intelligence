"""
Statement reconciliation. Pure functions over a ParsedStatement -- no I/O.

A statement is RECONCILED only when ALL of these hold:
  * every row parsed (no malformed rows),
  * opening balance - debits + credits reproduces EVERY running balance and
    the final row balance (Decimal arithmetic, exact),
  * the header Book Balance equals the last row's running balance.

If the rows are internally consistent but the header Book Balance differs from
the last row balance, the statement is PARTIAL -- never "reconciled" -- and the
gap is recorded. (Observed in the 2026-10-12 sample: rows consistent, header
Book Balance higher than the last row by roughly INR 22 lakh; cause unknown.)

FAILED statements are quarantined by the ingest layer; their rows are not used.
"""

from __future__ import annotations

import decimal
from collections import defaultdict
from dataclasses import dataclass, field

from .parser import ParsedStatement

Decimal = decimal.Decimal
RECONCILED = "RECONCILED"
PARTIAL = "PARTIAL"
FAILED = "FAILED"


@dataclass
class ReconResult:
    status: str
    opening_balance: Decimal | None
    computed_closing: Decimal | None
    last_row_balance: Decimal | None
    header_book_balance: Decimal | None
    header_gap: Decimal | None
    total_debit: Decimal
    total_credit: Decimal
    rows_parsed: int
    malformed_rows: int
    running_balance_mismatches: int
    first_mismatch_row: int | None
    opening_date_matches_start: bool | None
    available_equals_book_minus_hold: bool | None
    duplicate_ref_groups: int = 0
    reversal_candidates: int = 0
    notes: list[str] = field(default_factory=list)


def reconcile(
    parsed: ParsedStatement,
    *,
    ref_by_row: dict[int, str] | None = None,
    reversal_count: int = 0,
    export_date=None,
) -> ReconResult:
    notes: list[str] = []
    rows = parsed.rows
    total_debit = sum((r.debit for r in rows), Decimal("0"))
    total_credit = sum((r.credit for r in rows), Decimal("0"))

    opening = parsed.opening_balance
    mismatches = 0
    first_mismatch: int | None = None
    computed_closing: Decimal | None = None

    if opening is None:
        notes.append("Opening balance missing from header: running balance cannot be verified.")
    else:
        running = opening
        for r in rows:
            running = running - r.debit + r.credit
            if running != r.balance:
                mismatches += 1
                if first_mismatch is None:
                    first_mismatch = r.index
                running = r.balance  # resync so one gap is not counted on every later row
        computed_closing = opening - total_debit + total_credit

    last_balance = rows[-1].balance if rows else None
    if rows and opening is not None and computed_closing != last_balance:
        notes.append("Opening - debits + credits does not equal the last row balance.")

    header_gap: Decimal | None = None
    if parsed.book_balance is not None and last_balance is not None:
        header_gap = parsed.book_balance - last_balance

    opening_matches: bool | None = None
    if parsed.opening_date and parsed.start_date:
        opening_matches = parsed.opening_date == parsed.start_date

    avail_ok: bool | None = None
    if (
        parsed.book_balance is not None
        and parsed.available_balance is not None
        and parsed.hold_value is not None
    ):
        avail_ok = parsed.book_balance - parsed.hold_value == parsed.available_balance

    dup_groups = 0
    if ref_by_row:
        seen: dict[tuple[str, str], int] = defaultdict(int)
        side_by_idx = {r.index: ("credit" if r.credit else "debit") for r in rows}
        for idx, ref in ref_by_row.items():
            if ref:
                seen[(ref, side_by_idx.get(idx, ""))] += 1
        dup_groups = sum(1 for v in seen.values() if v > 1)

    if not rows:
        status = FAILED
        notes.append("No transaction rows parsed.")
    elif parsed.malformed:
        status = FAILED
        notes.append(f"{len(parsed.malformed)} malformed row(s).")
    elif opening is None:
        status = FAILED
    elif mismatches:
        status = FAILED
        notes.append(f"{mismatches} running-balance mismatch(es); first at row {first_mismatch}.")
    elif computed_closing != last_balance:
        status = FAILED
    elif header_gap is None or header_gap != 0:
        status = PARTIAL
        if header_gap is not None:
            notes.append(
                f"Header Book Balance differs from last row balance by {header_gap} -- "
                "rows are internally consistent but completeness vs the bank's own total "
                "is NOT established."
            )
            if (
                export_date is not None
                and parsed.end_date is not None
                and export_date > parsed.end_date
            ):
                notes.append(
                    f"The file was exported on {export_date}, after the statement's End Date "
                    f"{parsed.end_date}. The header Book Balance is a point-in-time snapshot at "
                    "export, so a difference is expected here and is NOT evidence of missing "
                    "rows. The closing balance stays unconfirmed until the next statement's "
                    "opening balance matches it."
                )
        else:
            notes.append("Header Book Balance not available: completeness not established.")
    else:
        status = RECONCILED

    if opening_matches is False:
        notes.append("Opening-balance date differs from statement Start Date.")
    if avail_ok is False:
        notes.append("Available Balance != Book Balance - Hold Value.")
    if not parsed.footer_seen:
        notes.append("Statement footer not seen: file may be truncated.")
        if status == RECONCILED:
            status = PARTIAL

    return ReconResult(
        status=status,
        opening_balance=opening,
        computed_closing=computed_closing,
        last_row_balance=last_balance,
        header_book_balance=parsed.book_balance,
        header_gap=header_gap,
        total_debit=total_debit,
        total_credit=total_credit,
        rows_parsed=len(rows),
        malformed_rows=len(parsed.malformed),
        running_balance_mismatches=mismatches,
        first_mismatch_row=first_mismatch,
        opening_date_matches_start=opening_matches,
        available_equals_book_minus_hold=avail_ok,
        duplicate_ref_groups=dup_groups,
        reversal_candidates=reversal_count,
        notes=notes,
    )
