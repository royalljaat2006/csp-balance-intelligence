"""
Human feedback on estimates, kept apart from what SBI actually reported.

A person can say an estimate was useful, looks wrong, or that they are not sure, and why (missing
data, a wrongly classified transaction, unusual activity, the estimate itself). That is an opinion,
stored as an opinion: the record has no numeric field, never changes an outcome, a prediction or an
accuracy figure, and never counts as a verified value. Only an SBI-observed value scores a
prediction (learning.outcomes).

The free-text note is screened so that account numbers, phone numbers and e-mail addresses are not
stored. A name cannot be detected by a program; the form tells people not to write one.

Status moves OPEN -> IN_REVIEW -> RESOLVED or DISMISSED (and back to OPEN to reopen). Every change
is one insert-only FeedbackEvent with who, when and the note. Staff change status; anyone allowed to
see the estimate may submit.
"""

from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone

from .models import FeedbackEvent, LivePrediction, PredictionFeedback

Verdict = PredictionFeedback.Verdict
Reason = PredictionFeedback.Reason
Status = PredictionFeedback.Status

MAX_NOTE = 500
_EMAIL = re.compile(r"\S+@\S+")
_LONG_NUMBER = re.compile(r"\d{9,}")

ALLOWED = {
    Status.OPEN: {Status.IN_REVIEW, Status.RESOLVED, Status.DISMISSED},
    Status.IN_REVIEW: {Status.OPEN, Status.RESOLVED, Status.DISMISSED},
    Status.RESOLVED: {Status.OPEN},
    Status.DISMISSED: {Status.OPEN},
}


class FeedbackError(ValueError):
    """The feedback or the status change is not acceptable (the message says why)."""


def screen_note(note: str | None) -> str:
    """The trimmed note, or FeedbackError if it looks like it holds personal data."""
    note = (note or "").strip()
    if len(note) > MAX_NOTE:
        raise FeedbackError(f"The note can be at most {MAX_NOTE} characters.")
    if _EMAIL.search(note) or _LONG_NUMBER.search(re.sub(r"[\s-]", "", note)):
        raise FeedbackError("Please remove account numbers, phone numbers and e-mail addresses "
                            "from the note.")
    return note


@transaction.atomic
def submit(prediction: LivePrediction, actor: str, verdict: str, reason: str = "",
           note: str = "") -> PredictionFeedback:
    if verdict not in Verdict.values:
        raise FeedbackError("Choose whether the estimate was useful, looks wrong, or you are "
                            "not sure.")
    reason = reason or ""
    if verdict != Verdict.USEFUL and reason not in Reason.values:
        raise FeedbackError("Choose the reason the estimate looks wrong or unclear.")
    if reason and reason not in Reason.values:
        raise FeedbackError("That reason is not one of the choices.")
    note = screen_note(note)
    fb = PredictionFeedback.objects.create(
        prediction=prediction, submitted_by=actor[:150], verdict=verdict, reason=reason, note=note)
    FeedbackEvent.objects.create(
        feedback=fb, actor=actor[:150], action="SUBMITTED", to_status=Status.OPEN, note=note)
    return fb


@transaction.atomic
def set_status(
    feedback: PredictionFeedback, user, new_status: str, note: str = ""
) -> FeedbackEvent:
    if not getattr(user, "is_staff", False) or not getattr(user, "is_active", False):
        raise FeedbackError("Only staff can change the status of feedback.")
    fb = PredictionFeedback.objects.select_for_update().get(pk=feedback.pk)
    if new_status not in Status.values or new_status not in ALLOWED[Status(fb.status)]:
        raise FeedbackError(f"Feedback that is {fb.get_status_display().lower()} cannot move to "
                            f"{new_status.lower().replace('_', ' ')}.")
    note = screen_note(note)
    if new_status in (Status.RESOLVED, Status.DISMISSED) and not note:
        raise FeedbackError("Say what was done (or why it was dismissed) before closing it.")
    old = fb.status
    fb.status = new_status
    if new_status in (Status.RESOLVED, Status.DISMISSED):
        fb.resolved_by, fb.resolved_at = user.get_username(), timezone.now()
        fb.resolution_note = note
    elif new_status == Status.OPEN:
        fb.resolved_by, fb.resolved_at, fb.resolution_note = "", None, ""
    fb.save(update_fields=["status", "resolved_by", "resolved_at", "resolution_note"])
    return FeedbackEvent.objects.create(
        feedback=fb, actor=user.get_username(), action="STATUS_CHANGED", from_status=old,
        to_status=new_status, note=note)


def queue(status: str | None = None, *, limit: int = 50, offset: int = 0):
    qs = PredictionFeedback.objects.select_related("prediction", "prediction__csp")
    if status:
        qs = qs.filter(status=status)
    return qs[offset: offset + limit], qs.count()
