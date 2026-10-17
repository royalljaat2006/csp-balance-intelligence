"""The movement-based shadow estimator end to end: stored rows, cycle boundaries, history, the
count rule, the month-end outlook and isolation from production tables.

Everything here is SYNTHETIC (invented CSPs and amounts) and shows that the arithmetic and the
data rules behave as agreed. It is not evidence of accuracy against real SBI figures."""

import datetime as dt
import decimal
import inspect
import os
import time

import pytest
from bankflow import estimates, flows, movement, outlook, sbi_comparison
from bankflow.ingest import run_import
from bankflow.models import DailyEstimate, FlowAggregate
from csp.models import Csp, DailyBalance, MonthlySummary
from django.utils import timezone

from tests.bankflow_fixtures import desc_deposit, desc_moneytrf, desc_withdrawal, make_statement

D = decimal.Decimal
d = dt.date
pytestmark = pytest.mark.django_db
OCT = d(2026, 10, 1)


def day(n: int) -> dt.date:
    return OCT + dt.timedelta(days=n - 1)


@pytest.fixture
def complete(monkeypatch):
    """Every day up to the 12th is a proven-complete statement day."""
    days = {day(n) for n in range(1, 13)}
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: days)
    return days


def _csp(code="1A850001", n=300):
    return Csp.objects.create(csp_code=code, name=f"CSP {code}", account_count=n)


def _bal(code, when, value, n):
    DailyBalance.objects.create(csp_id=code, balance_date=when, daily_avg_balance=D(str(value)),
                                account_count=n, source="calling_sheet")


def _flow(code, when, deposit=0, withdrawal=0, **other):
    cats = {}
    if deposit:
        cats["deposit"] = {"count": 1, "credit": "0", "debit": str(deposit)}
    if withdrawal:
        cats["withdrawal"] = {"count": 1, "credit": str(withdrawal), "debit": "0"}
    for k, (cr, db) in other.items():
        cats[k] = {"count": 1, "credit": str(cr), "debit": str(db)}
    FlowAggregate.objects.create(
        csp_id=code, flow_date=when, resolution_mode=flows.MODE_EXACT, txn_count=len(cats),
        credit_amount=withdrawal, debit_amount=deposit, recon_status="RECONCILED",
        by_category=cats)


def _row(code, n):
    return DailyEstimate.objects.get(csp_id=code, estimate_date=day(n))


def _seed(code="1A850001"):
    _csp(code)
    _bal(code, day(1), 1000, 300)
    for n in range(2, 13):  # the sheet is polled every day with the same value
        _bal(code, day(n), 1000, 300)


# ------------------------------------------------------------------ the agreed arithmetic
def test_baseline_day_is_the_baseline_alone_and_movement_starts_the_next_day(complete):
    _seed()
    _flow("1A850001", day(1), deposit=50000)         # receipt-day movement must NOT be applied
    _flow("1A850001", day(2), deposit=3000, withdrawal=1000)
    estimates.compute_daily_estimates(day(3))
    one, two, three = _row("1A850001", 1), _row("1A850001", 2), _row("1A850001", 3)
    assert one.estimated_combined == D("300000.00") and one.estimated_value == D("1000.00")
    assert one.reason_code == "BASELINE_DAY" and one.net_movement == 0
    assert two.estimated_combined == D("302000.00") and two.estimated_value == D("1006.67")
    assert (two.eligible_deposits, two.eligible_withdrawals, two.net_movement) == (
        D("3000.00"), D("1000.00"), D("2000.00"))
    assert three.estimated_combined == D("302000.00")  # a quiet complete day carries forward
    assert three.reason_code == "NO_ELIGIBLE_MOVEMENT_CARRY_FORWARD" and three.is_final
    assert all(r.method == "movement" for r in (one, two, three))


def test_excluded_and_unclassified_rows_do_not_change_the_estimate(complete):
    _seed()
    _flow("1A850001", day(2), deposit=100, transfer=(0, 9999), offus=(7777, 0),
          other_product=(0, 5555), pool_operations=(1234, 0), unclassified=(0, 4242))
    estimates.compute_daily_estimates(day(2))
    r = _row("1A850001", 2)
    assert r.net_movement == D("100.00") and r.estimated_combined == D("300100.00")
    assert r.details["excluded_txns"] == 4 and r.details["unclassified_txns"] == 1


# ------------------------------------------------------------------ gaps and partial days
def test_a_missing_statement_carries_forward_and_marks_the_row_provisional(complete, monkeypatch):
    _seed()
    _flow("1A850001", day(2), deposit=1000)
    monkeypatch.setattr(flows, "proven_complete_dates",
                        lambda: {day(2), day(4), day(5)})  # day 3 has no statement
    estimates.compute_daily_estimates(day(5))
    assert _row("1A850001", 3).estimated_combined == D("301000.00")
    assert _row("1A850001", 3).reason_code == "STATEMENT_GAP_CARRY_FORWARD"
    assert not _row("1A850001", 3).is_final
    assert _row("1A850001", 3).details["provisional"] is True
    assert _row("1A850001", 2).is_final
    assert not _row("1A850001", 5).is_final  # an open day earlier in the cycle keeps it open


def test_the_gap_is_filled_and_finalised_once_the_statement_arrives(complete, monkeypatch):
    _seed()
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: {day(2), day(4)})
    estimates.compute_daily_estimates(day(4))
    assert not _row("1A850001", 4).is_final
    _flow("1A850001", day(3), deposit=700)
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: {day(2), day(3), day(4)})
    estimates.compute_daily_estimates(day(4))
    assert _row("1A850001", 3).estimated_combined == D("300700.00") and _row("1A850001", 3).is_final
    assert _row("1A850001", 4).estimated_combined == D("300700.00") and _row("1A850001", 4).is_final


def test_today_is_partial_until_complete(monkeypatch):
    today = timezone.localdate()
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: set())
    _csp()
    _bal("1A850001", today - dt.timedelta(days=2), 1000, 300)
    _bal("1A850001", today, 1000, 300)
    _flow("1A850001", today, deposit=250)
    estimates.compute_daily_estimates(today)
    r = DailyEstimate.objects.get(csp_id="1A850001", estimate_date=today)
    assert r.reason_code == "TODAY_PARTIAL_MOVEMENT" and not r.is_final
    assert r.estimated_combined == D("300250.00")  # the part of today that exists
    assert any("Today is still in progress" in x for x in r.details["provisional_reasons"])


# ------------------------------------------------------------------ account counts
def test_a_count_change_never_scales_the_combined_balance(complete):
    _seed()
    DailyBalance.objects.filter(csp_id="1A850001", balance_date__gte=day(5)).update(
        account_count=330)
    estimates.compute_daily_estimates(day(7))
    before, after = _row("1A850001", 4), _row("1A850001", 5)
    assert before.estimated_combined == after.estimated_combined == D("300000.00")
    assert before.estimated_value == D("1000.00") and after.estimated_value == D("909.09")
    assert (after.baseline_account_count, after.current_account_count,
            after.account_count_change) == (300, 330, 30)
    # movement after the change still lands on the combined balance
    _flow("1A850001", day(8), deposit=3300)
    estimates.compute_daily_estimates(day(8))
    assert _row("1A850001", 8).estimated_combined == D("303300.00")
    assert _row("1A850001", 8).estimated_value == D("919.09")


def test_blank_zero_and_non_numeric_counts_keep_the_last_valid_count_and_are_flagged(complete):
    _seed()
    DailyBalance.objects.filter(csp_id="1A850001", balance_date=day(4)).update(account_count=330)
    DailyBalance.objects.filter(csp_id="1A850001", balance_date=day(5)).update(account_count=0)
    DailyBalance.objects.filter(csp_id="1A850001", balance_date=day(6)).update(account_count=None)
    estimates.compute_daily_estimates(day(6))
    r5, r6 = _row("1A850001", 5), _row("1A850001", 6)
    assert r5.current_account_count == r6.current_account_count == 330
    assert r5.details["count"]["invalid_reading_ignored"] is True
    assert r6.details["count"]["stale"] is True
    assert any("stale or was blank/zero" in x for x in r6.details["provisional_reasons"])
    assert r6.estimated_value == D("909.09")


def test_the_200_account_gate_still_applies_and_keeps_the_combined_balance(complete):
    _csp("SMALL", n=150)
    _bal("SMALL", day(1), 900, 150)
    _bal("SMALL", day(2), 900, 150)
    estimates.compute_daily_estimates(day(2))
    r = _row("SMALL", 2)
    assert r.status == "UNAVAILABLE" and r.reason_code == "BELOW_200_ACCOUNT_GATE"
    assert r.estimated_value is None and r.estimated_combined == D("135000.00")


# ------------------------------------------------------------------ cycles and history
def test_a_new_baseline_starts_a_new_cycle_and_keeps_the_old_one(complete):
    _seed()
    _flow("1A850001", day(2), deposit=3000)
    _flow("1A850001", day(6), deposit=88888)   # the receipt day of the new baseline
    _flow("1A850001", day(7), withdrawal=330)
    estimates.compute_daily_estimates(day(5))  # the first cycle is stored by the daily runs
    DailyBalance.objects.filter(csp_id="1A850001", balance_date__gte=day(6)).update(
        daily_avg_balance=D("1100"), account_count=330)
    estimates.compute_daily_estimates(day(8))
    old_last, new_first = _row("1A850001", 5), _row("1A850001", 6)
    assert old_last.baseline_value == D("1000.00") and old_last.estimated_combined == D("303000.00")
    assert new_first.baseline_date == day(6) and new_first.baseline_value == D("1100.00")
    assert new_first.estimated_combined == D("363000.00")   # 1100 x 330, that day's flow ignored
    assert new_first.reason_code == "BASELINE_DAY"
    assert _row("1A850001", 7).estimated_combined == D("362670.00")
    assert _row("1A850001", 8).estimated_value == D("1099.00")


def test_final_rows_are_never_rewritten_and_late_rows_do_not_trigger_a_recalculation(complete):
    _seed()
    _flow("1A850001", day(2), deposit=1000)
    estimates.compute_daily_estimates(day(4))
    frozen = {n: _row("1A850001", n).estimated_combined for n in (1, 2, 3, 4)}
    assert all(_row("1A850001", n).is_final for n in (1, 2, 3, 4))
    _flow("1A850001", day(3), deposit=99999)   # a late transaction for a finished day
    _flow("1A850001", day(5), withdrawal=500)
    estimates.compute_daily_estimates(day(6))
    for n, combined in frozen.items():
        assert _row("1A850001", n).estimated_combined == combined, n
    assert _row("1A850001", 5).estimated_combined == D("300500.00")   # builds on the stored day 4
    assert _row("1A850001", 6).estimated_combined == D("300500.00")


def test_a_row_from_a_retired_rule_is_superseded_but_its_values_are_kept(complete):
    _seed()
    DailyEstimate.objects.create(
        csp_id="1A850001", estimate_date=day(3), method="account_count_proportional",
        status="PROVISIONAL", reason_code="OLD", baseline_value=D(1000), baseline_date=day(1),
        estimated_value=D("1234.00"), calculated_at=timezone.now())
    estimates.compute_daily_estimates(day(5))
    row = _row("1A850001", 3)
    assert row.method == "movement" and row.estimated_value == D("1000.00")
    kept = row.details["superseded_row"]
    assert kept["method"] == "account_count_proportional" and kept["estimated_value"] == "1234.00"
    assert kept["status"] == "PROVISIONAL" and kept["reason_code"] == "OLD"
    # the chain is whole: every day of the cycle is a movement row
    assert set(DailyEstimate.objects.values_list("method", flat=True)) == {"movement"}


def test_rerunning_the_same_day_is_idempotent(complete):
    _seed()
    _flow("1A850001", day(2), deposit=1000)
    estimates.compute_daily_estimates(day(3))
    first = list(DailyEstimate.objects.order_by("estimate_date").values_list(
        "estimate_date", "estimated_combined", "is_final"))
    estimates.compute_daily_estimates(day(3))
    assert first == list(DailyEstimate.objects.order_by("estimate_date").values_list(
        "estimate_date", "estimated_combined", "is_final"))


# ------------------------------------------------------------------ attribution, end to end
def test_sub_csp_rows_roll_up_and_unmatched_and_held_out_rows_do_not_count(tmp_path):
    _csp("1A850001")
    rows = [
        (desc_withdrawal("62850709168200095001", "1A850001"), "", "4000.00"),
        (desc_deposit("62850709168200095002", "1A8500011"), "1500.00", ""),   # sub-CSP: parent
        (desc_deposit("62850709168200095003", "1A85000"), "999.00", ""),      # 7 chars: nobody
        (desc_deposit("62850709168200095004", "1A859999"), "888.00", ""),     # unknown CSP
        (desc_moneytrf("62850709168200095005", "1A850001"), "777.00", ""),    # excluded label
        ("BY TRANSFER-62850748548500089299 YONO WITHDRAWAL@KO 1A850001-TRANSFER FROM 459914210-",
         "", "50.00"),                                                       # held out
    ]
    f = tmp_path / "s.xls"
    f.write_text(make_statement(rows), encoding="utf-8")
    old = time.time() - 60
    os.utime(f, (old, old))
    run_import(tmp_path)
    agg = FlowAggregate.objects.get(csp_id="1A850001", resolution_mode=flows.MODE_EXACT)
    m = movement.split(agg.by_category)
    assert (m.deposits, m.withdrawals, m.net) == (D("1500.00"), D("4000.00"), D("-2500.00"))
    assert m.counted_txns == 2 and m.excluded_txns == 1 and m.unclassified_txns == 1


# ------------------------------------------------------------------ month-end outlook (task B)
def test_mtd_average_and_month_end_projection_come_from_the_stored_daily_estimates(complete):
    _seed()
    _flow("1A850001", day(2), deposit=3000)    # +10.00 per account from day 2
    _flow("1A850001", day(4), withdrawal=3000)  # back to 1000.00 from day 4
    estimates.compute_daily_estimates(day(5))
    o = outlook.month_end_outlook("2026-10", today=day(5))
    assert o["csps_with_outlook"] == 1
    row = o["rows"][0]
    # per-account estimates: 1000, 1010, 1010, 1000, 1000 over days 1..5
    assert row["mtd_average"] == D("1004.00") and row["latest_estimate"] == D("1000.00")
    assert row["days_counted"] == 5 and row["days_remaining"] == 26
    assert row["projected_average"] == D("1000.65")  # (5020 + 1000 x 26) / 31
    assert row["provisional"] is True                # unvalidated: always informational
    assert o["network"]["projected_average"] == D("1000.65")
    assert o["sbi_reported"]["latest_average"] == D("1000.00")   # a separate, observed number


# ------------------------------------------------------------------ accuracy reporting (task D)
def test_aggregate_and_simple_average_variance_are_reported_separately(complete, monkeypatch):
    for code, prev, new in (("1A850001", 1000, 1010), ("1A850002", 1000, 1000)):
        _csp(code)
        _bal(code, OCT - dt.timedelta(days=10), prev, 300)
        _bal(code, day(1), new if code.endswith("1") else 1000.5, 300)
    # no movement: both predictions are 1000.00 (count unchanged)
    r = sbi_comparison.sbi_comparison()
    s = r["summary"]
    assert s["valid_comparisons"] == 0 and r["not_comparable"] == 2   # no complete statements yet
    # with the interval covered by complete statements they become comparable
    days = {OCT - dt.timedelta(days=10) + dt.timedelta(days=i) for i in range(12)}
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: days)
    s = sbi_comparison.sbi_comparison()["summary"]
    assert s["valid_comparisons"] == 2 and s["excluded_zero_reported"] == 0
    agg = s["aggregate"]
    assert agg["predicted_total"] == D("1000.00") * 600
    assert agg["reported_total"] == (D("1010") + D("1000.5")) * 300
    assert round(agg["variance_pct"], 4) == round(
        float(D("2000") - D("2010.5")) / 2010.5 * 100, 4)
    assert round(s["simple_avg_pct_variance"], 4) == round(
        (float(D(1000) - D(1010)) / 1010 * 100 + float(D(1000) - D("1000.5")) / 1000.5 * 100) / 2,
        4)


# ------------------------------------------------------------------ isolation (shadow only)
def test_the_estimator_never_writes_production_tables(complete):
    csp = _csp()
    for n in range(1, 8):
        _bal("1A850001", day(n), 1000, 300)
    MonthlySummary.objects.create(csp=csp, month="2026-10", days_in_month=31, days_with_data=7,
                                  mtd_mab=D("1000"), projected_mab=D("1000"), slab="S1")
    _flow("1A850001", day(2), deposit=5000)
    daily = list(DailyBalance.objects.order_by("id").values_list(
        "id", "csp_id", "balance_date", "daily_avg_balance", "account_count", "source"))
    monthly = list(MonthlySummary.objects.values_list("id", "mtd_mab", "projected_mab", "slab"))
    estimates.compute_daily_estimates(day(7))
    outlook.month_end_outlook("2026-10", today=day(7))
    sbi_comparison.sbi_comparison()
    assert daily == list(DailyBalance.objects.order_by("id").values_list(
        "id", "csp_id", "balance_date", "daily_avg_balance", "account_count", "source"))
    assert monthly == list(MonthlySummary.objects.values_list(
        "id", "mtd_mab", "projected_mab", "slab"))


def test_the_estimator_modules_contain_no_production_writes():
    for mod in (movement, estimates, outlook, sbi_comparison):
        src = inspect.getsource(mod)
        for forbidden in ("DailyBalance.objects.create", "DailyBalance.objects.bulk",
                          "DailyBalance.objects.update", "MonthlySummary.objects.create",
                          "MonthlySummary.objects.update", "DailyBalance(", "rule19"):
            assert forbidden not in src, (mod.__name__, forbidden)
