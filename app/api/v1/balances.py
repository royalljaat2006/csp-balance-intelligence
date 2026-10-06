"""Daily-balance resource, nested under a CSP — scope `balance:read`."""

from __future__ import annotations

import datetime as dt

from csp import services
from django.http import Http404
from ninja import Router

from api.auth import RequireScope
from api.middleware import ApiRequest
from api.pagination import clamp_limit, validate_date_range
from api.schemas import CurrentBalanceOut, DailyBalanceOut, DataEnvelope, ListEnvelope, Pagination

router = Router(tags=["balances"])

_scope = "balance:read"


@router.get(
    "/{csp_code}/balance",
    response=DataEnvelope[CurrentBalanceOut],
    auth=RequireScope(_scope),
    summary="Get current balance",
    description="The CSP's most recent daily average balance figure (PRD §7.1).",
)
def current_balance(request: ApiRequest, csp_code: str):
    csp = services.get_csp_or_404(csp_code)
    latest = services.get_current_balance(csp)
    if latest is None:
        raise Http404(f"No balance data yet for CSP {csp_code}.")
    return DataEnvelope(
        data=CurrentBalanceOut(
            csp_code=csp_code,
            date=latest.balance_date,
            daily_avg_balance=float(latest.daily_avg_balance),
            source=latest.source,
        ),
        request_id=request.request_id,
    )


@router.get(
    "/{csp_code}/balances/history",
    response=ListEnvelope[DailyBalanceOut],
    auth=RequireScope(_scope),
    summary="Get balance history",
    description="Daily average balance time series for one CSP, optionally bounded by date range.",
)
def balance_history(
    request: ApiRequest,
    csp_code: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    limit: int = 100,
    offset: int = 0,
):
    limit = clamp_limit(limit)
    validate_date_range(date_from, date_to)
    csp = services.get_csp_or_404(csp_code)
    qs = services.get_balance_history(csp, date_from=date_from, date_to=date_to)
    total = qs.count()
    rows = list(qs[offset : offset + limit])
    return ListEnvelope(
        data=[
            DailyBalanceOut(
                date=r.balance_date, daily_avg_balance=float(r.daily_avg_balance), source=r.source
            )
            for r in rows
        ],
        pagination=Pagination(limit=limit, offset=offset, total=total),
        request_id=request.request_id,
    )
