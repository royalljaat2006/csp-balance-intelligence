"""
Nemotron-backed narration/prioritization models.

Everything here is either read-only narration (Insight, AnomalyFlag) or a
*draft* awaiting explicit human approval (DraftMessage) — PriorityCall's
reasoning is informational, not an instruction to act unattended. No model
in this app sends anything or writes to csp/api's own tables — autopilot
reads via csp.services like any other consumer and writes only its own
tables, the same "two front doors, one domain layer" boundary the API and
dashboard already keep (see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

from csp.models import Csp, IngestLog
from django.db import models


class Insight(models.Model):
    """A Nemotron-generated narrative summary of network-wide numbers for
    one month — e.g. "12 CSPs dropped from S2 to NIL this week, concentrated
    in Bihar circle." Generated on a schedule
    (management/commands/generate_insights.py), never per page view."""

    month = models.CharField(max_length=7)  # "YYYY-MM", matches MonthlySummary.month
    headline = models.CharField(max_length=255)
    body = models.TextField()
    model_used = models.CharField(max_length=100)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-generated_at"]

    def __str__(self) -> str:
        return f"{self.month}: {self.headline}"


class PriorityCall(models.Model):
    """One CSP's rank + reason in a Nemotron-generated call-prioritization
    list for a month — reasons over gap + trend + account growth together,
    instead of the dashboard's plain smallest-gap sort. Informational only:
    ops decides who to actually call, this just orders the list."""

    month = models.CharField(max_length=7)
    csp = models.ForeignKey(Csp, on_delete=models.CASCADE, related_name="priority_calls")
    rank = models.PositiveSmallIntegerField()
    reason = models.CharField(max_length=500)
    signals_used = models.JSONField(default=dict, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["csp", "month"], name="uniq_priority_call_csp_month")
        ]
        ordering = ["month", "rank"]

    def __str__(self) -> str:
        return f"{self.month} #{self.rank}: {self.csp_id}"


class AnomalyFlag(models.Model):
    """A Nemotron-reviewed ingestion run flagged as worth a human look —
    e.g. an implausible overnight account-count jump. Generated right after
    ingest_calling_sheet/ingest_transactions finish
    (management/commands/review_ingest_anomalies.py). Purely observational:
    never blocks, retries, or modifies the ingestion it reviews."""

    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    ingest_log = models.ForeignKey(
        IngestLog, on_delete=models.CASCADE, related_name="anomaly_flags"
    )
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.LOW)
    description = models.TextField()
    raw_context = models.JSONField(default=dict, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-generated_at"]

    def __str__(self) -> str:
        return f"{self.get_severity_display()}: {self.description[:60]}"


class ReviewedIngestRun(models.Model):
    """Marks that autopilot has already reviewed an ingestion run for
    anomalies — tracked separately from AnomalyFlag because "reviewed, no
    anomalies found" and "not yet reviewed" must be distinguishable, or
    review_ingest_anomalies would re-check (and re-bill Nemotron for) the
    same clean runs on every pass."""

    ingest_log = models.OneToOneField(
        IngestLog, on_delete=models.CASCADE, related_name="autopilot_review"
    )
    reviewed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"reviewed: ingest_log #{self.ingest_log_id}"


class DraftMessage(models.Model):
    """A Nemotron-drafted nudge for one CSP (e.g. "you're only ₹125 away
    from S2") — DRAFT ONLY. This codebase has no messaging-provider
    integration, so nothing here is ever sent automatically: status starts
    at DRAFT, and only a human moving it to APPROVED in the admin (see
    admin.py's approve_drafts action) means "this is fine to send" — even
    then, actually sending it today is a manual copy-paste step, not
    something this code does on its own."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SENT = "sent", "Sent"

    class Channel(models.TextChoices):
        SMS = "sms", "SMS"
        WHATSAPP = "whatsapp", "WhatsApp"
        EMAIL = "email", "Email"

    csp = models.ForeignKey(Csp, on_delete=models.CASCADE, related_name="draft_messages")
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.SMS)
    text = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    generated_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-generated_at"]

    def __str__(self) -> str:
        return f"{self.csp_id} ({self.get_channel_display()}, {self.status})"


class AgentTask(models.Model):
    """One coordinator-level investigation — the parent that ties together
    every specialist AgentRun/AgentFinding/AgentMessage spawned while
    working on it. `parent_task` is set when the coordinator replans after
    a rejected verification (see agent.py) — the child task IS the retry,
    not a fresh, disconnected investigation."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        NEEDS_DATA = "needs_data", "Needs data"
        FAILED = "failed", "Failed"

    description = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    parent_task = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="replans"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    final_status = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"AgentTask #{self.pk} ({self.status})"


class AgentRun(models.Model):
    """One execution of one agent (the coordinator, or a specialist:
    data_agent/balance_agent/transaction_agent/risk_agent/
    verification_agent/action_agent). `observation` holds only facts
    returned by trusted tools (csp.services/csp.comparison via
    agent_tools.py); `reasoning_summary` is Nemotron's narrative
    *hypothesis* over those facts and must never be presented as a
    confirmed fact by any consumer of this record — see agent.py's module
    docstring for the trust boundary this enforces. `agent_name` is what
    Agent Monitoring's heatmap groups rows by — one real row per agent that
    has actually run, never an invented one."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        NEEDS_DATA = "needs_data", "Needs data"
        FAILED = "failed", "Failed"

    agent_name = models.CharField(max_length=100, default="csp_operations_agent")
    task_fk = models.ForeignKey(
        AgentTask, on_delete=models.CASCADE, null=True, blank=True, related_name="runs"
    )
    task = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # Trusted, tool-sourced facts only — never an LLM's own numbers.
    observation = models.JSONField(default=dict, blank=True)
    tools_used = models.JSONField(default=list, blank=True)
    # An AI-generated hypothesis/narrative, NOT a verified fact. Every
    # template rendering this must label it as such (see ai_operations.html).
    reasoning_summary = models.TextField(blank=True, default="")
    action_plan = models.TextField(blank=True, default="")
    final_status = models.CharField(max_length=255, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"AgentRun #{self.pk} ({self.agent_name}, {self.status})"


class AgentFinding(models.Model):
    """A structured, evidence-carrying claim one specialist hands off to
    another (or to the coordinator) — never free-form agent-to-agent chat.
    `evidence` is always real data returned by a canonical service (see
    `source`); `verification_status` starts PENDING and is only ever moved
    by the Verification Agent re-checking `evidence` against a fresh call
    to that same canonical service. An agent's own finding is NEVER treated
    as truth by another agent until verification_status == VERIFIED."""

    class VerificationStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"
        NEEDS_REVIEW = "needs_review", "Needs review"
        INSUFFICIENT_DATA = "insufficient_data", "Insufficient data"
        CONFLICTING_DATA = "conflicting_data", "Conflicting data"
        STALE_DATA = "stale_data", "Stale data"

    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name="findings")
    source_agent = models.CharField(max_length=100)
    target_agent = models.CharField(max_length=100, blank=True, default="")
    csp = models.ForeignKey(
        Csp, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    finding_type = models.CharField(max_length=100)
    evidence = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=200)
    data_timestamp = models.DateTimeField(null=True, blank=True)
    verification_status = models.CharField(
        max_length=20, choices=VerificationStatus.choices, default=VerificationStatus.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.finding_type} for {self.csp_id} ({self.verification_status})"


class AgentMessage(models.Model):
    """One structured handoff between agents (coordinator -> specialist, or
    specialist -> coordinator) — a JSON payload, never free-form chat. The
    complete, real routing trail behind the Agent Monitoring activity
    timeline and any "which agent talked to which" audit question."""

    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name="messages")
    from_agent = models.CharField(max_length=100)
    to_agent = models.CharField(max_length=100)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.from_agent} -> {self.to_agent}"


class AgentVerification(models.Model):
    """The Verification Agent's independent check of one AgentFinding —
    kept as its own row (not just a field on AgentFinding) so a finding can
    be re-verified after a replan without losing the earlier verdict."""

    class Result(models.TextChoices):
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"
        NEEDS_REVIEW = "needs_review", "Needs review"

    finding = models.ForeignKey(
        AgentFinding, on_delete=models.CASCADE, related_name="verifications"
    )
    verifier_agent = models.CharField(max_length=100, default="verification_agent")
    result = models.CharField(max_length=20, choices=Result.choices)
    reasoning = models.TextField(blank=True, default="")
    verified_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-verified_at"]

    def __str__(self) -> str:
        return f"{self.get_result_display()} for finding #{self.finding_id}"


class AgentAction(models.Model):
    """One thing the agent proposed/did during a run. A message_draft action
    always ties to a real DraftMessage row and inherits ITS status as the
    single source of truth (see agent_tools.verify_action) — this model
    never maintains an independent belief about whether a message was
    approved/sent that could drift from the real DraftMessage record."""

    class ActionType(models.TextChoices):
        MESSAGE_DRAFT = "message_draft", "Message draft"
        FOLLOWUP_TASK = "followup_task", "Follow-up task"
        ESCALATION = "escalation", "Escalation recommendation"
        REVIEW_FLAG = "review_flag", "Marked for review"
        INVESTIGATION = "investigation", "Investigation task"

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        PENDING_APPROVAL = "pending_approval", "Pending approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        EXECUTED = "executed", "Executed"
        FAILED = "failed", "Failed"
        NEEDS_REVIEW = "needs_review", "Needs review"

    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="actions")
    finding = models.ForeignKey(
        "AgentFinding", on_delete=models.SET_NULL, null=True, blank=True, related_name="actions"
    )
    csp = models.ForeignKey(
        Csp, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    description = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROPOSED)
    draft_message = models.ForeignKey(
        DraftMessage, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_result = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_action_type_display()} for {self.csp_id} ({self.status})"


class AgentToolCall(models.Model):
    """One tool invocation during a run — the factual audit trail behind
    "which tools did it use" (per-run) and the Agent Monitoring page's tool
    usage table (aggregated). Never stores hidden chain-of-thought, only
    which trusted tool was called, whether it succeeded, and a short
    factual summary of what it returned."""

    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="tool_calls")
    tool_name = models.CharField(max_length=100)
    called_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField(default=True)
    summary = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["called_at"]

    def __str__(self) -> str:
        return f"{self.tool_name} ({'ok' if self.success else 'failed'})"
