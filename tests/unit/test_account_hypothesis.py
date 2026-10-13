"""Account-count hypothesis: pairing rules, candidate maths, verdicts, no look-ahead."""

import datetime as dt
from decimal import Decimal as D

from bankflow import account_hypothesis as ah

d = dt.date


def _pair(csp="A", ev=None, a0="1000", a1="1030", n0=350, n1=360):
    ev = ev or d(2026, 9, 1)
    return ah.Pair(csp, ev, ev - dt.timedelta(days=30), D(a0), D(a1), n0, n1)


def _week(e):
    return d(2026, 1, 1) + dt.timedelta(days=e * 30)


def test_350_to_360_is_2_857_percent():
    got = ah.predict(ah.PRIMARY, D("1000"), 350, 360)
    assert round((got / D("1000") - 1) * 100, 3) == D("2.857")


def test_other_candidates():
    assert ah.predict("carry_forward", D("1000"), 350, 360) == D("1000")
    assert round(ah.predict("total_constant", D("1000"), 350, 360), 2) == D("972.22")


def test_unchanged_count_gives_identical_predictions():
    for c in ah.CANDIDATES:
        assert ah.predict(c, D("1000"), 350, 350) == D("1000")


def test_prediction_ignores_the_future_value():
    # look-ahead guard: predict() has no parameter for the realised value, and pairs
    # that differ only in that value get identical predictions
    p1, p2 = _pair(a1="1030"), _pair(a1="5")
    for c in ah.CANDIDATES:
        assert ah.predict(c, p1.prev_value, p1.prev_count, p1.new_count) == ah.predict(
            c, p2.prev_value, p2.prev_count, p2.new_count
        )


def test_pairs_use_refreshes_not_daily_polls_and_skip_missing_baselines():
    series = {
        "A": [  # constant on days 1-3, refresh on day 4, counts creep daily
            (d(2026, 9, 1), D("1000"), 350),
            (d(2026, 9, 2), D("1000"), 352),
            (d(2026, 9, 3), D("1000"), 355),
            (d(2026, 9, 4), D("1030"), 360),
        ],
        "B": [(d(2026, 9, 1), D("500"), None), (d(2026, 9, 4), D("520"), 300)],  # no count
        "C": [(d(2026, 9, 1), D("0"), 300), (d(2026, 9, 4), D("520"), 300)],  # zero baseline
        "D": [(d(2026, 9, 1), D("500"), 300)],  # single observation
    }
    pairs = ah.pairs_from_series(series)
    assert [(p.csp, p.prev_count, p.new_count, p.event_date) for p in pairs] == [
        ("A", 350, 360, d(2026, 9, 4))
    ]


def test_proportional_data_beats_carry_forward_and_validates():
    pairs = [_pair(f"C{i}", _week(e)) for e in range(10) for i in range(4)]
    m = ah.evaluate_pairs(pairs)
    assert m["n_events"] == 10 and m["n_pairs"] == 40
    assert m["candidates"][ah.PRIMARY]["mae"] < m["candidates"]["carry_forward"]["mae"]
    assert ah.classify(m)[0] == ah.VALIDATED


def test_count_unchanged_pairs_are_not_evidence():
    m = ah.evaluate_pairs([_pair(n0=350, n1=350, a1="1100") for _ in range(50)])
    assert m["n_pairs"] == 0
    assert ah.classify(m)[0] == ah.INSUFFICIENT


def test_one_event_is_insufficient_however_many_csps():
    m = ah.evaluate_pairs([_pair(f"C{i}", d(2026, 9, 1)) for i in range(200)])
    assert m["n_events"] == 1 and m["n_pairs"] == 200
    status, detail = ah.classify(m)
    assert status == ah.INSUFFICIENT and "refresh event" in detail


def test_wrong_hypothesis_is_not_supported_when_data_suffices():
    # count rises 350 -> 360 but the value FALLS: proportional is worse than carry-forward
    pairs = [_pair(f"C{i}", _week(e), a1="980") for e in range(10) for i in range(4)]
    assert ah.classify(ah.evaluate_pairs(pairs))[0] == ah.NOT_SUPPORTED


def test_provisional_between_the_bars():
    pairs = [_pair(f"C{i}", _week(e)) for e in range(4) for i in range(5)]
    assert ah.classify(ah.evaluate_pairs(pairs))[0] == ah.PROVISIONAL
