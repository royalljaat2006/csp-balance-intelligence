"""
The learning loop over the shadow Avg Balance estimate -- frozen predictions, how they scored
against the SBI values that followed, human feedback, and the candidate methods.

Reads use scope `report:read`; submitting feedback needs the separate `feedback:write` scope, so a
read-only key can never write. Feedback is an opinion and is stored apart from the SBI-observed
outcome: it has no numeric field and never changes an outcome or an accuracy figure. Estimated
values are always labelled predictions and are never actual balances.
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any

from django.shortcuts import get_object_or_404
from learning import accuracy, feedback, overview
from learning.models import LivePrediction, PredictionFeedback
from ninja import Router, Schema, Status
from ninja.errors import HttpError

from api.auth import RequireScope
from api.middleware import ApiRequest
from api.pagination import clamp_limit, validate_date_range
from api.schemas import DataEnvelope, ListEnvelope, Pagination

router = Router(tags=["learning"])
_READ = "report:read"
_WRITE = "feedback:write"


def _plain(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _prediction(p: LivePrediction) -> dict:
    out = getattr(p, "outcome", None)
    return _plain({
        "id": p.pk, "csp_code": p.csp_id, "model": p.model.name, "model_version": p.model.version,
        "methodology": p.methodology, "horizon": p.horizon,
        "created_at": p.created_at, "estimate_date": p.estimate_date,
        "period": {"start": p.period_start, "end": p.period_end},
        "observed_baseline": {  # SBI-provided; the sheet has no measurement date
            "avg_balance": p.baseline_value, "first_seen": p.baseline_first_seen,
            "last_seen": p.baseline_last_seen, "account_count": p.baseline_account_count,
        },
        "account_counts": {
            "calling_sheet": p.account_count, "pmjdy": p.pmjdy_account_count,
            "pmjdy_snapshot_at": p.pmjdy_snapshot_at,
        },
        "flow": {
            "eligible_deposits": p.flow_deposits, "eligible_withdrawals": p.flow_withdrawals,
            "net_customer_movement": p.net_customer_movement,
            "pool_credits": p.flow_withdrawals, "pool_debits": p.flow_deposits,
            "net_pool_flow": p.flow_withdrawals - p.flow_deposits, "coverage": p.flow_coverage,
        },
        "prediction": {  # model output, NOT an actual balance
            "avg_balance": p.predicted_value, "combined_balance": p.predicted_combined,
            "carry_forward": p.carry_forward_value,
        },
        "confidence": {"status": p.confidence_status, "reason": p.confidence_reason,
                       "statement_state": p.statement_state},
        "outcome": None if out is None else {
            "observed_avg_balance": out.actual_value, "first_seen": out.actual_first_seen,
            "account_count": out.actual_account_count, "horizon_days": out.horizon_days,
            "evaluated_at": out.evaluated_at, "error": out.error_amount,
            "error_direction": out.direction, "abs_error": out.abs_error,
            "pct_error": out.pct_error, "carry_forward_error": out.carry_forward_error,
            "data_quality": out.quality, "data_quality_detail": out.quality_detail,
        },
    })


@router.get(
    "/predictions",
    response=ListEnvelope[dict],
    auth=RequireScope(_READ),
    summary="Frozen predictions, with the SBI value that scored them when it is known",
    description=(
        "Each row is one prediction saved before the SBI value it is about was seen, with every "
        "input that produced it. `outcome` is null until that CSP's next SBI refresh is seen. "
        "The prediction itself is never changed by the outcome."
    ),
)
def list_predictions(
    request: ApiRequest, csp: str = "", date_from: dt.date | None = None,
    date_to: dt.date | None = None, scored: bool | None = None, quality: str = "",
    limit: int = 50, offset: int = 0,
):
    limit = clamp_limit(limit)
    validate_date_range(date_from, date_to)
    qs = LivePrediction.objects.select_related("model", "outcome").order_by(
        "-estimate_date", "csp_id")
    if csp:
        qs = qs.filter(csp_id=csp.strip()[:16])
    if date_from:
        qs = qs.filter(estimate_date__gte=date_from)
    if date_to:
        qs = qs.filter(estimate_date__lte=date_to)
    if scored is not None:
        qs = qs.filter(outcome__isnull=not scored)
    if quality:
        qs = qs.filter(outcome__quality=quality[:20])
    total = qs.count()
    return ListEnvelope(
        data=[_prediction(p) for p in qs[max(0, offset): max(0, offset) + limit]],
        pagination=Pagination(limit=limit, offset=offset, total=total),
        request_id=request.request_id,
    )


@router.get(
    "/predictions/{prediction_id}",
    response=DataEnvelope[dict],
    auth=RequireScope(_READ),
    summary="One frozen prediction",
)
def get_prediction(request: ApiRequest, prediction_id: int):
    p = get_object_or_404(LivePrediction.objects.select_related("model", "outcome"),
                          pk=prediction_id)
    return DataEnvelope(data=_prediction(p), request_id=request.request_id)


@router.get(
    "/evaluation",
    response=DataEnvelope[dict],
    auth=RequireScope(_READ),
    summary="How accurate the predictions have been, or INSUFFICIENT_EVIDENCE",
    description=(
        "Accuracy over scored predictions. `accuracy_claim` is INSUFFICIENT_EVIDENCE until enough "
        "independent refresh events and monthly cycles exist; then `metrics` is null and no "
        "figure is given. Outcomes hurt by data problems are counted apart in `data_quality` and "
        "are not blamed on the model. Signed error is estimate minus SBI value."
    ),
)
def evaluation(request: ApiRequest, model: str = ""):
    return DataEnvelope(data=_plain(accuracy.accuracy_report(model=model[:64] or None)),
                        request_id=request.request_id)


@router.get(
    "/models",
    response=DataEnvelope[dict],
    auth=RequireScope(_READ),
    summary="Candidate and active estimation methods, their evaluations and change history",
)
def list_models(request: ApiRequest):
    return DataEnvelope(
        data=_plain({"active": overview.active(), "models": overview.models(),
                     "history": overview.history()}),
        request_id=request.request_id,
    )


@router.get(
    "/feedback",
    response=ListEnvelope[dict],
    auth=RequireScope(_READ),
    summary="Human feedback on estimates, with its status",
)
def list_feedback(request: ApiRequest, status: str = "", limit: int = 50, offset: int = 0):
    limit = clamp_limit(limit)
    rows, total = feedback.queue(status[:10] or None, limit=limit, offset=max(0, offset))
    return ListEnvelope(
        data=[_plain({
            "id": f.pk, "prediction_id": f.prediction_id, "csp_code": f.prediction.csp_id,
            "verdict": f.verdict, "reason": f.reason, "note": f.note, "status": f.status,
            "submitted_by": f.submitted_by, "submitted_at": f.submitted_at,
            "resolved_by": f.resolved_by, "resolved_at": f.resolved_at,
            "resolution_note": f.resolution_note,
            "events": [{"at": e.at, "actor": e.actor, "action": e.action,
                        "from": e.from_status, "to": e.to_status, "note": e.note}
                       for e in f.events.all()],
        }) for f in rows.prefetch_related("events")],
        pagination=Pagination(limit=limit, offset=offset, total=total),
        request_id=request.request_id,
    )


class FeedbackIn(Schema):
    verdict: str
    reason: str = ""
    note: str = ""


@router.post(
    "/predictions/{prediction_id}/feedback",
    response={201: DataEnvelope[dict]},
    auth=RequireScope(_WRITE),
    summary="Say whether an estimate was useful or looks wrong",
    description=(
        "An opinion, kept apart from SBI's observed value: it has no numeric field and never "
        "changes an outcome or an accuracy figure. `verdict` is USEFUL, LOOKS_WRONG or UNSURE; "
        "`reason` (MISSING_DATA, WRONG_CLASSIFICATION, UNUSUAL_ACTIVITY, MODEL_ERROR, OTHER) is "
        "required unless the verdict is USEFUL. The note must not hold account numbers, phone "
        "numbers, e-mail addresses or names."
    ),
)
def submit_feedback(request: ApiRequest, prediction_id: int, payload: FeedbackIn):
    p = get_object_or_404(LivePrediction, pk=prediction_id)
    consumer = getattr(request, "api_consumer", None)
    actor = f"api:{consumer.name}" if consumer else "api"
    try:
        fb: PredictionFeedback = feedback.submit(
            p, actor, payload.verdict, payload.reason, payload.note)
    except feedback.FeedbackError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, DataEnvelope(
        data=_plain({"id": fb.pk, "prediction_id": p.pk, "status": fb.status,
                     "verdict": fb.verdict, "reason": fb.reason}),
        request_id=request.request_id,
    ))
