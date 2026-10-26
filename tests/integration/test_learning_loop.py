"""
The learning loop's records: frozen predictions, their matching to the SBI value that followed,
the data-quality labels, and the guarantee that history is never rewritten.

SYNTHETIC data throughout (invented CSPs and amounts); these tests show the rules behave as
designed, not that the estimator is accurate against real SBI figures.
"""


import pytest
from bankflow import estimates, flows
from bankflow.models import AccountCountObservation, DailyEstimate, UnresolvedFlowDaily
from csp.models import DailyBalance, MonthlySummary
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from learning import outcomes, registry
from learning.models import ImmutableRecord, LivePrediction, PredictionOutcome
from learning.predictions import freeze_predictions

from tests.learning_helpers import (
    D,
    balances,
    day,
    flow,
    make_csp,
    stored_outcome,
    stored_prediction,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def complete(monkeypatch):
    days = {day(n) for n in range(1, 21)}
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: days)
    return days


def _first_cycle(code="1A850001", deposit=3000):
    """Days 1-6 at 1000 with a deposit on day 3, estimated and frozen BEFORE any refresh exists."""
    make_csp(code)
    balances(code, 1, 6, 1000)
    flow(code, day(3), deposit=deposit)
    estimates.compute_daily_estimates(day(6))
    return freeze_predictions(today=day(6))


def _refresh(code, first, last, value, n=300):
    balances(code, first, last, value, n)


# ------------------------------------------------------------------ what a frozen prediction holds
def test_a_prediction_holds_every_input_that_produced_it(complete):
    frozen = _first_cycle()
    assert frozen["created"] == 6 and frozen["skipped_provisional"] == 0
    p = LivePrediction.objects.get(csp_id="1A850001", estimate_date=day(5))
    assert p.model.name == "movement" and p.horizon == "next_sbi_refresh"
    assert (p.period_start, p.period_end) == (day(1), day(5))
    assert p.baseline_value == D("1000") and p.baseline_first_seen == day(1)
    assert p.baseline_account_count == 300 and p.account_count == 300
    assert (p.flow_deposits, p.flow_withdrawals, p.net_customer_movement) == (
        D("3000"), D("0"), D("3000"))
    assert p.predicted_value == D("1010.00") and p.carry_forward_value == D("1000")
    assert p.predicted_combined == D("303000.00")
    assert p.statement_state == "complete" and p.flow_coverage == 1.0
    assert p.confidence_status == "INSUFFICIENT_EVIDENCE" and p.confidence_reason
    assert "movement" in p.methodology.lower() or "growth" in p.methodology.lower()
    assert p.inputs["model"]["name"] == "movement" and p.created_at is not None


def test_the_baseline_day_prediction_is_the_baseline_and_no_snapshot_is_invented(complete):
    _first_cycle()
    p = LivePrediction.objects.get(estimate_date=day(1))
    assert p.predicted_value == p.baseline_value == D("1000.00")
    assert p.pmjdy_account_count is None and p.pmjdy_snapshot_at is None  # none was observed


def test_a_pmjdy_snapshot_is_recorded_only_when_one_exists(complete):
    make_csp()
    balances("1A850001", 1, 3, 1000)
    seen = timezone.now()
    AccountCountObservation.objects.create(csp_id="1A850001", source="pmjdy", account_count=312,
                                           observed_at=seen, snapshot_date=timezone.localdate())
    estimates.compute_daily_estimates(day(3))
    freeze_predictions(today=day(3))
    p = LivePrediction.objects.get(estimate_date=day(2))
    assert p.pmjdy_account_count == 312 and p.pmjdy_snapshot_at == seen
    assert p.account_count == 300  # the Calling Sheet count the estimator used stays separate


def test_only_final_estimates_are_frozen_unless_provisional_is_asked_for(monkeypatch):
    make_csp()
    balances("1A850001", 1, 6, 1000)
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: {day(1), day(2)})
    estimates.compute_daily_estimates(day(6))
    first = freeze_predictions(today=day(6))
    assert first["created"] == 2 and first["skipped_provisional"] == 4
    both = freeze_predictions(today=day(6), include_provisional=True)
    assert both["created"] == 4 and both["already_frozen"] == 2
    states = set(LivePrediction.objects.values_list("statement_state", flat=True))
    assert states == {"complete", "provisional"}


def test_freezing_is_idempotent_and_survives_a_changed_working_row(complete):
    _first_cycle()
    before = list(LivePrediction.objects.order_by("id").values())
    assert freeze_predictions(today=day(6))["created"] == 0
    DailyEstimate.objects.filter(estimate_date=day(5)).update(estimated_value=D("9999"))
    assert freeze_predictions(today=day(6))["created"] == 0
    assert list(LivePrediction.objects.order_by("id").values()) == before


def test_unavailable_estimates_are_not_frozen(complete):
    make_csp("1A850009", n=150)  # below the 200-account gate: no estimate value
    balances("1A850009", 1, 4, 1000, n=150)
    estimates.compute_daily_estimates(day(4))
    r = freeze_predictions(today=day(4))
    assert r["created"] == 0 and r["skipped_unavailable"] == 4


# ------------------------------------------------------------------ matching to the SBI value
def test_a_prediction_is_scored_against_the_next_sbi_value_of_its_own_csp(complete):
    _first_cycle()
    assert outcomes.record_outcomes()["waiting_for_next_refresh"] == 6  # no refresh yet
    assert PredictionOutcome.objects.count() == 0
    _refresh("1A850001", 7, 12, 1020)
    r = outcomes.record_outcomes()
    assert r["scored"] == 6 and r["by_quality"] == {"OK": 6}
    o = PredictionOutcome.objects.get(prediction__estimate_date=day(5))
    assert o.actual_value == D("1020") and o.actual_first_seen == day(7)
    assert o.horizon_days == 2
    assert o.error_amount == D("-10.00") and o.direction == "UNDER"  # estimate minus SBI value
    assert o.abs_error == D("10.00") and round(o.pct_error, 4) == round(10 / 1020 * 100, 4)
    assert o.carry_forward_error == D("-20.00")
    first = PredictionOutcome.objects.get(prediction__estimate_date=day(1))
    assert first.horizon_days == 6 and first.error_amount == D("-20.00")
    p = LivePrediction.objects.get(pk=o.prediction_id)
    assert p.predicted_value == D("1010.00") and p.baseline_value == D("1000.00")  # untouched


def test_an_over_estimate_is_labelled_as_such(complete):
    _first_cycle(deposit=30000)  # predicts 1100
    _refresh("1A850001", 7, 12, 1020)
    outcomes.record_outcomes()
    o = PredictionOutcome.objects.get(prediction__estimate_date=day(5))
    assert o.error_amount == D("80.00") and o.direction == "OVER"


def test_recording_twice_scores_nothing_new(complete):
    _first_cycle()
    _refresh("1A850001", 7, 12, 1020)
    outcomes.record_outcomes()
    ids = list(PredictionOutcome.objects.order_by("id").values_list("id", flat=True))
    assert outcomes.record_outcomes()["pending"] == 0
    assert list(PredictionOutcome.objects.order_by("id").values_list("id", flat=True)) == ids


def test_sbi_updates_arriving_on_different_days_for_different_csps(complete):
    for code in ("1A850001", "1A850002", "1A850003"):
        make_csp(code)
        balances(code, 1, 6, 1000)
    estimates.compute_daily_estimates(day(6))
    freeze_predictions(today=day(6))
    _refresh("1A850001", 7, 12, 1050)      # refreshes on day 7
    balances("1A850002", 7, 8, 1000)
    _refresh("1A850002", 9, 12, 1080)      # refreshes on day 9
    balances("1A850003", 7, 12, 1000)      # never refreshes
    r = outcomes.record_outcomes()
    assert r["scored"] == 12 and r["waiting_for_next_refresh"] == 6
    a = PredictionOutcome.objects.filter(prediction__csp_id="1A850001")
    b = PredictionOutcome.objects.filter(prediction__csp_id="1A850002")
    assert {(o.actual_value, o.actual_first_seen) for o in a} == {(D("1050"), day(7))}
    assert {(o.actual_value, o.actual_first_seen) for o in b} == {(D("1080"), day(9))}
    assert not PredictionOutcome.objects.filter(prediction__csp_id="1A850003").exists()
    # CSP 3 is scored later, on its own date, without touching the others
    _refresh("1A850003", 13, 14, 1010)
    assert outcomes.record_outcomes()["scored"] == 6
    c = PredictionOutcome.objects.filter(prediction__csp_id="1A850003").first()
    assert c.actual_first_seen == day(13) and c.horizon_days >= 7
    assert a.count() == 6 and b.count() == 6


def test_a_value_that_returns_to_an_earlier_one_is_the_next_refresh_not_the_baseline(complete):
    make_csp()
    balances("1A850001", 1, 4, 1000)
    estimates.compute_daily_estimates(day(4))
    freeze_predictions(today=day(4))
    _refresh("1A850001", 5, 6, 1200)
    _refresh("1A850001", 7, 9, 1000)  # back to the first value
    outcomes.record_outcomes()
    assert {o.actual_value for o in PredictionOutcome.objects.all()} == {D("1200")}


# ------------------------------------------------------------------ no leakage
def test_a_prediction_made_on_or_after_the_refresh_day_is_never_scored(complete):
    make_csp()
    balances("1A850001", 1, 4, 1000)
    balances("1A850001", 5, 8, 1100)  # the new value is first seen on day 5
    same_day = stored_prediction(estimate=day(5), first_seen=day(1), baseline="1000",
                                 predicted="1100")
    after = stored_prediction(estimate=day(7), first_seen=day(1), baseline="1000",
                              predicted="1100")
    before = stored_prediction(estimate=day(4), first_seen=day(1), baseline="1000",
                               predicted="1010")
    r = outcomes.record_outcomes()
    assert r["scored"] == 1 and r["not_before_outcome"] == 2
    assert PredictionOutcome.objects.get().prediction_id == before.pk
    assert not PredictionOutcome.objects.filter(prediction__in=[same_day, after]).exists()


def test_a_prediction_whose_baseline_is_not_in_the_csp_history_is_left_unmatched():
    make_csp()
    balances("1A850001", 1, 4, 1000)
    balances("1A850001", 5, 8, 1100)
    stored_prediction(estimate=day(3), first_seen=day(1), baseline="777", predicted="777")
    stored_prediction(estimate=day(4), first_seen=day(2), baseline="1000", predicted="1000")
    r = outcomes.record_outcomes()
    assert r["unmatched_baseline"] == 2 and r["scored"] == 0


def test_the_prediction_never_sees_the_value_it_is_scored_on(complete):
    _first_cycle()
    snap = list(LivePrediction.objects.order_by("id").values())
    _refresh("1A850001", 7, 12, 4242)  # a wildly different SBI value
    outcomes.record_outcomes()
    assert list(LivePrediction.objects.order_by("id").values()) == snap
    assert all(p.predicted_value < D("1100") for p in LivePrediction.objects.all())


# ------------------------------------------------------------------ data problems vs model error
def test_good_data_is_ok(complete):
    _first_cycle()
    _refresh("1A850001", 7, 12, 1020)
    outcomes.record_outcomes()
    o = PredictionOutcome.objects.get(prediction__estimate_date=day(5))
    assert o.quality == "OK" and o.quality_detail["interval_statement_coverage"] == 1.0


def test_missing_statements_are_a_data_problem_not_a_model_error(monkeypatch):
    monkeypatch.setattr(flows, "proven_complete_dates", lambda: {day(1), day(2), day(3)})
    make_csp()
    balances("1A850001", 1, 6, 1000)
    flow("1A850001", day(3), deposit=3000)
    estimates.compute_daily_estimates(day(6))
    freeze_predictions(today=day(6))  # only the three complete days are final
    _refresh("1A850001", 7, 12, 1020)
    outcomes.record_outcomes()
    o = PredictionOutcome.objects.first()
    assert o.quality == "STATEMENT_GAP" and "STATEMENT_GAP" in o.quality_detail["flags"]
    assert o.quality_detail["interval_statement_coverage"] < 0.8


def test_many_unmatched_codes_are_flagged(complete):
    make_csp()
    balances("1A850001", 1, 6, 1000)
    flow("1A850001", day(3), deposit=3000)
    estimates.compute_daily_estimates(day(6))
    freeze_predictions(today=day(6))
    _refresh("1A850001", 7, 12, 1020)
    UnresolvedFlowDaily.objects.create(flow_date=day(4), status="unmatched", txn_count=3,
                                       credit_amount=D("2000"), debit_amount=D("0"))
    outcomes.record_outcomes()
    o = PredictionOutcome.objects.get(prediction__estimate_date=day(5))
    assert o.quality == "UNMATCHED_CODES"
    assert o.quality_detail["unmatched_code_share"] == pytest.approx(0.4)


def test_missing_codes_and_pool_operations_do_not_count_as_unmatched(complete):
    make_csp()
    balances("1A850001", 1, 6, 1000)
    flow("1A850001", day(3), deposit=3000)
    estimates.compute_daily_estimates(day(6))
    freeze_predictions(today=day(6))
    _refresh("1A850001", 7, 12, 1020)
    for status in ("missing", "pool_operations_no_csp"):
        UnresolvedFlowDaily.objects.create(flow_date=day(4), status=status, txn_count=3,
                                           credit_amount=D("9000"), debit_amount=D("0"))
    outcomes.record_outcomes()
    assert PredictionOutcome.objects.get(prediction__estimate_date=day(5)).quality == "OK"


@pytest.mark.parametrize("kwargs,expected", [
    ({"state": "provisional"}, "PROVISIONAL_DATA"),
    ({"count_stale": True}, "COUNT_UNRELIABLE"),
    ({"stale": True}, "STALE_SOURCE"),
])
def test_other_data_problems_are_labelled(kwargs, expected):
    make_csp()
    balances("1A850001", 1, 4, 1000)
    balances("1A850001", 5, 8, 1100)
    p = stored_prediction(estimate=day(3), first_seen=day(1), baseline="1000", predicted="1010",
                          **kwargs)
    ctx = outcomes._Context(day(1), day(5))
    ctx.complete = [day(k) for k in range(1, 6)]
    o = outcomes.score(p, D("1100"), day(5), 300, ctx)
    assert o.quality == expected


def test_when_several_problems_apply_the_gap_names_the_outcome():
    make_csp()
    p = stored_prediction(estimate=day(3), state="provisional", stale=True, count_stale=True,
                          first_seen=day(1))
    ctx = outcomes._Context(day(1), day(5))
    ctx.complete = []
    o = outcomes.score(p, D("1100"), day(5), 300, ctx)
    assert o.quality == "STATEMENT_GAP"
    assert set(o.quality_detail["flags"]) == {
        "STATEMENT_GAP", "PROVISIONAL_DATA", "COUNT_UNRELIABLE", "STALE_SOURCE"}


def test_a_zero_sbi_value_has_no_percentage_error():
    make_csp()
    p = stored_prediction(estimate=day(3), first_seen=day(1))
    o = stored_outcome(p, 0, event=day(5))
    assert o.pct_error is None and o.error_amount == D("1000.00") and o.direction == "OVER"


# ------------------------------------------------------------------ history is never rewritten
def test_a_saved_prediction_cannot_be_changed_or_removed(complete):
    _first_cycle()
    p = LivePrediction.objects.first()
    p.predicted_value = D("1")
    with pytest.raises(ImmutableRecord):
        p.save()
    with pytest.raises(ImmutableRecord):
        p.delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        LivePrediction.objects.filter(pk=p.pk).update(predicted_value=D("1"))
    with pytest.raises(DatabaseError), transaction.atomic():
        LivePrediction.objects.filter(pk=p.pk).delete()
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        cur.execute("UPDATE learning_liveprediction SET predicted_value = 1")
    assert LivePrediction.objects.get(pk=p.pk).predicted_value != D("1")


def test_a_saved_outcome_cannot_be_changed_or_removed(complete):
    _first_cycle()
    _refresh("1A850001", 7, 12, 1020)
    outcomes.record_outcomes()
    o = PredictionOutcome.objects.first()
    o.actual_value = D("5")
    with pytest.raises(ImmutableRecord):
        o.save()
    with pytest.raises(ImmutableRecord):
        o.delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        PredictionOutcome.objects.filter(pk=o.pk).update(actual_value=D("5"))


def test_a_later_restatement_does_not_rewrite_an_earlier_outcome(complete):
    _first_cycle()
    _refresh("1A850001", 7, 12, 1020)
    outcomes.record_outcomes()
    before = list(PredictionOutcome.objects.order_by("id").values())
    DailyBalance.objects.filter(balance_date__gte=day(7)).update(daily_avg_balance=D("1500"))
    outcomes.record_outcomes()
    assert list(PredictionOutcome.objects.order_by("id").values()) == before


# ------------------------------------------------------------------ production tables untouched
def test_the_loop_never_writes_production_or_estimate_tables(complete):
    csp = make_csp()
    balances("1A850001", 1, 6, 1000)
    flow("1A850001", day(3), deposit=3000)
    MonthlySummary.objects.create(csp=csp, month="2026-10", days_in_month=31, days_with_data=6,
                                  mtd_mab=D("1000"), projected_mab=D("1000"), slab="S1")
    estimates.compute_daily_estimates(day(6))
    _refresh("1A850001", 7, 12, 1020)
    daily = list(DailyBalance.objects.order_by("id").values_list(
        "id", "csp_id", "balance_date", "daily_avg_balance", "account_count", "source"))
    monthly = list(MonthlySummary.objects.values_list("id", "mtd_mab", "projected_mab", "slab"))
    est = list(DailyEstimate.objects.order_by("id").values_list(
        "id", "estimated_value", "status", "is_final"))
    freeze_predictions(today=day(12))
    outcomes.record_outcomes()
    assert daily == list(DailyBalance.objects.order_by("id").values_list(
        "id", "csp_id", "balance_date", "daily_avg_balance", "account_count", "source"))
    assert monthly == list(MonthlySummary.objects.values_list(
        "id", "mtd_mab", "projected_mab", "slab"))
    assert est == list(DailyEstimate.objects.order_by("id").values_list(
        "id", "estimated_value", "status", "is_final"))


def test_the_learning_modules_contain_no_production_writes():
    import inspect

    from learning import accuracy, candidates, feedback, overview, predictions
    from learning import outcomes as out
    for mod in (accuracy, candidates, feedback, out, overview, predictions, registry):
        src = inspect.getsource(mod)
        for forbidden in ("DailyBalance.objects.create", "DailyBalance.objects.bulk",
                          "DailyBalance.objects.update", "MonthlySummary.objects.create",
                          "MonthlySummary.objects.update", "DailyBalance(",
                          "DailyEstimate.objects.create", "DailyEstimate.objects.update",
                          "DailyEstimate.objects.bulk", "rule19"):
            assert forbidden not in src, (mod.__name__, forbidden)


def test_the_estimate_status_rules_and_200_account_gate_are_unchanged(complete):
    make_csp("1A850009", n=150)
    balances("1A850009", 1, 3, 1000, n=150)
    estimates.compute_daily_estimates(day(3))
    row = DailyEstimate.objects.get(csp_id="1A850009", estimate_date=day(3))
    assert row.status == "UNAVAILABLE" and row.reason_code == "BELOW_200_ACCOUNT_GATE"


# ------------------------------------------------------------------ the commands
def test_the_commands_run_the_loop_end_to_end_and_schedule_nothing(complete):
    from io import StringIO

    from django.core.management import call_command

    make_csp()
    balances("1A850001", 1, 6, 1000)
    flow("1A850001", day(3), deposit=3000)
    estimates.compute_daily_estimates(day(6))
    out = StringIO()
    call_command("freeze_predictions", "--since-days", "30", stdout=out)
    assert "created=6" in out.getvalue() and LivePrediction.objects.count() == 6
    out = StringIO()
    call_command("freeze_predictions", "--date", str(day(5)), stdout=out)
    assert "created=0 already_frozen=1" in out.getvalue()
    _refresh("1A850001", 7, 12, 1020)
    out = StringIO()
    call_command("record_outcomes", stdout=out)
    assert "scored=6" in out.getvalue()
    out = StringIO()
    call_command("evaluate_candidates", "--dry-run", stdout=out)
    text = out.getvalue()
    assert "movement: INSUFFICIENT_EVIDENCE" in text and "Dry run: nothing was stored." in text
    assert "account_count_only: BLOCKED" in text
    from learning.models import CandidateEvaluation
    assert CandidateEvaluation.objects.count() == 0


def test_nothing_in_the_project_schedules_the_learning_commands():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    for rel in ("compose.yml", "scripts/entrypoint.sh"):
        text = (root / rel).read_text(encoding="utf-8")
        for name in ("freeze_predictions", "record_outcomes", "evaluate_candidates",
                     "promote_model"):
            assert name not in text, (rel, name)
    for mod in ("app/bankflow/estimates.py",
                "app/bankflow/management/commands/compute_daily_estimates.py"):
        assert "learning" not in (root / mod).read_text(encoding="utf-8"), mod
