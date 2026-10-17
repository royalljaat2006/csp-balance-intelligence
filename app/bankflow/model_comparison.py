"""
Held-out evaluation of the movement estimator against the carry-forward baseline.

For each SBI refresh of a CSP (the Calling Sheet Avg Balance changing between two readings) the
movement rule predicts the NEW value from the PREVIOUS reading alone:

    predicted = (previous Avg Balance x previous count + net eligible movement) / count at refresh

where the movement is the attributed customer deposits minus withdrawals over
(previous-first-seen, new-first-seen] -- the same window the daily estimator walks. The reported
value is never an input. The rule has no fitted parameters, so there is nothing to train: every
refresh with enough statement coverage is a genuine held-out test, and there is no look-ahead.

The reference is carry-forward (the previous value unchanged). A pair needs flow coverage >=
MIN_COVERAGE of its interval, measured as the share of days with PROVEN-complete statement data:
an interval with no statements is not "zero movement", the pair is excluded and counted.

Verdicts come from the same rules as before (account_hypothesis.classify): p90, events, monthly
cycles, win share, gain and bootstrap interval. Nothing here approves anything for production.
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from bankflow import flows, movement
from bankflow.account_hypothesis import Pair, ScoredRow, classify, score_rows
from bankflow.models import FlowAggregate

MIN_COVERAGE = 0.8
METHOD = movement.METHOD
LABELS = {METHOD: "Movement estimator"}


def flow_index(until: dt.date | None = None):
    """(per-CSP cumulative flow, sorted proven-complete days). Exact attribution only."""
    complete = sorted(flows.proven_complete_dates())
    complete_set = set(complete)
    qs = FlowAggregate.objects.filter(resolution_mode=flows.MODE_EXACT)
    if until is not None:
        qs = qs.filter(flow_date__lte=until)
    per: dict[str, dict[dt.date, Decimal]] = defaultdict(dict)
    for csp, d, by_cat in qs.values_list("csp_id", "flow_date", "by_category").iterator():
        if d in complete_set:
            per[csp][d] = per[csp].get(d, Decimal(0)) + flows.signed_flow(
                by_cat, flows.DEFAULT_DEFINITION
            )
    cum: dict[str, tuple[list[dt.date], list[Decimal]]] = {}
    for csp, by_day in per.items():
        days = sorted(by_day)
        running, acc = [], Decimal(0)
        for d in days:
            acc += by_day[d]
            running.append(acc)
        cum[csp] = (days, running)
    return cum, complete


def _window_flow(cum, csp: str, start: dt.date, end: dt.date) -> Decimal:
    days, running = cum.get(csp, ([], []))
    hi = bisect.bisect_right(days, end)
    lo = bisect.bisect_right(days, start)
    upper = running[hi - 1] if hi else Decimal(0)
    lower = running[lo - 1] if lo else Decimal(0)
    return upper - lower


def _coverage(complete: list[dt.date], start: dt.date, end: dt.date) -> float:
    total = (end - start).days
    if total <= 0:
        return 0.0
    return (bisect.bisect_right(complete, end) - bisect.bisect_right(complete, start)) / total


@dataclass(frozen=True)
class Evaluated:
    """One SBI refresh with the movement prediction made for it (or why it was left out)."""

    pair: Pair
    movement: Decimal       # net eligible movement over the interval (proven-complete days)
    coverage: float         # share of the interval's days with proven-complete statements
    predicted: Decimal      # per-account prediction for the refresh date
    usable: bool            # coverage >= MIN_COVERAGE


def evaluate(pairs: list[Pair], *, until: dt.date | None = None) -> list[Evaluated]:
    cum, complete = flow_index(until)
    out = []
    for p in pairs:
        cov = _coverage(complete, p.prev_date, p.event_date)
        mv = _window_flow(cum, p.csp, p.prev_date, p.event_date)
        predicted = movement.predict_per_account(p.prev_value, p.prev_count, mv, p.new_count)
        out.append(Evaluated(p, mv, cov, predicted, cov >= MIN_COVERAGE))
    return out


def compare(pairs: list, *, until: dt.date | None = None) -> dict:
    evaluated = evaluate(pairs, until=until)
    usable = [e for e in evaluated if e.usable]
    events = sorted({e.pair.event_date for e in usable})
    basis = {
        "pairs_total": len(pairs),
        "pairs_with_flow_coverage": len(usable),
        "pairs_dropped_low_coverage": len(evaluated) - len(usable),
        "min_coverage": MIN_COVERAGE,
        "events_with_flow_coverage": len(events),
        "mean_coverage": (sum(e.coverage for e in usable) / len(usable)) if usable else None,
        "flow_definition": flows.DEFAULT_DEFINITION,
        "fitted_parameters": 0,
    }
    held = [
        ScoredRow(e.pair.csp, e.pair.event_date, e.pair.new_value,
                  {"carry_forward": e.pair.prev_value, METHOD: e.predicted})
        for e in usable
    ]
    basis["held_out_pairs"] = len(held)
    metrics = score_rows(held, ("carry_forward", METHOD), METHOD)
    status, detail = classify(metrics, LABELS[METHOD])
    return {"basis": basis, "methods": {METHOD: {
        "status": status, "detail": detail, "metrics": metrics, "parameters": {},
    }}}
