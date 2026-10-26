"""
What the Agent Learning Centre and the learning API show, assembled from the stored records.

Nothing here computes a new number that is not derived from stored predictions, outcomes,
evaluations or events; everything the accuracy engine will not claim is withheld here too, and the
reasons an estimate cannot yet be trusted are listed in plain words.
"""

from __future__ import annotations

import datetime as dt

from bankflow.models import HypothesisRun, ModelVersion
from django.db.models import Count

from . import accuracy, registry
from .models import (
    CandidateEvaluation,
    LivePrediction,
    ModelEvent,
    PredictionFeedback,
    PredictionOutcome,
)

LABELS = {
    registry.BASELINE: "Carry-forward (last SBI value, unchanged)",
    registry.MOVEMENT: "Account growth and transaction flow",
    registry.FLOW_ONLY: "Transaction flow only",
    registry.ACCOUNT_COUNT_ONLY: "Account count only",
}
QUALITY_WORDS = {
    "OK": "Good data",
    "STATEMENT_GAP": "Bank statements were missing or incomplete",
    "UNMATCHED_CODES": "Many transactions could not be matched to a CSP",
    "STALE_SOURCE": "The SBI value was stale when the estimate was made",
    "COUNT_UNRELIABLE": "The account count reading was blank or stale",
    "PROVISIONAL_DATA": "The estimate was made from incomplete data",
}


STATUS_WORDS = {
    "NOT_RUN": "Not tested yet",
    "VALIDATED": "Validated",
    "PROVISIONAL": "Promising, not proven",
    "INSUFFICIENT_EVIDENCE": "Not enough evidence yet",
    "NOT_SUPPORTED": "Not supported by the data",
}


def label(name: str) -> str:
    return LABELS.get(name, name)


def _plain_verdict(ev: CandidateEvaluation) -> str:
    """What an evaluation found, in everyday words."""
    sofar = (f"{ev.n_pairs} comparison{'' if ev.n_pairs == 1 else 's'} over {ev.n_events} SBI "
             f"refresh day{'' if ev.n_events == 1 else 's'} in {ev.n_cycles} "
             f"month{'' if ev.n_cycles == 1 else 's'}")
    V = CandidateEvaluation.Verdict
    if ev.verdict == V.BLOCKED:
        return "A business rule stops this method being used. It is scored for evidence only."
    if ev.verdict == V.RECOMMENDED:
        return f"Met every test ({sofar}). A person must still approve switching to it."
    if ev.verdict == V.NOT_RECOMMENDED:
        failed = [c["label"] for c in ev.criteria if c.get("met") is not True]
        return f"Enough data ({sofar}) but it did not pass: {'; '.join(failed).lower()}."
    return (f"Only {sofar} so far. Not enough yet: at least 30 comparisons over 8 refresh days "
            "and 6 months are needed.")


def _eval_row(ev: CandidateEvaluation | None) -> dict | None:
    if ev is None:
        return None
    metrics = ev.metrics or {}
    cand = (metrics.get("candidates") or {}).get(ev.model.name) or {}
    base = (metrics.get("candidates") or {}).get(registry.BASELINE) or {}
    cmp_ = metrics.get("primary_vs_carry_forward") or {}
    return {
        "id": ev.pk, "verdict": ev.verdict, "verdict_label": ev.get_verdict_display(),
        "detail": ev.detail, "plain": _plain_verdict(ev), "created_at": ev.created_at,
        "n_pairs": ev.n_pairs,
        "n_events": ev.n_events, "n_cycles": ev.n_cycles, "criteria": ev.criteria,
        "holdout_from": ev.holdout_from, "training_cutoff": ev.training_cutoff,
        "candidate_mae": cand.get("mae"), "carry_forward_mae": base.get("mae"),
        "relative_gain": cmp_.get("relative_mae_gain"),
        "gain_ci95": cmp_.get("mae_gain_ci95_over_events"),
        "win_share": cmp_.get("event_win_share"),
        "vs_active": metrics.get("vs_active"), "stability": metrics.get("stability"),
        "limitations": ev.limitations,
    }


def models() -> list[dict]:
    """Every registered method with its stage, what blocks promotion, and its latest evaluation."""
    out = []
    for mv in registry.registered():
        ev = registry.latest_evaluation(mv)
        out.append({
            "name": mv.name, "version": mv.version, "label": label(mv.name), "stage": mv.stage,
            "stage_label": mv.get_stage_display(), "description": mv.description,
            "live_supported": mv.live_supported, "blocked_reason": mv.blocked_reason,
            "training_cutoff": mv.training_cutoff,
            "promotion_problems": (registry.promotion_problems(mv)
                                   if mv.stage == ModelVersion.Stage.CANDIDATE else []),
            "latest_evaluation": _eval_row(ev),
        })
    return out


def history(limit: int = 30) -> list[dict]:
    return [{
        "at": e.at, "kind": e.kind, "kind_label": e.get_kind_display(), "model": e.model.name,
        "model_label": label(e.model.name),
        "previous": e.previous.name if e.previous else None, "actor": e.actor, "reason": e.reason,
        "evaluation_verdict": e.evaluation.verdict if e.evaluation else None,
        "snapshot": e.snapshot,
    } for e in ModelEvent.objects.select_related("model", "previous", "evaluation")[:limit]]


def active() -> dict:
    """The active method and what is known about how far to trust it."""
    mv = registry.active_model()
    if mv is None:
        return {"model": None, "text": "No method is registered as active yet."}
    ev = registry.latest_evaluation(mv)
    run = HypothesisRun.objects.first()
    return {
        "model": mv.name, "label": label(mv.name), "description": mv.description,
        "stage": mv.stage,
        "evaluation": _eval_row(ev),
        "estimator_status": run.status if run else "NOT_RUN",
        "estimator_status_label": STATUS_WORDS.get(run.status if run else "NOT_RUN", "Unknown"),
        "production_approval": "Not granted",
        "text": ("This method produces the estimates you see. They are shadow estimates: they are "
                 "never written to the SBI balance and never used for incentive slabs."),
    }


def counts() -> dict:
    total = LivePrediction.objects.count()
    scored = PredictionOutcome.objects.count()
    by_quality = dict(PredictionOutcome.objects.values_list("quality").annotate(n=Count("id")))
    return {"predictions": total, "scored": scored, "waiting": total - scored,
            "scored_by_quality": by_quality}


def comparisons(limit: int = 25) -> list[dict]:
    """The latest outcomes, estimated against observed: the facts behind every metric."""
    out = []
    rows = (PredictionOutcome.objects
            .select_related("prediction", "prediction__csp", "prediction__model")
            .annotate(n_feedback=Count("prediction__feedback"))[:limit])
    for o in rows:
        p = o.prediction
        out.append({
            "prediction_id": p.pk, "csp_code": p.csp_id, "csp_name": p.csp.name,
            "estimate_date": p.estimate_date, "observed_on": o.actual_first_seen,
            "horizon_days": o.horizon_days, "estimated": p.predicted_value,
            "observed": o.actual_value, "carry_forward": p.carry_forward_value,
            "error": o.error_amount, "direction": o.direction,
            "direction_label": o.get_direction_display(), "quality": o.quality,
            "quality_label": QUALITY_WORDS.get(o.quality, o.quality),
            "model": p.model.name, "feedback_count": o.n_feedback,
        })
    return out


def feedback_waiting(limit: int = 20) -> list[dict]:
    rows = (PredictionFeedback.objects
            .filter(status__in=[PredictionFeedback.Status.OPEN,
                                PredictionFeedback.Status.IN_REVIEW])
            .select_related("prediction", "prediction__csp").order_by("submitted_at")[:limit])
    return [{
        "id": f.pk, "csp_code": f.prediction.csp_id, "csp_name": f.prediction.csp.name,
        "estimate_date": f.prediction.estimate_date, "verdict": f.verdict,
        "verdict_label": f.get_verdict_display(), "reason": f.reason,
        "reason_label": f.get_reason_display() if f.reason else "", "note": f.note,
        "status": f.status, "status_label": f.get_status_display(), "by": f.submitted_by,
        "at": f.submitted_at,
    } for f in rows]


def why_not_trusted(report: dict, cnt: dict) -> list[str]:
    """Plain-language reasons the estimate cannot yet be relied on, from the stored facts."""
    reasons = []
    ev = report["evidence"]
    if cnt["predictions"] == 0:
        reasons.append("No predictions have been saved yet, so there is nothing to check against "
                       "SBI's later figures. Run the freeze step after estimates are final.")
    elif cnt["scored"] == 0:
        reasons.append(f"{cnt['predictions']} predictions are saved but SBI has not yet reported "
                       "a new figure for any of those CSPs, so none can be checked.")
    if report["accuracy_claim"] == accuracy.INSUFFICIENT and cnt["scored"]:
        reasons.append(f"Only {ev['n_outcomes']} estimate(s) have been checked against SBI so far; "
                       "the box above says how many more are needed.")
    dq = report["data_quality"]
    for q, n in dq["problems"].items():
        reasons.append(f"{n} checked prediction(s): {QUALITY_WORDS.get(q, q).lower()}. These are "
                       "left out of the accuracy figures so a data problem is not blamed on "
                       "the model.")
    if cnt["waiting"] and cnt["scored"]:
        reasons.append(f"{cnt['waiting']} saved predictions are still waiting for SBI's next "
                       "figure.")
    return reasons


def learning_overview(*, now: dt.datetime | None = None) -> dict:
    registry_ready = ModelVersion.objects.filter(in_registry=True).exists()
    cnt = counts()
    report = accuracy.accuracy_report()
    return {
        "registry_ready": registry_ready,
        "active": active() if registry_ready else None,
        "counts": cnt, "accuracy": report,
        "comparisons": comparisons(), "feedback_waiting": feedback_waiting(),
        "feedback_open_total": PredictionFeedback.objects.filter(
            status__in=["OPEN", "IN_REVIEW"]).count(),
        "models": models() if registry_ready else [],
        "history": history() if registry_ready else [],
        "why_not_trusted": why_not_trusted(report, cnt),
        "quality_words": QUALITY_WORDS,
    }
