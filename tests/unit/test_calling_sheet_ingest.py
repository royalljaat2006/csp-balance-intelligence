"""
Calling-sheet parsing — column-name matching + Indian-currency/count parsing.
Sample values below are taken verbatim from the real sheet's format
(confirmed 2026-09-13), not invented.
"""

import decimal

import pytest
from ingestion.calling_sheet_ingest import (
    CallingSheetValidationError,
    clean_mobile,
    find_columns,
    parse_currency,
    parse_int,
    parse_sheet_values,
)

D = decimal.Decimal

REAL_HEADER = (
    [
        "CSP ID",
        "CSP Name",
        "Gender",
        "CSP Mail ID",
        "CSP Mobile number",
    ]
    + [f"col{i}" for i in range(5, 74)]
    + [
        "Total Accounts",
        "Total Balance",
        "Avg Balance",
        "Amount to be deposited (for eligiblity/higher slab)",
    ]
)


def _sheet(rows: list[list[str]]) -> list[list[str]]:
    blank = [""] * len(REAL_HEADER)
    return [blank, REAL_HEADER, *rows]


def _row(
    csp_id="1A850247",
    name="Abhimanyu Kumar Singh",
    email="abhimanyuksingh94@gmail.com",
    mobile="9973531951",
    accounts="981",
    total_balance="₹ 34,00,000",
    avg_balance="₹ 3,466",
    amount_deposit="₹ 5,30,000",
):
    row = [""] * len(REAL_HEADER)
    row[0], row[1], row[3], row[4] = csp_id, name, email, mobile
    row[-4], row[-3], row[-2], row[-1] = accounts, total_balance, avg_balance, amount_deposit
    return row


def test_find_columns_locates_by_name():
    cols = find_columns(REAL_HEADER)
    assert cols["CSP ID"] == 0
    assert cols["Avg Balance"] == len(REAL_HEADER) - 2
    assert cols["Amount to be deposited (for eligiblity/higher slab)"] == len(REAL_HEADER) - 1


def test_find_columns_missing_required_raises():
    with pytest.raises(CallingSheetValidationError, match="Total Accounts"):
        find_columns([h for h in REAL_HEADER if h != "Total Accounts"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("₹ 34,00,000", D("3400000")),
        ("₹ 3,466", D("3466")),
        ("₹ 1,86,00,000", D("18600000")),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_parse_currency(raw, expected):
    assert parse_currency(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"), [("981", 981), ("1,781", 1781), ("", None), (None, None)]
)
def test_parse_int(raw, expected):
    assert parse_int(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9973531951", "9973531951"),
        (" 99550 73559", "9955073559"),
        ("9905985993/ 7563993807", "9905985993"),  # real row: two numbers, one cell
        ("7080591551, 8528067923/ 8707427390", "7080591551"),  # real row: three numbers
        ("", ""),
        (None, ""),
    ],
)
def test_clean_mobile(raw, expected):
    assert clean_mobile(raw) == expected


def test_parse_sheet_values_happy_path():
    sheet = _sheet([_row()])
    result = parse_sheet_values(sheet)
    assert result.rows_read == 1
    assert result.rows_skipped == 0
    row = result.rows[0]
    assert row.csp_code == "1A850247"
    assert row.account_count == 981
    assert row.total_balance == D("3400000")
    assert row.avg_balance == D("3466")
    assert row.amount_to_deposit_reported == D("530000")


def test_parse_sheet_values_skips_blank_csp_id():
    sheet = _sheet([_row(csp_id="")])
    result = parse_sheet_values(sheet)
    assert result.rows_read == 0
    assert result.rows_skipped == 1


def test_parse_sheet_values_keeps_row_with_blank_balance_fields():
    # A CSP with an ID but no accounts/balance yet -- real sheet has 52 of these.
    sheet = _sheet([_row(accounts="", total_balance="", avg_balance="", amount_deposit="")])
    result = parse_sheet_values(sheet)
    assert result.rows_read == 1
    row = result.rows[0]
    assert row.account_count is None
    assert row.avg_balance is None


def test_parse_sheet_values_missing_column_raises():
    bad_header = [h for h in REAL_HEADER if h != "Avg Balance"]
    sheet = [[""] * len(bad_header), bad_header, [""] * len(bad_header)]
    with pytest.raises(CallingSheetValidationError):
        parse_sheet_values(sheet)
