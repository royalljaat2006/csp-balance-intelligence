"""'Needs attention' must flag what matters, not every wobble; and only 1A85XXXX codes are CSPs."""

import datetime as dt
import decimal

import pytest
from bankflow import intelligence
from bankflow.models import FlowAggregate
from csp.models import CSP_CODE_RE, INVALID_CODE_STATUS, Csp
from django.core.management import call_command
from django.utils import timezone
from ingestion.calling_sheet_ingest import REQUIRED_COLUMNS, parse_sheet_values

D = decimal.Decimal
pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()


def flow(code, day, credit, debit):
    FlowAggregate.objects.create(
        csp_id=code, flow_date=day, resolution_mode="exact_only", txn_count=2,
        credit_amount=D(credit), debit_amount=D(debit), recon_status="RECONCILED",
        by_category={"deposit": {"count": 1, "credit": str(credit), "debit": str(debit)}})


def csp(code):
    return Csp.objects.create(csp_code=code, name=code, account_count=300)


@pytest.fixture
def covered():
    """Statements cover 20 of the last 30 days (a filler CSP with balanced flow)."""
    csp("1A859999")
    for i in range(20):
        flow("1A859999", TODAY - dt.timedelta(days=i), 1000, 1000)


def types(ex, code=None):
    return {i["type"] for i in ex["items"] if code is None or i["csp_code"] == code}


def run():
    return intelligence.exceptions(*intelligence.resolve_range(30), limit=1000)


def test_a_small_wobble_on_big_turnover_is_not_an_alert(covered):
    csp("1A850001")
    flow("1A850001", TODAY, 5_000_000, 5_020_000)  # out by 20,000 = 0.2 % of 1 crore
    assert "NEGATIVE_NET_FLOW" not in types(run(), "1A850001")


def test_a_big_rupee_amount_that_is_a_tiny_share_is_not_an_alert(covered):
    csp("1A850002")
    flow("1A850002", TODAY, 50_000_000, 50_200_000)  # out by 2 lakh but only 0.2 % of activity
    assert "NEGATIVE_NET_FLOW" not in types(run(), "1A850002")


def test_a_large_share_of_a_tiny_amount_is_not_an_alert(covered):
    csp("1A850003")
    flow("1A850003", TODAY, 1_000, 60_000)  # 98 % imbalance but only Rs 59,000
    assert "NEGATIVE_NET_FLOW" not in types(run(), "1A850003")


def test_material_outflows_are_flagged_biggest_first_with_severity(covered):
    cases = (("1A850010", 200_000, 450_000),     # out 2.5 lakh, 38 % -> medium
             ("1A850011", 200_000, 1_000_000),   # out 8 lakh, 67 % -> high
             ("1A850012", 300_000, 460_000))     # out 1.6 lakh, 21 % -> not flagged
    for code, credit, debit in cases:
        csp(code)
        flow(code, TODAY, credit, debit)
    ex = run()
    flagged = [i for i in ex["items"] if i["type"] == "NEGATIVE_NET_FLOW"]
    assert [i["csp_code"] for i in flagged] == ["1A850011", "1A850010"]  # largest outflow first
    assert [i["severity"] for i in flagged] == ["high", "medium"]
    assert flagged[0]["metrics"]["imbalance_pct"] == pytest.approx(66.7, abs=0.1)
    assert "1A850012" not in {i["csp_code"] for i in flagged}


def test_inflows_never_trigger_the_outflow_alert(covered):
    csp("1A850020")
    flow("1A850020", TODAY, 9_000_000, 100_000)
    assert "NEGATIVE_NET_FLOW" not in types(run(), "1A850020")


def test_alerts_pause_with_a_visible_notice_when_statements_cover_too_little():
    csp("1A850030")
    flow("1A850030", TODAY, 100_000, 5_000_000)  # a screaming outflow, but only 1 day of 30 loaded
    ex = run()
    assert "NEGATIVE_NET_FLOW" not in types(ex)
    note = next(i for i in ex["items"] if i["type"] == "STATEMENT_COVERAGE_LOW")
    assert note["severity"] == "low" and "paused" in note["reason"] and note["csp_code"] is None


def test_real_scale_noise_is_cut_down_but_real_problems_survive(covered):
    # 40 CSPs with a few-percent wobble, 3 with a real problem
    for i in range(40):
        code = f"1A8501{i:02d}"
        csp(code)
        flow(code, TODAY, 1_000_000, 1_060_000 + i * 1_000)
    for i, debit in enumerate((900_000, 2_000_000, 5_000_000)):
        code = f"1A8502{i:02d}"
        csp(code)
        flow(code, TODAY, 400_000, debit)
    flagged = [i for i in run()["items"] if i["type"] == "NEGATIVE_NET_FLOW"]
    assert {i["csp_code"] for i in flagged} == {"1A850200", "1A850201", "1A850202"}  # 3 of 43


def test_thresholds_are_settings(covered, settings):
    csp("1A850040")
    flow("1A850040", TODAY, 200_000, 500_000)  # out 3 lakh = 43 % of activity: flagged by default
    assert "NEGATIVE_NET_FLOW" in types(run(), "1A850040")
    settings.ALERT_MIN_OUTFLOW = 400_000
    assert "NEGATIVE_NET_FLOW" not in types(run(), "1A850040")


def test_new_accounts_with_no_money_movement_at_all_is_flagged(covered):
    from bankflow.models import PmjdyMonthly

    for code, accounts, with_flow in (("1A850050", 14, False), ("1A850051", 14, True),
                                      ("1A850052", 3, False)):
        csp(code)
        PmjdyMonthly.objects.create(csp_id=code, month=f"{TODAY:%Y-%m}", mtd=accounts, ftd=0,
                                    target=7, observed_at=timezone.now(), snapshot_date=TODAY)
        if with_flow:
            flow(code, TODAY, 1000, 1000)
    flagged = {i["csp_code"] for i in run()["items"] if i["type"] == "GROWTH_WITHOUT_FLOW_EVIDENCE"}
    assert flagged == {"1A850050"}  # enough new accounts, nothing at all seen; the others not


# ---------------------------------------------------------------- only 1A85XXXX codes are CSPs
@pytest.mark.parametrize("code, ok", [
    ("1A850247", True), ("1A859999", True), ("1a850247", False), ("1A85024", False),
    ("1A8502477", False), ("f", False), ("", False), ("1A85024N", False), ("2B850247", False),
])
def test_the_code_format(code, ok):
    assert bool(CSP_CODE_RE.match(code)) is ok


def _sheet(*codes):
    header1 = [""] * len(REQUIRED_COLUMNS)
    values = [header1, list(REQUIRED_COLUMNS)]
    for c in codes:
        row = [""] * len(REQUIRED_COLUMNS)
        row[REQUIRED_COLUMNS.index("CSP ID")] = c
        row[REQUIRED_COLUMNS.index("Total Accounts")] = "300"
        row[REQUIRED_COLUMNS.index("Avg Balance")] = "4,000"
        values.append(row)
    return values


def test_the_sheet_parser_skips_rows_that_are_not_1a85_codes():
    r = parse_sheet_values(_sheet("1A850247", "f", "1A85024", " 1A850248 ", "note: call back"))
    assert [row.csp_code for row in r.rows] == ["1A850247", "1A850248"]
    assert r.rows_skipped == 3 and r.rows_invalid_code == 3  # counted, never silently dropped


def test_invalid_codes_are_kept_out_of_every_csp_list_and_can_be_flagged():
    csp("1A850060")
    csp("f")
    assert Csp.objects.tracked().count() == 2  # before the clean-up the stray row still counts
    call_command("flag_invalid_csp_codes", dry_run=True)
    assert Csp.objects.get(csp_code="f").status == ""  # dry run changes nothing
    call_command("flag_invalid_csp_codes")
    call_command("flag_invalid_csp_codes")  # idempotent
    assert Csp.objects.get(csp_code="f").status == INVALID_CODE_STATUS
    assert list(Csp.objects.tracked().values_list("csp_code", flat=True)) == ["1A850060"]
    assert Csp.objects.filter(csp_code="f").exists()  # nothing is deleted
    assert intelligence.dashboard_summary(*intelligence.resolve_range(30))["csps_tracked"] == 1
