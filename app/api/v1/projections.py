"""Projection resource — month-end estimate + trend for a CSP. Scope `projection:read`."""

from __future__ import annotations

from csp import services
from django.http import Http404
from ninja import Router

from api.auth import RequireScope
from api.middleware import ApiRequest
from api.schemas import DataEnvelope, ProjectionOut

router = Router(tags=["projections"])

_scope = "projection:read"


@router.get(
    "/{csp_code}/projection",
    response=DataEnvelope[ProjectionOut],
    auth=RequireScope(_scope),
    summary="Get month-end projection",
    description=(
        "Projected month-end MAB/slab and the 7-day / month-over-month trend for a CSP "
        "(PRD FR4). `projected_mab` is an estimate, not a final figure, until the month closes."
    ),
)
def projection(request: ApiRequest, csp_code: str, month: str | None = None):
    summary = services.get_monthly_summary(csp_code, month)
    if summary is None:
        raise Http404(f"No monthly summary yet for CSP {csp_code}.")
    return DataEnvelope(
        data=ProjectionOut(
            csp_code=csp_code,
            month=summary.month,
            days_in_month=summary.days_in_month,
            days_with_data=summary.days_with_data,
            projected_mab=(
                float(summary.projected_mab) if summary.projected_mab is not None else None
            ),
            projected_slab=summary.projected_slab,
            trend_7d_pct=(
                float(summary.trend_7d_pct) if summary.trend_7d_pct is not None else None
            ),
            trend_flag=summary.trend_flag,
            mom_change_abs=(
                float(summary.mom_change_abs) if summary.mom_change_abs is not None else None
            ),
            mom_change_pct=(
                float(summary.mom_change_pct) if summary.mom_change_pct is not None else None
            ),
        ),
        request_id=request.request_id,
    )
