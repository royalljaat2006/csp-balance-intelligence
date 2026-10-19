"""
manage.py reconcile_account_totals -- READ ONLY. Why does the dashboard's total of customer
accounts differ from the sum of "Total Accounts" in the Calling Sheet?

Reads the sheet once (the same worksheet the ingest reads) and compares it with what the system
holds. Nothing is written: no IngestLog, no balances, no cache change. It lists, with CSP codes and
counts only (no names, mobiles or balances):

  * the sheet column sum, and how much of it the ingest cannot use (rows whose CSP ID is not
    1A85 + 4 digits, rows with a count but no CSP ID);
  * every CSP whose sheet count differs from the count the dashboard uses, and why: a blank cell
    (the ingest keeps the previous count), a CSP the system does not track, a CSP missing from the
    sheet, or a change not yet ingested.

This command changes no ingest rule; it only shows which rows explain a difference.
"""

from __future__ import annotations

from csp.models import CSP_CODE_RE, Csp
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ingestion.calling_sheet_ingest import (
    DATA_START_ROW,
    HEADER_ROW,
    find_columns,
    parse_int,
)

_TIMEOUT = 30
SHOW = 25


def read_sheet(values: list[list[str]]) -> dict:
    """Classify every data row of the sheet by what the ingest would do with it."""
    cols = find_columns(values[HEADER_ROW - 1])
    need = max(cols.values())
    valid: dict[str, int | None] = {}
    invalid: list[tuple[str, int | None]] = []
    no_code: list[int] = []
    short_rows = 0
    for raw in values[DATA_START_ROW - 1:]:
        if len(raw) <= need:
            short_rows += 1
            continue
        code = raw[cols["CSP ID"]].strip()
        n = parse_int(raw[cols["Total Accounts"]])
        if not code:
            if n:
                no_code.append(n)
        elif not CSP_CODE_RE.match(code):
            invalid.append((code, n))
        else:
            valid[code] = n
    return {"valid": valid, "invalid": invalid, "no_code": no_code, "short_rows": short_rows}


def compare(sheet: dict, system: dict[str, int], tracked: set[str]) -> dict:
    """Per-CSP differences between the sheet and the counts the dashboard uses."""
    diffs = []
    for code, n in sorted(sheet["valid"].items()):
        have = system.get(code)
        if code not in tracked:
            diffs.append({"code": code, "sheet": n, "system": have,
                          "why": "not a tracked CSP in the system (not counted)"})
        elif n is None:
            diffs.append({"code": code, "sheet": None, "system": have,
                          "why": "the sheet cell is blank; the ingest keeps the previous count"})
        elif have != n:
            diffs.append({"code": code, "sheet": n, "system": have,
                          "why": "the system has a different count (not ingested yet?)"})
    for code in sorted(set(system) - set(sheet["valid"])):
        diffs.append({"code": code, "sheet": None, "system": system[code],
                      "why": "in the system but not in the sheet now; the old count is kept"})
    return {"differences": diffs}


class Command(BaseCommand):
    help = "Read only: explain differences between the sheet's Total Accounts and the dashboard."

    def handle(self, *args, **opts):
        import gspread
        from bankflow import intelligence

        missing = [n for n in ("GOOGLE_SERVICE_ACCOUNT_FILE", "CALLING_SHEET_ID")
                   if not getattr(settings, n)]
        if missing:
            raise CommandError(f"Cannot read the sheet: {', '.join(missing)} not configured.")
        gc = gspread.service_account(filename=settings.GOOGLE_SERVICE_ACCOUNT_FILE)
        gc.set_timeout(_TIMEOUT)
        values = (gc.open_by_key(settings.CALLING_SHEET_ID)
                  .worksheet(settings.CALLING_SHEET_WORKSHEET_NAME).get_all_values())
        sheet = read_sheet(values)

        today = timezone.localdate()
        tracked = set(Csp.objects.tracked().values_list("csp_code", flat=True))
        system = intelligence._accounts_as_of(today, tracked)
        out = self.stdout.write

        sheet_valid = sum(n or 0 for n in sheet["valid"].values())
        sheet_invalid = sum(n or 0 for _c, n in sheet["invalid"])
        sheet_nocode = sum(sheet["no_code"])
        sheet_all = sheet_valid + sheet_invalid + sheet_nocode
        system_total = sum(system.values())
        res = compare(sheet, system, tracked)
        counted = sum(n or 0 for c, n in sheet["valid"].items() if c in tracked)

        out(f"Sheet column sum (every row):                 {sheet_all:>10,}")
        out(f"  rows with a valid CSP ID:                   {sheet_valid:>10,}")
        out(f"  rows with an invalid CSP ID (ignored):      {sheet_invalid:>10,}  "
            f"({len(sheet['invalid'])} rows)")
        out(f"  rows with a count but no CSP ID (ignored):  {sheet_nocode:>10,}  "
            f"({len(sheet['no_code'])} rows)")
        out(f"Dashboard total of customer accounts today:   {system_total:>10,}")
        out(f"Sheet total over tracked CSPs:                {counted:>10,}")
        out(f"Gap, sheet column sum minus dashboard:        {sheet_all - system_total:>+10,}")
        if sheet["invalid"]:
            out("Rows with an invalid CSP ID (code shown as typed, count):")
            for code, n in sheet["invalid"][:SHOW]:
                out(f"  {code!r}: {n if n is not None else 'blank'}")
        if res["differences"]:
            out(f"CSPs whose count differs ({len(res['differences'])}; first {SHOW}):")
            for d in res["differences"][:SHOW]:
                out(f"  {d['code']}: sheet {d['sheet'] if d['sheet'] is not None else 'blank'}"
                    f", system {d['system'] if d['system'] is not None else 'none'}"
                    f" -- {d['why']}")
        else:
            out("Every valid CSP's count in the sheet equals the count the dashboard uses.")
        out("Nothing was changed.")
