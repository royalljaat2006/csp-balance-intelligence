"""The movement estimator's pure arithmetic: signs, labels, counts and the day-by-day roll.

No database. Amounts are invented."""

import datetime as dt
from decimal import Decimal as D

from bankflow import movement
from bankflow.normalize import CHANNEL_MAP, classify
from ingestion.xlsx_validation import TRANSACTION_TYPE_CATEGORY

d = dt.date


def _cat(deposit=None, withdrawal=None, **other):
    out = {}
    if deposit:
        out["deposit"] = {"count": 1, "credit": str(deposit[0]), "debit": str(deposit[1])}
    if withdrawal:
        out["withdrawal"] = {"count": 1, "credit": str(withdrawal[0]), "debit": str(withdrawal[1])}
    for k, v in other.items():
        out[k] = {"count": v[2] if len(v) > 2 else 1, "credit": str(v[0]), "debit": str(v[1])}
    return out


# ------------------------------------------------------------------ signs
def test_a_pool_debit_is_a_deposit_and_a_pool_credit_is_a_withdrawal():
    m = movement.split(_cat(deposit=(0, 1000), withdrawal=(400, 0)))
    assert (m.deposits, m.withdrawals, m.net) == (D(1000), D(400), D(600))


def test_reversals_reduce_the_figure_they_reverse():
    m = movement.split(_cat(deposit=(150, 1000), withdrawal=(400, 100)))
    assert (m.deposits, m.withdrawals, m.net) == (D(850), D(300), D(550))


def test_excluded_and_unclassified_rows_never_enter_the_net():
    m = movement.split(_cat(
        deposit=(0, 100), transfer=(0, 9999), offus=(7777, 0), other_product=(0, 5555),
        pool_operations=(1234, 0), unclassified=(0, 4242, 3),
    ))
    assert m.net == D(100) and m.counted_txns == 1
    assert m.excluded_txns == 4 and m.unclassified_txns == 3


# ------------------------------------------------------------------ labels
def _category(label: str) -> str:
    desc = f"BY TRANSFER-628507091682{label}@KO 1A850001-TRANSFER FROM 4599142105216-"
    return classify(desc, debit=None, credit=None)[3]


def test_every_approved_statement_label_is_counted():
    for label in ("AEPSWDL", "RupayWDL", "Withdrawal"):
        assert _category(label) == "withdrawal", label
    for label in ("AEPSDeposit", "RupayDeposit", "Deposit", "Initial Deposit"):
        assert _category(label) == "deposit", label


def test_every_excluded_label_is_kept_out():
    for label in ("MoneyTRF", "AEPSOffUsWDL", "RupayOffUsWDL", "Loan Deposit", "SSA"):
        assert _category(label) not in ("withdrawal", "deposit"), label
    for text in ("CSH DEP (CDM) 123456", "INB EKO DEDUC 555"):
        assert classify(text, debit=None, credit=None)[3] == "pool_operations"
    for label in ("outimps", "ppf", "yono fi wdl", "yono withdrawal", "SomethingNew"):
        assert _category(label) == "unclassified", label


def test_the_statement_and_workbook_allowlists_are_separate():
    # the transaction workbook keeps its own seven-type list (it includes YONO WITHDRAWAL, which the
    # bank-statement allow-list holds out); neither list is derived from the other
    assert len(TRANSACTION_TYPE_CATEGORY) == 7
    assert "YONO WITHDRAWAL" in TRANSACTION_TYPE_CATEGORY and "yono withdrawal" not in CHANNEL_MAP
    assert set(CHANNEL_MAP) != {t.lower() for t in TRANSACTION_TYPE_CATEGORY}


# ------------------------------------------------------------------ account counts
def test_blank_zero_and_negative_counts_are_never_valid():
    t = movement.CountTimeline([(d(2026, 10, 1), 300), (d(2026, 10, 3), 0),
                                (d(2026, 10, 4), None), (d(2026, 10, 5), -5)])
    assert t.valid_on(d(2026, 10, 5)) == (300, d(2026, 10, 1))
    st = t.state_on(d(2026, 10, 5))
    assert st["count"] == 300 and st["invalid_reading_ignored"] is True and st["stale"] is True


def test_a_new_valid_count_replaces_the_old_one_from_its_own_day():
    t = movement.CountTimeline([(d(2026, 10, 1), 300), (d(2026, 10, 4), 330)])
    assert t.valid_on(d(2026, 10, 3))[0] == 300 and t.valid_on(d(2026, 10, 4))[0] == 330
    assert t.state_on(d(2026, 10, 4))["stale"] is False


def test_no_valid_count_at_all():
    t = movement.CountTimeline([(d(2026, 10, 1), 0)])
    assert t.valid_on(d(2026, 10, 9)) == (None, None) and t.state_on(d(2026, 10, 9))["stale"]


# ------------------------------------------------------------------ the roll
def _roll(movements, complete, statements, *, n=300, end=None, today=None, counts=None):
    end, today = end or d(2026, 10, 5), today or d(2026, 10, 20)
    t = counts or movement.CountTimeline([(d(2026, 10, 1), n)])
    start = movement.baseline_day(d(2026, 10, 1), D(1000), n, t)
    return start, movement.roll(previous=start, end=end, movements=movements,
                                complete_days=complete, statement_days=statements, counts=t,
                                today=today)


def test_the_receipt_day_is_the_baseline_alone():
    start, _ = _roll({d(2026, 10, 1): movement.split(_cat(deposit=(0, 99999)))}, set(), set())
    assert start.combined == D(300000) and start.per_account == D("1000.00") and start.final


def test_movement_applies_from_the_next_day_and_a_quiet_day_carries_forward():
    mv = {d(2026, 10, 2): movement.split(_cat(deposit=(0, 3000), withdrawal=(1000, 0)))}
    days = {d(2026, 10, 2) + dt.timedelta(days=i) for i in range(4)}
    _, rows = _roll(mv, days, set())
    assert [r.combined for r in rows] == [D(302000)] * 4
    assert rows[0].reason == movement.R_APPLIED and rows[1].reason == movement.R_NO_MOVEMENT
    assert all(r.final for r in rows)
    assert rows[0].per_account == D("1006.67")


def test_a_missing_statement_is_a_gap_not_proof_of_no_activity():
    mv = {d(2026, 10, 2): movement.split(_cat(deposit=(0, 3000)))}
    complete = {d(2026, 10, 2), d(2026, 10, 4), d(2026, 10, 5)}  # the 3rd has no statement
    _, rows = _roll(mv, complete, set())
    by = {r.date.day: r for r in rows}
    assert by[3].reason == movement.R_GAP and not by[3].final and by[3].provisional
    assert by[3].combined == by[2].combined
    # later days are complete but cannot be final while an earlier day of the cycle is open
    assert not by[4].final and not by[5].final
    assert movement.P_CHAIN in by[4].provisional


def test_a_partial_day_applies_what_exists_and_is_provisional():
    mv = {d(2026, 10, 2): movement.split(_cat(deposit=(0, 500)))}
    _, rows = _roll(mv, set(), {d(2026, 10, 2)})
    first = rows[0]
    assert first.combined == D(300500) and first.reason == movement.R_PARTIAL and not first.final


def test_today_is_partial_until_the_statement_is_complete():
    mv = {d(2026, 10, 5): movement.split(_cat(deposit=(0, 500)))}
    _, rows = _roll(mv, set(), {d(2026, 10, 5)}, today=d(2026, 10, 5))
    assert rows[-1].reason == movement.R_TODAY and not rows[-1].final
    assert movement.P_TODAY in rows[-1].provisional


def test_a_count_change_moves_the_divisor_never_the_combined_balance():
    t = movement.CountTimeline([(d(2026, 10, 1), 300), (d(2026, 10, 3), 330)])
    days = {d(2026, 10, 1) + dt.timedelta(days=i) for i in range(1, 5)}
    _, rows = _roll({}, days, set(), counts=t)
    assert {r.combined for r in rows} == {D(300000)}
    assert [r.per_account for r in rows] == [D("1000.00"), D("909.09"), D("909.09"), D("909.09")]


def test_prediction_for_a_refresh_uses_only_the_previous_reading_the_counts_and_movement():
    assert movement.predict_per_account(D(1000), 300, D(30000), 330) == D(1000)
    assert movement.predict_per_account(D(1000), 300, D(0), 330) == D(300000) / D(330)
