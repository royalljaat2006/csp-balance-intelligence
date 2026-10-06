"""
Transaction resource, nested under a CSP — scope `transaction:read`.

Note: `csp.models.Transaction` exists and this endpoint is fully wired to
it, but ingestion doesn't upsert rows into it yet (see
ingestion/management/commands/ingest_transactions.py) — so today this
returns an empty list for every CSP, not because the endpoint is fake, but
because nothing has populated the table yet.
"""

from __future__ import annotations

import datetime as dt

from csp import services
from ninja import Router

from api.auth import RequireScope
from api.middleware import ApiRequest
from api.pagination import clamp_limit, validate_date_range
from api.schemas import ListEnvelope, Pagination, TransactionOut

router = Router(tags=["transactions"])

_scope = "transaction:read"


@router.get(
    "/{csp_code}/transactions",
    response=ListEnvelope[TransactionOut],
    auth=RequireScope(_scope),
    summary="List transactions",
    description=(
        "ONUS-family transactions for one CSP (the 7-type allow-list, PRD §7.2), "
        "optionally bounded by date range."
    ),
)
def list_transactions(
    request: ApiRequest,
    csp_code: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    limit: int = 50,
    offset: int = 0,
):
    limit = clamp_limit(limit)
    validate_date_range(date_from, date_to)
    csp = services.get_csp_or_404(csp_code)
    rows, total = services.list_transactions(
        csp, date_from=date_from, date_to=date_to, limit=limit, offset=offset
    )
    return ListEnvelope(
        data=[
            TransactionOut(
                ref_number=r.ref_number,
                txn_datetime=r.txn_datetime,
                txn_type=r.txn_type,
                category=r.category,
                direction=r.direction,
                amount=float(r.amount),
            )
            for r in rows
        ],
        pagination=Pagination(limit=limit, offset=offset, total=total),
        request_id=request.request_id,
    )
