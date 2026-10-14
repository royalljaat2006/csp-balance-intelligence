"""Business-confirmed meaning of the statement (seniors, 2026-10-14): a pool CREDIT is a customer
withdrawal and a pool DEBIT a customer deposit; initial deposit counts as a deposit; transfers,
off-us, loan/SSA and unknown channels (outimps, ppf, yono fi wdl) are not counted."""

import decimal
import os
import time

import pytest
from bankflow import flows, intelligence
from bankflow.ingest import reclassify_stored, reresolve_all, run_import
from bankflow.models import FlowAggregate, StatementTransaction
from bankflow.normalize import normalize_row
from csp.models import Csp

from tests.bankflow_fixtures import (
    desc_deposit,
    desc_moneytrf,
    desc_offus,
    desc_withdrawal,
    make_statement,
)

D = decimal.Decimal
pytestmark = pytest.mark.django_db
KO = "1A852455"


def initial_deposit(ref: str, ko: str) -> str:
    return f"TO TRANSFER-{ref} Initial Deposit@KO {ko}-TRANSFER TO 45600001234  Mr. Test-"


def other(ref: str, label: str, ko: str, side: str = "BY") -> str:
    return f"{side} TRANSFER-{ref} {label}@KO {ko}-TRANSFER FROM 4599142105216-"


def _import(tmp_path, rows):
    f = tmp_path / "s.xls"
    f.write_text(make_statement(rows), encoding="utf-8")
    old = time.time() - 60
    os.utime(f, (old, old))
    run_import(tmp_path)


@pytest.fixture
def csp():
    return Csp.objects.create(csp_code=KO, name="X", account_count=300)


def test_initial_deposit_is_a_deposit_but_loan_ssa_and_unknown_labels_are_not():
    def cat(desc, debit="", credit=""):
        return normalize_row(desc, "", debit=D(debit or 0), credit=D(credit or 0)).category

    assert cat(initial_deposit("62850748548500089273", KO), debit="100") == "deposit"
    loan = other("62850748548500089274", "Loan Deposit", KO, "TO")
    assert cat(loan, debit="5") == "other_product"
    assert cat(other("62850748548500089275", "SSATXN", KO, "TO"), debit="5") != "deposit"
    for label in ("outimps", "PPF", "YONO FI WDL"):
        assert cat(other("62850748548500089276", label, KO), credit="10") == "unclassified", label
    assert cat(desc_moneytrf("62850748548500089277", KO), debit="3") == "transfer"
    assert cat(desc_offus("62850748548500089278", KO), credit="4") == "offus"
    # a reversal is a credit on a deposit channel
    n = normalize_row(initial_deposit("62850748548500089279", KO), "", debit=D(0), credit=D(100))
    assert n.is_reversal_candidate and n.reversal_reason == "deposit_channel_credit"


def test_money_in_the_dashboard_is_withdrawals_and_deposits_only(tmp_path, csp):
    _import(tmp_path, [
        (desc_withdrawal("62850709168200095256", KO), "", "4000.00"),       # customer withdrew 4000
        (desc_deposit("62850709168200095257", KO), "1500.00", ""),          # deposited 1500
        (initial_deposit("62850709168200095258", KO), "100.00", ""),        # initial deposit 100
        (desc_moneytrf("62850709168200095259", KO), "999.00", ""),          # not counted
        (desc_offus("62850709168200095260", KO), "", "777.00"),             # not counted
    ])
    flows.rebuild_aggregates()
    fl = intelligence.dashboard_summary(*intelligence.resolve_range(30))["flows"]
    assert fl["withdrawals"] == D(4000)
    assert fl["deposits"] == D(1600)                 # deposit 1500 + initial deposit 100
    assert fl["net_customer_flow"] == D(-2400)       # deposits minus withdrawals
    assert fl["other_types"]["txn_count"] == 2       # transfer + off-us are reported apart
    assert fl["pool_credits"] == D(4000) and fl["pool_debits"] == D(1600)


def test_rows_stored_with_the_old_map_are_brought_in_line_and_aggregates_follow(tmp_path, csp):
    _import(tmp_path, [(initial_deposit("62850709168200095258", KO), "100.00", "")])
    StatementTransaction.objects.update(category="other_product")  # as stored before 2026-10-14
    flows.rebuild_aggregates()
    before = intelligence.dashboard_summary(*intelligence.resolve_range(30))["flows"]
    assert before["deposits"] == 0 and before["other_types"]["debit"] == D(100)

    assert reclassify_stored() == 1
    assert reclassify_stored() == 0                   # idempotent
    assert StatementTransaction.objects.get().category == "deposit"
    reresolve_all()                                   # what build_bank_flows runs
    after = intelligence.dashboard_summary(*intelligence.resolve_range(30))["flows"]
    assert after["deposits"] == D(100) and after["other_types"]["txn_count"] == 0
    agg = FlowAggregate.objects.get(csp_id=KO, resolution_mode="exact_only")
    assert agg.by_category["deposit"]["debit"] == "100.00"


def test_a_withdrawal_reversal_is_netted_into_withdrawals(tmp_path, csp):
    _import(tmp_path, [
        (desc_withdrawal("62850709168200095256", KO), "", "4000.00"),
        (desc_withdrawal("62850709168200095257", KO), "300.00", ""),   # a reversal
    ])
    flows.rebuild_aggregates()
    fl = intelligence.dashboard_summary(*intelligence.resolve_range(30))["flows"]
    assert fl["withdrawals"] == D(3700) and fl["deposits"] == 0
    assert fl["net_customer_flow"] == D(-3700)
