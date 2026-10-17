"""Held-out evaluation of the movement estimator against carry-forward.

The movement rule has no fitted parameters, so every refresh with enough complete statement
coverage is a genuine held-out test. These tests use SYNTHETIC worlds (clearly artificial data) to
prove the arithmetic, the leakage rules and the verdict logic; they say nothing about real SBI
accuracy."""

import datetime as dt
import decimal
import random

import pytest
from bankflow import flows, model_comparison
from bankflow.account_hypothesis import load_series, pairs_from_series, run_hypothesis_test
from bankflow.models import FlowAggregate
from csp.models import Csp, DailyBalance

D = decimal.Decimal
d = dt.date
pytestmark = pytest.mark.django_db
START = d(2026, 1, 1)


@pytest.fixture
def complete_days(monkeypatch):
    days = {d(2025, 12, 1) + dt.timedelta(days=i) for i in range(500)}
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: days)
    return days


def _flow(csp, day, deposit=0, withdrawal=0):
    """A day of statement movement. Pool debit = customer deposit, pool credit = withdrawal."""
    cats = {}
    if deposit:
        cats["deposit"] = {"count": 1, "credit": "0", "debit": str(deposit)}
    if withdrawal:
        cats["withdrawal"] = {"count": 1, "credit": str(withdrawal), "debit": "0"}
    FlowAggregate.objects.create(
        csp_id=csp, flow_date=day, resolution_mode="exact_only", txn_count=len(cats),
        credit_amount=withdrawal, debit_amount=deposit, recon_status="RECONCILED",
        by_category=cats,
    )


def _world(*, events=12, csps=4, growth=False, movement_driven=True, seed=3, n0=300,
           statements=True):
    """Synthetic: a refresh every 30 days. When `movement_driven`, each new Avg Balance is exactly
    (previous average x previous count + deposits - withdrawals) / new count, the agreed rule.
    When `growth`, the count rises at each refresh (which must only change the divisor)."""
    rng = random.Random(seed)
    codes = [f"P{i}" for i in range(csps)]
    for c in codes:
        Csp.objects.create(csp_code=c, name=c, account_count=n0)
    value = {c: D("1000") for c in codes}
    count = {c: n0 for c in codes}
    for k in range(events + 1):
        day = START + dt.timedelta(days=30 * k)
        for c in codes:
            if k > 0:
                net = D(0)
                for j in range(1, 31):
                    dep, wd = D(rng.randint(500, 4000)), D(rng.randint(0, 3000))
                    if statements:
                        _flow(c, START + dt.timedelta(days=30 * (k - 1) + j), dep, wd)
                        net += dep - wd
                new_n = count[c] + (rng.randint(5, 25) if growth else 0)
                combined = value[c] * count[c] + (net if movement_driven else 0)
                value[c], count[c] = (combined / new_n).quantize(D("0.01")), new_n
            DailyBalance.objects.create(csp_id=c, balance_date=day, daily_avg_balance=value[c],
                                        account_count=count[c], source="calling_sheet")
    return codes, START + dt.timedelta(days=30 * events)


def test_values_that_follow_the_movement_rule_validate_on_held_out_events(complete_days):
    _world()
    _, status, _, run = run_hypothesis_test()
    assert status == "VALIDATED", run.detail
    assert run.selected_method == "movement"
    assert run.parameters == {}  # nothing is fitted
    m = run.metrics["candidates"]
    assert m["movement"]["p90_ape_pct"] < 0.1  # rounding to a paisa only
    assert m["carry_forward"]["mape_pct"] > m["movement"]["mape_pct"]


def test_a_count_rise_changes_only_the_divisor_never_the_combined_balance(complete_days):
    """Money is conserved when accounts are added: the average FALLS. The movement rule predicts
    that; the old proportional rule (average rises with the count) does not, and it is not
    selectable."""
    _world(growth=True, movement_driven=False, statements=False)
    _, status, _, run = run_hypothesis_test()
    assert status == "VALIDATED" and run.selected_method == "movement"
    diag = run.metrics["account_count_rule_diagnostic"]
    assert diag["status"] != "VALIDATED"  # the retired rule fails on the same data
    pairs = pairs_from_series(load_series())
    ev = [e for e in model_comparison.evaluate(pairs) if e.usable][0]
    p = ev.pair
    assert ev.predicted == p.prev_value * p.prev_count / p.new_count < p.prev_value


def test_no_movement_signal_is_not_validated(complete_days):
    _world(movement_driven=False)  # values never respond to the statements
    _, status, _, run = run_hypothesis_test()
    assert status != "VALIDATED" and run.selected_method == "carry_forward"


def test_the_prediction_never_uses_the_reported_value(complete_days):
    _world(events=4, csps=2)
    before = {(e.pair.csp, e.pair.event_date): e.predicted
              for e in model_comparison.evaluate(pairs_from_series(load_series()))}
    DailyBalance.objects.filter(balance_date__gt=START).update(daily_avg_balance=D("1"))
    after = {(e.pair.csp, e.pair.event_date): e.predicted
             for e in model_comparison.evaluate(pairs_from_series(load_series()))}
    # every prediction after the first refresh depends on the PREVIOUS reading only; changing all
    # later readings to 1 leaves the first interval's prediction untouched
    first = min(k[1] for k in before)
    assert [v for k, v in before.items() if k[1] == first] == [
        v for k, v in after.items() if k[1] == first]


def test_a_later_outlier_does_not_change_an_earlier_verdict(complete_days):
    codes, last = _world(events=12)
    cutoff = START + dt.timedelta(days=30 * 9)
    _, early, _, _ = run_hypothesis_test(persist=False, until=cutoff)
    DailyBalance.objects.filter(csp_id=codes[0], balance_date=last).update(
        daily_avg_balance=D("999999"))
    _, again, _, _ = run_hypothesis_test(persist=False, until=cutoff)
    assert early == again


def test_insufficient_statement_coverage_excludes_pairs_and_blocks_validation(monkeypatch):
    _world(events=12)
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: set())
    metrics, status, _, _ = run_hypothesis_test(persist=False)
    assert status == "INSUFFICIENT_EVIDENCE"
    assert metrics["comparison"]["basis"]["pairs_with_flow_coverage"] == 0
    assert metrics["comparison"]["basis"]["pairs_dropped_low_coverage"] > 0


def test_a_gap_is_not_scored_as_zero_movement(monkeypatch):
    """Half the days missing -> below the 80% bar -> the pair is left out, not predicted flat."""
    _world(events=2, csps=1)
    half = {START + dt.timedelta(days=i) for i in range(0, 200, 2)}
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: half)
    ev = model_comparison.evaluate(pairs_from_series(load_series()))
    assert ev and not any(e.usable for e in ev)
    assert all(e.coverage < model_comparison.MIN_COVERAGE for e in ev)


def test_the_gate_is_never_met_by_one_event(complete_days):
    _world(events=1, csps=3)
    _, status, _, run = run_hypothesis_test()
    assert status == "INSUFFICIENT_EVIDENCE" and run.selected_method == "carry_forward"
