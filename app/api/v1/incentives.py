"""Incentive resource — the Rule #19 slab/rate/gap view of a CSP's month. Scope `incentive:read`."""

from __future__ import annotations

from csp import services
from django.http import Http404
from ninja import Router

from api.auth import RequireScope
from api.middleware import ApiRequest
from api.schemas import DataEnvelope, IncentiveOut

router = Router(tags=["incentives"])

_scope = "incentive:read"


@router.get(
    "/{csp_code}/incentive",
    response=DataEnvelope[IncentiveOut],
    auth=RequireScope(_scope),
    summary="Get incentive position",
    description=(
        "Where this CSP sits against the SBI Rule #19 slabs for a given month "
        "(defaults to current): current slab, incentive rate, projected annual "
        "incentive, and the gap to the minimum / next slab (PRD §5.1)."
    ),
)
def incentive(request: ApiRequest, csp_code: str, month: str | None = None):
    summary = services.get_monthly_summary(csp_code, month)
    if summary is None:
        raise Http404(f"No monthly summary yet for CSP {csp_code}.")
    return DataEnvelope(
        data=IncentiveOut(
            csp_code=csp_code,
            month=summary.month,
            mtd_mab=float(summary.mtd_mab),
            slab=summary.slab,
            incentive_rate_pa=float(summary.incentive_rate_pa),
            projected_incentive_annual=(
                float(summary.projected_incentive_annual)
                if summary.projected_incentive_annual is not None
                else None
            ),
            gap_to_min=float(summary.gap_to_min),
            gap_to_next_slab=(
                float(summary.gap_to_next_slab) if summary.gap_to_next_slab is not None else None
            ),
        ),
        request_id=request.request_id,
    )
