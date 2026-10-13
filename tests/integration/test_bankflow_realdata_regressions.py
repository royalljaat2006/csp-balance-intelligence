"""
Regression tests for defects exposed by REAL statements (the 308,651-row August
2026 export and its spreadsheet-re-saved copy), which the earlier synthetic
fixtures did not cover.
"""

import datetime as dt
import decimal
import os
import time

import pytest
from bankflow import flows
from bankflow.ingest import run_import
from bankflow.models import StatementFile, StatementTransaction
from bankflow.parser import parse_money, parse_statement
from csp.models import Csp

from tests.bankflow_fixtures import (
    desc_deposit,
    desc_withdrawal,
    make_statement,
    resave_like_spreadsheet,
    sample_rows,
)

D = decimal.Decimal
pytestmark = pytest.mark.django_db


@pytest.fixture
def csps(db):
    for code in ("1A852455", "1A850295"):
        Csp.objects.create(csp_code=code, name=code, account_count=250)


def _write(tmp_path, name, text, age=60):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    os.utime(p, (time.time() - age,) * 2)
    return p


def test_money_is_always_two_decimal_places():
    assert str(parse_money("1000")) == "1000.00"
    assert str(parse_money('"8,05,743.26"')) == "805743.26"
    assert parse_money("1000") == parse_money("1000.00")


def test_resaved_spreadsheet_copy_parses_like_the_original():
    original = make_statement(sample_rows(), opening="805743.26", start="01 Aug 2026")
    resaved = resave_like_spreadsheet(original)
    assert '"8,05,743.26"' in resaved and "01-Aug-26" in resaved  # the real-file quirks
    a, b = parse_statement(original.encode()), parse_statement(resaved.encode())
    assert (b.opening_balance, b.start_date, b.end_date) == (
        D("805743.26"),
        dt.date(2026, 8, 1),
        dt.date(2026, 8, 1),
    )
    assert [(r.debit, r.credit, r.balance) for r in a.rows] == [
        (r.debit, r.credit, r.balance) for r in b.rows
    ]
    assert not b.malformed


def test_resaved_copy_of_an_imported_statement_adds_no_rows_and_no_conflicts(tmp_path, csps):
    original = make_statement(sample_rows(), opening="805743.26")
    _write(tmp_path, "export.xls", original)
    run_import(tmp_path)
    n = StatementTransaction.objects.count()

    _write(tmp_path, "export (1).xls", resave_like_spreadsheet(original))
    run = run_import(tmp_path)
    assert run.files_imported == 1 and run.files_quarantined == 0
    assert StatementTransaction.objects.count() == n  # same ledger rows, not duplicated
    second = StatementFile.objects.get(filename="export (1).xls")
    assert second.reconciliation.overlap_rows == len(sample_rows())
    assert second.reconciliation.conflicting_rows == 0


def _three_day_statement():
    rows = [
        (desc_withdrawal("62850709168200000001", "1A852455"), "", "100.00", "01/08/2026"),
        (desc_deposit("62850709168200000002", "1A852455"), "40.00", "", "02/08/2026"),
        (desc_withdrawal("62850709168200000003", "1A852455"), "", "70.00", "03/08/2026"),
    ]
    return make_statement(rows, opening="1000.00", start="01 Aug 2026", end="03 Aug 2026")


def test_multi_day_statement_proves_every_day_but_its_last(tmp_path, csps):
    _write(tmp_path, "month.xls", _three_day_statement())
    run_import(tmp_path)
    sf = StatementFile.objects.get()
    assert (sf.first_row_date, sf.last_row_date) == (dt.date(2026, 8, 1), dt.date(2026, 8, 3))
    # 1 and 2 Aug are chained to later rows inside one verified ledger; 3 Aug (the export
    # boundary) has no proof that nothing posted after the export.
    assert flows.proven_complete_dates() == {dt.date(2026, 8, 1), dt.date(2026, 8, 2)}


def test_multi_day_statement_last_day_is_proven_by_the_next_statements_opening(tmp_path, csps):
    _write(tmp_path, "month.xls", _three_day_statement())
    closing = D("1000.00") + D("100.00") - D("40.00") + D("70.00")
    nxt = make_statement(
        [(desc_withdrawal("62850709168200000009", "1A852455"), "", "5.00")],
        opening=str(closing),
        start="04 Aug 2026",
        txn_date="04/08/2026",
    )
    _write(tmp_path, "next.xls", nxt)
    run_import(tmp_path)
    assert dt.date(2026, 8, 3) in flows.proven_complete_dates()


def test_export_after_statement_end_explains_a_header_gap_without_excusing_it(tmp_path, csps):
    text = make_statement(sample_rows(), opening="1000.00", book="999999.00", start="01 Aug 2026")
    _write(tmp_path, "aug.xls", text)  # file timestamp is "now": far after 1 Aug
    run_import(tmp_path)
    rec = StatementFile.objects.get().reconciliation
    assert rec.status == "PARTIAL"  # still NOT reconciled
    assert any("point-in-time snapshot" in n for n in rec.notes)
    assert any("unconfirmed until the next statement" in n for n in rec.notes)


def test_per_file_size_limit_is_configurable_for_full_month_statements(tmp_path, csps):
    _write(tmp_path, "m.xls", _three_day_statement())
    refused = run_import(tmp_path, max_file_mb=None)
    assert refused.files_imported == 1  # default 50 MB limit: a small file passes
    StatementFile.objects.all().delete()
    from bankflow import ingest

    ingest.MAX_FILE_BYTES, old = 10, ingest.MAX_FILE_BYTES
    try:
        assert run_import(tmp_path).files_failed == 1
        StatementFile.objects.all().delete()
        assert (
            run_import(tmp_path, max_file_mb=75).files_imported == 1
        )  # explicit operator override
    finally:
        ingest.MAX_FILE_BYTES = old


def test_spreadsheet_quote_wrapping_does_not_create_duplicate_rows(tmp_path, csps):
    # A description containing a quote character is re-saved wrapped in quotes -- the real
    # August copy had 36 such rows, which were double-counted before this was handled.
    quoted = (
        'BY TRANSFER-62850709168200077777AEPSWDL TXN@KO1A852455-TRANSFER FROM 111 Mr. "Bunty" X-',
        "",
        "500.00",
    )
    original = make_statement([*sample_rows(), quoted], opening="1000.00")
    resaved = resave_like_spreadsheet(original).replace(
        quoted[0], '"' + quoted[0] + '"'
    )  # spreadsheet-style wrapping
    a, b = parse_statement(original.encode()), parse_statement(resaved.encode())
    assert a.rows[-1].description == b.rows[-1].description

    _write(tmp_path, "export.xls", original)
    run_import(tmp_path)
    n = StatementTransaction.objects.count()
    _write(tmp_path, "export (1).xls", resaved)
    run_import(tmp_path)
    assert StatementTransaction.objects.count() == n


def test_coverage_counts_every_day_inside_a_multi_day_statement(tmp_path, csps):
    from bankflow import services

    _write(tmp_path, "month.xls", _three_day_statement())
    run_import(tmp_path)
    rep = services.coverage_report()
    assert rep["days_covered"] == 3 and rep["missing_date_ranges"] == []
    assert rep["proven_complete_days"] == 2
