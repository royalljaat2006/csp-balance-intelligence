"""
Historical comparison / trend-intelligence resource — csp.comparison's
canonical engine, exposed for the CSP Daily Comparison dashboard tab and
any other consumer. Scope `comparison:read`.

Two routers, both calling the exact same csp.comparison functions (never a
second copy of the comparison/filter/sort logic):
  - `router`         — CSP-scoped, mounted at /csps (mirrors balances.py,
    incentives.py, ...): /csps/{csp_code}/comparison, /trend, /mtd-readiness.
  - `network_router`  — bulk/network-wide, mounted at /comparisons: the
    dashboard tab's own list/top-movers/at-risk data sources.
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Literal

from csp import comparison as cmp
from csp import services
from ninja import Router

from api.auth import RequireScope
from api.middleware import ApiRequest
from api.pagination import MAX_LOOKBACK_DAYS, clamp_limit
from api.schemas import (
    AtRiskFlagOut,
    ComparisonMetricOut,
    CspComparisonOut,
    DataEnvelope,
    ListEnvelope,
    MtdReadinessOut,
    Pagination,
    SlabMovementOut,
    TrendIntelligenceOut,
)

router = Router(tags=["comparisons"])
network_router = Router(tags=["comparisons"])

_scope = "comparison:read"

Mode = Literal["overall", "onus"]
ComparisonType = Literal["yesterday", "same_date_previous_month", "custom"]
Fallback = Literal["last_day_of_month", "none"]
TrendLiteral = Literal["GROWTH", "DECLINE", "NO_CHANGE", "NO_DATA"]
MovementLiteral = Literal[
    "STRONG_GROWTH", "MODERATE_GROWTH", "STABLE", "MODERATE_DECLINE", "SHARP_DECLINE", "NO_DATA"
]
SlabLiteral = Literal["NIL", "S1", "S2", "S3", "S4"]
SortBy = Literal["pct_change", "abs_change", "csp_code", "csp_name", "mtd_avg_so_far"]
SortDir = Literal["asc", "desc"]


def _metric_out(result: cmp.ComparisonResult) -> ComparisonMetricOut:
    return ComparisonMetricOut(
        current_date=result.current_date,
        comparison_date=result.comparison_date,
        current_value=float(result.current_value) if result.current_value is not None else None,
        previous_value=(
            float(result.previous_value) if result.previous_value is not None else None
        ),
        abs_change=float(result.abs_change) if result.abs_change is not None else None,
        pct_change=float(result.pct_change) if result.pct_change is not None else None,
        trend=result.trend,
        movement=result.movement,
    )


def _comparison_out(c: cmp.CspComparison) -> CspComparisonOut:
    return CspComparisonOut(
        csp_code=c.csp_code,
        csp_name=c.csp_name,
        mode=c.mode,
        current_date=c.current_date,
        comparison_date=c.comparison_date,
        balance=_metric_out(c.balance),
        txn_count=_metric_out(c.txn_count),
        txn_amount=_metric_out(c.txn_amount),
        slab_movement=SlabMovementOut(
            current_slab=c.slab_movement.current_slab,
            previous_slab=c.slab_movement.previous_slab,
            moved=c.slab_movement.moved,
            upgraded=c.slab_movement.upgraded,
            downgraded=c.slab_movement.downgraded,
        ),
        current_slab=c.current_slab,
        previous_slab=c.previous_slab,
        mtd_avg_so_far=float(c.mtd_avg_so_far) if c.mtd_avg_so_far is not None else None,
        gap_to_min=float(c.gap_to_min) if c.gap_to_min is not None else None,
        gap_to_next_slab=float(c.gap_to_next_slab) if c.gap_to_next_slab is not None else None,
        current_data_status=c.current_data_status,
        comparison_data_status=c.comparison_data_status,
    )


def _resolve_dates(
    current_date: dt.date | None,
    comparison_date: dt.date | None,
    comparison_type: str,
    fallback: str,
) -> tuple[dt.date, dt.date | None]:
    """The one place every comparison-related endpoint below turns its query
    params into an actual (current, comparison) date pair — via
    csp.comparison.resolve_comparison_date(), never re-derived per-endpoint."""
    current = current_date or dt.date.today()
    resolved_comparison = cmp.resolve_comparison_date(
        current,
        comparison_type=comparison_type,
        explicit_comparison_date=comparison_date,
        fallback=fallback,
    )
    return current, resolved_comparison


@router.get(
    "/{csp_code}/comparison",
    response=DataEnvelope[CspComparisonOut],
    auth=RequireScope(_scope),
    summary="Compare one CSP across two dates",
    description=(
        "Balance, transaction activity, and slab movement for one CSP between "
        "current_date and a comparison date — pick the comparison date via "
        "comparison_type (yesterday, same_date_previous_month) or pass an exact "
        "comparison_date directly (comparison_date always wins if both are given)."
    ),
)
def csp_comparison(
    request: ApiRequest,
    csp_code: str,
    mode: Mode = "overall",
    current_date: dt.date | None = None,
    comparison_type: ComparisonType = "yesterday",
    comparison_date: dt.date | None = None,
    fallback: Fallback = "last_day_of_month",
):
    services.get_csp_or_404(csp_code)
    current, resolved_comparison = _resolve_dates(
        current_date, comparison_date, comparison_type, fallback
    )
    result = cmp.compare_csp_metrics(csp_code, current, resolved_comparison, mode=mode)
    return DataEnvelope(data=_comparison_out(result), request_id=request.request_id)


@router.get(
    "/{csp_code}/trend",
    response=DataEnvelope[TrendIntelligenceOut],
    auth=RequireScope(_scope),
    summary="Get one CSP's trend intelligence",
    description=(
        "Consecutive growth/decline streaks, the last trend reversal, and 7d/30d "
        "rolling balance averages, walked from real daily-balance history."
    ),
)
def csp_trend(
    request: ApiRequest, csp_code: str, as_of_date: dt.date | None = None, lookback_days: int = 45
):
    services.get_csp_or_404(csp_code)
    as_of = as_of_date or dt.date.today()
    lookback_days = max(1, min(lookback_days, MAX_LOOKBACK_DAYS))
    result = cmp.get_trend_intelligence(csp_code, as_of, lookback_days=lookback_days)
    return DataEnvelope(
        data=TrendIntelligenceOut(
            csp_code=result.csp_code,
            as_of_date=result.as_of_date,
            current_value=(
                float(result.current_value) if result.current_value is not None else None
            ),
            previous_value=(
                float(result.previous_value) if result.previous_value is not None else None
            ),
            avg_7d=float(result.avg_7d) if result.avg_7d is not None else None,
            prev_avg_7d=float(result.prev_avg_7d) if result.prev_avg_7d is not None else None,
            avg_30d=float(result.avg_30d) if result.avg_30d is not None else None,
            consecutive_growth_days=result.consecutive_growth_days,
            consecutive_decline_days=result.consecutive_decline_days,
            last_growth_date=result.last_growth_date,
            last_decline_date=result.last_decline_date,
            trend_reversal=result.trend_reversal,
        ),
        request_id=request.request_id,
    )


@router.get(
    "/{csp_code}/mtd-readiness",
    response=DataEnvelope[MtdReadinessOut],
    auth=RequireScope(_scope),
    summary="Get one CSP's MTD readiness vs previous month",
    description=(
        "Current month-to-date average vs the previous month's MTD average through "
        "the same day-of-month — the same-date-previous-month comparison, at MTD grain."
    ),
)
def csp_mtd_readiness(request: ApiRequest, csp_code: str, as_of_date: dt.date | None = None):
    services.get_csp_or_404(csp_code)
    as_of = as_of_date or dt.date.today()
    result = cmp.get_mtd_readiness(csp_code, as_of)
    current_mtd = result["current_mtd"]
    previous_mtd = result["previous_mtd"]
    return DataEnvelope(
        data=MtdReadinessOut(
            current_mtd=float(current_mtd) if current_mtd is not None else None,
            current_days=result["current_days"],
            previous_mtd=float(previous_mtd) if previous_mtd is not None else None,
            comparison_date=result["comparison_date"],
            comparison=_metric_out(result["comparison"]),
        ),
        request_id=request.request_id,
    )


@network_router.get(
    "",
    response=ListEnvelope[CspComparisonOut],
    auth=RequireScope(_scope),
    summary="List every CSP's comparison for a given date pair",
    description=(
        "The CSP Daily Comparison dashboard tab's own data source — filter by trend, "
        "movement bucket, slab, or a CSP code/name search, then sort and paginate. "
        "Reuses csp.comparison.filter_and_sort_comparisons(), the exact function the "
        "dashboard tab itself calls, so results are always identical."
    ),
)
def list_comparisons(
    request: ApiRequest,
    mode: Mode = "overall",
    current_date: dt.date | None = None,
    comparison_type: ComparisonType = "yesterday",
    comparison_date: dt.date | None = None,
    fallback: Fallback = "last_day_of_month",
    trend: TrendLiteral | None = None,
    movement: MovementLiteral | None = None,
    slab: SlabLiteral | None = None,
    search: str | None = None,
    sort_by: SortBy = "pct_change",
    sort_dir: SortDir = "desc",
    limit: int = 50,
    offset: int = 0,
):
    limit = clamp_limit(limit)
    current, resolved_comparison = _resolve_dates(
        current_date, comparison_date, comparison_type, fallback
    )
    # DB-side pre-filter (scalability fast-pass, 2026-09-21): when slab/search
    # narrows the set, only compute comparisons for the matching CSPs instead
    # of all of them. None means no pre-filter was applicable (trend/movement
    # alone, or no filter) — behaviour there is unchanged from before.
    matching_codes = cmp.resolve_matching_csp_codes(current, slab=slab, search=search)
    comparisons = cmp.bulk_compare_csps(
        current, resolved_comparison, mode=mode, csp_codes=matching_codes
    )
    filtered = cmp.filter_and_sort_comparisons(
        comparisons,
        trend=trend,
        movement=movement,
        slab=slab,
        search=search,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    total = len(filtered)
    page = filtered[offset : offset + limit]
    return ListEnvelope(
        data=[_comparison_out(c) for c in page],
        pagination=Pagination(limit=limit, offset=offset, total=total),
        request_id=request.request_id,
    )


@network_router.get(
    "/top-movers",
    response=ListEnvelope[CspComparisonOut],
    auth=RequireScope(_scope),
    summary="List the biggest balance movers",
    description="Top N CSPs by balance change (percent or absolute), growth or decline direction.",
)
def top_movers(
    request: ApiRequest,
    mode: Mode = "overall",
    current_date: dt.date | None = None,
    comparison_type: ComparisonType = "yesterday",
    comparison_date: dt.date | None = None,
    fallback: Fallback = "last_day_of_month",
    by: Literal["pct", "abs"] = "pct",
    direction: Literal["growth", "decline"] = "growth",
    limit: int = 10,
):
    limit = clamp_limit(limit)
    current, resolved_comparison = _resolve_dates(
        current_date, comparison_date, comparison_type, fallback
    )
    comparisons = cmp.bulk_compare_csps(current, resolved_comparison, mode=mode)
    movers = cmp.top_movers(comparisons, by=by, direction=direction, limit=limit)
    return ListEnvelope(
        data=[_comparison_out(c) for c in movers],
        pagination=Pagination(limit=limit, offset=0, total=len(movers)),
        request_id=request.request_id,
    )


@network_router.get(
    "/at-risk",
    response=ListEnvelope[AtRiskFlagOut],
    auth=RequireScope(_scope),
    summary="List explainable at-risk CSPs",
    description=(
        "CSPs flagged for a sharp decline, a slab downgrade, or being within "
        "near_threshold rupees of the eligibility minimum — every flag names its "
        "exact reason, never an opaque score."
    ),
)
def at_risk(
    request: ApiRequest,
    mode: Mode = "overall",
    current_date: dt.date | None = None,
    comparison_type: ComparisonType = "yesterday",
    comparison_date: dt.date | None = None,
    fallback: Fallback = "last_day_of_month",
    near_threshold: float | None = None,
):
    current, resolved_comparison = _resolve_dates(
        current_date, comparison_date, comparison_type, fallback
    )
    comparisons = cmp.bulk_compare_csps(current, resolved_comparison, mode=mode)
    threshold = decimal.Decimal(str(near_threshold)) if near_threshold is not None else None
    flags = cmp.at_risk_csps(comparisons, near_threshold=threshold)
    return ListEnvelope(
        data=[
            AtRiskFlagOut(csp_code=f.csp_code, csp_name=f.csp_name, reasons=f.reasons)
            for f in flags
        ],
        pagination=Pagination(limit=len(flags), offset=0, total=len(flags)),
        request_id=request.request_id,
    )
