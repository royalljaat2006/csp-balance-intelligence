"""
Pure unit tests for the ingestion validation helpers — no DB, no Django
settings needed beyond import. Encodes the PRD §7.2 7-type allow-list.
"""

import datetime as dt
import decimal

import pytest
from ingestion.xlsx_validation import (
    IngestionValidationError,
    classify_transaction_type,
    direction_for_category,
    parse_amount,
    parse_txn_date,
    parse_txn_datetime,
    validate_file_basics,
    validate_file_extension,
    validate_file_size,
    validate_required_columns,
    validate_required_sheet,
)


def test_validate_file_extension_accepts_xlsx(tmp_path):
    f = tmp_path / "Transaction Sep'26 (1).xlsx"
    f.write_bytes(b"x")
    validate_file_extension(f)  # does not raise


@pytest.mark.parametrize("suffix", [".csv", ".xls", ".exe", ".xlsm"])
def test_validate_file_extension_rejects_others(tmp_path, suffix):
    f = tmp_path / f"file{suffix}"
    f.write_bytes(b"x")
    with pytest.raises(IngestionValidationError):
        validate_file_extension(f)


def test_validate_file_size_rejects_empty(tmp_path):
    f = tmp_path / "empty.xlsx"
    f.write_bytes(b"")
    with pytest.raises(IngestionValidationError):
        validate_file_size(f)


def test_validate_file_size_rejects_oversized(tmp_path):
    f = tmp_path / "huge.xlsx"
    f.write_bytes(b"x" * 100)
    with pytest.raises(IngestionValidationError):
        validate_file_size(f, max_bytes=10)


def test_validate_file_basics_reports_reason_without_raising(tmp_path):
    f = tmp_path / "bad.csv"
    f.write_bytes(b"x")
    result = validate_file_basics(f)
    assert result.ok is False
    assert "bad.csv" in result.reason


def test_validate_required_sheet_missing():
    with pytest.raises(IngestionValidationError):
        validate_required_sheet(["GTV", "Failed"])


def test_validate_required_sheet_present():
    validate_required_sheet(["GTV", "Success", "Failed"])  # does not raise


def test_validate_required_columns_missing():
    with pytest.raises(IngestionValidationError):
        validate_required_columns(["KO ID", "Amount"])


def test_validate_required_columns_present():
    validate_required_columns(
        [
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
    )  # does not raise


@pytest.mark.parametrize(
    ("txn_type", "expected_category"),
    [
        ("AEPS ONUS Withdrawal", "withdrawal"),
        ("ATM Onus Withdrawal", "withdrawal"),
        ("Withdrawal", "withdrawal"),
        ("YONO WITHDRAWAL", "withdrawal"),
        ("AEPS ONUS Deposit", "deposit"),
        ("Deposit", "deposit"),
        ("AEPS ONUS Fund Transfer", "fund_transfer"),
    ],
)
def test_classify_transaction_type_allow_listed(txn_type, expected_category):
    assert classify_transaction_type(txn_type) == expected_category


@pytest.mark.parametrize(
    "txn_type",
    [
        "AEPS OFFUS Withdrawal",
        "ATM Offus Withdrawal",
        "Money Transfer",
        "IMPS Fund Transfer",
        "BHARAT BILL PAYMENTS",
        "Initial deposit",
        "Loan Deposit",
    ],
)
def test_classify_transaction_type_out_of_scope(txn_type):
    assert classify_transaction_type(txn_type) is None


def test_direction_for_category():
    assert direction_for_category("withdrawal") == "in_pool"
    assert direction_for_category("deposit") == "out_pool"
    assert direction_for_category("fund_transfer") == "other"


def test_parse_txn_datetime_valid():
    result = parse_txn_datetime("01-08-2026 08:05:23 AM")
    assert result == dt.datetime(2026, 8, 1, 8, 5, 23, tzinfo=dt.UTC)


def test_parse_txn_datetime_pm():
    result = parse_txn_datetime("01-08-2026 02:25:48 PM")
    assert result == dt.datetime(2026, 8, 1, 14, 25, 48, tzinfo=dt.UTC)


@pytest.mark.parametrize("raw", ["not a date", "", None, 12345, "2026-08-01 08:05:23"])
def test_parse_txn_datetime_invalid_returns_none(raw):
    assert parse_txn_datetime(raw) is None


def test_parse_txn_date_valid():
    assert parse_txn_date("01-08-2026") == dt.date(2026, 8, 1)


@pytest.mark.parametrize("raw", ["not a date", "", None, "01/08/2026"])
def test_parse_txn_date_invalid_returns_none(raw):
    assert parse_txn_date(raw) is None


def test_parse_amount_valid():
    assert parse_amount("2000") == decimal.Decimal("2000")
    assert parse_amount("1,500.50") == decimal.Decimal("1500.50")


@pytest.mark.parametrize("raw", ["not a number", "", None, "0", "-100", "abc123"])
def test_parse_amount_invalid_or_nonpositive_returns_none(raw):
    assert parse_amount(raw) is None
