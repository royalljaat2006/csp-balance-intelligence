"""
Account-count hypothesis test -- now a DIAGNOSTIC only.

The business rule is that an account-count change never scales the combined balance, so the
proportional rule below is no longer a candidate estimator; it is still scored (and stored under
`account_count_rule_diagnostic`) so the evidence remains visible. The estimator that is validated
is the movement rule (bankflow.model_comparison).

Hypothesis under test (NOT assumed true): when a CSP's account count rises from
n0 to n1 between two SBI Avg Balance refreshes, the new Avg Balance is
a0 * n1 / n0 (e.g. 350 -> 360 gives +2.857%).

The Calling Sheet "Avg Balance" is a per-account average (total / accounts, see
targets.target_report), so the *primary* candidate above says the average rises
with the count. Two other candidates are scored for context only, because they
describe the opposite economics and the data can tell them apart:

  carry_forward          a1 = a0                  (count change has no effect)
  proportional  [PRIMARY] a1 = a0 * n1 / n0       (the stated hypothesis)
  total_constant         a1 = a0 * n0 / n1        (new accounts hold nothing)

Only `proportional` can make the report VALIDATED; the others are descriptive,
so picking the best of three is never what passes the test.

Observations: DailyBalance rows excluding DEMO and CARRY_FORWARD (neither is an
SBI observation). A *refresh* is a CSP's value changing between consecutive
observation dates; the count used for a value is the count on the first date
that value was seen (the sheet has no measurement-date column, so this is an
approximation and is reported as a limitation). A pair is (previous refreshed
value, next refreshed value); the candidates predict the next value from the
previous value and the two counts only -- the next value is never an input, so
there is no look-ahead. Pairs are grouped by refresh event (the date the next
value first appeared); metrics are per event, then averaged, because CSPs that
refresh together are not independent.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from common.cache import bumps_generation
from csp.models import DailyBalance

from bankflow.evaluation import _bootstrap_ci

PRIMARY = "proportional"
CANDIDATES = ("carry_forward", PRIMARY, "total_constant")

# Pre-registered thresholds (also written into every run's config).
MIN_EVENTS_VALIDATED = 8
MIN_PAIRS_VALIDATED = 30
MIN_EVENTS_PROVISIONAL = 3
MIN_PAIRS_PROVISIONAL = 10
MIN_CYCLES_VALIDATED = 6
MIN_CYCLES_PROVISIONAL = 2
MIN_REL_GAIN = 0.05
MIN_EVENT_WIN_SHARE = 2 / 3
BOOTSTRAP_REPS = 2000
BOOTSTRAP_SEED = 20261013

VALIDATED = "VALIDATED"
PROVISIONAL = "PROVISIONAL"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
NOT_SUPPORTED = "NOT_SUPPORTED"


@dataclass(frozen=True)
class Pair:
    csp: str
    event_date: dt.date  # first date the NEW value was observed
    prev_date: dt.date  # first date the PREVIOUS value was observed
    prev_value: Decimal
    new_value: Decimal
    prev_count: int
    new_count: int

    @property
    def count_changed(self) -> bool:
        return self.new_count != self.prev_count


def predict(candidate: str, prev_value: Decimal, prev_count: int, new_count: int) -> Decimal:
    """Candidate prediction. Takes ONLY the previous value and the two counts."""
    if candidate == "carry_forward" or prev_count <= 0 or new_count <= 0:
        return prev_value
    if candidate == PRIMARY:
        return prev_value * Decimal(new_count) / Decimal(prev_count)
    if candidate == "total_constant":
        return prev_value * Decimal(prev_count) / Decimal(new_count)
    raise ValueError(f"unknown candidate {candidate!r}")


def pairs_from_series(series: dict[str, list[tuple[dt.date, Decimal, int | None]]]) -> list[Pair]:
    """series: csp -> chronologically sorted (date, avg, account_count) observations."""
    pairs: list[Pair] = []
    for csp, rows in series.items():
        # collapse to refreshes: first date each successive distinct value appears
        refreshes: list[tuple[dt.date, Decimal, int | None]] = []
        for d, avg, n in sorted(rows, key=lambda r: r[0]):
            if not refreshes or avg != refreshes[-1][1]:
                refreshes.append((d, avg, n))
        for (d0, a0, n0), (d1, a1, n1) in zip(refreshes, refreshes[1:], strict=False):
            if n0 is None or n1 is None or n0 <= 0 or n1 <= 0 or a0 <= 0:
                continue  # missing/zero baseline: no valid pair, never imputed
            pairs.append(Pair(csp, d1, d0, a0, a1, n0, n1))
    return pairs


def load_series(
    *, until: dt.date | None = None
) -> dict[str, list[tuple[dt.date, Decimal, int | None]]]:
    qs = DailyBalance.objects.exclude(
        source__in=[DailyBalance.Source.DEMO, DailyBalance.Source.CARRY_FORWARD]
    )
    if until is not None:
        qs = qs.filter(balance_date__lte=until)
    out: dict[str, list] = defaultdict(list)
    for csp_id, d, avg, n in qs.order_by("csp_id", "balance_date").values_list(
        "csp_id", "balance_date", "daily_avg_balance", "account_count"
    ).iterator():
        out[csp_id].append((d, avg, n))
    return dict(out)


def _pct(err: float, actual: Decimal) -> float | None:
    return None if actual == 0 else abs(err) / float(actual) * 100


@dataclass(frozen=True)
class ScoredRow:
    """One held-out prediction problem: the realised value and every candidate's prediction."""

    csp: str
    event_date: dt.date
    actual: Decimal
    preds: dict


def _cycle(d: dt.date) -> tuple[int, int]:
    return (d.year, d.month)


def score_rows(rows: list[ScoredRow], candidates: tuple, primary: str) -> dict:
    """Pure scoring of candidates vs carry_forward, grouped by refresh event. `n_cycles` counts
    distinct calendar months of events: CSPs that refresh on different days of one month are one
    refresh cycle, not several independent ones."""
    by_event: dict[dt.date, list[ScoredRow]] = defaultdict(list)
    for r in rows:
        by_event[r.event_date].append(r)

    per_candidate: dict[str, dict] = {}
    event_mae: dict[str, dict[dt.date, float]] = {c: {} for c in candidates}
    for c in candidates:
        abs_errs: list[float] = []
        pct_errs: list[float] = []
        signed: list[float] = []
        for ev, rs in by_event.items():
            ev_errs = []
            for r in rs:
                e = float(r.preds[c] - r.actual)
                ev_errs.append(abs(e))
                abs_errs.append(abs(e))
                signed.append(e)
                pe = _pct(e, r.actual)
                if pe is not None:
                    pct_errs.append(pe)
            event_mae[c][ev] = statistics.fmean(ev_errs)
        per_candidate[c] = {
            "mae": statistics.fmean(abs_errs) if abs_errs else None,
            "mae_event_mean": statistics.fmean(event_mae[c].values()) if event_mae[c] else None,
            "median_abs_error": statistics.median(abs_errs) if abs_errs else None,
            "mape_pct": statistics.fmean(pct_errs) if pct_errs else None,
            "median_ape_pct": statistics.median(pct_errs) if pct_errs else None,
            "p90_ape_pct": (
                sorted(pct_errs)[min(len(pct_errs) - 1, int(0.9 * len(pct_errs)))]
                if pct_errs
                else None
            ),
            "bias": statistics.fmean(signed) if signed else None,
        }

    n_events = len(by_event)
    base = event_mae["carry_forward"]
    prim = event_mae[primary]
    gains = [base[e] - prim[e] for e in by_event]  # > 0 means the candidate helped
    wins = sum(1 for g in gains if g > 0)
    base_mean = statistics.fmean(base.values()) if base else None
    rel_gain = statistics.fmean(gains) / base_mean if gains and base_mean else None
    ci = _bootstrap_ci(gains, BOOTSTRAP_REPS, BOOTSTRAP_SEED) if n_events >= 2 else (None, None)
    return {
        "n_pairs": len(rows),
        "n_events": n_events,
        "n_cycles": len({_cycle(e) for e in by_event}),
        "n_csps": len({r.csp for r in rows}),
        "first_event": min(by_event) if by_event else None,
        "last_event": max(by_event) if by_event else None,
        "primary": primary,
        "candidates": per_candidate,
        "primary_vs_carry_forward": {
            "events_primary_better": wins,
            "event_win_share": (wins / n_events) if n_events else None,
            "relative_mae_gain": rel_gain,
            "mae_gain_ci95_over_events": list(ci),
        },
    }


def evaluate_pairs(pairs: list[Pair], *, only_count_changed: bool = True) -> dict:
    """Growth-only hypothesis: score every account-count candidate on the supplied pairs."""
    used = [p for p in pairs if p.count_changed] if only_count_changed else list(pairs)
    rows = [
        ScoredRow(
            p.csp, p.event_date, p.new_value,
            {c: predict(c, p.prev_value, p.prev_count, p.new_count) for c in CANDIDATES},
        )
        for p in used
    ]
    m = score_rows(rows, CANDIDATES, PRIMARY)
    # directional sanity: when the count rose, did the value rise?
    up = [p for p in used if p.new_count > p.prev_count]
    m["n_pairs_all"] = len(pairs)
    m["count_up_value_rose"] = {
        "count_up_pairs": len(up),
        "value_rose": sum(1 for p in up if p.new_value > p.prev_value),
    }
    return m


def classify(m: dict, label: str = "Proportional rule") -> tuple[str, str]:
    """(status, detail) from the metrics -- the single place verdicts are decided.
    Independence is by refresh EVENT and by calendar-month CYCLE: validation needs both."""
    n_e, n_p, n_c = m["n_events"], m["n_pairs"], m.get("n_cycles", m["n_events"])
    cmp_ = m["primary_vs_carry_forward"]
    if n_e < MIN_EVENTS_PROVISIONAL or n_p < MIN_PAIRS_PROVISIONAL or n_c < MIN_CYCLES_PROVISIONAL:
        return INSUFFICIENT, (
            f"{n_p} pairs over {n_e} refresh event(s) / {n_c} monthly cycle(s); need at least "
            f"{MIN_PAIRS_PROVISIONAL} pairs / {MIN_EVENTS_PROVISIONAL} events / "
            f"{MIN_CYCLES_PROVISIONAL} cycles to say anything and {MIN_PAIRS_VALIDATED} / "
            f"{MIN_EVENTS_VALIDATED} / {MIN_CYCLES_VALIDATED} to validate."
        )
    gain = cmp_["relative_mae_gain"] or 0.0
    lo = cmp_["mae_gain_ci95_over_events"][0]
    full = (
        n_e >= MIN_EVENTS_VALIDATED and n_p >= MIN_PAIRS_VALIDATED and n_c >= MIN_CYCLES_VALIDATED
    )
    passes = (
        full
        and (cmp_["event_win_share"] or 0) >= MIN_EVENT_WIN_SHARE
        and gain >= MIN_REL_GAIN
        and lo is not None
        and lo > 0
    )
    if passes:
        return VALIDATED, (
            f"{label} beat carry-forward in {cmp_['events_primary_better']}/{n_e} events "
            f"(MAE gain {gain:.1%}, CI above 0)."
        )
    if full:
        return NOT_SUPPORTED, (
            f"Enough data ({n_p} pairs, {n_e} events, {n_c} cycles) but {label.lower()} missed the "
            f"pre-registered criteria (win share {cmp_['event_win_share']:.0%}, "
            f"MAE gain {gain:.1%})."
        )
    if gain > 0:
        return PROVISIONAL, (
            f"{label} is ahead of carry-forward on {n_p} pairs / {n_e} events / {n_c} cycles "
            f"(MAE gain {gain:.1%}) but below the {MIN_PAIRS_VALIDATED}-pair / "
            f"{MIN_EVENTS_VALIDATED}-event / {MIN_CYCLES_VALIDATED}-cycle bar for validation."
        )
    return INSUFFICIENT, (
        f"{n_p} pairs / {n_e} events: {label.lower()} is not ahead of carry-forward "
        f"(MAE gain {gain:.1%}); too little data to conclude either way."
    )


_RANK = {VALIDATED: 3, PROVISIONAL: 2, INSUFFICIENT: 1, NOT_SUPPORTED: 0}


@bumps_generation
def run_hypothesis_test(*, persist: bool = True, until: dt.date | None = None):
    """Evaluate the movement estimator on DailyBalance + statement history; returns
    (metrics, status, detail, run|None).

    The estimator under test is the agreed movement rule (bankflow.movement), scored against
    carry-forward on held-out SBI refreshes. The old account-count rule is scored too but only as
    a stored diagnostic: it can never be selected. `selected_method` is `movement` when the
    verdict is VALIDATED or PROVISIONAL, otherwise `carry_forward`."""
    from bankflow import model_comparison
    from bankflow.models import HypothesisRun

    series = load_series(until=until)
    pairs = pairs_from_series(series)
    comparison = model_comparison.compare(pairs, until=until)
    res = comparison["methods"][model_comparison.METHOD]
    status, detail, metrics = res["status"], res["detail"], dict(res["metrics"])
    selected = (
        model_comparison.METHOD if status in (VALIDATED, PROVISIONAL) else "carry_forward"
    )

    growth = evaluate_pairs(pairs)
    g_status, g_detail = classify(growth, "Account-count rule")
    all_dates = sorted({d for rows in series.values() for d, _, _ in rows})
    metrics["n_pairs_all"] = len(pairs)
    metrics["comparison"] = {
        "basis": comparison["basis"],
        "selected": selected,
        "methods": {k: {"status": v["status"], "detail": v["detail"], "metrics": v["metrics"],
                        "parameters": v["parameters"]} for k, v in comparison["methods"].items()},
    }
    metrics["account_count_rule_diagnostic"] = {
        "status": g_status, "detail": g_detail, "metrics": growth,
        "note": "Diagnostic only. An account-count change does not scale the combined balance.",
    }
    config = {
        "estimator": model_comparison.METHOD,
        "reference": "carry_forward",
        "min_events_validated": MIN_EVENTS_VALIDATED,
        "min_pairs_validated": MIN_PAIRS_VALIDATED,
        "min_cycles_validated": MIN_CYCLES_VALIDATED,
        "min_events_provisional": MIN_EVENTS_PROVISIONAL,
        "min_pairs_provisional": MIN_PAIRS_PROVISIONAL,
        "min_cycles_provisional": MIN_CYCLES_PROVISIONAL,
        "min_rel_gain": MIN_REL_GAIN,
        "min_event_win_share": MIN_EVENT_WIN_SHARE,
        "p90_limit_pct": 5.0,
        "min_flow_coverage": model_comparison.MIN_COVERAGE,
        "bootstrap_reps": BOOTSTRAP_REPS,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "until": until.isoformat() if until else None,
        "observation_dates": len(all_dates),
        "first_observation": all_dates[0].isoformat() if all_dates else None,
        "last_observation": all_dates[-1].isoformat() if all_dates else None,
        "limitations": [
            "The count used for a value is the count on the first date the value was seen; the "
            "sheet has no measurement date.",
            "Avg Balance is a per-account average; refresh times differ per CSP and are unknown, "
            "and a refresh that repeats the previous value cannot be detected.",
            "The movement rule has no fitted parameters, so no pair is used for training.",
            "Only proven-complete statement days enter the interval's movement, and a pair needs "
            "at least 80% of its days covered; pairs below that are excluded and counted.",
            "Event dates are the day the new value was first seen, not SBI's own measurement date.",
        ],
    }
    run = None
    if persist:
        run = HypothesisRun.objects.create(
            status=status,
            detail=detail[:300],
            n_pairs=metrics["n_pairs"],
            n_events=metrics["n_events"],
            metrics=_jsonable(metrics),
            config=config,
            selected_method=selected,
            parameters={},
        )
    return metrics, status, detail, run


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_jsonable(x) for x in v]
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v
