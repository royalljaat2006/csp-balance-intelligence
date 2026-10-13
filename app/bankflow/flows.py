"""
CSP-wise transaction-flow aggregates (feature layer).

Aggregates are rebuilt deterministically from StatementTransaction +
CodeResolution, so they are always reproducible and never hand-edited.

`resolution_mode`:
  exact_only        only EXACT code matches are attributed to a CSP (primary).
  with_provisional  also attributes unconfirmed trailing-character variants
                    (sensitivity analysis only; the rule is unconfirmed).

Signed flow *definitions* are configurable and evaluated offline. They are
computed from the stored per-category sums, so trying another definition never
needs another ingest. The default, `customer_wd_dep`, is a HYPOTHESIS about
customer-balance effect (see normalize.py), not an established fact.
"""

from __future__ import annotations

import datetime as dt
import decimal
from collections import defaultdict
from collections.abc import Iterable

from csp.models import Csp

from .models import (
    CodeResolution,
    FlowAggregate,
    StatementReconciliation,
    StatementTransaction,
    UnresolvedFlowDaily,
)
from .resolution import EXACT, MISSING, VARIANT

Decimal = decimal.Decimal
ZERO = Decimal("0")

MODE_EXACT = "exact_only"
MODE_PROVISIONAL = "with_provisional"
MODES = (MODE_EXACT, MODE_PROVISIONAL)

# name -> (basis, categories or None for "all")
FLOW_DEFINITIONS: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "customer_wd_dep": ("customer", ("withdrawal", "deposit")),
    "customer_wd_dep_xfer": ("customer", ("withdrawal", "deposit", "transfer")),
    "pool_net": ("pool", None),
}
DEFAULT_DEFINITION = "customer_wd_dep"
_RECON_RANK = {"RECONCILED": 0, "PARTIAL": 1, "FAILED": 2}


def signed_flow(by_category: dict, definition: str) -> Decimal:
    """customer basis: sum(debit - credit); pool basis: sum(credit - debit)."""
    basis, cats = FLOW_DEFINITIONS[definition]
    total = ZERO
    for cat, vals in by_category.items():
        if cats is not None and cat not in cats:
            continue
        credit, debit = Decimal(vals["credit"]), Decimal(vals["debit"])
        total += (debit - credit) if basis == "customer" else (credit - debit)
    return total


def _worst(a: str, b: str) -> str:
    return a if _RECON_RANK.get(a, 0) >= _RECON_RANK.get(b, 0) else b


def rebuild_aggregates(dates: Iterable[dt.date] | None = None) -> dict[str, int]:
    """Rebuild FlowAggregate/UnresolvedFlowDaily for `dates` (default: every
    date that has statement transactions). Idempotent: existing rows for those
    dates are replaced."""
    if dates is None:
        dates = set(StatementTransaction.objects.values_list("txn_date", flat=True).distinct())
    known = set(Csp.objects.values_list("csp_code", flat=True))
    rebuilt = {"dates": 0, "aggregates": 0, "unresolved": 0}
    recon_by_statement = dict(StatementReconciliation.objects.values_list("statement_id", "status"))

    for day in sorted(set(dates)):
        # streamed (not materialised): a day can hold 100k+ rows
        txns = (
            StatementTransaction.objects.filter(txn_date=day)
            .select_related("resolution")
            .iterator(chunk_size=5000)
        )
        agg: dict[tuple[str, str], dict] = {}
        unresolved: dict[str, dict] = {}

        for t in txns:
            res: CodeResolution | None = getattr(t, "resolution", None)
            recon_status = recon_by_statement.get(t.first_statement_id, "")

            # --- unresolved bookkeeping (exact_only view: what is NOT attributed)
            exact_code = res.resolved_csp_id if res and res.status == EXACT else None
            if exact_code is None:
                status = res.status if res else MISSING
                if t.category == "pool_operations" and status == MISSING:
                    status = "pool_operations_no_csp"
                u = unresolved.setdefault(status, {"n": 0, "credit": ZERO, "debit": ZERO})
                u["n"] += 1
                u["credit"] += t.credit
                u["debit"] += t.debit

            # --- attributable code per mode
            codes = {MODE_EXACT: exact_code}
            prov = None
            if res and res.status == VARIANT and res.candidate_codes:
                prov = res.candidate_codes[0]
            codes[MODE_PROVISIONAL] = exact_code or (prov if prov in known else None)

            for mode, code in codes.items():
                if code is None or code not in known:
                    continue
                a = agg.setdefault(
                    (code, mode),
                    {
                        "n": 0,
                        "cn": 0,
                        "dn": 0,
                        "credit": ZERO,
                        "debit": ZERO,
                        "cat": defaultdict(lambda: {"count": 0, "credit": ZERO, "debit": ZERO}),
                        "typ": defaultdict(lambda: {"count": 0, "credit": ZERO, "debit": ZERO}),
                        "rev": ZERO,
                        "dup": 0,
                        "recon": "",
                    },
                )
                a["n"] += 1
                if t.credit:
                    a["cn"] += 1
                if t.debit:
                    a["dn"] += 1
                a["credit"] += t.credit
                a["debit"] += t.debit
                for bucket, key in ((a["cat"], t.category), (a["typ"], t.channel_normalized)):
                    b = bucket[key]
                    b["count"] += 1
                    b["credit"] += t.credit
                    b["debit"] += t.debit
                if t.is_reversal_candidate:
                    a["rev"] += t.credit or t.debit
                if t.is_duplicate_ref:
                    a["dup"] += 1
                a["recon"] = (
                    _worst(a["recon"] or "RECONCILED", recon_status) if recon_status else a["recon"]
                )

        def ser(d):
            return {
                k: {"count": v["count"], "credit": str(v["credit"]), "debit": str(v["debit"])}
                for k, v in d.items()
            }

        FlowAggregate.objects.filter(flow_date=day).delete()
        UnresolvedFlowDaily.objects.filter(flow_date=day).delete()
        FlowAggregate.objects.bulk_create(
            [
                FlowAggregate(
                    csp_id=code,
                    flow_date=day,
                    resolution_mode=mode,
                    txn_count=a["n"],
                    credit_count=a["cn"],
                    debit_count=a["dn"],
                    credit_amount=a["credit"],
                    debit_amount=a["debit"],
                    by_category=ser(a["cat"]),
                    by_type=ser(a["typ"]),
                    reversal_candidate_amount=a["rev"],
                    duplicate_ref_count=a["dup"],
                    recon_status=a["recon"],
                )
                for (code, mode), a in agg.items()
            ],
            batch_size=1000,
        )
        UnresolvedFlowDaily.objects.bulk_create(
            [
                UnresolvedFlowDaily(
                    flow_date=day,
                    status=status,
                    txn_count=u["n"],
                    credit_amount=u["credit"],
                    debit_amount=u["debit"],
                )
                for status, u in unresolved.items()
            ]
        )
        rebuilt["dates"] += 1
        rebuilt["aggregates"] += len(agg)
        rebuilt["unresolved"] += len(unresolved)
    return rebuilt


def day_closing_balances() -> dict[dt.date, Decimal]:
    """Running balance of the LAST ledger row stored for each date (rows are
    stored in statement order; later hourly exports have higher ids)."""
    out: dict[dt.date, Decimal] = {}
    rows = StatementTransaction.objects.order_by("txn_date", "first_statement_id", "row_index")
    for d, bal in rows.values_list("txn_date", "balance").iterator(chunk_size=5000):
        out[d] = bal  # later rows overwrite earlier ones; the last per date wins
    return out


def proven_complete_dates() -> set[dt.date]:
    """Dates whose flow data is PROVEN complete by ledger continuity. Two independent
    proofs, either is enough:

    1. Within one imported statement (every running balance verified against the
       opening balance), every date from its first row date up to -- but not
       including -- its last row date is complete: later rows chain onto this
       day's closing balance, so no row of the day can be missing. (A multi-day
       statement, e.g. a full month, proves all of its days except the last.)
    2. Across statements: the next imported statement's opening balance equals
       the date's last stored running balance.

    The last day of an hourly/intra-day export has neither proof, so it is
    partial and excluded from evaluation windows.

    Assumes the bank's opening balance equals the prior closing balance (a
    continuous ledger); only checkable where the statements chain."""
    from .models import StatementFile

    complete: set[dt.date] = set()
    by_start: dict[dt.date, set[Decimal]] = defaultdict(set)
    for first, last, start, opening in StatementFile.objects.filter(
        status=StatementFile.Status.IMPORTED
    ).values_list("first_row_date", "last_row_date", "statement_start", "opening_balance"):
        if first is not None and last is not None:
            day = first
            while day < last:
                complete.add(day)
                day += dt.timedelta(days=1)
        if start is not None and opening is not None:
            by_start[start].add(opening)

    starts = sorted(by_start)
    for day, closing in day_closing_balances().items():
        nxt = next((s for s in starts if s > day), None)
        if nxt is not None and closing in by_start[nxt]:
            complete.add(day)
    return complete


def window_flows(
    csp_ids: Iterable[str],
    start_exclusive: dt.date,
    end_inclusive: dt.date,
    *,
    definition: str = DEFAULT_DEFINITION,
    mode: str = MODE_EXACT,
    complete_only: bool = True,
    complete_days: set[dt.date] | None = None,
) -> tuple[dict[str, Decimal], float]:
    """Signed flow per CSP over (start_exclusive, end_inclusive], plus coverage =
    the share of calendar days in that window with PROVEN-complete statement
    data. A window with no (complete) statements has zero coverage -- it is
    never reported as zero flow. Partial (intra-day) days are excluded unless
    `complete_only=False`."""
    total_days = (end_inclusive - start_exclusive).days
    if total_days <= 0:
        return {}, 0.0
    if complete_only and complete_days is None:
        complete_days = proven_complete_dates()
    ids = set(csp_ids)
    flows: dict[str, Decimal] = {}
    days_with_data: set[dt.date] = set()
    qs = FlowAggregate.objects.filter(
        resolution_mode=mode, flow_date__gt=start_exclusive, flow_date__lte=end_inclusive
    ).values("csp_id", "flow_date", "by_category")
    for row in qs.iterator():
        if complete_only and row["flow_date"] not in (complete_days or set()):
            continue
        days_with_data.add(row["flow_date"])
        if row["csp_id"] in ids:
            flows[row["csp_id"]] = flows.get(row["csp_id"], ZERO) + signed_flow(
                row["by_category"], definition
            )
    if complete_only:
        # a proven-complete statement day counts as covered even if none of its
        # rows could be attributed to a CSP
        days_with_data = {
            d for d in (complete_days or set()) if start_exclusive < d <= end_inclusive
        }
    return flows, len(days_with_data) / total_days
