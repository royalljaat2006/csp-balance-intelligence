"""Four-way estimator comparison: carry-forward, growth-only, flow-only, growth-and-flow.
Time-ordered validation, no leakage, no double counting, honest coverage rules."""

import datetime as dt
import decimal
import random

import pytest
from bankflow import estimates, flows, model_comparison
from bankflow.account_hypothesis import load_series, pairs_from_series, run_hypothesis_test
from bankflow.models import DailyEstimate, FlowAggregate
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


def _flow(csp, day, deposit):
    """A day of customer-deposit flow: customer-basis signed flow = debit - credit = +deposit."""
    FlowAggregate.objects.create(
        csp_id=csp, flow_date=day, resolution_mode="exact_only", txn_count=1,
        credit_amount=0, debit_amount=deposit, recon_status="RECONCILED",
        by_category={"deposit": {"count": 1, "credit": "0", "debit": str(deposit)}},
    )


def _world(*, events=12, csps=4, beta=2.0, growth=False, seed=3, n0=300, flow_driven=True):
    """Refresh every 30 days. Either value moves by beta * (flow per account) over the interval
    (flow_driven) and/or by the account-count ratio (growth)."""
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
                total = D(0)
                for j in range(1, 31):
                    amt = D(rng.randint(500, 4000))
                    _flow(c, START + dt.timedelta(days=30 * (k - 1) + j), amt)
                    total += amt
                new_n = count[c] + (rng.randint(5, 25) if growth else 0)
                new_v = value[c] * D(new_n) / D(count[c]) if growth else value[c]
                if flow_driven:
                    new_v += D(str(beta)) * total / D(new_n)
                value[c], count[c] = new_v.quantize(D("0.01")), new_n
            DailyBalance.objects.create(csp_id=c, balance_date=day, daily_avg_balance=value[c],
                                        account_count=count[c], source="calling_sheet")
    return codes, START + dt.timedelta(days=30 * events)


def test_flow_driven_values_validate_a_flow_model_on_held_out_events(complete_days):
    _world(beta=2.0)
    _, status, _, run = run_hypothesis_test()
    assert status == "VALIDATED", run.metrics["comparison"]["methods"]
    assert run.selected_method in ("flow_only", "growth_and_flow")
    assert abs(run.parameters["beta"] - 2.0) < 1e-6  # recovered from training data only
    flow = run.metrics["comparison"]["methods"]["flow_only"]["metrics"]
    assert flow["n_events"] >= 8 and flow["n_pairs"] >= 30 and flow["n_cycles"] >= 6
    assert flow["candidates"]["flow_only"]["mae"] < flow["candidates"]["carry_forward"]["mae"]


def test_growth_driven_values_do_not_double_count_through_the_flow_term(complete_days):
    _world(beta=0.0, growth=True, flow_driven=False)
    _, _, _, run = run_hypothesis_test()
    both = run.metrics["comparison"]["methods"]["growth_and_flow"]
    # the flow coefficient is fitted on what growth leaves unexplained (only cent rounding), so
    # combined ~ growth: the growth effect is not added a second time through the flow term
    assert abs(both["parameters"]["beta"]) < 1e-3
    c = both["metrics"]["candidates"]
    assert c["growth_and_flow"]["mae"] == pytest.approx(0.0, abs=0.05)  # cent rounding only
    assert run.metrics["comparison"]["methods"]["flow_only"]["metrics"]["candidates"][
        "flow_only"]["mae"] > 1.0  # flows alone cannot explain a pure account-count effect


def test_no_signal_is_not_validated(complete_days):
    _world(beta=0.0, flow_driven=False, growth=False)
    _, status, _, run = run_hypothesis_test()
    assert status != "VALIDATED" and run.selected_method == "carry_forward"


def test_training_never_uses_pairs_from_after_the_prediction_origin(complete_days, monkeypatch):
    _world(beta=2.0)
    pairs = pairs_from_series(load_series())
    seen = []
    real = model_comparison._fit_params

    def spy(train):
        seen.append(max(r.pair.event_date for r in train))
        return real(train)

    monkeypatch.setattr(model_comparison, "_fit_params", spy)
    model_comparison.compare(pairs)
    events = sorted({p.event_date for p in pairs})
    origins = [min(p.prev_date for p in pairs if p.event_date == e) for e in events[3:]]
    held_fits = seen[:-1]  # the last call is the production fit on everything
    assert len(held_fits) == len(origins)
    assert all(trained <= origin for trained, origin in zip(held_fits, origins, strict=True))


def test_future_outlier_does_not_change_earlier_validation(complete_days):
    _world(beta=2.0, events=12)
    pairs = pairs_from_series(load_series())
    early = [p for p in pairs if p.event_date <= START + dt.timedelta(days=30 * 9)]
    a = model_comparison.compare(early)["methods"]["flow_only"]["metrics"]
    # corrupt only the newest value; the metrics on the early events must be unaffected
    DailyBalance.objects.filter(balance_date=START + dt.timedelta(days=360)).update(
        daily_avg_balance=D("999999"))
    pairs2 = pairs_from_series(load_series())
    early2 = [p for p in pairs2 if p.event_date <= START + dt.timedelta(days=30 * 9)]
    b = model_comparison.compare(early2)["methods"]["flow_only"]["metrics"]
    assert a["candidates"] == b["candidates"]


def test_insufficient_transaction_coverage_excludes_pairs_and_blocks_validation(monkeypatch):
    _world(beta=2.0)
    only = {START + dt.timedelta(days=i) for i in range(0, 360, 4)}  # a quarter of the days
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: only)
    _, status, _, run = run_hypothesis_test()
    basis = run.metrics["comparison"]["basis"]
    assert basis["pairs_with_flow_coverage"] == 0 and basis["pairs_dropped_low_coverage"] > 0
    assert run.metrics["comparison"]["methods"]["flow_only"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert status != "VALIDATED" and run.selected_method == "carry_forward"


def test_estimate_uses_selected_flow_model_and_reports_measured_error(complete_days):
    codes, last = _world(beta=2.0)
    _, status, _, run = run_hypothesis_test()
    assert status == "VALIDATED"
    extra = [D(1000), D(2000), D(3000)]
    for i, amt in enumerate(extra, start=1):
        _flow("P0", last + dt.timedelta(days=i), amt)
    est_day = last + dt.timedelta(days=3)
    DailyBalance.objects.create(csp_id="P0", balance_date=est_day,
                                daily_avg_balance=DailyBalance.objects.get(
                                    csp_id="P0", balance_date=last).daily_avg_balance,
                                account_count=300, source="calling_sheet")
    estimates.compute_daily_estimates(est_day)
    e = DailyEstimate.objects.get(csp_id="P0", estimate_date=est_day)
    assert e.status == "VALIDATED" and e.method == run.selected_method
    assert e.expected_error_pct is not None and "held-out" in e.error_basis
    expected = e.baseline_value + D(str(run.parameters["beta"])) * sum(extra) / D(300)
    assert abs(e.estimated_value - expected) < D("0.02")
    assert e.flow_net == sum(extra)  # each flow counted once, however the count moved


def test_flow_model_without_flow_coverage_falls_back_with_a_reason(complete_days, monkeypatch):
    codes, last = _world(beta=2.0)
    run_hypothesis_test()
    est_day = last + dt.timedelta(days=3)
    DailyBalance.objects.create(csp_id="P1", balance_date=est_day, daily_avg_balance=D("1234"),
                                account_count=300, source="calling_sheet")
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: set())  # no proven days
    estimates.compute_daily_estimates(est_day)
    e = DailyEstimate.objects.get(csp_id="P1", estimate_date=est_day)
    assert e.reason_code == "INSUFFICIENT_FLOW_COVERAGE" and e.method == "carry_forward"
    assert e.status == "INSUFFICIENT_EVIDENCE" and e.estimated_value == e.baseline_value


def test_pmjdy_vs_calling_sheet_count_mismatch_is_not_treated_as_growth():
    from bankflow import pmjdy

    Csp.objects.create(csp_code="M", name="M", account_count=300)
    DailyBalance.objects.create(csp_id="M", balance_date=d(2026, 9, 1),
                                daily_avg_balance=D("1000"), account_count=300,
                                source="calling_sheet")
    DailyBalance.objects.create(csp_id="M", balance_date=d(2026, 10, 13),
                                daily_avg_balance=D("1000"), account_count=300,
                                source="calling_sheet")
    pmjdy.capture_counts({"M": 360})  # PMJDY says 360 now, but has no reading at the baseline date
    estimates.compute_daily_estimates(d(2026, 10, 13))
    e = DailyEstimate.objects.get(csp_id="M", estimate_date=d(2026, 10, 13))
    assert e.reason_code == "COUNT_SOURCE_MISMATCH" and e.estimated_value == e.baseline_value
    assert Csp.objects.get(csp_code="M").account_count == 300  # master data untouched
