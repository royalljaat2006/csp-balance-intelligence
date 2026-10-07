"""
Domain/service layer for the csp app — PRD §5/§8 FR4, Phase 4 requirement that
"business logic must remain in service/domain layers instead of being
embedded directly inside API endpoint functions."

No calculation here is new: this module moves the existing overview/list
logic out of the old api/endpoints.py verbatim, plus adds a few small
lookups (current balance, transactions) that simply query already-existing
tables — none of it changes what a number means, only where the code lives.
"""

from __future__ import annotations

import datetime as dt

from django.db import connection, transaction
from django.db.models import Avg, Count, QuerySet, Sum
from django.db.utils import OperationalError, ProgrammingError
from django.shortcuts import get_object_or_404
from ingestion.xlsx_validation import ONUS_TXN_TYPES

from .models import Csp, DailyBalance, IngestLog, MonthlySummary, Transaction

# How stale a CALLING SHEET ingest can be before the network-wide status
# stops reading "fresh" — same threshold /health has always used (FR7).
_FRESH_AFTER_HOURS = 26
_DELAYED_AFTER_HOURS = 48
_INGEST_SOURCES = ["calling_sheet", "transactions", "telegram"]


def current_month() -> str:
    return dt.date.today().strftime("%Y-%m")


def get_data_freshness() -> dict:
    """The one place "how fresh is our data" is decided — read by /health
    (common/views.py) and every dashboard page's freshness indicator, so
    the two surfaces can never disagree about what "fresh" means. Reports
    the most recent successful ingest per source, plus a single overall
    status (fresh/delayed/stale/missing) driven by the CALLING SHEET, the
    same source /health has always keyed staleness on (FR7) since it's the
    primary balance source everything else derives from."""
    last_success = {
        source: (
            IngestLog.objects.filter(source=source, status=IngestLog.Status.SUCCESS)
            .order_by("-started_at")
            .values_list("started_at", flat=True)
            .first()
        )
        for source in _INGEST_SOURCES
    }
    calling_sheet_at = last_success["calling_sheet"]
    if calling_sheet_at is None:
        overall_status = "missing"
    else:
        age_hours = (dt.datetime.now(dt.UTC) - calling_sheet_at).total_seconds() / 3600
        if age_hours <= _FRESH_AFTER_HOURS:
            overall_status = "fresh"
        elif age_hours <= _DELAYED_AFTER_HOURS:
            overall_status = "delayed"
        else:
            overall_status = "stale"

    return {
        "calling_sheet_last_success": calling_sheet_at,
        "transactions_last_success": last_success["transactions"],
        "telegram_last_success": last_success["telegram"],
        "overall_status": overall_status,
        "calling_sheet_stale": overall_status != "fresh",
    }


def get_csp_or_404(csp_code: str) -> Csp:
    return get_object_or_404(Csp, pk=csp_code)


def get_monthly_summary(csp_code: str, month: str | None = None) -> MonthlySummary | None:
    month = month or current_month()
    return MonthlySummary.objects.filter(csp_id=csp_code, month=month).select_related("csp").first()


def list_monthly_summaries(
    *,
    month: str | None = None,
    slab: str | None = None,
    trend: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[MonthlySummary], int]:
    month = month or current_month()
    qs = MonthlySummary.objects.filter(month=month).select_related("csp")
    if slab:
        qs = qs.filter(slab=slab)
    if trend:
        qs = qs.filter(trend_flag=trend)
    total = qs.count()
    return list(qs[offset : offset + limit]), total


def get_overview(month: str | None = None) -> dict:
    """Cross-CSP rollup — same output as before (slab counts, total, avg
    MAB), now computed as DB-side aggregates instead of pulling every
    MonthlySummary row for the month into Python and looping/summing
    there. Identical result, one query instead of loading N rows to derive
    a handful of numbers from them."""
    month = month or current_month()
    qs = MonthlySummary.objects.filter(month=month)
    slab_counts = {choice.value: 0 for choice in MonthlySummary.Slab}
    for row in qs.values("slab").annotate(count=Count("csp_id")):
        slab_counts[row["slab"]] = row["count"]

    # Excludes CSPs that only exist as a historical-backfill stub (status=
    # "historical_only", see ingestion/calling_sheet_ingest.py's note on
    # Csp.status) -- a CSP code seen only in old Calling Sheet snapshots or
    # referenced by a transaction but never present in a live poll. Counting
    # those here would inflate "CSPs tracked" with network churn instead of
    # reflecting the current roster, which is what this KPI is for.
    total_csps = Csp.objects.exclude(status="historical_only").count()
    aggregates = qs.aggregate(with_data=Count("csp_id"), avg_mab=Avg("mtd_mab"))
    with_data = aggregates["with_data"]
    avg_mab = float(aggregates["avg_mab"]) if aggregates["avg_mab"] is not None else None

    return {
        "month": month,
        "as_of": dt.date.today(),
        "csp_total": total_csps,
        "csp_with_data": with_data,
        "slab_distribution": slab_counts,
        "in_nil_count": slab_counts.get("NIL", 0),
        "compliant_count": with_data - slab_counts.get("NIL", 0),
        "eligible_count": qs.filter(is_eligible=True).count(),
        "avg_mab": avg_mab,
    }


def get_current_balance(csp: Csp) -> DailyBalance | None:
    return csp.daily_balances.order_by("-balance_date").first()


def get_balance_history(
    csp: Csp, *, date_from: dt.date | None = None, date_to: dt.date | None = None
) -> QuerySet[DailyBalance]:
    qs = csp.daily_balances.all().order_by("balance_date")
    if date_from:
        qs = qs.filter(balance_date__gte=date_from)
    if date_to:
        qs = qs.filter(balance_date__lte=date_to)
    return qs


def get_csp_account_growth(csp: Csp) -> list[dict]:
    """One CSP's account count over time, day by day, plus net-new accounts
    vs. the previous *known* reading (readings can have gaps — CALLING SHEET
    isn't guaranteed to run every single day — so this is a delta between
    consecutive data points, not necessarily consecutive calendar days).
    Net change only: opens minus closes, not a count of accounts opened —
    see docs/ARCHITECTURE.md for why (no per-account open-date data yet)."""
    rows = list(
        csp.daily_balances.exclude(account_count__isnull=True)
        .order_by("balance_date")
        .values("balance_date", "account_count")
    )
    out = []
    prev_count = None
    for r in rows:
        out.append(
            {
                "date": r["balance_date"],
                "account_count": r["account_count"],
                "net_new": (r["account_count"] - prev_count) if prev_count is not None else None,
            }
        )
        prev_count = r["account_count"]
    return out


def get_all_csps_account_growth() -> dict[str, dict]:
    """Every CSP's most recent account_count reading + net-new vs. its own
    previous reading, keyed by csp_code — the per-CSP-at-a-glance version of
    get_csp_account_growth, for a network-wide table broken down by CSP
    (rather than get_network_account_growth's single summed total)."""
    rows = (
        DailyBalance.objects.exclude(account_count__isnull=True)
        .order_by("csp_id", "balance_date")
        .values("csp_id", "balance_date", "account_count")
    )
    out: dict[str, dict] = {}
    prev_count_by_csp: dict[str, int] = {}
    for r in rows:
        csp_code = r["csp_id"]
        prev_count = prev_count_by_csp.get(csp_code)
        out[csp_code] = {
            "date": r["balance_date"],
            "account_count": r["account_count"],
            "net_new": (r["account_count"] - prev_count) if prev_count is not None else None,
        }
        prev_count_by_csp[csp_code] = r["account_count"]
    return out


def get_network_balance_trend(*, days: int | None = None) -> list[dict]:
    """Network-wide average daily balance over time — the one canonical
    "how is the network's balance moving" series, read by the Overview
    page's Network Balance Overview chart, Balance Intelligence, and
    Trends & Analytics, so the three can never show a different number
    for the same day. `days` (optional) bounds it to the trailing N days
    from the latest reading; omitted returns the full history on file."""
    qs = DailyBalance.objects.values("balance_date").annotate(
        avg_balance=Avg("daily_avg_balance"), reporting_count=Count("csp_id")
    ).order_by("balance_date")
    if days:
        latest = qs.order_by("-balance_date").values_list("balance_date", flat=True).first()
        if latest:
            qs = qs.filter(balance_date__gte=latest - dt.timedelta(days=days - 1))
    return [
        {
            "date": r["balance_date"],
            "avg_balance": float(r["avg_balance"]),
            "reporting_count": r["reporting_count"],
        }
        for r in qs
    ]


def get_network_account_growth() -> list[dict]:
    """Network-wide account count over time, summed across every CSP that
    reported an account_count on a given day — same day-over-day delta
    caveat as get_csp_account_growth, applied at network scale."""
    rows = (
        DailyBalance.objects.exclude(account_count__isnull=True)
        .values("balance_date")
        .annotate(total_accounts=Sum("account_count"), csp_count=Count("csp_id"))
        .order_by("balance_date")
    )
    out = []
    prev_total = None
    for r in rows:
        out.append(
            {
                "date": r["balance_date"],
                "total_accounts": r["total_accounts"],
                "csp_count": r["csp_count"],
                "net_new": (r["total_accounts"] - prev_total) if prev_total is not None else None,
            }
        )
        prev_total = r["total_accounts"]
    return out


_DAILY_ACTIVITY_COLUMNS = [
    "activity_date",
    "txn_count",
    "txn_amount",
    "withdrawal_count",
    "withdrawal_amount",
    "deposit_count",
    "deposit_amount",
    "cash_in_pool",
    "cash_out_pool",
    "net_flow",
    # ONUS-mode counterparts (dbt/models/marts/daily_activity.sql) — kept
    # alongside the Overall-mode columns above rather than a second query,
    # so a caller can read both modes from one already-fetched row.
    "onus_txn_count",
    "onus_txn_amount",
    "onus_withdrawal_count",
    "onus_withdrawal_amount",
    "onus_deposit_count",
    "onus_deposit_amount",
    "onus_cash_in_pool",
    "onus_cash_out_pool",
    "onus_net_flow",
]


def _query_daily_activity(
    *,
    csp_code: str | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> list[dict]:
    """
    Reads the dbt-materialized `daily_activity` mart (PRD FR4) — not a
    Django model, so this is raw SQL, same pattern as
    csp/management/commands/sync_monthly_summary.py. Wrapped in its own
    savepoint so a missing table (dbt hasn't run yet) degrades to an empty
    trend instead of poisoning the rest of the request's DB connection.
    """
    clauses = []
    params: list = []
    if csp_code:
        clauses.append("csp_code = %s")
        params.append(csp_code)
    if date_from:
        clauses.append("activity_date >= %s")
        params.append(date_from)
    if date_to:
        clauses.append("activity_date <= %s")
        params.append(date_to)
    where_sql = ("where " + " and ".join(clauses)) if clauses else ""

    columns = ", ".join(_DAILY_ACTIVITY_COLUMNS)
    sql = f"select {columns} from daily_activity {where_sql} order by activity_date"
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    except (ProgrammingError, OperationalError):
        # Postgres: "relation daily_activity does not exist"; SQLite (dev/test
        # fallback): "no such table" — either way, dbt hasn't run yet.
        return []
    return [dict(zip(_DAILY_ACTIVITY_COLUMNS, row, strict=True)) for row in rows]


def get_network_daily_trend(
    *, date_from: dt.date | None = None, date_to: dt.date | None = None
) -> list[dict]:
    """Network-wide daily transaction activity, summed across every CSP —
    real data (PRD §7.2 ONUS allow-list), descriptive context, not an input
    to MAB. Empty if the dbt mart hasn't been built yet. No default window —
    the ledger is a fixed historical range (e.g. one exported month), not a
    rolling "last N days" from today, so callers/views own the date filter."""
    rows = _query_daily_activity(date_from=date_from, date_to=date_to)
    by_date: dict[dt.date, dict] = {}
    for row in rows:
        d = row["activity_date"]
        bucket = by_date.setdefault(d, dict.fromkeys(_DAILY_ACTIVITY_COLUMNS[1:], 0))
        for col in _DAILY_ACTIVITY_COLUMNS[1:]:
            bucket[col] += row[col] or 0
    return [{"activity_date": d, **v} for d, v in sorted(by_date.items())]


def get_csp_daily_activity(
    csp_code: str, *, date_from: dt.date | None = None, date_to: dt.date | None = None
) -> list[dict]:
    """One CSP's daily transaction activity — real data, spans however much
    history the ingested transaction file(s) cover."""
    return _query_daily_activity(csp_code=csp_code, date_from=date_from, date_to=date_to)


def get_daily_activity_date_bounds() -> tuple[dt.date, dt.date] | None:
    """(earliest, latest) activity_date in the mart — None if it's empty.
    Used to size the Trends page's date picker sensibly instead of
    defaulting to "today" when the ledger is historical."""
    rows = _query_daily_activity()
    if not rows:
        return None
    dates = [r["activity_date"] for r in rows]
    return min(dates), max(dates)


def list_transactions(
    csp: Csp | None = None,
    *,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    txn_type: str | None = None,
    onus_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Transaction], int]:
    """`csp=None` lists network-wide (the Transactions Intelligence page);
    passing a `Csp` scopes it to just that one (CSP detail page, unchanged
    behavior). `onus_only` reuses xlsx_validation.is_onus_transaction_type's
    same ONUS_TXN_TYPES set — never a second ONUS type list."""
    qs = (
        csp.transactions.all()
        if csp is not None
        else Transaction.objects.select_related("csp")
    ).order_by("-txn_datetime")
    if date_from:
        qs = qs.filter(txn_date__gte=date_from)
    if date_to:
        qs = qs.filter(txn_date__lte=date_to)
    if txn_type:
        qs = qs.filter(txn_type=txn_type)
    if onus_only:
        qs = qs.filter(txn_type__in=ONUS_TXN_TYPES)
    total = qs.count()
    return list(qs[offset : offset + limit]), total


def get_active_transaction_csp_count(target_date: dt.date) -> int:
    """Distinct CSPs with at least one allow-listed transaction on a given
    date — real ledger data (Transaction rows only exist for allow-listed
    types per ingestion/transaction_ingest.py)."""
    return Transaction.objects.filter(txn_date=target_date).values("csp_id").distinct().count()


def get_transaction_type_distribution(
    *, date_from: dt.date | None = None, date_to: dt.date | None = None
) -> list[dict]:
    """Count + total amount per allow-listed transaction type actually on
    file — never the full raw-file type list (only allow-listed types ever
    reach the Transaction table, per ingestion/transaction_ingest.py)."""
    qs = Transaction.objects.all()
    if date_from:
        qs = qs.filter(txn_date__gte=date_from)
    if date_to:
        qs = qs.filter(txn_date__lte=date_to)
    return list(
        qs.values("txn_type")
        .annotate(count=Count("ref_number"), amount=Sum("amount"))
        .order_by("-count")
    )
