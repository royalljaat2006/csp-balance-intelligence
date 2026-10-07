"""
Core data model — see PRD.md §10 for the full design rationale.

Ingestion (FR1/FR2), the dbt calculation layer (FR4), and the API (FR5) all
read/write these tables. Kept intentionally minimal at this stage: this is
the scaffolding pass, not the ingestion/calculation implementation.
"""

from django.db import models

#: Csp.status value for a bare stub created only to satisfy a foreign key
#: (a historical Calling Sheet backfill, or a transaction/mart row
#: referencing a code with no live Calling Sheet presence yet) -- never
#: a CSP that's actually part of the current roster. ingest_calling_sheet's
#: Csp upsert clears this the moment a live poll actually sees the CSP, so
#: it's never sticky once a CSP is genuinely active again.
HISTORICAL_ONLY_STATUS = "historical_only"


class CspQuerySet(models.QuerySet):
    def tracked(self):
        """Every CSP actually part of the current roster -- i.e. everything
        this app should display or compute anything for. Excludes a bare
        stub (see HISTORICAL_ONLY_STATUS). Use this, not .all(), for every
        CSP listing/count/bulk-comparison a person or dashboard sees --
        .all() is for internal bookkeeping (e.g. resolving a known
        csp_code) where the distinction doesn't apply."""
        return self.exclude(status=HISTORICAL_ONLY_STATUS)


class Csp(models.Model):
    """CSP master data, sourced from the CALLING SHEET (PRD §7.1)."""

    csp_code = models.CharField(max_length=32, primary_key=True)
    name = models.CharField(max_length=255, blank=True)
    email = models.EmailField(max_length=255, blank=True)
    mobile = models.CharField(max_length=20, blank=True)
    account_count = models.PositiveIntegerField(null=True, blank=True)
    zone = models.CharField(max_length=100, blank=True)
    tl_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=32, blank=True)
    raw_attrs = models.JSONField(default=dict, blank=True)
    first_seen_date = models.DateField(null=True, blank=True)
    last_seen_date = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = CspQuerySet.as_manager()

    class Meta:
        ordering = ["csp_code"]

    def __str__(self) -> str:
        return f"{self.csp_code} ({self.name})" if self.name else self.csp_code


class DailyBalance(models.Model):
    """One row per CSP per day — the input to Monthly Average Balance (PRD §5.2)."""

    class Source(models.TextChoices):
        CALLING_SHEET = "calling_sheet", "Calling sheet"
        HISTORY = "history", "History backfill"
        CARRY_FORWARD = "carry_forward", "Carried forward"
        DEMO = "demo", "Synthetic demo data (dev only, see seed_demo_balances)"

    csp = models.ForeignKey(Csp, on_delete=models.CASCADE, related_name="daily_balances")
    balance_date = models.DateField()
    daily_avg_balance = models.DecimalField(max_digits=14, decimal_places=2)
    account_count = models.PositiveIntegerField(
        null=True, blank=True, help_text="CSP's total account count as of this day's reading."
    )
    source = models.CharField(max_length=20, choices=Source.choices)
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["csp", "balance_date"], name="uniq_csp_balance_date")
        ]
        indexes = [models.Index(fields=["csp", "balance_date"])]
        ordering = ["csp_id", "balance_date"]

    def __str__(self) -> str:
        return f"{self.csp_id} @ {self.balance_date} = {self.daily_avg_balance}"


class Transaction(models.Model):
    """
    ONUS-family Success transactions only — see PRD §7.2 for the exact
    7-type allow-list. Secondary activity context; not an input to MAB.
    """

    class Category(models.TextChoices):
        WITHDRAWAL = "withdrawal", "Withdrawal"
        DEPOSIT = "deposit", "Deposit"
        FUND_TRANSFER = "fund_transfer", "Fund transfer"

    class Direction(models.TextChoices):
        IN_POOL = "in_pool", "Into pool"
        OUT_POOL = "out_pool", "Out of pool"
        OTHER = "other", "Other"

    ref_number = models.CharField(max_length=64, primary_key=True)
    csp = models.ForeignKey(Csp, on_delete=models.CASCADE, related_name="transactions")
    txn_datetime = models.DateTimeField()
    txn_date = models.DateField()
    txn_type = models.CharField(max_length=64)
    category = models.CharField(max_length=20, choices=Category.choices)
    direction = models.CharField(max_length=10, choices=Direction.choices)
    from_account = models.CharField(max_length=32, blank=True)
    to_account = models.CharField(max_length=32, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    source_file = models.CharField(max_length=255, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["csp", "txn_date"])]
        ordering = ["-txn_datetime"]

    def __str__(self) -> str:
        return f"{self.ref_number} ({self.txn_type})"


class MonthlySummary(models.Model):
    """
    Per-CSP, per-month rollup — built by the dbt calculation layer (FR4) plus
    the Python projection step. The API (FR5) reads this table directly.
    """

    class Slab(models.TextChoices):
        NIL = "NIL", "Up to 2,500 (NIL)"
        S1 = "S1", "2,501-4,000 (1.10% p.a.)"
        S2 = "S2", "4,001-6,000 (1.20% p.a.)"
        S3 = "S3", "6,001-10,000 (1.25% p.a.)"
        S4 = "S4", "Above 10,000 (1.30% p.a.)"

    class Trend(models.TextChoices):
        IMPROVING = "improving", "Improving"
        STABLE = "stable", "Stable"
        DECLINING = "declining", "Declining"

    csp = models.ForeignKey(Csp, on_delete=models.CASCADE, related_name="monthly_summaries")
    month = models.CharField(max_length=7)  # "YYYY-MM"
    days_in_month = models.PositiveSmallIntegerField()
    days_with_data = models.PositiveSmallIntegerField()
    mtd_mab = models.DecimalField(max_digits=14, decimal_places=2)
    projected_mab = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    slab = models.CharField(max_length=4, choices=Slab.choices)
    projected_slab = models.CharField(max_length=4, choices=Slab.choices, blank=True)
    incentive_rate_pa = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    projected_incentive_annual = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    gap_to_min = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gap_to_next_slab = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    prev_month_mab = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    mom_change_abs = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    mom_change_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    trend_7d_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    trend_flag = models.CharField(max_length=10, choices=Trend.choices, blank=True)
    is_eligible = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["csp", "month"], name="uniq_csp_month")]
        indexes = [models.Index(fields=["month", "slab"])]
        ordering = ["-month", "csp_id"]

    def __str__(self) -> str:
        return f"{self.csp_id} {self.month}: {self.slab}"


class DailyCspSnapshot(models.Model):
    """
    One row per CSP per business day — the historical-comparison foundation
    (2026-09-18 platform extension). Deliberately narrow: DailyBalance
    already IS the canonical daily balance history (unique on csp+date,
    never overwritten) and the dbt daily_activity mart already IS the
    canonical daily ONUS/Overall transaction history — this model does NOT
    duplicate either. It stores only what's genuinely missing: a daily
    Rule 19 slab classification, which today only exists at MONTH grain
    (MonthlySummary). "Slab as of a specific day" means the running
    month-to-date average *through that day* (mtd_avg_so_far), not that
    single day's raw balance — Rule 19 is a monthly-average rule, so
    classifying by one day's balance would show slab "changes" that don't
    correspond to anything real about the incentive. Same canonical
    csp.rules functions as MonthlySummary, fed a different (but equally
    real) input.

    Built and read by csp/comparison.py — never computed ad hoc by a view,
    the API, or an Autopilot prompt.
    """

    class DataStatus(models.TextChoices):
        FRESH = "fresh", "Fresh"
        DELAYED = "delayed", "Delayed"
        STALE = "stale", "Stale"
        NO_DATA = "no_data", "No data"

    csp = models.ForeignKey(Csp, on_delete=models.CASCADE, related_name="daily_snapshots")
    business_date = models.DateField()
    mtd_avg_so_far = models.DecimalField(max_digits=14, decimal_places=2)
    days_with_data_mtd = models.PositiveSmallIntegerField(default=0)
    slab = models.CharField(max_length=4, choices=MonthlySummary.Slab.choices)
    incentive_rate_pa = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    gap_to_min = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gap_to_next_slab = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    is_eligible = models.BooleanField(default=False)
    data_status = models.CharField(
        max_length=10, choices=DataStatus.choices, default=DataStatus.NO_DATA
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["csp", "business_date"], name="uniq_csp_snapshot_date"
            )
        ]
        indexes = [models.Index(fields=["business_date", "slab"])]
        ordering = ["-business_date", "csp_id"]

    def __str__(self) -> str:
        return f"{self.csp_id} @ {self.business_date}: {self.slab}"


class IngestLog(models.Model):
    """Audit trail for every ingestion run (FR1/FR2/FR3, FR7 health)."""

    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        RECEIVED = "received", "Received"
        PROCESSING = "processing", "Processing"
        DUPLICATE_SKIPPED = "duplicate_skipped", "Duplicate, skipped"
        INVALID_SOURCE = "invalid_source", "Invalid source"

    source = models.CharField(max_length=32)  # calling_sheet | transactions | history | telegram
    file_name = models.CharField(max_length=255, blank=True)
    file_mtime = models.DateTimeField(null=True, blank=True)
    # Reconciliation (Phase 13) — content-hash/external-ref based dedup, on
    # top of (and independent from) Transaction.ref_number's own row-level
    # upsert idempotency, which already makes re-processing a file safe.
    # These exist to make a repeat *file* cheap to detect and observable,
    # not to newly guarantee correctness that didn't already exist.
    source_hash = models.CharField(
        max_length=64,
        blank=True,
        help_text=(
            "Content fingerprint for file-level dedup: sha256 for folder-watch "
            "files, Telegram's own file_unique_id for Telegram-delivered ones."
        ),
    )
    external_ref = models.CharField(
        max_length=255,
        blank=True,
        help_text="Telegram update_id, or blank for other sources.",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUCCESS)
    rows_read = models.PositiveIntegerField(default=0)
    rows_valid = models.PositiveIntegerField(default=0)
    rows_rejected = models.PositiveIntegerField(default=0)
    rows_duplicate = models.PositiveIntegerField(default=0)
    rows_unmapped_csp = models.PositiveIntegerField(default=0)
    rows_upserted = models.PositiveIntegerField(default=0)
    error_summary = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["source_hash"])]

    def __str__(self) -> str:
        return f"{self.source} @ {self.started_at:%Y-%m-%d %H:%M} ({self.status})"
