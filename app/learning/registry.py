"""
The registry of estimation methods and the audited route from candidate to active.

Stages (bankflow.ModelVersion.stage): BASELINE is the reference every method must beat;
CANDIDATE is being evaluated; ACTIVE is the method whose estimates are shown; RETIRED was replaced.
Exactly one method is ACTIVE. Every change is a learning.ModelEvent (who, why, when, on which
evaluation) that is never edited.

Promotion changes the registry only after ALL of these hold; there is no automatic promotion:
  * an active superuser asks for it and gives a written reason (the explicit approval);
  * the method is a CANDIDATE that the live estimator can really run (`live_supported`) and no
    business rule blocks it;
  * its latest evaluation is RECOMMENDED -- the pre-declared criteria were met -- and is recent.
A single wrong prediction never reaches this path: it only ever feeds the evaluation.

Rollback restores the method that was active before the last promotion and records what its own
evaluation said then; it never claims a method was validated when it was not.

Today only the movement rule can run in the live estimator, and it is already ACTIVE as a shadow
estimate, so no promotion is possible yet. A new method needs its own inference code, reviewed and
shipped separately, before it can be marked `live_supported`.
"""

from __future__ import annotations

import datetime as dt

from bankflow.corrections import is_authorised
from bankflow.models import ModelVersion
from django.db import transaction
from django.utils import timezone

from .models import CandidateEvaluation, ModelEvent

Stage = ModelVersion.Stage

BASELINE = "carry_forward"
MOVEMENT = "movement"
FLOW_ONLY = "flow_only"
ACCOUNT_COUNT_ONLY = "account_count_only"

MAX_EVALUATION_AGE_DAYS = 7
MIN_REASON_CHARS = 10

SEED = (
    {
        "name": BASELINE, "stage": Stage.BASELINE, "live_supported": False,
        "description": "Carry-forward: the next SBI value is the last SBI value, unchanged.",
        "definition": {"rule": "predicted = last SBI Avg Balance"},
    },
    {
        "name": MOVEMENT, "stage": Stage.ACTIVE, "live_supported": True,
        "description": ("Account growth and transaction flow: (Avg Balance x accounts + eligible "
                        "deposits - eligible withdrawals) / today's valid account count."),
        "definition": {"rule": "(avg x count + net eligible movement) / new count",
                       "implementation": "bankflow.movement"},
    },
    {
        "name": FLOW_ONLY, "stage": Stage.CANDIDATE, "live_supported": False,
        "description": ("Transaction flow only: the same movement, but divided by the account "
                        "count at the baseline, so account growth plays no part."),
        "definition": {"rule": "avg + net eligible movement / baseline count"},
    },
    {
        "name": ACCOUNT_COUNT_ONLY, "stage": Stage.CANDIDATE, "live_supported": False,
        "blocked_reason": ("Business rule: an account-count change never scales the combined "
                           "balance. Scored for evidence only."),
        "description": ("Account count only: the SBI value grows in proportion to the account "
                        "count."),
        "definition": {"rule": "avg x new count / baseline count"},
    },
)


class RegistryError(ValueError):
    """A registry action that is not allowed (the message says why)."""


def ensure_registry(actor: str = "system") -> list[ModelVersion]:
    """Create the methods that are not registered yet (idempotent). Writes only the registry."""
    out = []
    for spec in SEED:
        mv, created = ModelVersion.objects.get_or_create(
            name=spec["name"], version="1",
            defaults={
                "definition": spec["definition"], "in_registry": True, "stage": spec["stage"],
                "live_supported": spec["live_supported"],
                "blocked_reason": spec.get("blocked_reason", ""),
                "description": spec["description"],
            },
        )
        if created:
            ModelEvent.objects.create(
                model=mv, kind=ModelEvent.Kind.REGISTERED, actor=actor,
                reason=f"Registered as {spec['stage']}.", snapshot={"stage": spec["stage"]},
            )
        out.append(mv)
    return out


def registered() -> list[ModelVersion]:
    return list(ModelVersion.objects.filter(in_registry=True).order_by("id"))


def active_model() -> ModelVersion | None:
    return ModelVersion.objects.filter(in_registry=True, stage=Stage.ACTIVE).first()


def latest_evaluation(model: ModelVersion) -> CandidateEvaluation | None:
    return CandidateEvaluation.objects.filter(model=model).first()


def _check_actor(user, reason: str) -> str:
    if not is_authorised(user):
        raise RegistryError("Only an active superuser may change the active model.")
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON_CHARS:
        raise RegistryError(
            f"A written reason of at least {MIN_REASON_CHARS} characters is required.")
    return reason


def promotion_problems(model: ModelVersion, *, now: dt.datetime | None = None) -> list[str]:
    """Everything that currently stops this method being promoted (empty list: it may be)."""
    now = now or timezone.now()
    problems = []
    if not model.in_registry or model.stage != Stage.CANDIDATE:
        problems.append("Only a registered candidate can be promoted.")
    if model.blocked_reason:
        problems.append(f"Blocked: {model.blocked_reason}")
    if not model.live_supported:
        problems.append("The live estimator cannot run this method yet; it needs its own "
                        "reviewed inference code first.")
    ev = latest_evaluation(model)
    if ev is None:
        problems.append("It has not been evaluated.")
    elif ev.verdict != CandidateEvaluation.Verdict.RECOMMENDED:
        problems.append(f"Its latest evaluation is {ev.get_verdict_display().lower()}.")
    elif now - ev.created_at > dt.timedelta(days=MAX_EVALUATION_AGE_DAYS):
        problems.append(f"Its evaluation is more than {MAX_EVALUATION_AGE_DAYS} days old; "
                        "evaluate again first.")
    return problems


@transaction.atomic
def promote(model: ModelVersion, user, reason: str) -> ModelEvent:
    reason = _check_actor(user, reason)
    model = ModelVersion.objects.select_for_update().get(pk=model.pk)
    problems = promotion_problems(model)
    if problems:
        raise RegistryError(" ".join(problems))
    current = ModelVersion.objects.select_for_update().filter(
        in_registry=True, stage=Stage.ACTIVE).first()
    ev = latest_evaluation(model)
    before = latest_evaluation(current) if current else None
    if current:
        current.stage = Stage.RETIRED
        current.save(update_fields=["stage"])
    model.stage = Stage.ACTIVE
    model.save(update_fields=["stage"])
    return ModelEvent.objects.create(
        model=model, kind=ModelEvent.Kind.PROMOTED, actor=user.get_username(), reason=reason,
        previous=current, evaluation=ev,
        snapshot={
            "previous": current.name if current else None,
            "previous_verdict": before.verdict if before else "NOT_EVALUATED",
            "evaluation_verdict": ev.verdict if ev else None,
            "criteria": ev.criteria if ev else [],
        },
    )


@transaction.atomic
def rollback(user, reason: str) -> ModelEvent:
    """Undo the latest promotion: the method active before it becomes active again."""
    reason = _check_actor(user, reason)
    current = ModelVersion.objects.select_for_update().filter(
        in_registry=True, stage=Stage.ACTIVE).first()
    last = (ModelEvent.objects.filter(kind=ModelEvent.Kind.PROMOTED, model=current)
            .select_related("previous").first() if current else None)
    if last is None or last.previous is None:
        raise RegistryError("There is no promotion to roll back.")
    restored = ModelVersion.objects.select_for_update().get(pk=last.previous.pk)
    if not restored.live_supported:
        raise RegistryError("The method that was active before cannot run in the live estimator.")
    assert current is not None
    current.stage = Stage.CANDIDATE
    current.save(update_fields=["stage"])
    restored.stage = Stage.ACTIVE
    restored.save(update_fields=["stage"])
    was = latest_evaluation(restored)
    return ModelEvent.objects.create(
        model=restored, kind=ModelEvent.Kind.ROLLED_BACK, actor=user.get_username(),
        reason=reason, previous=current, evaluation=was,
        snapshot={"left": current.name, "restored": restored.name,
                  "restored_verdict": was.verdict if was else "NOT_EVALUATED"},
    )


def history(limit: int = 50) -> list[ModelEvent]:
    return list(ModelEvent.objects.select_related("model", "previous")[:limit])
