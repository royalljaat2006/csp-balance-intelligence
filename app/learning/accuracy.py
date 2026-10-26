"""
The accuracy engine: how far the frozen predictions were from the SBI values that followed.

Rules this module keeps:
  * Only outcomes labelled OK measure the MODEL. Outcomes hurt by missing statements, unmatched
    codes, stale or provisional data are counted and shown apart (`data_quality`), never mixed in.
  * One outcome per CSP per refresh enters the headline: the prediction made closest to the refresh
    (smallest horizon). The other days are used only in the by-horizon table, so a CSP is not
    counted once per day it was predicted.
  * Independence is by refresh event and by calendar-month cycle, the same unit the validation
    gate uses (bankflow.account_hypothesis). Too few of either is INSUFFICIENT_EVIDENCE, and then
    no accuracy figure is produced at all -- not even a descriptive one -- for that slice.
  * Rows are taken in date order, and any row whose prediction was not strictly earlier than the
    value it is scored on is dropped and counted (`leakage_excluded`), as a second line of defence
    behind the matching in outcomes.py.
  * A percentage error is computed only where the SBI value is large enough for a percentage to
    mean something (MIN_PCT_DENOMINATOR); the rest are counted.

Signed error is predicted - actual: positive means the estimate was too high.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from bankflow import account_hypothesis as ah

from .models import LivePrediction, PredictionOutcome

Q = PredictionOutcome.Quality
INSUFFICIENT = ah.INSUFFICIENT

MIN_SLICE_OUTCOMES = ah.MIN_PAIRS_PROVISIONAL
MIN_SLICE_EVENTS = ah.MIN_EVENTS_PROVISIONAL
MIN_SLICE_CYCLES = ah.MIN_CYCLES_PROVISIONAL
MIN_CSP_OUTCOMES = ah.MIN_EVENTS_VALIDATED
#: below this SBI value (rupees per account) a percentage error is not meaningful
MIN_PCT_DENOMINATOR = Decimal("100")

HORIZON_BUCKETS = ((1, 1, "1 day"), (2, 3, "2 to 3 days"), (4, 7, "4 to 7 days"),
                   (8, 10_000, "8 days or more"))
COHORTS = ((0, 199, "Under 200 accounts"), (200, 499, "200 to 499 accounts"),
           (500, 999, "500 to 999 accounts"), (1000, 1999, "1,000 to 1,999 accounts"),
           (2000, 10**9, "2,000 or more accounts"))


@dataclass(frozen=True)
class Row:
    csp: str
    model: str
    estimate_date: dt.date
    event_date: dt.date  # the day the SBI value was first seen
    horizon: int
    predicted: Decimal
    actual: Decimal
    carry_forward: Decimal
    error: Decimal  # predicted - actual
    quality: str
    account_count: int | None
    count_changed: bool

    @property
    def month(self) -> str:
        return f"{self.event_date:%Y-%m}"


def load(*, since: dt.date | None = None, model: str | None = None) -> tuple[list[Row], int]:
    """(rows in date order, number dropped for leakage)."""
    qs = PredictionOutcome.objects.select_related("prediction", "prediction__model")
    if since is not None:
        qs = qs.filter(actual_first_seen__gte=since)
    if model:
        qs = qs.filter(prediction__model__name=model)
    rows, dropped = [], 0
    for o in qs.order_by("actual_first_seen", "id").iterator():
        p: LivePrediction = o.prediction
        if p.estimate_date >= o.actual_first_seen:
            dropped += 1
            continue
        rows.append(Row(
            csp=p.csp_id, model=p.model.name, estimate_date=p.estimate_date,
            event_date=o.actual_first_seen, horizon=o.horizon_days, predicted=p.predicted_value,
            actual=o.actual_value, carry_forward=p.carry_forward_value, error=o.error_amount,
            quality=o.quality, account_count=p.account_count,
            count_changed=(o.actual_account_count is not None
                           and p.baseline_account_count is not None
                           and o.actual_account_count != p.baseline_account_count),
        ))
    return rows, dropped


def headline(rows: list[Row]) -> list[Row]:
    """OK outcomes, one per CSP per refresh: the prediction made closest to it."""
    best: dict[tuple[str, dt.date, str], Row] = {}
    for r in rows:
        if r.quality != Q.OK:
            continue
        key = (r.csp, r.event_date, r.model)
        if key not in best or r.horizon < best[key].horizon:
            best[key] = r
    return sorted(best.values(), key=lambda r: (r.event_date, r.csp))


def error_metrics(rows: list[Row]) -> dict:
    """MAE, RMSE, mean signed error, over/under counts and, where meaningful, MAPE. Not a claim:
    callers decide whether the slice has enough evidence to show it."""
    n = len(rows)
    if not n:
        return {"n": 0}
    err = [float(r.error) for r in rows]
    cf = [float(r.carry_forward - r.actual) for r in rows]
    pct = [abs(float(r.error)) / float(r.actual) * 100 for r in rows
           if r.actual >= MIN_PCT_DENOMINATOR]
    mae = statistics.fmean(abs(e) for e in err)
    cf_mae = statistics.fmean(abs(e) for e in cf)
    return {
        "n": n, "mae": mae, "rmse": math.sqrt(statistics.fmean(e * e for e in err)),
        "median_abs_error": statistics.median(abs(e) for e in err),
        "bias": statistics.fmean(err),
        "over": sum(1 for e in err if e > 0), "under": sum(1 for e in err if e < 0),
        "exact": sum(1 for e in err if e == 0),
        "mape_pct": statistics.fmean(pct) if pct else None, "mape_n": len(pct),
        "mape_excluded_small_actual": n - len(pct),
        "carry_forward_mae": cf_mae, "beats_carry_forward": mae < cf_mae,
    }


def _independence(rows: list[Row]) -> tuple[int, int]:
    return len({r.event_date for r in rows}), len({r.month for r in rows})


def supported(rows: list[Row], *, min_outcomes: int = MIN_SLICE_OUTCOMES) -> dict:
    """Metrics for a slice, or INSUFFICIENT_EVIDENCE with the counts and what is missing."""
    n = len(rows)
    events, cycles = _independence(rows)
    short = []
    if n < min_outcomes:
        short.append(f"{n} of the {min_outcomes} outcomes needed")
    if events < MIN_SLICE_EVENTS:
        short.append(f"{events} of the {MIN_SLICE_EVENTS} SBI refresh days needed")
    if cycles < MIN_SLICE_CYCLES:
        short.append(f"{cycles} of the {MIN_SLICE_CYCLES} monthly cycles needed")
    if short:
        return {"status": INSUFFICIENT, "n": n, "events": events, "cycles": cycles,
                "missing": short}
    return {"status": "SUPPORTED", "n": n, "events": events, "cycles": cycles,
            **error_metrics(rows)}


def _s(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def plain_evidence(status: str, n: int, events: int, cycles: int, gain: dict) -> str:
    """The headline verdict in everyday words, from the same counts the verdict was decided on."""
    so_far = (f"{_s(n, 'check')} over {_s(events, 'SBI refresh day')} in {_s(cycles, 'month')} "
              "so far")
    if n == 0:
        return ("Nothing has been checked against an SBI figure yet. To say anything we need at "
                f"least {MIN_SLICE_OUTCOMES} checks over {MIN_SLICE_EVENTS} SBI refresh days and "
                f"{MIN_SLICE_CYCLES} months.")
    if status == INSUFFICIENT and (n < MIN_SLICE_OUTCOMES or events < MIN_SLICE_EVENTS
                                   or cycles < MIN_SLICE_CYCLES):
        return (f"Only {so_far}. To say anything we need at least {MIN_SLICE_OUTCOMES} checks "
                f"over {MIN_SLICE_EVENTS} refresh days and {MIN_SLICE_CYCLES} months, and to call "
                f"the estimate accurate at least {ah.MIN_PAIRS_VALIDATED} checks over "
                f"{ah.MIN_EVENTS_VALIDATED} refresh days and {ah.MIN_CYCLES_VALIDATED} months.")
    wins, share = gain.get("events_primary_better"), gain.get("event_win_share")
    better = ("" if wins is None or share is None
              else f"Our estimate was closer to SBI's figure than carry-forward on "
                   f"{wins} of {_s(events, 'refresh day')}")
    rel = gain.get("relative_mae_gain")
    size = ""
    if rel is not None:
        size = f", with errors about {abs(rel):.0%} {'smaller' if rel > 0 else 'larger'}"
    if status == ah.VALIDATED:
        return f"{better}{size}, over {_s(cycles, 'month')}. This meets the evidence bar."
    if status == ah.PROVISIONAL:
        return (f"{better}{size}, but {so_far} is not yet enough to call it accurate: that needs "
                f"{ah.MIN_PAIRS_VALIDATED} checks over {ah.MIN_EVENTS_VALIDATED} refresh days and "
                f"{ah.MIN_CYCLES_VALIDATED} months.")
    if status == ah.NOT_SUPPORTED:
        return (f"There is enough to judge ({so_far}) and the estimate was not reliably better "
                f"than carry-forward. {better}{size}.")
    return (f"{so_far.capitalize()}. The estimate is not yet ahead of carry-forward, and there is "
            "too little to conclude either way.")


def evidence(rows: list[Row]) -> dict:
    """The headline verdict, by the same pre-registered criteria the validation gate uses: the
    live estimate against carry-forward, event by event."""
    scored = [ah.ScoredRow(r.csp, r.event_date, r.actual,
                           {"carry_forward": r.carry_forward, "model": r.predicted})
              for r in rows]
    metrics = ah.score_rows(scored, ("carry_forward", "model"), "model")
    status, detail = ah.classify(metrics, "The live estimate")
    gain = metrics["primary_vs_carry_forward"]
    return {"status": status, "detail": detail,
            "plain": plain_evidence(status, metrics["n_pairs"], metrics["n_events"],
                                    metrics["n_cycles"], gain),
            "n_outcomes": metrics["n_pairs"],
            "n_events": metrics["n_events"], "n_cycles": metrics["n_cycles"],
            "n_csps": metrics["n_csps"],
            "p90_abs_pct_error": metrics["candidates"]["model"]["p90_ape_pct"],
            "gain_vs_carry_forward": metrics["primary_vs_carry_forward"]}


def _bucket(value: int, table) -> str:
    for lo, hi, label in table:
        if lo <= value <= hi:
            return label
    return "unknown"


def _group(rows: list[Row], key) -> dict:
    out: dict = defaultdict(list)
    for r in rows:
        out[key(r)].append(r)
    return out


def by_horizon(ok_rows: list[Row]) -> list[dict]:
    groups = _group(ok_rows, lambda r: _bucket(r.horizon, HORIZON_BUCKETS))
    return [{"horizon": label, **supported(groups[label])}
            for _lo, _hi, label in HORIZON_BUCKETS if label in groups]


def by_cohort(head: list[Row]) -> list[dict]:
    out = []
    groups = _group(head, lambda r: _bucket(r.account_count or 0, COHORTS))
    for _lo, _hi, label in COHORTS:
        if label in groups:
            out.append({"cohort": label, **supported(groups[label])})
    changed = _group(head, lambda r: "Account count changed" if r.count_changed
                     else "Account count unchanged")
    for label in sorted(changed):
        out.append({"cohort": label, **supported(changed[label])})
    return out


def by_csp(head: list[Row]) -> list[dict]:
    return [{"csp_code": code, **supported(rs, min_outcomes=MIN_CSP_OUTCOMES)}
            for code, rs in sorted(_group(head, lambda r: r.csp).items())]


def by_model(head: list[Row]) -> list[dict]:
    groups = _group(head, lambda r: r.model)
    return [{"model": name, **supported(rs)} for name, rs in sorted(groups.items())]


def trend(head: list[Row]) -> list[dict]:
    """Per month of SBI refresh: how many estimates were too high or too low (always shown, they
    are counts), and the mean signed error only when the month has enough evidence."""
    out = []
    for month, rs in sorted(_group(head, lambda r: r.month).items()):
        over = sum(1 for r in rs if r.error > 0)
        under = sum(1 for r in rs if r.error < 0)
        sup = supported(rs)
        out.append({"month": month, "n": len(rs), "over": over, "under": under,
                    "mean_signed_error": sup.get("bias") if sup["status"] == "SUPPORTED" else None,
                    "status": sup["status"]})
    return out


def data_quality(rows: list[Row]) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r.quality] += 1
    total = len(rows)
    return {"total": total, "ok": counts.get(Q.OK, 0),
            "problems": {q: n for q, n in sorted(counts.items()) if q != Q.OK},
            "problem_share": (total - counts.get(Q.OK, 0)) / total if total else None}


def accuracy_report(*, since: dt.date | None = None, model: str | None = None) -> dict:
    """Everything the dashboard and the API show about accuracy, in one place."""
    rows, leaked = load(since=since, model=model)
    ok = [r for r in rows if r.quality == Q.OK]
    head = headline(rows)
    ev = evidence(head)
    claim = ev["status"]
    return {
        "evaluated": len(rows), "leakage_excluded": leaked,
        "headline_outcomes": len(head),
        "evidence": ev,
        # the accuracy claim: INSUFFICIENT_EVIDENCE unless the pre-declared bar is met
        "accuracy_claim": claim,
        "metrics": None if claim == INSUFFICIENT else error_metrics(head),
        "by_horizon": by_horizon(ok), "by_cohort": by_cohort(head), "by_csp": by_csp(head),
        "by_model": by_model(head), "trend": trend(head), "data_quality": data_quality(rows),
        "notes": [
            "Signed error is estimate minus SBI value: positive means the estimate was too high.",
            "Only outcomes with good surrounding data measure the model; the rest are counted as "
            "data problems.",
            "One outcome per CSP per refresh enters the headline: the prediction made closest "
            "to it.",
            "Accuracy is claimed only when the pre-declared evidence bar is met; otherwise the "
            "status is INSUFFICIENT_EVIDENCE and no figure is given.",
        ],
    }
