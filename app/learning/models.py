"""
Records of the learning loop around the shadow Avg Balance estimate.

Four tables are INSERT-ONLY (a prediction, the outcome that scored it, a feedback event, a
candidate evaluation, a model event): the model refuses to update or delete a saved row, and the
migration adds a database trigger so a queryset update or a raw statement cannot either. History is
therefore never rewritten, including when the SBI value is later restated. Feedback status is the
one mutable thing, and every change to it is written as a FeedbackEvent.

SBI-observed values and model estimates are never mixed: an outcome stores the observed value in
`actual_value` and the estimate in the prediction it belongs to. A person's feedback is a separate
record and has no numeric field, so it cannot be mistaken for an observed value.
"""

from __future__ import annotations

from bankflow.models import ModelVersion, money
from csp.models import Csp
from django.db import models


class ImmutableRecord(Exception):  # noqa: N818 -- read as a statement, not an error class name
    """A saved learning record cannot be changed or removed."""


class InsertOnly(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecord(f"{type(self).__name__} #{self.pk} is insert-only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecord(f"{type(self).__name__} #{self.pk} is insert-only")


class LivePrediction(InsertOnly):
    """One frozen estimate of what a CSP's next SBI-reported Avg Balance will be, with everything
    needed to reproduce it. Written once from the stored daily estimate; the estimate row may
    later be recomputed, this row never is."""

    csp = models.ForeignKey(Csp, on_delete=models.PROTECT, related_name="+")
    model = models.ForeignKey(ModelVersion, on_delete=models.PROTECT, related_name="+")
    estimate_date = models.DateField()  # the day the estimate is for
    created_at = models.DateTimeField(auto_now_add=True)  # when it was frozen
    horizon = models.CharField(max_length=40, default="next_sbi_refresh")

    # effective observation period: the day the baseline value was first seen, to the estimate day
    period_start = models.DateField()
    period_end = models.DateField()

    # the SBI-observed side (Calling Sheet). The sheet has no measurement-date column, so these are
    # the days the system FIRST SAW the value, never SBI's own effective date.
    baseline_value = money()
    baseline_first_seen = models.DateField()
    baseline_last_seen = models.DateField(null=True, blank=True)
    baseline_account_count = models.PositiveIntegerField(null=True, blank=True)

    # account counts: the Calling Sheet count the estimator used, and the PMJDY snapshot if any
    account_count = models.PositiveIntegerField(null=True, blank=True)
    pmjdy_account_count = models.PositiveIntegerField(null=True, blank=True)
    pmjdy_snapshot_at = models.DateTimeField(null=True, blank=True)

    # attributed, eligible customer movement over (period_start, period_end]
    flow_deposits = money(default=0)  # a pool debit
    flow_withdrawals = money(default=0)  # a pool credit
    net_customer_movement = money(default=0)  # deposits - withdrawals
    flow_coverage = models.FloatField(null=True, blank=True)

    predicted_value = money()  # per account
    predicted_combined = money(null=True, blank=True)
    carry_forward_value = money()  # the simple baseline: the SBI value unchanged

    confidence_status = models.CharField(max_length=24)  # VALIDATED / PROVISIONAL / ...
    confidence_reason = models.CharField(max_length=300, blank=True)
    statement_state = models.CharField(max_length=12)  # complete | provisional at freeze time
    methodology = models.TextField(blank=True)
    inputs = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["csp", "estimate_date", "model"], name="uniq_learning_prediction"
            )
        ]
        indexes = [
            models.Index(fields=["csp", "baseline_first_seen"]),
            models.Index(fields=["estimate_date"]),
        ]
        ordering = ["-estimate_date", "csp_id"]


class PredictionOutcome(InsertOnly):
    """The SBI-observed value that scored one prediction. Exactly one per prediction; the
    prediction is never touched, and a later restatement of the value is a new SBI observation for
    the next cycle, not a rewrite of this one."""

    class Direction(models.TextChoices):
        OVER = "OVER", "Estimate too high"
        UNDER = "UNDER", "Estimate too low"
        EXACT = "EXACT", "Exact"

    class Quality(models.TextChoices):
        OK = "OK", "Usable for judging the model"
        STATEMENT_GAP = "STATEMENT_GAP", "Bank statements missing or incomplete"
        UNMATCHED_CODES = "UNMATCHED_CODES", "Many transactions could not be matched to a CSP"
        STALE_SOURCE = "STALE_SOURCE", "SBI value was stale when the estimate was made"
        COUNT_UNRELIABLE = "COUNT_UNRELIABLE", "Account count reading was blank or stale"
        PROVISIONAL_DATA = "PROVISIONAL_DATA", "Estimate was made from incomplete data"

    prediction = models.OneToOneField(
        LivePrediction, on_delete=models.PROTECT, related_name="outcome"
    )
    actual_value = money()  # the next SBI-reported Avg Balance
    actual_first_seen = models.DateField()  # the day the system first saw it
    actual_account_count = models.PositiveIntegerField(null=True, blank=True)
    horizon_days = models.PositiveSmallIntegerField()  # actual_first_seen - estimate_date
    evaluated_at = models.DateTimeField(auto_now_add=True)

    error_amount = money()  # predicted - actual: positive means the estimate was too high
    abs_error = money()
    pct_error = models.FloatField(null=True, blank=True)  # of actual; null when actual is zero
    direction = models.CharField(max_length=6, choices=Direction.choices)
    carry_forward_error = money()  # carry-forward value - actual
    quality = models.CharField(max_length=20, choices=Quality.choices)
    quality_detail = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=["actual_first_seen"]), models.Index(fields=["quality"])]
        ordering = ["-actual_first_seen", "-id"]


class PredictionFeedback(models.Model):
    """A person's opinion of one estimate. Deliberately has NO numeric value: it can say an
    estimate looks wrong, never what the true value is, so it can never stand in for an SBI
    observation. Only `status` and the resolution fields change; each change is a FeedbackEvent."""

    class Verdict(models.TextChoices):
        USEFUL = "USEFUL", "Useful"
        LOOKS_WRONG = "LOOKS_WRONG", "Looks wrong"
        UNSURE = "UNSURE", "Not sure"

    class Reason(models.TextChoices):
        MISSING_DATA = "MISSING_DATA", "Data is missing"
        WRONG_CLASSIFICATION = "WRONG_CLASSIFICATION", "A transaction was classified wrongly"
        UNUSUAL_ACTIVITY = "UNUSUAL_ACTIVITY", "Unusual activity at this CSP"
        MODEL_ERROR = "MODEL_ERROR", "The estimate itself is off"
        OTHER = "OTHER", "Something else"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Waiting for review"
        IN_REVIEW = "IN_REVIEW", "Being looked at"
        RESOLVED = "RESOLVED", "Resolved"
        DISMISSED = "DISMISSED", "Dismissed"

    prediction = models.ForeignKey(
        LivePrediction, on_delete=models.PROTECT, related_name="feedback"
    )
    submitted_by = models.CharField(max_length=150)  # a username or an API consumer name
    submitted_at = models.DateTimeField(auto_now_add=True)
    verdict = models.CharField(max_length=12, choices=Verdict.choices)
    reason = models.CharField(max_length=24, choices=Reason.choices, blank=True)
    note = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    resolved_by = models.CharField(max_length=150, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-submitted_at", "-id"]
        indexes = [models.Index(fields=["status", "-submitted_at"])]


class FeedbackEvent(InsertOnly):
    """The audit trail of one piece of feedback: submitted, then each change of status."""

    feedback = models.ForeignKey(
        PredictionFeedback, on_delete=models.PROTECT, related_name="events"
    )
    at = models.DateTimeField(auto_now_add=True)
    actor = models.CharField(max_length=150)
    action = models.CharField(max_length=20)  # SUBMITTED | STATUS_CHANGED
    from_status = models.CharField(max_length=10, blank=True)
    to_status = models.CharField(max_length=10)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["at", "id"]


class CandidateEvaluation(InsertOnly):
    """One time-ordered comparison of a candidate method against carry-forward (and the active
    method), with the pre-declared criteria and whether each was met. Insert-only history."""

    class Verdict(models.TextChoices):
        RECOMMENDED = "RECOMMENDED", "Meets the promotion criteria"
        NOT_RECOMMENDED = "NOT_RECOMMENDED", "Does not meet the promotion criteria"
        INSUFFICIENT = "INSUFFICIENT_EVIDENCE", "Insufficient evidence"
        BLOCKED = "BLOCKED", "Blocked by a business rule"

    model = models.ForeignKey(ModelVersion, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    verdict = models.CharField(max_length=24, choices=Verdict.choices)
    detail = models.CharField(max_length=400, blank=True)
    holdout_from = models.DateField(null=True, blank=True)
    training_cutoff = models.DateField(null=True, blank=True)
    n_pairs = models.PositiveIntegerField(default=0)
    n_events = models.PositiveIntegerField(default=0)
    n_cycles = models.PositiveIntegerField(default=0)
    metrics = models.JSONField(default=dict, blank=True)
    criteria = models.JSONField(default=list, blank=True)
    limitations = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["model", "-created_at"])]


class ModelEvent(InsertOnly):
    """Model change history: registered, promoted, rolled back. Who, why, when, on what evidence."""

    class Kind(models.TextChoices):
        REGISTERED = "REGISTERED", "Registered"
        PROMOTED = "PROMOTED", "Promoted to active"
        ROLLED_BACK = "ROLLED_BACK", "Rolled back"

    model = models.ForeignKey(ModelVersion, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    at = models.DateTimeField(auto_now_add=True)
    actor = models.CharField(max_length=150)
    reason = models.TextField(blank=True)
    # the model that was active before this event (PROMOTED) or the one being left (ROLLED_BACK)
    previous = models.ForeignKey(
        ModelVersion, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    evaluation = models.ForeignKey(
        CandidateEvaluation, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-at", "-id"]

