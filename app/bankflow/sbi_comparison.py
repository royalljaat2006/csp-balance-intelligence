"""
Estimate vs SBI-reported value -- read-only evidence for the validation gate.

Every row is one SBI refresh of one CSP (a change in its Avg Balance between two readings). The
prediction is made from the PREVIOUS reading and the two account counts only -- the reported value
is never an input -- using the existing `account_hypothesis.predict` (the account-count rule, the
only rule the estimator is allowed to apply). The carry-forward baseline is the previous reading.

This module only compares and summarises. Verdicts come from the stored `HypothesisRun`
(`account_hypothesis.classify`); nothing here marks the predictor validated.

Percentage variance = (predicted - reported) / reported x 100. When the reported value is zero it
is N/A and the row is left out of every percentage average.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from decimal import Decimal

from bankflow import account_hypothesis as ah
from bankflow.models import HypothesisRun

ZERO = Decimal(0)
CENT = Decimal("0.01")

#: the agreed acceptance criteria (see the product brief). Thresholds for events, cycles, win share,
#: gain and the confidence interval are the ones the stored verdict already uses.
P90_LIMIT_PCT = 5.0


def _pct(pred: Decimal, reported: Decimal) -> Decimal | None:
    return None if reported == 0 else ((pred - reported) / reported * 100)


def _row(p: ah.Pair) -> dict:
    pred = ah.predict(ah.PRIMARY, p.prev_value, p.prev_count, p.new_count)
    cf = p.prev_value
    return {
        "csp_code": p.csp, "event_date": p.event_date, "previous_date": p.prev_date,
        "previous_value": p.prev_value, "reported": p.new_value, "predicted": pred.quantize(CENT),
        "carry_forward": cf, "previous_count": p.prev_count, "new_count": p.new_count,
        "count_changed": p.count_changed,
        "difference": (pred - p.new_value).quantize(CENT),
        "pct_variance": _pct(pred, p.new_value),
        "carry_forward_difference": (cf - p.new_value).quantize(CENT),
        "carry_forward_pct_variance": _pct(cf, p.new_value),
    }


def _mean(values: list) -> float | None:
    return statistics.fmean(float(v) for v in values) if values else None


def summarise(rows: list[dict]) -> dict:
    """Aggregates over the given rows.

    aggregate_*   totals of COMBINED balances (per-account value x the CSP's new account count), so
                  a large CSP weighs more than a small one;
    simple_avg_*  the plain mean of the per-CSP % variances, zero-reported rows excluded."""
    n = len(rows)
    pct = [r["pct_variance"] for r in rows if r["pct_variance"] is not None]
    cf_pct = [r["carry_forward_pct_variance"] for r in rows
              if r["carry_forward_pct_variance"] is not None]
    sum_pred = sum((r["predicted"] * r["new_count"] for r in rows), ZERO)
    sum_cf = sum((r["carry_forward"] * r["new_count"] for r in rows), ZERO)
    sum_rep = sum((r["reported"] * r["new_count"] for r in rows), ZERO)
    mae = _mean([abs(r["difference"]) for r in rows])
    cf_mae = _mean([abs(r["carry_forward_difference"]) for r in rows])
    return {
        "valid_comparisons": n, "excluded_zero_reported": n - len(pct),
        "aggregate": {
            "predicted_total": sum_pred, "carry_forward_total": sum_cf,
            "reported_total": sum_rep,
            "variance_pct": float((sum_pred - sum_rep) / sum_rep * 100) if sum_rep else None,
            "carry_forward_variance_pct": (float((sum_cf - sum_rep) / sum_rep * 100)
                                           if sum_rep else None),
            "basis": "Totals of per-account value x account count, over the comparisons shown.",
        },
        "simple_avg_pct_variance": _mean(pct),
        "simple_avg_abs_pct_variance": _mean([abs(x) for x in pct]),
        "carry_forward_simple_avg_pct_variance": _mean(cf_pct),
        "mean_abs_error": mae, "carry_forward_mean_abs_error": cf_mae,
        "beats_carry_forward": (mae < cf_mae) if mae is not None and cf_mae is not None else None,
    }


def _events(rows: list[dict]) -> list[dict]:
    by: dict = defaultdict(list)
    for r in rows:
        by[r["event_date"]].append(r)
    out = []
    for day in sorted(by, reverse=True):
        rs = by[day]
        mae = _mean([abs(r["difference"]) for r in rs])
        cf = _mean([abs(r["carry_forward_difference"]) for r in rs])
        out.append({"event_date": day, "month": f"{day:%Y-%m}", "comparisons": len(rs),
                    "mean_abs_error": mae, "carry_forward_mean_abs_error": cf,
                    "model_won": (mae < cf) if mae is not None and cf is not None else None})
    return out


def _cycles(events: list[dict]) -> list[dict]:
    by: dict = defaultdict(list)
    for e in events:
        by[e["month"]].append(e)
    return [{"month": m, "events": len(es), "comparisons": sum(e["comparisons"] for e in es),
             "wins": sum(1 for e in es if e["model_won"])} for m, es in sorted(by.items())]


def _method_metrics(run: HypothesisRun) -> dict:
    """Stored metrics for the account-count rule (the only rule the estimator may apply)."""
    return run.metrics or {}


def gate(run: HypothesisRun | None) -> dict:
    """The agreed acceptance criteria, each with the stored observation. Never a verdict of its
    own: `satisfied` is True only when every computable criterion is met, and even then the
    separate approval is outstanding, so the predictor stays shadow-only."""
    if run is None:
        return {
            "has_run": False, "status": "NOT_RUN", "satisfied": False, "criteria": [],
            "text": "No validation run exists yet. The predictor has not been evaluated.",
            "approval": "Not granted",
        }
    m = _method_metrics(run)
    cmp_ = m.get("primary_vs_carry_forward") or {}
    p90 = ((m.get("candidates") or {}).get(ah.PRIMARY) or {}).get("p90_ape_pct")
    ci = cmp_.get("mae_gain_ci95_over_events") or [None, None]
    n_events, n_cycles, n_pairs = m.get("n_events", 0), m.get("n_cycles", 0), m.get("n_pairs", 0)
    win, gain = cmp_.get("event_win_share"), cmp_.get("relative_mae_gain")

    def c(label, required, observed, met):
        return {"label": label, "required": required, "observed": observed, "met": met}

    criteria = [
        c("90th-percentile absolute % error on held-out refreshes", f"{P90_LIMIT_PCT:g}% or less",
          None if p90 is None else f"{p90:.2f}%", None if p90 is None else p90 <= P90_LIMIT_PCT),
        c("Beats carry-forward", "Lower error than carry-forward",
          None if gain is None else (f"{gain:.1%} lower error" if gain > 0
                                     else f"{-gain:.1%} higher error"),
          None if gain is None else gain > 0),
        c("SBI refresh events", f"At least {ah.MIN_EVENTS_VALIDATED}", str(n_events),
          n_events >= ah.MIN_EVENTS_VALIDATED),
        c("Monthly cycles", f"At least {ah.MIN_CYCLES_VALIDATED}", str(n_cycles),
          n_cycles >= ah.MIN_CYCLES_VALIDATED),
        c("Events won against carry-forward", "At least two thirds",
          None if win is None else f"{win:.0%}",
          None if win is None else win >= ah.MIN_EVENT_WIN_SHARE),
        c("Gain over carry-forward", f"At least {ah.MIN_REL_GAIN:.0%}",
          None if gain is None else f"{gain:.1%}",
          None if gain is None else gain >= ah.MIN_REL_GAIN),
        c("Confidence interval for the gain", "Lower bound above zero",
          None if ci[0] is None else f"{ci[0]:,.2f}", None if ci[0] is None else ci[0] > 0),
        c("Comparisons (system requirement)", f"At least {ah.MIN_PAIRS_VALIDATED}", str(n_pairs),
          n_pairs >= ah.MIN_PAIRS_VALIDATED),
    ]
    met = all(x["met"] is True for x in criteria)
    return {
        "has_run": True, "status": run.status, "detail": run.detail, "run_at": run.created_at,
        "selected_method": run.selected_method, "criteria": criteria, "satisfied": met,
        "approval": "Not granted",
        "text": ("All measurable criteria are met, but a separate approval is still required "
                 "before any production use. The predictor remains shadow-only." if met else
                 "The criteria are not all met. The predictor remains shadow-only."),
    }


def sbi_comparison(
    *, only_count_changed: bool = True, search: str = "", limit: int = 50, offset: int = 0
) -> dict:
    pairs = ah.pairs_from_series(ah.load_series())
    rows_all = [_row(p) for p in pairs]
    rows = [r for r in rows_all if r["count_changed"]] if only_count_changed else rows_all
    needle = search.strip().lower()
    if needle:
        rows = [r for r in rows if needle in r["csp_code"].lower()]
    rows.sort(key=lambda r: (r["event_date"], r["csp_code"]), reverse=True)
    events = _events(rows)
    run = HypothesisRun.objects.first()
    return {
        "only_count_changed": only_count_changed,
        "total_refreshes": len(rows_all),
        "summary": summarise(rows),
        "events": events, "cycles": _cycles(events),
        "gate": gate(run),
        "rows": rows[offset: offset + limit], "total_rows": len(rows),
        "unit": "rupees per account (SBI Avg Balance is a per-account average)",
        "notes": [
            "Each row is an SBI refresh. The prediction uses only the previous reading and the "
            "account counts, never the reported value.",
            "A reported value of zero has no percentage variance (N/A) and is left out of the "
            "percentage averages.",
            "These are observed comparisons. Insufficient evidence is not a pass.",
        ],
    }
