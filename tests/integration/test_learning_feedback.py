"""
Human feedback: an opinion about an estimate, kept apart from what SBI reported. It has no numeric
field, never changes a prediction, an outcome or an accuracy figure, screens out personal data,
and every status change is an insert-only event with who and when.
"""

import pytest
from django.contrib.auth import get_user_model
from django.db import models
from learning import accuracy, feedback
from learning.models import (
    FeedbackEvent,
    ImmutableRecord,
    LivePrediction,
    PredictionFeedback,
    PredictionOutcome,
)

from tests.learning_helpers import d, make_csp, stored_outcome, stored_prediction

pytestmark = pytest.mark.django_db
S = PredictionFeedback.Status


@pytest.fixture
def prediction():
    make_csp()
    return stored_prediction(estimate=d(2026, 2, 3), first_seen=d(2026, 2, 1), predicted="1010")


@pytest.fixture
def staff():
    return get_user_model().objects.create_user(username="reviewer", password="x", is_staff=True)


# ------------------------------------------------------------------ submitting
def test_a_useful_vote_needs_no_reason(prediction):
    fb = feedback.submit(prediction, "ops", "USEFUL")
    assert fb.status == S.OPEN and fb.reason == "" and fb.submitted_by == "ops"
    event = FeedbackEvent.objects.get(feedback=fb)
    assert (event.action, event.to_status, event.actor) == ("SUBMITTED", "OPEN", "ops")


@pytest.mark.parametrize("reason", ["MISSING_DATA", "WRONG_CLASSIFICATION", "UNUSUAL_ACTIVITY",
                                    "MODEL_ERROR", "OTHER"])
def test_every_structured_reason_is_accepted(prediction, reason):
    fb = feedback.submit(prediction, "ops", "LOOKS_WRONG", reason, "balance seems low")
    assert fb.reason == reason and fb.note == "balance seems low"


def test_a_wrong_or_unsure_vote_must_say_why(prediction):
    for verdict in ("LOOKS_WRONG", "UNSURE"):
        with pytest.raises(feedback.FeedbackError):
            feedback.submit(prediction, "ops", verdict)
    with pytest.raises(feedback.FeedbackError):
        feedback.submit(prediction, "ops", "LOOKS_WRONG", "BECAUSE")
    with pytest.raises(feedback.FeedbackError):
        feedback.submit(prediction, "ops", "AMAZING")
    assert PredictionFeedback.objects.count() == 0


@pytest.mark.parametrize("note", [
    "account 123456789012 is short",
    "call me on 9876543210",
    "write to someone@example.com",
    "number 1234 5678 9012 3456",
    "x" * 501,
])
def test_notes_with_personal_data_or_too_long_are_refused(prediction, note):
    with pytest.raises(feedback.FeedbackError):
        feedback.submit(prediction, "ops", "LOOKS_WRONG", "OTHER", note)
    assert PredictionFeedback.objects.count() == 0


def test_normal_figures_and_codes_in_a_note_are_fine(prediction):
    fb = feedback.submit(prediction, "ops", "LOOKS_WRONG", "MODEL_ERROR",
                         "CSP 1A850001 shows 1,020 but near 1,300 is expected after the 5,000 "
                         "deposit")
    assert fb.note.startswith("CSP 1A850001")


# ------------------------------------------------------------------ opinion is not an observation
def test_feedback_has_no_numeric_field_so_it_cannot_stand_in_for_an_sbi_value():
    numeric = (models.DecimalField, models.FloatField, models.IntegerField)
    fields = [f.name for f in PredictionFeedback._meta.get_fields()
              if isinstance(f, numeric) and not f.primary_key and not f.is_relation]
    assert fields == []


def test_feedback_never_changes_a_prediction_an_outcome_or_the_accuracy_figures(prediction):
    stored_outcome(prediction, 1020, event=d(2026, 2, 8))
    before = (list(LivePrediction.objects.values()), list(PredictionOutcome.objects.values()),
              accuracy.accuracy_report())
    fb = feedback.submit(prediction, "ops", "LOOKS_WRONG", "MODEL_ERROR", "should be 5000")
    feedback.submit(prediction, "ops2", "USEFUL")
    assert (list(LivePrediction.objects.values()), list(PredictionOutcome.objects.values()),
            accuracy.accuracy_report()) == before
    assert PredictionOutcome.objects.count() == 1 and fb.prediction_id == prediction.pk


def test_feedback_on_an_unscored_prediction_creates_no_outcome(prediction):
    feedback.submit(prediction, "ops", "UNSURE", "MISSING_DATA")
    assert PredictionOutcome.objects.count() == 0


# ------------------------------------------------------------------ status and the audit trail
def test_status_moves_through_review_to_resolved_with_a_full_trail(prediction, staff):
    fb = feedback.submit(prediction, "ops", "LOOKS_WRONG", "MISSING_DATA", "no statement for 3 Feb")
    feedback.set_status(fb, staff, S.IN_REVIEW, "looking at the statement folder")
    feedback.set_status(fb, staff, S.RESOLVED, "statement arrived on 4 Feb and was imported")
    fb.refresh_from_db()
    assert fb.status == S.RESOLVED and fb.resolved_by == "reviewer" and fb.resolved_at
    assert fb.resolution_note.startswith("statement arrived")
    trail = list(fb.events.values_list("action", "from_status", "to_status", "actor"))
    assert trail == [("SUBMITTED", "", "OPEN", "ops"),
                     ("STATUS_CHANGED", "OPEN", "IN_REVIEW", "reviewer"),
                     ("STATUS_CHANGED", "IN_REVIEW", "RESOLVED", "reviewer")]


def test_closing_needs_a_note_and_only_staff_can_change_status(prediction, staff):
    fb = feedback.submit(prediction, "ops", "UNSURE", "OTHER")
    with pytest.raises(feedback.FeedbackError):
        feedback.set_status(fb, staff, S.RESOLVED, "")
    with pytest.raises(feedback.FeedbackError):
        feedback.set_status(fb, staff, S.DISMISSED)
    plain = get_user_model().objects.create_user(username="plain", password="x")
    with pytest.raises(feedback.FeedbackError):
        feedback.set_status(fb, plain, S.IN_REVIEW)
    inactive = get_user_model().objects.create_user(username="gone", password="x", is_staff=True,
                                                    is_active=False)
    with pytest.raises(feedback.FeedbackError):
        feedback.set_status(fb, inactive, S.IN_REVIEW)
    fb.refresh_from_db()
    assert fb.status == S.OPEN and fb.events.count() == 1


def test_a_closed_item_can_only_be_reopened_and_that_clears_the_resolution(prediction, staff):
    fb = feedback.submit(prediction, "ops", "UNSURE", "OTHER")
    feedback.set_status(fb, staff, S.DISMISSED, "duplicate of an earlier note")
    with pytest.raises(feedback.FeedbackError):
        feedback.set_status(fb, staff, S.IN_REVIEW, "x")
    feedback.set_status(fb, staff, S.OPEN, "reopened after a second report")
    fb.refresh_from_db()
    assert fb.status == S.OPEN and fb.resolved_by == "" and fb.resolved_at is None
    assert fb.events.count() == 3  # the whole history is kept


def test_the_resolution_note_is_screened_too(prediction, staff):
    fb = feedback.submit(prediction, "ops", "UNSURE", "OTHER")
    with pytest.raises(feedback.FeedbackError):
        feedback.set_status(fb, staff, S.RESOLVED, "called 9876543210 and fixed it")


def test_feedback_events_cannot_be_edited_or_removed(prediction):
    fb = feedback.submit(prediction, "ops", "USEFUL")
    event = fb.events.get()
    event.note = "tampered"
    with pytest.raises(ImmutableRecord):
        event.save()
    with pytest.raises(ImmutableRecord):
        event.delete()


def test_the_queue_lists_by_status(prediction, staff):
    a = feedback.submit(prediction, "ops", "USEFUL")
    feedback.submit(prediction, "ops", "UNSURE", "OTHER")
    feedback.set_status(a, staff, S.DISMISSED, "not needed")
    rows, total = feedback.queue(S.OPEN)
    assert total == 1 and len(list(rows)) == 1
    assert feedback.queue(None)[1] == 2
