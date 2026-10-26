"""Cross-CSP rollup — scope `report:read`."""

from __future__ import annotations

from autopilot import activity, quality
from csp import services
from ninja import Router

from api.auth import RequireScope
from api.middleware import ApiRequest
from api.pagination import clamp_limit
from api.schemas import (
    DataEnvelope,
    ListEnvelope,
    OverviewOut,
    Pagination,
    SlabDistribution,
)

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


@router.get(
    "/agent-quality",
    response=DataEnvelope[dict],
    auth=RequireScope(_scope),
    summary="How well the AI assistant is doing, from what people did with its output",
    description=(
        "Aggregate counts and rates only (no message text): the share of drafted messages "
        "approved as written, how much people change the ones they keep (Levenshtein, as a "
        "share of length), model-call and investigation completion, and tokens per answered "
        "call by week. `days` is 1 to 365. A measure with no rows behind it is null with "
        "status `no_data`, never 0."
    ),
)
def agent_quality(request: ApiRequest, days: int = 30):
    return DataEnvelope(data=quality.agent_quality(days), request_id=request.request_id)


@router.get(
    "/agent-interactions",
    response=ListEnvelope[dict],
    auth=RequireScope(_scope),
    summary="The AI assistant's model calls (metadata only)",
    description=(
        "One row per model call: when, purpose, model, outcome, token counts, finish reason and "
        "latency. The prompt, the context it was given and the reply are deliberately not "
        "included: they hold CSP names and figures and are shown to staff on the dashboard only."
    ),
)
def agent_interactions(
    request: ApiRequest, purpose: str = "", outcome: str = "", days: int = 30,
    limit: int = 50, offset: int = 0,
):
    limit = clamp_limit(limit)
    data = activity.call_metadata(
        purpose=purpose[:60], outcome=outcome[:12], days=days, limit=limit, offset=max(0, offset))
    return ListEnvelope(
        data=data["rows"],
        pagination=Pagination(limit=limit, offset=offset, total=data["total"]),
        request_id=request.request_id,
    )
