"""Unit tests (no DB) for the chronological, event-grouped fold logic."""

import datetime as dt
import random

from bankflow.evaluation import (
    EvalConfig,
    EventData,
    _fit_beta,
    run_folds,
    summarize,
)
from bankflow.targets import RefreshEvent


def _event(k: int, beta: float, noise: float, rng: random.Random, n: int = 60) -> EventData:
    d0 = dt.date(2026, 1, 1) + dt.timedelta(days=7 * k)
    ev = RefreshEvent(date=d0, previous_date=d0 - dt.timedelta(days=1), compared=n, changed=n)
    rows = []
    for i in range(n):
        x = rng.uniform(-50, 50)
        rows.append(
            {
                "csp": f"C{i}",
                "prev": 4000.0,
                "new": 4000.0 + beta * x,
                "x": x,
                "d": beta * x + rng.gauss(0, noise),
                "acct_changed": False,
            }
        )
    return EventData(
        event=ev, window_start=d0 - dt.timedelta(days=7), window_end=d0, coverage=1.0, rows=rows
    )


def test_fit_uses_only_earlier_events_never_the_test_event():
    rng = random.Random(1)
    # events 0-2 have slope +2; event 3 (the test event) has slope -9: any leak would show
    usable = [_event(k, 2.0, 0.1, rng) for k in range(3)] + [_event(3, -9.0, 0.1, rng)]
    cfg = EvalConfig(min_train_events=3)
    folds = run_folds(usable, cfg)
    assert len(folds) == 1
    assert abs(folds[0].beta - _fit_beta(usable[:3])) < 1e-12
    assert abs(folds[0].beta - 2.0) < 0.05  # not contaminated by the test event
    assert folds[0].n_train_events == 3


def test_folds_are_chronological_expanding_window():
    rng = random.Random(2)
    usable = [_event(k, 1.0, 0.5, rng) for k in range(8)]
    folds = run_folds(usable, EvalConfig(min_train_events=3))
    assert [f.n_train_events for f in folds] == [3, 4, 5, 6, 7]
    assert [f.event.event.date for f in folds] == sorted(f.event.event.date for f in folds)


def test_real_relationship_beats_no_change_baseline_across_events():
    rng = random.Random(3)
    usable = [_event(k, 1.5, 2.0, rng) for k in range(12)]
    cfg = EvalConfig(min_train_events=3)
    s = summarize(run_folds(usable, cfg), cfg)
    assert s["n_test_events"] == 9
    assert s["events_model_beats_nochange"] == 9
    assert s["improvement_ci95_over_events"][0] > 0
    assert s["rel_mae_improvement"] > 0.5


def test_pure_noise_does_not_beat_baseline():
    rng = random.Random(4)
    usable = [_event(k, 0.0, 30.0, rng) for k in range(12)]
    cfg = EvalConfig(min_train_events=3)
    s = summarize(run_folds(usable, cfg), cfg)
    assert s["improvement_ci95_over_events"][0] <= 0 or s["rel_mae_improvement"] < 0.05


def test_no_folds_when_too_few_events():
    rng = random.Random(5)
    assert (
        run_folds([_event(k, 1.0, 1.0, rng) for k in range(3)], EvalConfig(min_train_events=3))
        == []
    )
