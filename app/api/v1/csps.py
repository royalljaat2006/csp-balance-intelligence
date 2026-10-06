"""CSP identity + monthly-summary resource — scope `csp:read`."""

from __future__ import annotations

from typing import Literal

from csp import services
from ninja import Router

from api.auth import RequireScope
from api.middleware import ApiRequest
from api.pagination import clamp_limit
from api.schemas import CspDetailOut, CspSummaryOut, DataEnvelope, ListEnvelope, Pagination

router = Router(tags=["csps"])

_scope = "csp:read"


def _to_summary_out(row) -> CspSummaryOut:
    return CspSummaryOut(
        csp_code=row.csp_id,
        name=row.csp.name,
        mtd_mab=float(row.mtd_mab),
        projected_mab=float(row.projected_mab) if row.projected_mab is not None else None,
        slab=row.slab,
        trend_flag=row.trend_flag,
        is_eligible=row.is_eligible,
    )


@router.get(
    "",
    response=ListEnvelope[CspSummaryOut],
    auth=RequireScope(_scope),
    summary="List CSPs' monthly summary",
    description=(
        "Every CSP's Monthly Average Balance (MAB), slab, and trend for a given month "
        "(defaults to the current month). Filter by `slab` or `trend`."
    ),
)
def list_csps(
    request: ApiRequest,
    month: str | None = None,
    slab: Literal["NIL", "S1", "S2", "S3", "S4"] | None = None,
    trend: Literal["improving", "stable", "declining"] | None = None,
    limit: int = 50,
    offset: int = 0,
):
    limit = clamp_limit(limit)
    rows, total = services.list_monthly_summaries(
        month=month, slab=slab, trend=trend, limit=limit, offset=offset
    )
    return ListEnvelope(
        data=[_to_summary_out(r) for r in rows],
        pagination=Pagination(limit=limit, offset=offset, total=total),
        request_id=request.request_id,
    )


@router.get(
    "/{csp_code}",
    response=DataEnvelope[CspDetailOut],
    auth=RequireScope(_scope),
    summary="Get one CSP",
    description="CSP identity fields plus its current month's summary (null if no data yet).",
)
def get_csp(request: ApiRequest, csp_code: str, month: str | None = None):
    csp = services.get_csp_or_404(csp_code)
    summary = services.get_monthly_summary(csp_code, month)
    return DataEnvelope(
        data=CspDetailOut(
            csp_code=csp.csp_code,
            name=csp.name,
            mobile=csp.mobile,
            account_count=csp.account_count,
            status=csp.status,
            summary=_to_summary_out(summary) if summary else None,
        ),
        request_id=request.request_id,
    )
