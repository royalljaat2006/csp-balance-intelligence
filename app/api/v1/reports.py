"""Cross-CSP rollup — scope `report:read`."""

from __future__ import annotations

from csp import services
from ninja import Router

from api.auth import RequireScope
from api.middleware import ApiRequest
from api.schemas import DataEnvelope, OverviewOut, SlabDistribution

router = Router(tags=["reports"])

_scope = "report:read"


@router.get(
    "/overview",
    response=DataEnvelope[OverviewOut],
    auth=RequireScope(_scope),
    summary="Get the overall rollup",
    description=(
        "Slab distribution and average MAB across every CSP for a given month "
        '(defaults to current) — the "are we growing" headline view (PRD §1).'
    ),
)
def overview(request: ApiRequest, month: str | None = None):
    data = services.get_overview(month)
    return DataEnvelope(
        data=OverviewOut(
            month=data["month"],
            as_of=data["as_of"],
            csp_total=data["csp_total"],
            csp_with_data=data["csp_with_data"],
            slab_distribution=SlabDistribution(**data["slab_distribution"]),
            in_nil_count=data["in_nil_count"],
            compliant_count=data["compliant_count"],
            eligible_count=data["eligible_count"],
            avg_mab=data["avg_mab"],
        ),
        request_id=request.request_id,
    )
