"""
Four-way, time-ordered comparison of Avg Balance estimators.

  carry_forward     a1 = a0                                  (reference, never "validated")
  growth_only       a1 = a0 * n1 / n0                        (account-count hypothesis)
  flow_only         a1 = a0 + b * x                          x = attributed net customer flow
                                                             over the interval / n1 (per account)
  growth_and_flow   a1 = a0 * n1 / n0 + b * x

Where a0/n0 are the previous refreshed value and its count, a1/n1 the next, and the interval is
(previous-first-seen, new-first-seen]. The coefficient b is fitted through the origin on training
pairs only. Expanding-window: a held-out event is predicted with a b fitted on pairs whose new
value was already known at that event's prediction origin (= earliest previous-value date among
its pairs), and the first MIN_TRAIN_EVENTS events are training-only, so no pair ever helps train
the model that scores it. Pairs need flow coverage >= MIN_COVERAGE of the interval, measured as
the share of days with PROVEN-complete statement data -- an interval with no statements is not
"zero flow", it is excluded.

Flow signs use the existing, UNCONFIRMED `customer_wd_dep` mapping (bankflow.flows). Nothing here
assumes pool net flow equals the change in customers' balances: b is whatever the data supports,
and if the data supports nothing the verdict is INSUFFICIENT_EVIDENCE / NOT_SUPPORTED.
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections import defaultdict
from decimal import Decimal

from bankflow import flows
from bankflow.account_hypothesis import (
    ScoredRow,
    classify,
    predict,
    score_rows,
)
from bankflow.models import FlowAggregate

MIN_COVERAGE = 0.8
MIN_TRAIN_EVENTS = 3
MIN_TRAIN_PAIRS = 5
METHODS = ("flow_only", "growth_and_flow")
LABELS = {"flow_only": "Flow-only model", "growth_and_flow": "Growth-and-flow model"}


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


def _fit(xs: list[float], ys: list[float]) -> float | None:
    den = sum(x * x for x in xs)
    return None if den == 0 else sum(x * y for x, y in zip(xs, ys, strict=True)) / den


class _Row:
    __slots__ = ("pair", "x", "coverage")

    def __init__(self, pair, x, coverage):
        self.pair, self.x, self.coverage = pair, x, coverage


def _predictions(row: _Row, b_flow: float | None, b_both: float | None) -> dict:
    p = row.pair
    growth = predict("proportional", p.prev_value, p.prev_count, p.new_count)
    return {
        "carry_forward": p.prev_value,
        "growth_only": growth,
        "flow_only": p.prev_value + Decimal(str((b_flow or 0.0) * row.x)),
        "growth_and_flow": growth + Decimal(str((b_both or 0.0) * row.x)),
    }


def _fit_params(train: list[_Row]) -> tuple[float | None, float | None]:
    xs = [r.x for r in train]
    b_flow = _fit(xs, [float(r.pair.new_value - r.pair.prev_value) for r in train])
    b_both = _fit(
        xs,
        [
            float(
                r.pair.new_value - predict("proportional", r.pair.prev_value,
                                           r.pair.prev_count, r.pair.new_count)
            )
            for r in train
        ],
    )
    return b_flow, b_both


def compare(pairs: list, *, until: dt.date | None = None) -> dict:
    cum, complete = flow_index(until)
    usable: list[_Row] = []
    dropped_coverage = 0
    for p in pairs:
        cov = _coverage(complete, p.prev_date, p.event_date)
        if cov < MIN_COVERAGE:
            dropped_coverage += 1
            continue
        flow = _window_flow(cum, p.csp, p.prev_date, p.event_date)
        usable.append(_Row(p, float(flow) / p.new_count, cov))

    events = sorted({r.pair.event_date for r in usable})
    basis = {
        "pairs_total": len(pairs),
        "pairs_with_flow_coverage": len(usable),
        "pairs_dropped_low_coverage": dropped_coverage,
        "min_coverage": MIN_COVERAGE,
        "events_with_flow_coverage": len(events),
        "min_train_events": MIN_TRAIN_EVENTS,
        "flow_definition": flows.DEFAULT_DEFINITION,
        "flow_mapping_confirmed": False,
    }

    held: list[ScoredRow] = []
    by_event: dict[dt.date, list[_Row]] = defaultdict(list)
    for r in usable:
        by_event[r.pair.event_date].append(r)
    for i, ev in enumerate(events):
        if i < MIN_TRAIN_EVENTS:
            continue  # training-only
        origin = min(r.pair.prev_date for r in by_event[ev])
        train = [r for r in usable if r.pair.event_date <= origin]
        if len(train) < MIN_TRAIN_PAIRS:
            continue
        b_flow, b_both = _fit_params(train)
        for r in by_event[ev]:
            held.append(ScoredRow(r.pair.csp, ev, r.pair.new_value,
                                  _predictions(r, b_flow, b_both)))
    basis["held_out_pairs"] = len(held)

    # parameters for production use: fitted on every usable pair
    b_flow_all, b_both_all = _fit_params(usable) if usable else (None, None)
    params = {"flow_only": {"beta": b_flow_all}, "growth_and_flow": {"beta": b_both_all}}

    methods = {}
    for m in METHODS:
        metrics = score_rows(held, ("carry_forward", m), m)
        status, detail = classify(metrics, LABELS[m])
        if params[m]["beta"] is None and status in ("VALIDATED", "PROVISIONAL"):
            status, detail = "INSUFFICIENT_EVIDENCE", "No usable flow signal to fit a coefficient."
        methods[m] = {"status": status, "detail": detail, "metrics": metrics,
                      "parameters": params[m]}
    return {"basis": basis, "methods": methods}
