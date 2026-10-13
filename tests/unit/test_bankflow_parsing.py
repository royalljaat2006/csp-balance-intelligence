"""Unit tests (no DB): format detection, Decimal parsing, reconciliation,
description normalisation, KO-code resolution and flow signs."""

import datetime as dt
import decimal
import io

import pytest
from bankflow import flows
from bankflow.normalize import normalize_row
from bankflow.parser import (
    OLE2_MAGIC,
    StatementFormatError,
    detect_format,
    parse_date,
    parse_money,
    parse_statement,
)
from bankflow.reconcile import FAILED, PARTIAL, RECONCILED, reconcile
from bankflow.resolution import (
    AMBIGUOUS,
    EXACT,
    MISSING,
    UNMATCHED,
    VARIANT,
    CodeResolver,
)

from tests.bankflow_fixtures import (
    desc_deposit,
    desc_moneytrf,
    desc_offus,
    desc_withdrawal,
    indian,
    make_statement,
    sample_rows,
)

D = decimal.Decimal


# ---------------------------------------------------------------- format
def test_detects_tab_text_regardless_of_extension():
    assert detect_format(make_statement(sample_rows()).encode()) == "tsv_text"


def test_detects_real_xlsx_and_html_and_ole():
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.append(["a"])
    buf = io.BytesIO()
    wb.save(buf)
    assert detect_format(buf.getvalue()) == "xlsx"
    assert detect_format(b"<html><table></table></html>") == "html"
    assert detect_format(OLE2_MAGIC + b"\x00" * 100) == "xls_binary"


def test_unsupported_formats_raise_clear_errors():
    with pytest.raises(StatementFormatError, match="binary"):
        parse_statement(OLE2_MAGIC + b"\x00" * 100)
    with pytest.raises(StatementFormatError, match="HTML"):
        parse_statement(b"<html></html>")
    with pytest.raises(StatementFormatError):
        parse_statement(b"just some text\nwith no table\n")


def test_xlsx_with_same_layout_parses():
    import openpyxl

    text = make_statement(sample_rows())
    wb = openpyxl.Workbook()
    ws = wb.active
    for line in text.splitlines():
        ws.append(line.split("\t"))
    buf = io.BytesIO()
    wb.save(buf)
    parsed = parse_statement(buf.getvalue())
    assert parsed.detected_format == "xlsx"
    assert len(parsed.rows) == len(sample_rows())
    assert parsed.opening_balance == D("1000.00")


# ---------------------------------------------------------------- parsing
def test_parse_money_indian_grouping_is_exact_decimal():
    assert parse_money("57,68,774.00") == D("5768774.00")
    assert parse_money(" 4000.00 ") == D("4000.00")
    assert parse_money("") is None
    assert parse_money("1,00,000.10 Dr") == D("-100000.10")
    with pytest.raises(ValueError):
        parse_money("12abc")
    assert indian(D("5768774")) == "57,68,774.00"


def test_parse_date_formats():
    assert parse_date("12/10/2026") == dt.date(2026, 10, 12)
    assert parse_date("12 Oct 2026") == dt.date(2026, 10, 12)
    with pytest.raises(ValueError):
        parse_date("31/31/2026")


def test_parse_statement_header_and_rows():
    parsed = parse_statement(make_statement(sample_rows(), opening="5768774.00").encode())
    assert parsed.account_number == "00000034757671556"
    assert parsed.opening_balance == D("5768774.00")
    assert parsed.opening_date == dt.date(2026, 10, 12)
    assert parsed.start_date == parsed.end_date == dt.date(2026, 10, 12)
    assert parsed.footer_seen
    assert len(parsed.rows) == len(sample_rows())
    assert parsed.rows[0].credit == D("4000.00") and parsed.rows[0].debit == D("0")
    assert not parsed.malformed


def test_malformed_rows_are_reported_not_dropped_silently():
    text = make_statement(sample_rows())
    lines = text.splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.startswith("12/10/2026"))
    parts = lines[idx].split("\t")
    parts[6] = "12x"  # corrupt Credit
    lines[idx] = "\t".join(parts)
    parsed = parse_statement("\n".join(lines).encode())
    assert len(parsed.malformed) == 1
    assert len(parsed.rows) == len(sample_rows()) - 1


# ---------------------------------------------------------------- reconcile
def _recon(text):
    parsed = parse_statement(text.encode())
    return reconcile(parsed, ref_by_row={})


def test_reconciled_only_when_header_book_balance_matches_last_row():
    assert _recon(make_statement(sample_rows())).status == RECONCILED


def test_internally_consistent_but_header_gap_is_partial_not_reconciled():
    rec = _recon(make_statement(sample_rows(), book="999999.00"))
    assert rec.status == PARTIAL
    assert rec.running_balance_mismatches == 0
    assert rec.header_gap is not None and rec.header_gap != 0
    assert any("Header Book Balance differs" in n for n in rec.notes)


def test_running_balance_mismatch_fails():
    rec = _recon(make_statement(sample_rows(), corrupt_balance_at=3))
    assert rec.status == FAILED
    assert rec.running_balance_mismatches >= 1
    assert rec.first_mismatch_row == 4


def test_missing_footer_is_never_fully_reconciled():
    assert _recon(make_statement(sample_rows(), footer=False)).status == PARTIAL


def test_decimal_arithmetic_has_no_float_drift():
    rows = [
        (desc_withdrawal(f"6285070000000000{i:04d}", "1A852455"), "", "0.10") for i in range(30)
    ]
    rec = _recon(make_statement(rows, opening="0.20"))
    assert rec.status == RECONCILED
    assert rec.computed_closing == D("3.20")


# ---------------------------------------------------------------- normalise
def test_normalisation_covers_observed_label_variants():
    n = normalize_row(
        desc_withdrawal("62850709168200095256", "1A852455"), "x", debit=D(0), credit=D(4000)
    )
    assert (n.ref_number, n.channel_raw, n.ko_raw) == (
        "62850709168200095256",
        "AEPSWDL",
        "1A852455",
    )
    assert n.category == "withdrawal" and n.side == "credit" and not n.is_reversal_candidate

    n = normalize_row(
        desc_moneytrf("62850711251200095256", "1A852455"), "x", debit=D(3100), credit=D(0)
    )
    assert (n.channel_raw, n.category, n.ko_raw) == ("MoneyTRF", "transfer", "1A852455")

    n = normalize_row(
        desc_offus("62850754192500062149", "1A851803"), "x", debit=D(0), credit=D(100)
    )
    assert (n.channel_raw, n.category, n.ko_raw) == ("RupayOFFUSWDL", "offus", "1A851803")

    n = normalize_row("CSH DEP (CDM)-7696642763   3470--", "x", debit=D(0), credit=D(49500))
    assert n.category == "pool_operations" and n.ko_raw == ""


def test_bare_at_ko_pattern_is_extracted_without_txn_prefix():
    n = normalize_row(
        "TO TRANSFER-62850748548500089273 Initial Deposit@KO 1A8502957-TRANSFER TO 456 Mr X",
        "x",
        debit=D(100),
        credit=D(0),
    )
    assert n.ko_raw == "1A8502957" and n.channel_raw == "Initial Deposit"
    assert n.category == "other_product"


def test_customer_account_and_name_are_redacted_but_hash_preserved():
    desc = desc_withdrawal("62850709168200095256", "1A852455", acct="36471609123")
    n = normalize_row(desc, "x", debit=D(0), credit=D(1))
    assert "36471609123" not in n.description_redacted
    assert "Customer" not in n.description_redacted
    assert "62850709168200095256" in n.description_redacted
    assert len(n.description_hash) == 64
    other = normalize_row(desc.replace("36471609123", "36471609124"), "x", debit=D(0), credit=D(1))
    assert other.description_hash != n.description_hash  # full original still verifiable


def test_reversal_candidates_flagged_by_direction_against_channel():
    wd_debit = normalize_row(
        desc_withdrawal("62850709168200095256", "1A852455"), "x", debit=D(5), credit=D(0)
    )
    assert wd_debit.is_reversal_candidate
    dep_credit = normalize_row(
        desc_deposit("62850709168200095256", "1A852455"), "x", debit=D(0), credit=D(5)
    )
    assert dep_credit.is_reversal_candidate


# ---------------------------------------------------------------- resolution
KNOWN = {"1A852455", "1A850295", "1A850949", "1A850946", "1A850943", "1", "123", "ABCDE",
         "ZZ123456"}


def test_resolution_statuses():
    r = CodeResolver(KNOWN)
    assert r.resolve("1A852455").status == EXACT
    assert r.resolve("").status == MISSING
    assert r.resolve("1A859999").status == UNMATCHED

    # owner-confirmed format rule: 1A85 + 4 digits; whatever follows the 8th character is dropped
    t = r.resolve("1A8502957")
    assert t.status == EXACT and t.resolved_code == "1A850295" and t.method == "first_8_chars"
    assert r.resolve("1A850295").method == "exact"
    assert r.resolve("1A850295N").resolved_code == "1A850295"
    assert r.resolve("1A85029510").resolved_code == "1A850295"
    lower = r.resolve("1a8502957")
    assert lower.resolved_code == "1A850295"

    # a well-formed code that is not in the master list is never guessed
    gone = r.resolve("1A8518271")
    assert gone.status == UNMATCHED and gone.resolved_code is None
    assert gone.candidates == ["1A851827"]

    # other formats keep the conservative, flag-gated behaviour
    v = r.resolve("ZZ1234567")
    assert v.status == VARIANT and v.candidates == ["ZZ123456"] and v.resolved_code is None

    a = r.resolve("1A85094")
    assert a.status == AMBIGUOUS and len(a.candidates) == 3 and a.resolved_code is None


def test_variant_assigned_only_when_rule_confirmed():
    assert (
        CodeResolver(KNOWN, confirm_trailing_variants=True).resolve("ZZ1234567").resolved_code
        == "ZZ123456"
    )


def test_short_junk_master_codes_do_not_swallow_everything():
    r = CodeResolver({"1", "123", "1A852455"})
    assert r.resolve("1A851111").status == UNMATCHED  # not a "variant" of code "1"


def test_attributable_code_respects_mode():
    r = CodeResolver(KNOWN)
    variant = r.resolve("ZZ1234567")
    assert r.attributable_code(variant, include_provisional=False) is None
    assert r.attributable_code(variant, include_provisional=True) == "ZZ123456"
    assert r.attributable_code(r.resolve("1A8502957"), include_provisional=False) == "1A850295"
    assert r.attributable_code(r.resolve("1A85094"), include_provisional=True) is None


# ---------------------------------------------------------------- flow signs
BY_CAT = {
    "withdrawal": {"count": 2, "credit": "5000.00", "debit": "200.00"},  # 200 = reversal debit
    "deposit": {"count": 1, "credit": "50.00", "debit": "10000.00"},  # 50 = reversal credit
    "transfer": {"count": 1, "credit": "0", "debit": "3100.00"},
    "offus": {"count": 1, "credit": "100.00", "debit": "0"},
}


def test_signed_customer_flow_withdrawal_negative_deposit_positive():
    # withdrawals: -(5000-200) = -4800 ; deposits: +(10000-50) = +9950
    assert flows.signed_flow(BY_CAT, "customer_wd_dep") == D("5150.00")
    # transfer excluded by default, included in the _xfer variant
    assert flows.signed_flow(BY_CAT, "customer_wd_dep_xfer") == D("8250.00")


def test_pool_net_is_the_opposite_accounting_basis_not_customer_flow():
    pool = flows.signed_flow(BY_CAT, "pool_net")  # credits - debits across ALL categories
    assert pool == D("5150.00") - D("13300.00")
    assert pool != flows.signed_flow(BY_CAT, "customer_wd_dep")
