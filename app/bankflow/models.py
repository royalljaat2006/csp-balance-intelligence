"""
Bank-statement flow data model -- docs/BANK-FLOW.md.

Everything the SBI pool-account statement pipeline persists lives here and
only here. Nothing in this app writes to csp.DailyBalance, csp.Csp or any
Calling Sheet-derived table: the experimental flow data and the existing
balance source are kept physically separate on purpose.

Money is always Decimal. Customer account numbers and names that appear in
statement descriptions are NOT stored: `description_redacted` keeps the
audit-relevant part (reference, channel, KO code) and `description_hash` is
the SHA-256 of the full original text, so rows stay verifiable/de-duplicable
without retaining personal data.
"""

from __future__ import annotations

from django.db import models


def money(**kwargs) -> models.DecimalField:
    """Every monetary column: 16 digits, 2 decimals (Decimal, never float)."""
    return models.DecimalField(max_digits=16, decimal_places=2, **kwargs)


class ImportRun(models.Model):
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=500, blank=True)
    files_seen = models.PositiveIntegerField(default=0)
    files_imported = models.PositiveIntegerField(default=0)
    files_duplicate = models.PositiveIntegerField(default=0)
    files_quarantined = models.PositiveIntegerField(default=0)
    files_failed = models.PositiveIntegerField(default=0)
    files_skipped = models.PositiveIntegerField(default=0)
    error_summary = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]


class StatementFile(models.Model):
    class Status(models.TextChoices):
        IMPORTED = "imported", "Imported"
        QUARANTINED = "quarantined", "Quarantined"
        FAILED = "failed", "Failed (unreadable)"

    file_hash = models.CharField(max_length=64, unique=True)
    filename = models.CharField(max_length=255)
    source_path = models.CharField(max_length=1000, blank=True)
    detected_format = models.CharField(max_length=32, blank=True)
    byte_size = models.PositiveBigIntegerField(default=0)
    file_modified_at = models.DateTimeField(null=True, blank=True)
    account_masked = models.CharField(max_length=32, blank=True)
    account_suffix = models.CharField(max_length=16, blank=True, db_index=True)
    statement_start = models.DateField(null=True, blank=True)
    statement_end = models.DateField(null=True, blank=True)
    #: dates of the first / last ledger row actually present in this file (a statement
    #: may stop before its header End Date). Used for ledger-continuity completeness.
    first_row_date = models.DateField(null=True, blank=True)
    last_row_date = models.DateField(null=True, blank=True)
    opening_balance = money(null=True, blank=True)
    book_balance = money(null=True, blank=True)
    available_balance = money(null=True, blank=True)
    hold_value = money(null=True, blank=True)
    header = models.JSONField(default=dict, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices)
    status_reason = models.TextField(blank=True)
    import_run = models.ForeignKey(
        ImportRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="files"
    )
    imported_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now_add=True)
    seen_count = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-imported_at"]
        indexes = [models.Index(fields=["statement_end"])]


class StatementReconciliation(models.Model):
    class Status(models.TextChoices):
        RECONCILED = "RECONCILED", "Reconciled"
        PARTIAL = "PARTIAL", "Internally consistent, header gap unexplained"
        FAILED = "FAILED", "Failed"

    statement = models.OneToOneField(
        StatementFile, on_delete=models.CASCADE, related_name="reconciliation"
    )
    status = models.CharField(max_length=12, choices=Status.choices)
    opening_balance = money(null=True, blank=True)
    computed_closing = money(null=True, blank=True)
    last_row_balance = money(null=True, blank=True)
    header_book_balance = money(null=True, blank=True)
    header_gap = money(
        null=True, blank=True, help_text="Header Book Balance minus last row balance."
    )
    total_debit = money(default=0)
    total_credit = money(default=0)
    rows_parsed = models.PositiveIntegerField(default=0)
    malformed_rows = models.PositiveIntegerField(default=0)
    running_balance_mismatches = models.PositiveIntegerField(default=0)
    first_mismatch_row = models.PositiveIntegerField(null=True, blank=True)
    opening_date_matches_start = models.BooleanField(null=True)
    available_equals_book_minus_hold = models.BooleanField(null=True)
    duplicate_ref_groups = models.PositiveIntegerField(default=0)
    reversal_candidates = models.PositiveIntegerField(default=0)
    overlap_rows = models.PositiveIntegerField(default=0)
    conflicting_rows = models.PositiveIntegerField(default=0)
    notes = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class StatementTransaction(models.Model):
    class Category(models.TextChoices):
        WITHDRAWAL = "withdrawal", "Withdrawal (on-us)"
        DEPOSIT = "deposit", "Deposit (on-us)"
        TRANSFER = "transfer", "Money transfer"
        OFFUS = "offus", "Off-us withdrawal"
        OTHER_PRODUCT = "other_product", "Other-product deposit"
        POOL_OPERATIONS = "pool_operations", "Pool funding / deductions"
        UNCLASSIFIED = "unclassified", "Unclassified"

    row_key = models.CharField(max_length=64, unique=True)
    first_statement = models.ForeignKey(
        StatementFile, on_delete=models.CASCADE, related_name="transactions"
    )
    row_index = models.PositiveIntegerField()
    txn_date = models.DateField()
    value_date = models.DateField(null=True, blank=True)
    branch_code = models.CharField(max_length=16, blank=True)
    debit = money(default=0)
    credit = money(default=0)
    balance = money()
    description_hash = models.CharField(max_length=64)
    description_redacted = models.TextField(blank=True)
    ref_number = models.CharField(max_length=64, blank=True)
    channel_raw = models.CharField(max_length=64, blank=True)
    channel_normalized = models.CharField(max_length=64, blank=True)
    category = models.CharField(max_length=20, choices=Category.choices)
    side = models.CharField(max_length=8)  # credit | debit | zero (pool perspective)
    ko_raw = models.CharField(max_length=32, blank=True)
    is_duplicate_ref = models.BooleanField(default=False)
    is_reversal_candidate = models.BooleanField(default=False)
    reversal_reason = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["txn_date", "first_statement_id", "row_index"]
        indexes = [
            models.Index(fields=["txn_date"]),
            models.Index(fields=["ref_number"]),
            models.Index(fields=["description_hash"]),
            # "the last row of each day" is one index probe per day (flows.day_closing_balances)
            models.Index(fields=["txn_date", "first_statement", "row_index"],
                         name="idx_stmttxn_day_order"),
        ]

    @property
    def amount(self):
        return self.credit if self.credit else self.debit


class CodeResolution(models.Model):
    class Status(models.TextChoices):
        EXACT = "exact", "Exact match"
        VARIANT = "variant_provisional", "Variant (provisional, unconfirmed rule)"
        AMBIGUOUS = "ambiguous_truncated", "Ambiguous / truncated"
        UNMATCHED = "unmatched", "Unmatched"
        MISSING = "missing", "No KO code"

    transaction = models.OneToOneField(
        StatementTransaction, on_delete=models.CASCADE, related_name="resolution"
    )
    status = models.CharField(max_length=24, choices=Status.choices, db_index=True)
    method = models.CharField(max_length=40, blank=True)
    ko_raw = models.CharField(max_length=32, blank=True)
    resolved_csp = models.ForeignKey(
        "csp.Csp", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    candidate_codes = models.JSONField(default=list, blank=True)
    resolved_at = models.DateTimeField(auto_now=True)


class FlowAggregate(models.Model):
    """One CSP, one statement date, one resolution mode ('exact_only' uses only
    EXACT matches; 'with_provisional' also counts unconfirmed variants)."""

    csp = models.ForeignKey("csp.Csp", on_delete=models.CASCADE, related_name="+")
    flow_date = models.DateField()
    resolution_mode = models.CharField(max_length=20)
    txn_count = models.PositiveIntegerField(default=0)
    credit_count = models.PositiveIntegerField(default=0)
    debit_count = models.PositiveIntegerField(default=0)
    credit_amount = money(default=0)  # pool credits
    debit_amount = money(default=0)  # pool debits
    by_category = models.JSONField(default=dict, blank=True)
    by_type = models.JSONField(default=dict, blank=True)
    reversal_candidate_amount = money(default=0)
    duplicate_ref_count = models.PositiveIntegerField(default=0)
    recon_status = models.CharField(max_length=12, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["csp", "flow_date", "resolution_mode"], name="uniq_bankflow_csp_day_mode"
            )
        ]
        indexes = [models.Index(fields=["flow_date", "resolution_mode"])]

    @property
    def gross_inflow(self):
        return self.credit_amount

    @property
    def gross_outflow(self):
        return self.debit_amount

    @property
    def net_pool_flow(self):
        return self.credit_amount - self.debit_amount


class UnresolvedFlowDaily(models.Model):
    """Transactions that could not be attributed to a CSP, by status and day --
    so missing mappings stay visible instead of vanishing from the totals."""

    flow_date = models.DateField()
    status = models.CharField(max_length=24)
    txn_count = models.PositiveIntegerField(default=0)
    credit_amount = money(default=0)
    debit_amount = money(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["flow_date", "status"], name="uniq_bankflow_unres")
        ]


class ModelVersion(models.Model):
    name = models.CharField(max_length=64)
    version = models.CharField(max_length=32)
    definition = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["name", "version"], name="uniq_bankflow_model")
        ]


class EvaluationRun(models.Model):
    class Target(models.TextChoices):
        AVG_CHANGE = "avg_change", "Next Calling Sheet Avg Balance (change)"
        DIRECTION = "direction", "Direction of next genuine change"
        MONTH_END_MAB = "month_end_mab", "Month-end MAB"

    class Status(models.TextChoices):
        NOT_EVALUATED = "NOT_EVALUATED", "Not evaluated"
        INSUFFICIENT = "INSUFFICIENT_EVIDENCE", "Insufficient evidence"
        FAILED = "EVALUATION_FAILED", "Evaluation failed"
        PASSED = "VALIDATION_PASSED", "Validation passed"

    target = models.CharField(max_length=20, choices=Target.choices)
    model = models.ForeignKey(ModelVersion, null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=24, choices=Status.choices)
    outcome_detail = models.CharField(max_length=200, blank=True)
    config = models.JSONField(default=dict, blank=True)
    n_events = models.PositiveIntegerField(default=0)
    n_test_events = models.PositiveIntegerField(default=0)
    n_observations = models.PositiveIntegerField(default=0)
    train_period = models.CharField(max_length=64, blank=True)
    test_period = models.CharField(max_length=64, blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    notes = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class PredictionRecord(models.Model):
    """Held-out (test-event) predictions only -- never training rows."""

    run = models.ForeignKey(EvaluationRun, on_delete=models.CASCADE, related_name="predictions")
    csp = models.ForeignKey("csp.Csp", on_delete=models.CASCADE, related_name="+")
    event_date = models.DateField()
    previous_date = models.DateField()
    horizon_days = models.PositiveSmallIntegerField(default=0)
    observed_previous = money()
    observed_new = money()
    predicted_baseline = money()
    predicted_model = money()
    flow_per_account = money(null=True, blank=True)
    account_count_changed = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["run", "event_date"])]


class HypothesisRun(models.Model):
    """One run of the account-count hypothesis test (bankflow.account_hypothesis).
    Insert-only history; the newest row decides how DailyEstimate may use the rule."""

    # VALIDATED / PROVISIONAL / INSUFFICIENT_EVIDENCE / NOT_SUPPORTED
    status = models.CharField(max_length=24)
    detail = models.CharField(max_length=300, blank=True)
    n_pairs = models.PositiveIntegerField(default=0)
    n_events = models.PositiveIntegerField(default=0)
    metrics = models.JSONField(default=dict, blank=True)
    config = models.JSONField(default=dict, blank=True)
    # Which estimator the held-out comparison selected (carry_forward unless one of the
    # candidates passed/led) and its fitted coefficient(s), fitted on all usable events.
    selected_method = models.CharField(max_length=32, default="carry_forward")
    parameters = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class AccountCountObservation(models.Model):
    """Latest total account count per CSP as read from the PMJDY tab.

    Insert-only history: a row is written when the count differs from the CSP's most
    recent observation, or when no row exists yet for that IST calendar day (so every
    day has a snapshot even if nothing changed). The *current* count is always the
    newest row; older rows exist for growth history and model evaluation."""

    csp = models.ForeignKey("csp.Csp", on_delete=models.CASCADE, related_name="+")
    source = models.CharField(max_length=16, default="pmjdy")
    account_count = models.PositiveIntegerField()
    observed_at = models.DateTimeField()
    snapshot_date = models.DateField()  # IST calendar date of observed_at

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["csp", "source", "observed_at"], name="uniq_bankflow_acct_obs"
            )
        ]
        indexes = [models.Index(fields=["csp", "source", "-observed_at"])]


class PmjdyMonthly(models.Model):
    """New PMJDY accounts per CSP for one calendar month, as shown in the Calling Sheet's monthly
    blocks ("MTD Achievement <Month>'yy", "FTD Achievement ...", "Targets <Mon> yyyy").

    These count accounts OPENED (month-to-date, for-the-day, target), not the CSP's total account
    base (that is "Total Accounts"). Insert-only history: a row is written when any figure changes,
    or when the IST day has no row yet, so earlier readings are never overwritten."""

    csp = models.ForeignKey("csp.Csp", on_delete=models.CASCADE, related_name="+")
    month = models.CharField(max_length=7)  # "YYYY-MM" -- the month the sheet block is for
    mtd = models.PositiveIntegerField(null=True, blank=True)
    ftd = models.PositiveIntegerField(null=True, blank=True)
    target = models.PositiveIntegerField(null=True, blank=True)
    observed_at = models.DateTimeField()
    snapshot_date = models.DateField()  # IST calendar date of observed_at

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["csp", "month", "observed_at"], name="uniq_bankflow_pmjdy_monthly"
            )
        ]
        indexes = [models.Index(fields=["csp", "month", "-observed_at"])]


class BaselineSnapshot(models.Model):
    """The Calling Sheet value of each CSP as captured by the 06:00 IST snapshot.
    Insert-only: the first capture for (csp, snapshot_date) wins and is never
    overwritten, so a rerun is a no-op and earlier observations are preserved."""

    csp = models.ForeignKey("csp.Csp", on_delete=models.CASCADE, related_name="+")
    snapshot_date = models.DateField()
    avg_balance = money()
    account_count = models.PositiveIntegerField(null=True, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["csp", "snapshot_date"], name="uniq_bankflow_baseline")
        ]
        indexes = [models.Index(fields=["snapshot_date"])]


class DailyEstimate(models.Model):
    """Model ESTIMATE of a CSP's current Avg Balance. Stored apart from
    csp.DailyBalance and never written into it: the SBI-provided value stays the
    only actual. One row per CSP per estimate_date; recomputing the same day
    updates that row, earlier days are kept."""

    class Status(models.TextChoices):
        VALIDATED = "VALIDATED", "Validated"
        PROVISIONAL = "PROVISIONAL", "Provisional"
        INSUFFICIENT = "INSUFFICIENT_EVIDENCE", "Insufficient evidence"
        UNAVAILABLE = "UNAVAILABLE", "Unavailable"

    csp = models.ForeignKey("csp.Csp", on_delete=models.CASCADE, related_name="+")
    estimate_date = models.DateField()
    method = models.CharField(max_length=32)  # carry_forward | account_count_proportional
    status = models.CharField(max_length=24, choices=Status.choices)
    reason_code = models.CharField(max_length=48)
    # observed (SBI) side
    baseline_value = money(null=True, blank=True)
    baseline_date = models.DateField(null=True, blank=True)  # first date this value was seen
    last_seen_date = models.DateField(null=True, blank=True)
    # estimate side
    estimated_value = money(null=True, blank=True)
    estimated_delta = money(null=True, blank=True)
    baseline_account_count = models.PositiveIntegerField(null=True, blank=True)
    current_account_count = models.PositiveIntegerField(null=True, blank=True)
    account_count_change = models.IntegerField(null=True, blank=True)
    # attributed (exact-match) pool flow since baseline_date; informational, not in the value
    flow_net = money(null=True, blank=True)
    flow_coverage = models.FloatField(null=True, blank=True)
    # only when the account-count rule is VALIDATED: empirical 90th-pct abs % error
    expected_error_pct = models.FloatField(null=True, blank=True)
    error_basis = models.CharField(max_length=200, blank=True)
    hypothesis_run = models.ForeignKey(
        HypothesisRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    details = models.JSONField(default=dict, blank=True)
    calculated_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["csp", "estimate_date"], name="uniq_bankflow_estimate")
        ]
        indexes = [models.Index(fields=["estimate_date", "status"])]
