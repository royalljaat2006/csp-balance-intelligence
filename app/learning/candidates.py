"""
Time-ordered comparison of candidate estimation methods.

Every SBI refresh of a CSP (the Calling Sheet Avg Balance changing between two readings) is one
problem: given only the PREVIOUS reading, the account counts and the statement movement since, what
is the NEW value? Each registered method answers it, and the answer is scored against what SBI
reported. No method sees the value it is scored on, and every rule here has zero fitted parameters,
so there is nothing to train and every refresh on or after `holdout_from` is a genuine held-out
test, taken in date order.

Methods compared (learning.registry):
  carry_forward       the reference: the previous value unchanged
  account_count_only  average x new count / old count (blocked by a business rule; evidence only)
  flow_only           average + movement / OLD count (flow without account growth)
  movement            (average x old count + movement) / new count (the active shadow rule)

A method is RECOMMENDED only if every pre-declared criterion is met -- the same ones the movement
rule's validation gate uses (bankflow.sbi_comparison.criteria_for) -- and, when another method is
active, it also beats that method in at least two thirds of events with a confidence interval above
zero. Too little evidence is INSUFFICIENT_EVIDENCE, never a pass. Nothing here changes the active
method; that is learning.registry.promote, which needs a person.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from decimal import Decimal

from bankflow import account_hypothesis as ah
from bankflow import model_comparison as mc
from bankflow import movement
from bankflow.models import ModelVersion
from bankflow.sbi_comparison import criteria_for

from . import registry
from .models import CandidateEvaluation

V = CandidateEvaluation.Verdict
BOOTSTRAP_REPS = ah.BOOTSTRAP_REPS
BOOTSTRAP_SEED = 20261026

LABELS = {
    registry.MOVEMENT: "Movement rule",
    registry.FLOW_ONLY: "Flow-only rule",
    registry.ACCOUNT_COUNT_ONLY: "Account-count-only rule",
}


def _rule_carry_forward(e: mc.Evaluated) -> Decimal:
    return e.pair.prev_value


def _rule_account_count_only(e: mc.Evaluated) -> Decimal:
    return ah.predict(ah.PRIMARY, e.pair.prev_value, e.pair.prev_count, e.pair.new_count)


def _rule_flow_only(e: mc.Evaluated) -> Decimal:
    # the count at the baseline stays the divisor: flow without any account growth
    return movement.predict_per_account(e.pair.prev_value, e.pair.prev_count, e.movement,
                                        e.pair.prev_count)


def _rule_movement(e: mc.Evaluated) -> Decimal:
    return e.predicted


RULES = {
    registry.BASELINE: _rule_carry_forward,
    registry.ACCOUNT_COUNT_ONLY: _rule_account_count_only,
    registry.FLOW_ONLY: _rule_flow_only,
    registry.MOVEMENT: _rule_movement,
}


def _event_mae(rows: list[ah.ScoredRow], method: str) -> dict[dt.date, float]:
    by: dict[dt.date, list[float]] = defaultdict(list)
    for r in rows:
        by[r.event_date].append(abs(float(r.preds[method] - r.actual)))
    return {d: statistics.fmean(v) for d, v in by.items()}


def _vs(rows: list[ah.ScoredRow], reference: str, candidate: str) -> dict:
    """Event-by-event error of `candidate` against `reference`: positive gain = candidate better."""
    ref, cand = _event_mae(rows, reference), _event_mae(rows, candidate)
    gains = [ref[d] - cand[d] for d in sorted(ref)]
    n = len(gains)
    wins = sum(1 for g in gains if g > 0)
    ci = ah._bootstrap_ci(gains, BOOTSTRAP_REPS, BOOTSTRAP_SEED) if n >= 2 else (None, None)
    return {
        "reference": reference, "events": n, "events_better": wins,
        "win_share": (wins / n) if n else None,
        "mean_gain": statistics.fmean(gains) if gains else None,
        "gain_ci95": list(ci),
    }


def _stability(rows: list[ah.ScoredRow], method: str) -> dict:
    """Does the gain over carry-forward hold in the later half of the events as in the earlier?"""
    events = sorted({r.event_date for r in rows})
    if len(events) < 4:
        return {"available": False, "note": "Fewer than four events: no split into halves."}
    cut = events[len(events) // 2]
    first = [r for r in rows if r.event_date < cut]
    second = [r for r in rows if r.event_date >= cut]
    out = {"available": True, "split_at": cut.isoformat()}
    for label, part in (("earlier", first), ("later", second)):
        v = _vs(part, registry.BASELINE, method)
        out[label] = {"events": v["events"], "mean_gain": v["mean_gain"],
                      "win_share": v["win_share"]}
    return out


def _limitations(basis: dict) -> list[str]:
    return [
        "Event dates are the day the new SBI value was first seen, not SBI's own effective date.",
        "A refresh that repeats the previous value cannot be detected.",
        "No method has fitted parameters, so no data is used for training; every usable refresh "
        "on or after the hold-out date is a held-out test, taken in date order.",
        f"A refresh needs at least {mc.MIN_COVERAGE:.0%} of its days covered by complete bank "
        f"statements; {basis['pairs_dropped_low_coverage']} of {basis['pairs_total']} were "
        "left out for that reason.",
        "Statement movement is the attributed customer deposits minus withdrawals; pool flow is "
        "not assumed to equal a customer's balance change.",
    ]


def _evaluate_one(mv: ModelVersion, rows, active, basis, holdout_from) -> CandidateEvaluation:
    name = mv.name
    label = LABELS.get(name, name)
    metrics = ah.score_rows(rows, (registry.BASELINE, name), name)
    status, detail = ah.classify(metrics, label)
    criteria = criteria_for(metrics, name)

    extra: dict = {"fitted_parameters": 0, "basis": basis, "stability": _stability(rows, name)}
    if active is not None and active.name != name and active.name in RULES and rows:
        vs_active = _vs(rows, active.name, name)
        extra["vs_active"] = vs_active
        ci = vs_active["gain_ci95"][0]
        win = vs_active["win_share"]
        criteria.append({
            "label": f"Beats the active method ({LABELS.get(active.name, active.name)})",
            "required": "Lower error in at least two thirds of events, interval above zero",
            "observed": None if win is None else f"{win:.0%} of events",
            "met": (None if win is None or ci is None
                    else (win >= ah.MIN_EVENT_WIN_SHARE and ci > 0)),
        })

    all_met = all(c["met"] is True for c in criteria)
    if mv.blocked_reason:
        verdict, why = V.BLOCKED, f"{mv.blocked_reason} {detail}"
    elif status in (ah.INSUFFICIENT, ah.PROVISIONAL):
        verdict, why = V.INSUFFICIENT, detail
    elif all_met:
        verdict, why = V.RECOMMENDED, detail
    else:
        failed = [c["label"] for c in criteria if c["met"] is not True]
        verdict, why = V.NOT_RECOMMENDED, f"{detail} Not met: {'; '.join(failed)}."
    return CandidateEvaluation(
        model=mv, verdict=verdict, detail=why[:400], holdout_from=holdout_from,
        training_cutoff=None, n_pairs=metrics["n_pairs"], n_events=metrics["n_events"],
        n_cycles=metrics["n_cycles"], metrics=ah._jsonable({**metrics, **extra}),
        criteria=ah._jsonable(criteria), limitations=_limitations(basis),
    )


def evaluate_candidates(
    *, holdout_from: dt.date | None = None, until: dt.date | None = None, persist: bool = True
) -> list[CandidateEvaluation]:
    """Score every registered method on the usable SBI refreshes (on or after `holdout_from`) and,
    unless persist is False, store one insert-only CandidateEvaluation per method."""
    registry.ensure_registry()
    pairs = ah.pairs_from_series(ah.load_series(until=until))
    evaluated = mc.evaluate(pairs, until=until)
    usable = [e for e in evaluated
              if e.usable and (holdout_from is None or e.pair.event_date >= holdout_from)]
    rows = [
        ah.ScoredRow(e.pair.csp, e.pair.event_date, e.pair.new_value,
                     {name: rule(e) for name, rule in RULES.items()})
        for e in usable
    ]
    basis = {
        "pairs_total": len(pairs),
        "pairs_with_flow_coverage": sum(1 for e in evaluated if e.usable),
        "pairs_dropped_low_coverage": sum(1 for e in evaluated if not e.usable),
        "min_coverage": mc.MIN_COVERAGE,
        "held_out_pairs": len(rows),
        "holdout_from": holdout_from.isoformat() if holdout_from else None,
    }
    active = registry.active_model()
    out = []
    for mv in registry.registered():
        if mv.stage == ModelVersion.Stage.BASELINE or mv.name not in RULES:
            continue
        ev = _evaluate_one(mv, rows, active, basis, holdout_from)
        if persist:
            ev.save()
        out.append(ev)
    return out
