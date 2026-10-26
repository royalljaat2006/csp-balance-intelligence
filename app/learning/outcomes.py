"""
Match each frozen prediction to the SBI value that followed it, and score it.

The Calling Sheet series of a CSP is collapsed to its refreshes: the first day each successive
distinct Avg Balance was seen. A prediction belongs to the cycle that began with its baseline value;
the outcome is the NEXT refresh of that same CSP. Each CSP is matched on its own, so SBI updating
different CSPs on different days needs no special handling.

No leakage, by construction:
  * a prediction is scored only if its estimate day is strictly before the day the new value was
    first seen, so a prediction can never have seen the value it is judged on;
  * the baseline must be found in the CSP's own history with the same first-seen day and value,
    otherwise the prediction cannot be placed and is left unmatched (never guessed);
  * the SBI value is never overwritten into the prediction: the outcome is a separate, insert-only
    record, and a later restatement is just the next refresh for the next cycle.

Dates are the day the system FIRST SAW a value. The sheet has no measurement-date column, so SBI's
own effective date is unknown and is never invented.

Each outcome is also labelled with whether the data around it was good enough to judge the model.
A missing statement, many unmatched transaction codes, a stale source or a blank account count are
DATA failures, kept apart from model error: the accuracy engine measures the model only on outcomes
labelled OK.
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections import defaultdict
from decimal import Decimal

from bankflow import account_hypothesis as ah
from bankflow import flows
from bankflow.models import FlowAggregate, UnresolvedFlowDaily
from django.db.models import F, Sum

from .models import LivePrediction, PredictionOutcome

Q = PredictionOutcome.Quality
CENT = Decimal("0.01")
ZERO = Decimal("0")

#: share of the interval's days with proven-complete statements below which the interval is a gap
MIN_COVERAGE = 0.8
#: an operational limit, not a derived one: above this share of pool movement with a code that could
#: not be matched to a CSP, an interval is flagged. The measured share is stored with the outcome.
MAX_UNMATCHED_SHARE = 0.10
UNMATCHED_STATUSES = ("unmatched", "ambiguous_truncated", "variant_provisional")

#: the order in which a quality problem names an outcome when several apply
PRIORITY = (Q.STATEMENT_GAP, Q.UNMATCHED_CODES, Q.PROVISIONAL_DATA, Q.COUNT_UNRELIABLE,
            Q.STALE_SOURCE)


def refreshes(rows) -> list[tuple[dt.date, Decimal, int | None]]:
    """(first day seen, value, account count that day) for each successive distinct value."""
    out: list[tuple[dt.date, Decimal, int | None]] = []
    for d, avg, n in sorted(rows, key=lambda r: r[0]):
        if not out or avg != out[-1][1]:
            out.append((d, avg, n))
    return out


class _Context:
    """Per-run lookups shared by every outcome: complete statement days and unmatched shares."""

    def __init__(self, start: dt.date, end: dt.date):
        self.complete = sorted(flows.proven_complete_dates())
        self.unmatched: dict[dt.date, Decimal] = defaultdict(lambda: ZERO)
        self.attributed: dict[dt.date, Decimal] = defaultdict(lambda: ZERO)
        for day, total in (UnresolvedFlowDaily.objects
                           .filter(flow_date__gt=start, flow_date__lte=end,
                                   status__in=UNMATCHED_STATUSES)
                           .values("flow_date")
                           .annotate(t=Sum(F("credit_amount") + F("debit_amount")))
                           .values_list("flow_date", "t")):
            self.unmatched[day] += total or ZERO
        for day, total in (FlowAggregate.objects
                           .filter(flow_date__gt=start, flow_date__lte=end,
                                   resolution_mode=flows.MODE_EXACT)
                           .values("flow_date")
                           .annotate(t=Sum(F("credit_amount") + F("debit_amount")))
                           .values_list("flow_date", "t")):
            self.attributed[day] += total or ZERO

    def coverage(self, start: dt.date, end: dt.date) -> float:
        total = (end - start).days
        if total <= 0:
            return 0.0
        done = bisect.bisect_right(self.complete, end) - bisect.bisect_right(self.complete, start)
        return done / total

    def unmatched_share(self, start: dt.date, end: dt.date) -> float | None:
        days = [start + dt.timedelta(days=i) for i in range(1, (end - start).days + 1)]
        bad = sum((self.unmatched[d] for d in days), ZERO)
        good = sum((self.attributed[d] for d in days), ZERO)
        return float(bad / (bad + good)) if (bad + good) else None


def classify_quality(p: LivePrediction, nxt_day: dt.date, ctx: _Context) -> tuple[str, dict]:
    """(quality label, detail). OK only when nothing around the outcome undermines judging the
    model."""
    cov = ctx.coverage(p.baseline_first_seen, nxt_day)
    share = ctx.unmatched_share(p.baseline_first_seen, nxt_day)
    inputs = p.inputs or {}
    flags = []
    if cov < MIN_COVERAGE:
        flags.append(Q.STATEMENT_GAP)
    if share is not None and share > MAX_UNMATCHED_SHARE:
        flags.append(Q.UNMATCHED_CODES)
    if p.statement_state != "complete":
        flags.append(Q.PROVISIONAL_DATA)
    if (inputs.get("count") or {}).get("stale"):
        flags.append(Q.COUNT_UNRELIABLE)
    if inputs.get("stale"):
        flags.append(Q.STALE_SOURCE)
    label = next((q for q in PRIORITY if q in flags), Q.OK)
    return label, {
        "flags": [str(f) for f in flags],
        "interval": [p.baseline_first_seen.isoformat(), nxt_day.isoformat()],
        "interval_statement_coverage": round(cov, 4),
        "unmatched_code_share": None if share is None else round(share, 4),
        "limits": {"min_coverage": MIN_COVERAGE, "max_unmatched_share": MAX_UNMATCHED_SHARE},
    }


def _direction(error: Decimal) -> str:
    if error > 0:
        return PredictionOutcome.Direction.OVER
    if error < 0:
        return PredictionOutcome.Direction.UNDER
    return PredictionOutcome.Direction.EXACT


def score(p: LivePrediction, actual: Decimal, actual_day: dt.date, actual_count: int | None,
          ctx: _Context) -> PredictionOutcome:
    """The (unsaved) outcome of one prediction against the SBI value that followed it."""
    error = (p.predicted_value - actual).quantize(CENT)
    quality, detail = classify_quality(p, actual_day, ctx)
    return PredictionOutcome(
        prediction=p, actual_value=actual, actual_first_seen=actual_day,
        actual_account_count=actual_count,
        horizon_days=(actual_day - p.estimate_date).days,
        error_amount=error, abs_error=abs(error),
        pct_error=(float(abs(error) / actual * 100) if actual > 0 else None),
        direction=_direction(error),
        carry_forward_error=(p.carry_forward_value - actual).quantize(CENT),
        quality=quality, quality_detail=detail,
    )


def record_outcomes(*, until: dt.date | None = None) -> dict:
    """Score every prediction whose next SBI refresh has now been seen. Safe to repeat: a
    prediction that already has an outcome is never touched."""
    pending = list(LivePrediction.objects.filter(outcome__isnull=True)
                   .select_related("csp").order_by("csp_id", "estimate_date"))
    by_quality: dict[str, int] = {}
    result: dict = {"pending": len(pending), "scored": 0, "waiting_for_next_refresh": 0,
                    "unmatched_baseline": 0, "not_before_outcome": 0, "by_quality": by_quality}
    if not pending:
        return result
    by_csp: dict[str, list[LivePrediction]] = defaultdict(list)
    for p in pending:
        by_csp[p.csp_id].append(p)
    series = ah.load_series(until=until)
    start = min(p.baseline_first_seen for p in pending)
    seen = [d for rows in series.values() for d, _a, _n in rows]
    end = until or (max(seen) if seen else start)
    ctx = _Context(start, end)

    new: list[PredictionOutcome] = []
    for code, preds in by_csp.items():
        seq = refreshes(series.get(code, []))
        index = {(d, v): i for i, (d, v, _n) in enumerate(seq)}
        for p in preds:
            i = index.get((p.baseline_first_seen, p.baseline_value))
            if i is None:
                result["unmatched_baseline"] += 1
                continue
            if i + 1 >= len(seq):
                result["waiting_for_next_refresh"] += 1
                continue
            day, value, count = seq[i + 1]
            if p.estimate_date >= day:  # made on or after the day the value was first seen
                result["not_before_outcome"] += 1
                continue
            new.append(score(p, value, day, count, ctx))
    PredictionOutcome.objects.bulk_create(new, batch_size=1000, ignore_conflicts=True)
    result["scored"] = len(new)
    for o in new:
        by_quality[o.quality] = by_quality.get(o.quality, 0) + 1
    return result
