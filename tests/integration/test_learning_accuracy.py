"""
The accuracy engine: error metrics, who is counted, and when it refuses to claim anything.

SYNTHETIC outcomes (invented CSPs, balances and errors). The point is that the arithmetic is right
and that too little evidence yields INSUFFICIENT_EVIDENCE, not that any figure here is real.
"""

import datetime as dt

import pytest
from learning import accuracy
from learning.models import PredictionOutcome

from tests.learning_helpers import D, d, make_csp, stored_outcome, stored_prediction, synth

pytestmark = pytest.mark.django_db


def row(error, actual=1000, *, horizon=1, quality="OK", event=None, csp="1A850001",
        count=300, changed=False, model="movement"):
    event = event or d(2026, 3, 10)
    return accuracy.Row(
        csp=csp, model=model, estimate_date=event - dt.timedelta(days=horizon), event_date=event,
        horizon=horizon, predicted=D(actual) + D(error), actual=D(actual),
        carry_forward=D(actual) - D(50), error=D(error), quality=quality, account_count=count,
        count_changed=changed)


# ------------------------------------------------------------------ the arithmetic
def test_error_metrics_are_computed_from_signed_errors():
    rows = [row(10), row(-10), row(30), row(10, actual=40)]
    m = accuracy.error_metrics(rows)
    assert m["n"] == 4
    assert m["mae"] == pytest.approx(15.0)
    assert m["rmse"] == pytest.approx(300 ** 0.5)
    assert m["bias"] == pytest.approx(10.0)  # positive: the estimates were too high on average
    assert (m["over"], m["under"], m["exact"]) == (3, 1, 0)
    assert m["median_abs_error"] == pytest.approx(10.0)


def test_a_percentage_is_given_only_where_the_sbi_value_is_large_enough():
    m = accuracy.error_metrics([row(10), row(-10), row(30), row(10, actual=40)])
    assert m["mape_n"] == 3 and m["mape_excluded_small_actual"] == 1
    assert m["mape_pct"] == pytest.approx((1.0 + 1.0 + 3.0) / 3)  # 10/1000, 10/1000, 30/1000


def test_no_rows_give_no_metrics():
    assert accuracy.error_metrics([]) == {"n": 0}


def test_systematic_over_and_under_estimation_shows_in_the_bias_sign():
    assert accuracy.error_metrics([row(20), row(30), row(10)])["bias"] > 0
    assert accuracy.error_metrics([row(-20), row(-30), row(-10)])["bias"] < 0


def test_an_estimate_that_beats_carry_forward_is_marked_so():
    # carry-forward is off by 50 in these rows
    assert accuracy.error_metrics([row(5), row(-5)])["beats_carry_forward"] is True
    assert accuracy.error_metrics([row(80), row(-80)])["beats_carry_forward"] is False


# ------------------------------------------------------------------ who is counted
def test_data_problems_are_counted_apart_and_never_measure_the_model():
    rows = [row(5, quality="OK"), row(900, quality="STATEMENT_GAP"),
            row(900, quality="PROVISIONAL_DATA")]
    head = accuracy.headline(rows)
    assert len(head) == 1 and head[0].error == D("5")
    dq = accuracy.data_quality(rows)
    assert dq["total"] == 3 and dq["ok"] == 1
    assert dq["problems"] == {"PROVISIONAL_DATA": 1, "STATEMENT_GAP": 1}
    assert dq["problem_share"] == pytest.approx(2 / 3)


def test_the_headline_takes_the_prediction_closest_to_the_refresh():
    rows = [row(40, horizon=5), row(25, horizon=2), row(10, horizon=1),
            row(7, horizon=1, csp="1A850002")]
    head = accuracy.headline(rows)
    assert sorted((r.csp, r.horizon) for r in head) == [("1A850001", 1), ("1A850002", 1)]
    horizons = {h["horizon"]: h["n"] for h in accuracy.by_horizon(rows)}
    assert horizons == {"1 day": 2, "2 to 3 days": 1, "4 to 7 days": 1}  # every day, by horizon


def test_one_csp_is_not_counted_once_per_day_it_was_predicted(db):
    make_csp()
    made = []
    for n in range(2, 7):
        p = stored_prediction(estimate=d(2026, 1, n), first_seen=d(2026, 1, 1),
                              predicted="1010")
        made.append(stored_outcome(p, 1020, event=d(2026, 1, 8)))
    report = accuracy.accuracy_report()
    assert report["evaluated"] == 5 and report["headline_outcomes"] == 1


def test_rows_are_taken_in_date_order_whatever_order_they_were_saved_in():
    make_csp()
    late = stored_prediction(estimate=d(2026, 5, 4), first_seen=d(2026, 5, 1))
    early = stored_prediction(estimate=d(2026, 2, 4), first_seen=d(2026, 2, 1))
    stored_outcome(late, 1010, event=d(2026, 5, 9))
    stored_outcome(early, 1010, event=d(2026, 2, 9))
    rows, dropped = accuracy.load()
    assert [r.event_date for r in rows] == [d(2026, 2, 9), d(2026, 5, 9)] and dropped == 0


def test_a_row_whose_prediction_is_not_earlier_than_its_outcome_is_dropped_and_counted():
    make_csp()
    p = stored_prediction(estimate=d(2026, 2, 9), first_seen=d(2026, 2, 1))
    stored_outcome(p, 1010, event=d(2026, 2, 9))  # same day: could have seen the value
    rows, dropped = accuracy.load()
    assert rows == [] and dropped == 1
    assert accuracy.accuracy_report()["leakage_excluded"] == 1


# ------------------------------------------------------------------ refusing to claim
def test_too_few_outcomes_is_insufficient_evidence_and_gives_no_accuracy_figure():
    synth(months=1, csps=2)
    report = accuracy.accuracy_report()
    assert report["accuracy_claim"] == "INSUFFICIENT_EVIDENCE"
    assert report["metrics"] is None
    assert report["evidence"]["n_outcomes"] == 2
    for table in ("by_horizon", "by_cohort", "by_csp", "by_model"):
        for entry in report[table]:
            assert entry["status"] == "INSUFFICIENT_EVIDENCE" and "mae" not in entry
            assert entry["missing"]
    assert all(t["mean_signed_error"] is None for t in report["trend"])


def test_nothing_scored_is_insufficient_evidence_not_zero_error():
    report = accuracy.accuracy_report()
    assert report["accuracy_claim"] == "INSUFFICIENT_EVIDENCE" and report["metrics"] is None
    assert report["evaluated"] == 0 and report["data_quality"]["problem_share"] is None


def test_many_outcomes_in_one_month_are_still_insufficient():
    """Hundreds of CSPs refreshing together are one cycle, not hundreds of independent tests."""
    for c in range(1, 40):
        code = f"1A85{c:04d}"
        make_csp(code)
        p = stored_prediction(code, estimate=d(2026, 3, 2), first_seen=d(2026, 3, 1))
        stored_outcome(p, 1010, event=d(2026, 3, 9))
    report = accuracy.accuracy_report()
    assert report["headline_outcomes"] == 39
    assert report["accuracy_claim"] == "INSUFFICIENT_EVIDENCE" and report["metrics"] is None
    assert report["evidence"]["n_events"] == 1 and report["evidence"]["n_cycles"] == 1


def test_enough_independent_evidence_allows_a_claim_by_the_preregistered_criteria():
    synth(months=8, csps=4)
    report = accuracy.accuracy_report()
    ev = report["evidence"]
    assert ev["n_outcomes"] == 32 and ev["n_events"] == 32 and ev["n_cycles"] == 8
    assert report["accuracy_claim"] == "VALIDATED"
    m = report["metrics"]
    assert m["n"] == 32 and m["mae"] == pytest.approx(5.0) and m["carry_forward_mae"] == 50.0
    assert m["beats_carry_forward"] is True and abs(m["bias"]) < 5
    assert report["data_quality"]["ok"] == 32


def test_an_estimate_no_better_than_carry_forward_is_not_supported_even_with_enough_data():
    synth(months=8, csps=4, model_error=50, carry_gap=50)
    report = accuracy.accuracy_report()
    assert report["accuracy_claim"] == "NOT_SUPPORTED"
    assert report["metrics"]["beats_carry_forward"] is False


def test_per_horizon_cohort_csp_and_model_slices_each_say_whether_they_have_enough():
    synth(months=8, csps=4)
    report = accuracy.accuracy_report()
    assert report["by_horizon"][0]["status"] in ("SUPPORTED", "INSUFFICIENT_EVIDENCE")
    cohorts = {c["cohort"]: c for c in report["by_cohort"]}
    assert cohorts["200 to 499 accounts"]["status"] == "SUPPORTED"
    assert cohorts["Account count unchanged"]["n"] == 32
    csp = {c["csp_code"]: c for c in report["by_csp"]}
    assert len(csp) == 4 and all(c["status"] == "SUPPORTED" for c in csp.values())  # 8 each
    assert [m["model"] for m in report["by_model"]] == ["movement"]
    assert report["by_model"][0]["n"] == 32


def test_the_trend_counts_over_and_under_estimates_per_month():
    synth(months=8, csps=4)
    trend = accuracy.accuracy_report()["trend"]
    assert [t["month"] for t in trend] == [f"2026-0{m}" for m in range(1, 9)]
    assert all(t["over"] + t["under"] == t["n"] == 4 for t in trend)
    assert all(t["mean_signed_error"] is None for t in trend)  # 4 outcomes: counts only


def test_filtering_by_model_and_since_date():
    synth(months=2, csps=2)
    only_later = accuracy.accuracy_report(since=d(2026, 2, 1))
    assert only_later["evaluated"] == 2
    assert accuracy.accuracy_report(model="no_such_model")["evaluated"] == 0


def test_outcomes_table_keeps_the_estimate_and_the_observed_value_separate():
    made = synth(months=1, csps=1)
    o = PredictionOutcome.objects.get(pk=made[0].pk)
    assert o.actual_value == D("1050") and o.prediction.predicted_value in (D("1055"), D("1045"))
    assert o.prediction.carry_forward_value == D("1000")


# ------------------------------------------------------------------ plain-language verdicts
def test_the_verdict_is_explained_in_everyday_words_with_the_real_counts():
    nothing = accuracy.accuracy_report()["evidence"]["plain"]
    assert "Nothing has been checked" in nothing and "10 checks over 3 SBI refresh days" in nothing
    synth(months=1, csps=2)
    few = accuracy.accuracy_report()["evidence"]["plain"]
    assert few.startswith("Only 2 checks over 2 SBI refresh days in 1 month so far.")
    assert "30 checks over 8 refresh days and 6 months" in few
    for jargon in ("pairs", "CI", "MAE", "cycle"):
        assert jargon not in few


def test_a_proven_verdict_names_how_often_the_estimate_was_closer():
    synth(months=8, csps=4)
    plain = accuracy.accuracy_report()["evidence"]["plain"]
    assert "closer to SBI's figure than carry-forward on 32 of 32 refresh days" in plain
    assert "errors about" in plain and "smaller" in plain and "meets the evidence bar" in plain


def test_a_not_better_verdict_says_so_plainly():
    synth(months=8, csps=4, model_error=50, carry_gap=50)
    plain = accuracy.accuracy_report()["evidence"]["plain"]
    assert "not reliably better than carry-forward" in plain
