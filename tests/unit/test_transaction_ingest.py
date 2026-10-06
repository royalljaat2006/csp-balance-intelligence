"""
ingest_workbook against small synthetic workbooks — covers the allow-list
filter, malformed-row rejection, CSP stub-creation, and idempotent re-import
(re-verifies, at unit scale, the same behaviour confirmed against the real
306k-row August sample file during development).
"""

import openpyxl
import pytest
from csp.models import Csp, Transaction
from ingestion.transaction_ingest import ingest_workbook

HEADER = [
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


def _make_workbook(tmp_path, rows):
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "Success"
    sheet.append(HEADER)
    for row in rows:
        sheet.append(row)
    path = tmp_path / "Transaction Test.xlsx"
    wb.save(path)
    return path


def _row(
    ko_id="1A850001",
    dt="01-08-2026 08:00:00 AM",
    ref="REF001",
    txn_type="AEPS ONUS Withdrawal",
    from_acct="XXXXXX11111",
    to_acct="XXXXXX71556",
    amount="2000",
    status="Success",
    date="01-08-2026",
    csp="1A850001",
):
    return [ko_id, dt, ref, txn_type, from_acct, to_acct, amount, status, date, csp]


@pytest.mark.django_db
def test_ingests_allow_listed_rows_and_stub_creates_csp(tmp_path):
    path = _make_workbook(tmp_path, [_row()])
    result = ingest_workbook(path, HEADER)

    assert result.rows_read == 1
    assert result.rows_valid == 1
    assert result.rows_rejected == 0
    assert result.rows_upserted == 1
    assert result.new_csp_codes == {"1A850001"}

    txn = Transaction.objects.get(ref_number="REF001")
    assert txn.csp_id == "1A850001"
    assert txn.category == "withdrawal"
    assert txn.direction == "in_pool"
    assert txn.amount == 2000

    assert Csp.objects.filter(csp_code="1A850001").exists()


@pytest.mark.django_db
def test_drops_out_of_scope_type(tmp_path):
    path = _make_workbook(tmp_path, [_row(txn_type="Money Transfer", ref="REF002")])
    result = ingest_workbook(path, HEADER)

    assert result.rows_read == 1
    assert result.rows_valid == 0
    assert result.rows_upserted == 0
    assert not Transaction.objects.filter(ref_number="REF002").exists()


@pytest.mark.django_db
def test_rejects_malformed_amount(tmp_path):
    path = _make_workbook(tmp_path, [_row(ref="REF003", amount="not-a-number")])
    result = ingest_workbook(path, HEADER)

    assert result.rows_valid == 1  # type was in scope
    assert result.rows_rejected == 1
    assert result.rows_upserted == 0


@pytest.mark.django_db
def test_rejects_malformed_date(tmp_path):
    path = _make_workbook(tmp_path, [_row(ref="REF004", date="2026/08/01")])
    result = ingest_workbook(path, HEADER)

    assert result.rows_rejected == 1
    assert result.rows_upserted == 0


@pytest.mark.django_db
def test_does_not_stub_create_csp_that_already_exists(tmp_path):
    Csp.objects.create(csp_code="1A850001", name="Already known")
    path = _make_workbook(tmp_path, [_row()])
    result = ingest_workbook(path, HEADER)

    assert result.new_csp_codes == set()
    csp = Csp.objects.get(csp_code="1A850001")
    assert csp.name == "Already known"  # untouched, not overwritten


@pytest.mark.django_db
def test_reingesting_same_file_is_idempotent(tmp_path):
    path = _make_workbook(tmp_path, [_row(amount="2000")])
    ingest_workbook(path, HEADER)
    assert Transaction.objects.count() == 1

    # Re-export with an updated amount for the same ref_number -> updates, doesn't duplicate.
    path2 = _make_workbook(tmp_path, [_row(amount="5000")])
    result = ingest_workbook(path2, HEADER)

    assert Transaction.objects.count() == 1
    assert result.new_csp_codes == set()
    assert Transaction.objects.get(ref_number="REF001").amount == 5000


@pytest.mark.django_db
def test_multiple_categories_get_correct_direction(tmp_path):
    path = _make_workbook(
        tmp_path,
        [
            _row(ref="W1", txn_type="AEPS ONUS Withdrawal"),
            _row(ref="D1", txn_type="AEPS ONUS Deposit"),
            _row(ref="F1", txn_type="AEPS ONUS Fund Transfer"),
        ],
    )
    ingest_workbook(path, HEADER)

    assert Transaction.objects.get(ref_number="W1").direction == "in_pool"
    assert Transaction.objects.get(ref_number="D1").direction == "out_pool"
    assert Transaction.objects.get(ref_number="F1").direction == "other"
