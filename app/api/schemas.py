"""
Response schemas — Phase 9 standard envelope + per-resource shapes.

Every endpoint returns either `DataEnvelope[X]` (single resource) or
`ListEnvelope[X]` (collection, with pagination) — never a bare model dump.
`request_id` is always populated from the request-ID middleware
(api/middleware.py), never generated ad hoc inside a view.
"""

from __future__ import annotations

import datetime as dt
from typing import Generic, TypeVar

from ninja import Schema

T = TypeVar("T")


class Pagination(Schema):
    limit: int
    offset: int
    total: int


class DataEnvelope(Schema, Generic[T]):  # noqa: UP046 — pydantic v2 generics, keep explicit Generic[T]
    data: T
    request_id: str


class ListEnvelope(Schema, Generic[T]):  # noqa: UP046
    data: list[T]
    pagination: Pagination
    request_id: str


# --- resource shapes ---------------------------------------------------


class SlabDistribution(Schema):
    NIL: int = 0
    S1: int = 0
    S2: int = 0
    S3: int = 0
    S4: int = 0


class OverviewOut(Schema):
    month: str
    as_of: dt.date
    csp_total: int
    csp_with_data: int
    slab_distribution: SlabDistribution
    in_nil_count: int
    compliant_count: int
    eligible_count: int
    avg_mab: float | None


class CspSummaryOut(Schema):
    csp_code: str
    name: str
    mtd_mab: float
    projected_mab: float | None
    slab: str
    trend_flag: str
    is_eligible: bool


class CspDetailOut(Schema):
    csp_code: str
    name: str
    mobile: str
    account_count: int | None
    status: str
    summary: CspSummaryOut | None


class DailyBalanceOut(Schema):
    date: dt.date
    daily_avg_balance: float
    source: str


class CurrentBalanceOut(Schema):
    csp_code: str
    date: dt.date
    daily_avg_balance: float
    source: str


class IncentiveOut(Schema):
    csp_code: str
    month: str
    mtd_mab: float
    slab: str
    incentive_rate_pa: float
    projected_incentive_annual: float | None
    gap_to_min: float
    gap_to_next_slab: float | None


class ProjectionOut(Schema):
    csp_code: str
    month: str
    days_in_month: int
    days_with_data: int
    projected_mab: float | None
    projected_slab: str
    trend_7d_pct: float | None
    trend_flag: str
    mom_change_abs: float | None
    mom_change_pct: float | None


class TransactionOut(Schema):
    ref_number: str
    txn_datetime: dt.datetime
    txn_type: str
    category: str
    direction: str
    amount: float


# --- comparison engine (csp/comparison.py) — 2026-09-18 platform extension -


class ComparisonMetricOut(Schema):
    current_date: dt.date
    comparison_date: dt.date | None
    current_value: float | None
    previous_value: float | None
    abs_change: float | None
    pct_change: float | None
    trend: str
    movement: str


class SlabMovementOut(Schema):
    current_slab: str | None
    previous_slab: str | None
    moved: bool
    upgraded: bool
    downgraded: bool


class CspComparisonOut(Schema):
    csp_code: str
    csp_name: str
    mode: str
    current_date: dt.date
    comparison_date: dt.date | None
    balance: ComparisonMetricOut
    txn_count: ComparisonMetricOut
    txn_amount: ComparisonMetricOut
    slab_movement: SlabMovementOut
    current_slab: str | None
    previous_slab: str | None
    mtd_avg_so_far: float | None
    gap_to_min: float | None
    gap_to_next_slab: float | None
    current_data_status: str
    comparison_data_status: str


class TrendIntelligenceOut(Schema):
    csp_code: str
    as_of_date: dt.date
    current_value: float | None
    previous_value: float | None
    avg_7d: float | None
    prev_avg_7d: float | None
    avg_30d: float | None
    consecutive_growth_days: int
    consecutive_decline_days: int
    last_growth_date: dt.date | None
    last_decline_date: dt.date | None
    trend_reversal: str


class MtdReadinessOut(Schema):
    current_mtd: float | None
    current_days: int
    previous_mtd: float | None
    comparison_date: dt.date | None
    comparison: ComparisonMetricOut


class AtRiskFlagOut(Schema):
    csp_code: str
    csp_name: str
    reasons: list[str]
