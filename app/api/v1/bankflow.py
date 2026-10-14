"""
Bank-flow validation resource -- scope `report:read` (reused deliberately: this
is an analysis/report surface, and a new scope would need an api migration).

One endpoint per capability, all thin wrappers over bankflow.services -- the
same functions the dashboard page calls. Read-only. Values here are analysis
outputs: predicted figures are labelled predictions and are never actual
balances.
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any, Literal

from bankflow import estimates, flows, intelligence, services
from ninja import Router

from api.auth import RequireScope
from api.middleware import ApiRequest
from api.pagination import clamp_limit, validate_date_range
from api.schemas import DataEnvelope, ListEnvelope, Pagination

router = Router(tags=["bank-flow"])
_scope = "report:read"


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


@router.get(
    "/summary",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Statement ingestion and reconciliation summary",
)
def summary(request: ApiRequest):
    return DataEnvelope(
        data=_plain(
            {
                "ingestion": services.ingestion_summary(),
                "quality": services.quality_flags(),
                "quarantined_files": services.quarantined_files(),
                "coverage": services.coverage_report(),
            }
        ),
        request_id=request.request_id,
    )


@router.get(
    "/code-resolution",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="KO-code match rates by count and by amount",
)
def code_resolution(request: ApiRequest):
    return DataEnvelope(
        data=_plain(
            {
                "match_rates": services.code_resolution_stats(),
                "unresolved_by_day": services.unresolved_by_day(),
            }
        ),
        request_id=request.request_id,
    )


@router.get(
    "/flows",
    response=ListEnvelope[dict],
    auth=RequireScope(_scope),
    summary="CSP-wise transaction-flow aggregates",
    description=(
        "Gross inflow/outflow and signed customer-balance flow per CSP over a date range, "
        "with latest Calling Sheet Avg Balance and held-out predictions where available. "
        "Signed flow is a HYPOTHESIS (see docs/BANK-FLOW.md), not an actual balance."
    ),
)
def csp_flows(
    request: ApiRequest,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    mode: Literal["exact_only", "with_provisional"] = "exact_only",
    definition: Literal["customer_wd_dep", "customer_wd_dep_xfer", "pool_net"] = "customer_wd_dep",
    order: Literal["gross", "net", "count"] = "gross",
    limit: int = 50,
    offset: int = 0,
):
    limit = clamp_limit(limit)
    validate_date_range(date_from, date_to)
    rows, total = services.csp_flow_table(
        date_from=date_from,
        date_to=date_to,
        mode=mode,
        definition=definition,
        limit=limit,
        offset=offset,
        order=order,
    )
    return ListEnvelope(
        data=[_plain({k: v for k, v in r.items() if k != "types"}) for r in rows],
        pagination=Pagination(limit=limit, offset=offset, total=total),
        request_id=request.request_id,
    )


@router.get(
    "/evaluations",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Latest model-evaluation result per target",
)
def evaluations(request: ApiRequest):
    return DataEnvelope(
        data=_plain(
            {
                "evaluations": services.evaluation_overview(),
                "definitions": sorted(flows.FLOW_DEFINITIONS),
            }
        ),
        request_id=request.request_id,
    )


@router.get(
    "/target",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Calling Sheet Avg Balance refresh-event evidence",
)
def target(request: ApiRequest):
    return DataEnvelope(data=_plain(services.target_overview()), request_id=request.request_id)


@router.get(
    "/estimates",
    response=ListEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Daily Avg Balance estimates per CSP (model output, not actual balances)",
    description=(
        "Each row keeps the SBI-observed Calling Sheet value (`observed`) apart from the "
        "model estimate (`estimate`). `status` is VALIDATED, PROVISIONAL, INSUFFICIENT_EVIDENCE "
        "or UNAVAILABLE; `expected_error_pct` is present only for VALIDATED. Estimates are never "
        "written to the actual balance table."
    ),
)
def daily_estimates(
    request: ApiRequest,
    estimate_date: dt.date | None = None,
    status: Literal["VALIDATED", "PROVISIONAL", "INSUFFICIENT_EVIDENCE", "UNAVAILABLE"]
    | None = None,
    limit: int = 50,
    offset: int = 0,
):
    limit = clamp_limit(limit)
    rows, total, used_date = estimates.estimate_rows(
        estimate_date=estimate_date, status=status, limit=limit, offset=offset
    )
    return ListEnvelope(
        data=_plain(rows),
        pagination=Pagination(limit=limit, offset=offset, total=total),
        request_id=request.request_id,
    )


@router.get(
    "/estimates/summary",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Estimate status counts and the account-count hypothesis verdict",
)
def estimates_summary(request: ApiRequest):
    return DataEnvelope(data=_plain(estimates.estimate_overview()), request_id=request.request_id)


@router.get(
    "/account-count-hypothesis",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Latest test of 'Avg Balance moves proportionally with account count'",
)
def account_count_hypothesis(request: ApiRequest):
    return DataEnvelope(
        data=_plain(estimates.hypothesis_overview()), request_id=request.request_id
    )


def _range(days: int, date_from: dt.date | None, date_to: dt.date | None):
    validate_date_range(date_from, date_to)
    return intelligence.resolve_range(days, date_from, date_to)


@router.get(
    "/dashboard-summary",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Dashboard headline metrics with source, interval and freshness",
    description=(
        "PMJDY account growth, customer withdrawals / deposits / net_customer_flow (deposits "
        "minus withdrawals; the raw pool_credits / pool_debits / net_pool_flow are kept), "
        "SBI-observed Avg Balance coverage and estimate coverage/validation status. A pool credit "
        "is a customer withdrawal and a pool debit a customer deposit (confirmed 2026-10-14)."
    ),
)
def dashboard_summary(
    request: ApiRequest, days: int = 30, date_from: dt.date | None = None,
    date_to: dt.date | None = None,
):
    frm, to = _range(days, date_from, date_to)
    return DataEnvelope(
        data=_plain(intelligence.dashboard_summary(frm, to)), request_id=request.request_id
    )


@router.get(
    "/network-movement",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Network flows, account growth and observed-vs-estimated Avg Balance over time",
)
def network_movement(
    request: ApiRequest, days: int = 30, date_from: dt.date | None = None,
    date_to: dt.date | None = None, granularity: Literal["daily", "hourly"] = "daily",
    hours: int = 48,
):
    if granularity == "hourly":
        return DataEnvelope(
            data=_plain(intelligence.hourly_activity(hours)), request_id=request.request_id
        )
    frm, to = _range(days, date_from, date_to)
    return DataEnvelope(
        data=_plain(intelligence.network_movement(frm, to)), request_id=request.request_id
    )


@router.get(
    "/csp-growth",
    response=ListEnvelope[dict],
    auth=RequireScope(_scope),
    summary="CSP Growth Explorer: accounts, flows, observed and estimated Avg Balance per CSP",
)
def csp_growth(
    request: ApiRequest, days: int = 30, date_from: dt.date | None = None,
    date_to: dt.date | None = None, search: str = "",
    status: Literal["VALIDATED", "PROVISIONAL", "INSUFFICIENT_EVIDENCE", "UNAVAILABLE", ""] = "",
    flag: str = "",
    sort: Literal[tuple(intelligence.SORTS)] = "net_flow",  # type: ignore[valid-type]
    order: Literal["asc", "desc"] = "desc", limit: int = 50, offset: int = 0,
):
    limit = clamp_limit(limit)
    frm, to = _range(days, date_from, date_to)
    rows, total, _meta = intelligence.csp_growth(
        frm, to, search=search[:64], status=status, flag=flag[:48], sort=sort,
        descending=order == "desc", limit=limit, offset=max(0, offset),
    )
    return ListEnvelope(
        data=_plain(rows),
        pagination=Pagination(limit=limit, offset=offset, total=total),
        request_id=request.request_id,
    )


@router.get(
    "/exceptions",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Evidence-based CSP exceptions and data-quality indicators",
)
def exceptions(
    request: ApiRequest, days: int = 30, date_from: dt.date | None = None,
    date_to: dt.date | None = None, limit: int = 100,
):
    frm, to = _range(days, date_from, date_to)
    return DataEnvelope(
        data=_plain(intelligence.exceptions(frm, to, limit=clamp_limit(limit))),
        request_id=request.request_id,
    )


@router.get(
    "/progress",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="What each automated step did today and yesterday, and when",
    description=(
        "Per step: schedule, last run, next due, health (on_time / late / waiting / never), and "
        "what happened today and yesterday (IST). Also the recent statement files with the time "
        "the bank file was produced and the time it was ingested, and a chronological timeline."
    ),
)
def progress(request: ApiRequest):
    from bankflow import progress as progress_mod

    return DataEnvelope(
        data=_plain(progress_mod.pipeline_progress()), request_id=request.request_id
    )


@router.get(
    "/day-compare",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Today (the present IST day) against yesterday, network-wide or for one CSP",
    description=(
        "Customer accounts, new accounts, customer withdrawals and deposits (a pool credit is a "
        "withdrawal and a pool debit a deposit; other transaction types are not counted), SBI "
        "average balance and our estimate, each as of today and as of yesterday with the change. "
        "Today's money movement is 'so far' until a statement proves the day complete, and the "
        "response says when a money comparison is not like-for-like. Missing figures are null."
    ),
)
def day_compare(request: ApiRequest, csp: str = ""):
    codes = [csp.strip()[:16]] if csp.strip() else None
    return DataEnvelope(
        data=_plain(intelligence.day_compare(csp_codes=codes)), request_id=request.request_id
    )
