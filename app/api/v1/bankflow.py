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


@router.get(
    "/csp-day-compare",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Every CSP: today (the present IST day) against yesterday",
    description=(
        "Per CSP: customer accounts, new accounts, customer withdrawals and deposits, deposits "
        "minus withdrawals, SBI average balance and our estimate, each for today and for "
        "yesterday with the change. Same definitions as day-compare. A figure with no stored "
        "value for that day is null, and a change needs both days. Money for today is 'so far' "
        "until a statement proves the day complete: see `like_for_like_money` and `money_note`."
    ),
)
def csp_day_compare(
    request: ApiRequest, search: str = "",
    sort: Literal[tuple(intelligence.DAY_SORTS)] = "accounts_change",  # type: ignore[valid-type]
    order: Literal["asc", "desc"] = "desc", limit: int = 50, offset: int = 0,
):
    limit = clamp_limit(limit)
    rows, total, meta = intelligence.csp_day_compare_rows(
        search=search[:64], sort=sort, descending=order == "desc", limit=limit,
        offset=max(0, offset),
    )
    return DataEnvelope(
        data=_plain({**meta, "rows": rows, "total": total, "limit": limit, "offset": offset}),
        request_id=request.request_id,
    )


@router.get(
    "/data-readiness",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Data readiness: from 'received' to 'can support an estimate'",
    description=(
        "Six stages (received, file checks, duplicates, CSP and type identification, counted vs "
        "excluded, estimates) plus a verdict that keeps 'imports ran without a technical "
        "failure' apart from 'complete enough for a reliable estimate'. Read-only."
    ),
)
def data_readiness(request: ApiRequest, days: int = 30):
    from bankflow import readiness

    return DataEnvelope(
        data=_plain(readiness.data_readiness(days=min(max(days, 1), 90))),
        request_id=request.request_id,
    )


@router.get(
    "/review",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Everything that needs a person's review",
    description=(
        "Unlinked or ambiguous CSP codes, unrecognised labels, possible duplicates and reversals, "
        "missing or stale account counts, statement coverage gaps, late rows and failed imports, "
        "each with what happened, the impact and the next step. Read-only: nothing here changes a "
        "figure, and flagged rows are never counted automatically."
    ),
)
def review(request: ApiRequest, days: int = 30):
    from bankflow import review as review_mod

    return DataEnvelope(
        data=_plain(review_mod.review_queue(min(max(days, 1), 90))), request_id=request.request_id
    )


@router.get(
    "/estimate-explanation/{csp_code}",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Why a CSP's estimate has its value",
    description=(
        "The stored baseline and its receipt date, the account counts, the combined baseline "
        "(average x count), eligible deposits and withdrawals since the baseline, and the stored "
        "estimate with its method and status. Movement is shown as evidence and is not added to "
        "the estimate. 404 for an unknown CSP."
    ),
)
def estimate_explanation(request: ApiRequest, csp_code: str):
    from bankflow import explain
    from django.http import Http404

    data = explain.explain_estimate(csp_code.strip()[:16])
    if data is None:
        raise Http404(f"No CSP with code {csp_code}.")
    return DataEnvelope(data=_plain(data), request_id=request.request_id)


@router.get(
    "/sbi-comparison",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Predicted vs SBI-reported values and the validation gate",
    description=(
        "One row per SBI refresh: predicted, SBI-reported, difference in rupees per account and "
        "percentage variance (N/A when the reported value is zero), with carry-forward alongside; "
        "aggregate and simple-average variance; results per refresh event and monthly cycle; and "
        "each acceptance criterion with its observation. The gate is never 'validated' on its "
        "own: a separate approval is required and none is recorded."
    ),
)
def sbi_comparison(
    request: ApiRequest, only_count_changed: bool = True, search: str = "",
    limit: int = 50, offset: int = 0,
):
    from bankflow import sbi_comparison as cmp_mod

    limit = clamp_limit(limit)
    return DataEnvelope(
        data=_plain(cmp_mod.sbi_comparison(
            only_count_changed=only_count_changed, search=search[:32], limit=limit,
            offset=max(0, offset))),
        request_id=request.request_id,
    )


@router.get(
    "/month-end-outlook",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="Shadow month-to-date average and month-end projection",
    description=(
        "From the stored daily estimates only: every calendar day counts once, a day without an "
        "estimate carries the previous one forward, and the projection carries the latest "
        "estimate over the remaining days. Informational and provisional; separate from the "
        "production MAB and from SBI-reported values."
    ),
)
def month_end_outlook(
    request: ApiRequest, month: str = "", search: str = "", limit: int = 25, offset: int = 0
):
    from bankflow import outlook

    limit = clamp_limit(limit)
    return DataEnvelope(
        data=_plain(outlook.month_end_outlook(
            month or None, search=search[:32], limit=limit, offset=max(0, offset))),
        request_id=request.request_id,
    )
