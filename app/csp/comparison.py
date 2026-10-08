"""
Canonical historical-comparison engine — the ONE place date-aware CSP
comparisons are computed. API, dashboard, and autopilot all call into this
module; none of them re-derive a comparison, a trend, or a slab-movement
call independently (see docs/ARCHITECTURE.md's "one domain layer" rule).

Reuses, never re-implements:
  - csp.rules for slab/rate/gap math (same functions MonthlySummary uses).
  - csp.models.DailyBalance for daily balance history (already canonical).
  - the dbt daily_activity mart for daily ONUS/Overall transaction history
    (already canonical — read via the same _query_daily_activity() pattern
    csp/services.py already established).
  - csp.models.DailyCspSnapshot for the one genuinely new thing: a daily
    Rule 19 slab classification (see that model's docstring for why it's
    the MTD-so-far average's slab, not a single day's raw balance).

Every "current vs previous" result is a ComparisonResult — trend states
are GROWTH/DECLINE/NO_CHANGE/NO_DATA, and NO_DATA is never confused with
DECLINE (a missing day is not the same claim as a falling balance).
"""

from __future__ import annotations

import datetime as dt
import decimal
from dataclasses import dataclass, field

from django.db import connection
from django.db.models import Max, Q
from django.db.utils import OperationalError, ProgrammingError
from django.utils import timezone

from . import rules
from .models import Csp, DailyBalance, DailyCspSnapshot, MonthlySummary

# ---------------------------------------------------------------------------
# Configuration — thresholds/fallback behaviour live here, not hardcoded
# inline, so they can be tuned without touching the comparison logic itself.
# ---------------------------------------------------------------------------


class Trend(str):
    GROWTH = "GROWTH"
    DECLINE = "DECLINE"
    NO_CHANGE = "NO_CHANGE"
    NO_DATA = "NO_DATA"


class MovementBucket(str):
    STRONG_GROWTH = "STRONG_GROWTH"
    MODERATE_GROWTH = "MODERATE_GROWTH"
    STABLE = "STABLE"
    MODERATE_DECLINE = "MODERATE_DECLINE"
    SHARP_DECLINE = "SHARP_DECLINE"
    NO_DATA = "NO_DATA"


class TrendReversal(str):
    DECLINE_TO_GROWTH = "DECLINE_TO_GROWTH"
    GROWTH_TO_DECLINE = "GROWTH_TO_DECLINE"
    STABLE_TO_GROWTH = "STABLE_TO_GROWTH"
    STABLE_TO_DECLINE = "STABLE_TO_DECLINE"
    NONE = "NONE"


class DataStatus(str):
    FRESH = "fresh"
    DELAYED = "delayed"
    STALE = "stale"
    NO_DATA = "no_data"


# Movement-bucket thresholds, as percentages. Configurable: change these
# constants (or promote to a DB-backed setting later) rather than editing
# classify_movement()'s body.
MOVEMENT_THRESHOLDS = {
    "strong_growth": decimal.Decimal("10"),  # > +10%
    "moderate_growth": decimal.Decimal("2"),  # +2% to +10%
    "moderate_decline": decimal.Decimal("-2"),  # -2% to -10% (i.e. <= -2)
    "sharp_decline": decimal.Decimal("-10"),  # < -10%
}

# Data-freshness thresholds, in days since the latest known reading.
FRESHNESS_DELAYED_AFTER_DAYS = 1
FRESHNESS_STALE_AFTER_DAYS = 3

# Same-date-previous-month fallback when the target month is shorter (e.g.
# 31 Jan -> Feb has no 31st). Explicit and named, per the "must not silently
# compare against an arbitrary date" rule.
FALLBACK_LAST_DAY_OF_MONTH = "last_day_of_month"
FALLBACK_NONE = "none"  # comparison_date is None; caller sees NO_DATA

# Named comparison "shapes" — the API and the CSP Daily Comparison dashboard
# tab both pick a comparison_date via resolve_comparison_date() below rather
# than each re-implementing "what does yesterday/same-date-previous-month
# even mean" independently.
COMPARISON_TYPE_YESTERDAY = "yesterday"
COMPARISON_TYPE_SAME_DATE_PREVIOUS_MONTH = "same_date_previous_month"
COMPARISON_TYPE_CUSTOM = "custom"


# ---------------------------------------------------------------------------
# Date math
# ---------------------------------------------------------------------------


def resolve_same_date_previous_month(
    current_date: dt.date, *, fallback: str = FALLBACK_LAST_DAY_OF_MONTH
) -> dt.date | None:
    """27 Sep -> 27 Aug. Handles month-length differences explicitly: if the
    target month doesn't have that day (e.g. 31 Jan has no 31 Dec... wait,
    31 Mar -> 31 Feb doesn't exist), `fallback` decides what happens —
    FALLBACK_LAST_DAY_OF_MONTH clamps to that month's actual last day (the
    documented, explicit behaviour), FALLBACK_NONE returns None (caller
    reports NO_DATA rather than guessing). Never silently picks an
    arbitrary nearby date."""
    year = current_date.year
    month = current_date.month - 1
    if month == 0:
        month = 12
        year -= 1

    last_day_of_target_month = _last_day_of_month(year, month)
    day = current_date.day
    if day > last_day_of_target_month:
        if fallback == FALLBACK_LAST_DAY_OF_MONTH:
            day = last_day_of_target_month
        else:
            return None
    return dt.date(year, month, day)


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        next_month_first = dt.date(year + 1, 1, 1)
    else:
        next_month_first = dt.date(year, month + 1, 1)
    return (next_month_first - dt.timedelta(days=1)).day


def resolve_comparison_date(
    current_date: dt.date,
    *,
    comparison_type: str = COMPARISON_TYPE_YESTERDAY,
    explicit_comparison_date: dt.date | None = None,
    fallback: str = FALLBACK_LAST_DAY_OF_MONTH,
) -> dt.date | None:
    """The one place "which date are we comparing against" gets decided —
    the API and the dashboard both call this rather than each
    re-implementing the yesterday/same-date-previous-month/custom-date
    choice. `explicit_comparison_date` (a caller-supplied exact date) always
    wins over `comparison_type` when both are given."""
    if explicit_comparison_date is not None:
        return explicit_comparison_date
    if comparison_type == COMPARISON_TYPE_SAME_DATE_PREVIOUS_MONTH:
        return resolve_same_date_previous_month(current_date, fallback=fallback)
    return current_date - dt.timedelta(days=1)  # COMPARISON_TYPE_YESTERDAY, the default


# ---------------------------------------------------------------------------
# Trend / movement classification
# ---------------------------------------------------------------------------


def classify_trend(
    current: decimal.Decimal | None, previous: decimal.Decimal | None
) -> str:
    """Never classifies a missing value as DECLINE — that's the one rule
    every caller of this module depends on."""
    if current is None or previous is None:
        return Trend.NO_DATA
    if current > previous:
        return Trend.GROWTH
    if current < previous:
        return Trend.DECLINE
    return Trend.NO_CHANGE


def pct_change(
    current: decimal.Decimal | None, previous: decimal.Decimal | None
) -> decimal.Decimal | None:
    """None when either side is missing, or when previous is exactly zero
    (an undefined percentage, not an infinite one) — callers show "—", not
    a garbage number."""
    if current is None or previous is None or previous == 0:
        return None
    return ((current - previous) / previous * 100).quantize(decimal.Decimal("0.01"))


def classify_movement(pct: decimal.Decimal | None) -> str:
    if pct is None:
        return MovementBucket.NO_DATA
    if pct > MOVEMENT_THRESHOLDS["strong_growth"]:
        return MovementBucket.STRONG_GROWTH
    if pct > MOVEMENT_THRESHOLDS["moderate_growth"]:
        return MovementBucket.MODERATE_GROWTH
    if pct >= MOVEMENT_THRESHOLDS["moderate_decline"]:
        return MovementBucket.STABLE
    if pct >= MOVEMENT_THRESHOLDS["sharp_decline"]:
        return MovementBucket.MODERATE_DECLINE
    return MovementBucket.SHARP_DECLINE


def classify_freshness(latest_known_date: dt.date | None, as_of: dt.date) -> str:
    if latest_known_date is None:
        return DataStatus.NO_DATA
    delay = (as_of - latest_known_date).days
    if delay <= FRESHNESS_DELAYED_AFTER_DAYS:
        return DataStatus.FRESH
    if delay <= FRESHNESS_STALE_AFTER_DAYS:
        return DataStatus.DELAYED
    return DataStatus.STALE


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass
class ComparisonResult:
    current_date: dt.date
    comparison_date: dt.date | None
    current_value: decimal.Decimal | None
    previous_value: decimal.Decimal | None
    abs_change: decimal.Decimal | None = None
    pct_change: decimal.Decimal | None = None
    trend: str = Trend.NO_DATA
    movement: str = MovementBucket.NO_DATA

    def __post_init__(self):
        if self.current_value is not None and self.previous_value is not None:
            self.abs_change = self.current_value - self.previous_value
        self.pct_change = pct_change(self.current_value, self.previous_value)
        self.trend = classify_trend(self.current_value, self.previous_value)
        self.movement = classify_movement(self.pct_change)


@dataclass
class SlabMovement:
    current_slab: str | None
    previous_slab: str | None
    moved: bool = False
    upgraded: bool = False
    downgraded: bool = False

    def __post_init__(self):
        _order = {c.value: i for i, c in enumerate(MonthlySummary.Slab)}
        if self.current_slab and self.previous_slab:
            self.moved = self.current_slab != self.previous_slab
            if self.moved:
                cur_i, prev_i = _order.get(self.current_slab), _order.get(self.previous_slab)
                if cur_i is not None and prev_i is not None:
                    self.upgraded = cur_i > prev_i
                    self.downgraded = cur_i < prev_i


@dataclass
class CspComparison:
    csp_code: str
    csp_name: str
    mode: str
    current_date: dt.date
    comparison_date: dt.date | None
    balance: ComparisonResult
    txn_count: ComparisonResult
    txn_amount: ComparisonResult
    slab_movement: SlabMovement
    current_slab: str | None
    previous_slab: str | None
    mtd_avg_so_far: decimal.Decimal | None
    gap_to_min: decimal.Decimal | None
    gap_to_next_slab: decimal.Decimal | None
    current_data_status: str
    comparison_data_status: str


@dataclass
class TrendIntelligence:
    csp_code: str
    as_of_date: dt.date
    current_value: decimal.Decimal | None
    previous_value: decimal.Decimal | None
    avg_7d: decimal.Decimal | None
    prev_avg_7d: decimal.Decimal | None
    avg_30d: decimal.Decimal | None
    consecutive_growth_days: int
    consecutive_decline_days: int
    last_growth_date: dt.date | None
    last_decline_date: dt.date | None
    trend_reversal: str


# ---------------------------------------------------------------------------
# Data access helpers — thin wrappers so the functions above never touch
# the ORM/raw-SQL directly; keeps the "one canonical query per concern"
# rule even inside this module.
# ---------------------------------------------------------------------------


def _balance_on(csp_code: str, business_date: dt.date) -> decimal.Decimal | None:
    row = DailyBalance.objects.filter(csp_id=csp_code, balance_date=business_date).first()
    return row.daily_avg_balance if row else None


def _latest_balance_date(csp_code: str | None = None) -> dt.date | None:
    qs = DailyBalance.objects.all()
    if csp_code:
        qs = qs.filter(csp_id=csp_code)
    row = qs.order_by("-balance_date").first()
    return row.balance_date if row else None


_ACTIVITY_COLUMNS = [
    "activity_date",
    "txn_count",
    "txn_amount",
    "onus_txn_count",
    "onus_txn_amount",
]


def _activity_on(csp_code: str, business_date: dt.date, *, mode: str) -> dict | None:
    """Reads the dbt daily_activity mart for one CSP/day — same
    degrade-to-empty-on-missing-mart contract as
    csp.services._query_daily_activity (the mart may not exist yet in a
    fresh/dev environment)."""
    columns = ", ".join(_ACTIVITY_COLUMNS)
    sql = f"select {columns} from daily_activity where csp_code = %s and activity_date = %s"
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, [csp_code, business_date])
            row = cursor.fetchone()
    except (ProgrammingError, OperationalError):
        return None
    if row is None:
        return None
    data = dict(zip(_ACTIVITY_COLUMNS, row, strict=True))
    if mode == "onus":
        return {"count": data["onus_txn_count"], "amount": data["onus_txn_amount"]}
    return {"count": data["txn_count"], "amount": data["txn_amount"]}


def _snapshot_on(csp_code: str, business_date: dt.date) -> DailyCspSnapshot | None:
    return DailyCspSnapshot.objects.filter(csp_id=csp_code, business_date=business_date).first()


# ---------------------------------------------------------------------------
# Public comparison API
# ---------------------------------------------------------------------------


def compare_csp_metrics(
    csp_code: str,
    current_date: dt.date,
    comparison_date: dt.date | None,
    *,
    mode: str = "overall",
) -> CspComparison:
    """The canonical per-CSP comparison — balance, transaction activity
    (in the given mode), and slab movement, for any two business dates.
    `comparison_date=None` (e.g. same-date-previous-month fell off the
    calendar and fallback=FALLBACK_NONE) yields NO_DATA throughout, never
    a guessed date."""
    if mode not in ("onus", "overall"):
        raise ValueError(f"mode must be 'onus' or 'overall', got {mode!r}")

    csp = Csp.objects.filter(pk=csp_code).first()
    csp_name = csp.name if csp else csp_code

    current_balance = _balance_on(csp_code, current_date)
    previous_balance = _balance_on(csp_code, comparison_date) if comparison_date else None
    balance_cmp = ComparisonResult(current_date, comparison_date, current_balance, previous_balance)

    current_activity = _activity_on(csp_code, current_date, mode=mode)
    previous_activity = (
        _activity_on(csp_code, comparison_date, mode=mode) if comparison_date else None
    )
    count_cmp = ComparisonResult(
        current_date,
        comparison_date,
        decimal.Decimal(current_activity["count"]) if current_activity else None,
        decimal.Decimal(previous_activity["count"]) if previous_activity else None,
    )
    amount_cmp = ComparisonResult(
        current_date,
        comparison_date,
        current_activity["amount"] if current_activity else None,
        previous_activity["amount"] if previous_activity else None,
    )

    current_snap = _snapshot_on(csp_code, current_date)
    previous_snap = _snapshot_on(csp_code, comparison_date) if comparison_date else None
    slab_move = SlabMovement(
        current_slab=current_snap.slab if current_snap else None,
        previous_slab=previous_snap.slab if previous_snap else None,
    )

    return CspComparison(
        csp_code=csp_code,
        csp_name=csp_name,
        mode=mode,
        current_date=current_date,
        comparison_date=comparison_date,
        balance=balance_cmp,
        txn_count=count_cmp,
        txn_amount=amount_cmp,
        slab_movement=slab_move,
        current_slab=current_snap.slab if current_snap else None,
        previous_slab=previous_snap.slab if previous_snap else None,
        mtd_avg_so_far=current_snap.mtd_avg_so_far if current_snap else None,
        gap_to_min=current_snap.gap_to_min if current_snap else None,
        gap_to_next_slab=current_snap.gap_to_next_slab if current_snap else None,
        current_data_status=classify_freshness(_latest_balance_date(csp_code), current_date)
        if current_balance is None
        else DataStatus.FRESH,
        comparison_data_status=(
            DataStatus.NO_DATA
            if comparison_date is None
            else (DataStatus.FRESH if previous_balance is not None else DataStatus.NO_DATA)
        ),
    )


def bulk_compare_csps(
    current_date: dt.date,
    comparison_date: dt.date | None,
    *,
    mode: str = "overall",
    csp_codes: list[str] | None = None,
) -> list[CspComparison]:
    """The dashboard-tab/API bulk version — one query per data source for
    ALL requested CSPs (not one query per CSP), avoiding N+1 across 539+
    CSPs. Falls back to per-CSP calls only if the caller needs a handful."""
    csps = Csp.objects.tracked()
    if csp_codes:
        csps = csps.filter(csp_code__in=csp_codes)
    all_codes = list(csps.values_list("csp_code", "name"))

    current_balances = dict(
        DailyBalance.objects.filter(balance_date=current_date).values_list(
            "csp_id", "daily_avg_balance"
        )
    )
    previous_balances = (
        dict(
            DailyBalance.objects.filter(balance_date=comparison_date).values_list(
                "csp_id", "daily_avg_balance"
            )
        )
        if comparison_date
        else {}
    )
    current_snaps = {
        s.csp_id: s for s in DailyCspSnapshot.objects.filter(business_date=current_date)
    }
    previous_snaps = (
        {s.csp_id: s for s in DailyCspSnapshot.objects.filter(business_date=comparison_date)}
        if comparison_date
        else {}
    )
    current_activity = _bulk_activity_on(current_date)
    previous_activity = _bulk_activity_on(comparison_date) if comparison_date else {}

    results = []
    for csp_code, name in all_codes:
        current_balance = current_balances.get(csp_code)
        previous_balance = previous_balances.get(csp_code)
        balance_cmp = ComparisonResult(
            current_date, comparison_date, current_balance, previous_balance
        )

        cur_act = current_activity.get(csp_code)
        prev_act = previous_activity.get(csp_code)
        cur_count = cur_act[f"{mode}_count" if mode == "onus" else "count"] if cur_act else None
        prev_count = prev_act[f"{mode}_count" if mode == "onus" else "count"] if prev_act else None
        cur_amount = cur_act[f"{mode}_amount" if mode == "onus" else "amount"] if cur_act else None
        prev_amount = (
            prev_act[f"{mode}_amount" if mode == "onus" else "amount"] if prev_act else None
        )
        count_cmp = ComparisonResult(
            current_date,
            comparison_date,
            decimal.Decimal(cur_count) if cur_count is not None else None,
            decimal.Decimal(prev_count) if prev_count is not None else None,
        )
        amount_cmp = ComparisonResult(current_date, comparison_date, cur_amount, prev_amount)

        current_snap = current_snaps.get(csp_code)
        previous_snap = previous_snaps.get(csp_code)
        slab_move = SlabMovement(
            current_slab=current_snap.slab if current_snap else None,
            previous_slab=previous_snap.slab if previous_snap else None,
        )

        results.append(
            CspComparison(
                csp_code=csp_code,
                csp_name=name or csp_code,
                mode=mode,
                current_date=current_date,
                comparison_date=comparison_date,
                balance=balance_cmp,
                txn_count=count_cmp,
                txn_amount=amount_cmp,
                slab_movement=slab_move,
                current_slab=current_snap.slab if current_snap else None,
                previous_slab=previous_snap.slab if previous_snap else None,
                mtd_avg_so_far=current_snap.mtd_avg_so_far if current_snap else None,
                gap_to_min=current_snap.gap_to_min if current_snap else None,
                gap_to_next_slab=current_snap.gap_to_next_slab if current_snap else None,
                current_data_status=DataStatus.FRESH
                if current_balance is not None
                else DataStatus.NO_DATA,
                comparison_data_status=DataStatus.NO_DATA
                if comparison_date is None or previous_balance is None
                else DataStatus.FRESH,
            )
        )
    return results


def resolve_matching_csp_codes(
    current_date: dt.date, *, slab: str | None = None, search: str | None = None
) -> list[str] | None:
    """
    DB-side pre-filter for slab/search (scalability fast-pass, 2026-09-21)
    — lets a filtered `list_comparisons` API call build/compare far fewer
    than all 539+ CSPs instead of materializing every CspComparison then
    discarding most of them in filter_and_sort_comparisons(). Returns None
    (meaning "no pre-filter, caller must pass no csp_codes") when neither
    filter is given — trend/movement are derived from the comparison
    itself and can't be pushed down this way without recomputing
    classify_trend/classify_movement in SQL, which risks semantic drift;
    left as a Python post-filter, same as before this change.
    """
    if not slab and not search:
        return None
    codes: set[str] | None = None
    if slab:
        codes = set(
            DailyCspSnapshot.objects.filter(business_date=current_date, slab=slab).values_list(
                "csp_id", flat=True
            )
        )
    if search:
        needle = search.strip()
        search_codes = set(
            Csp.objects.filter(
                Q(csp_code__icontains=needle) | Q(name__icontains=needle)
            ).values_list("csp_code", flat=True)
        )
        codes = search_codes if codes is None else (codes & search_codes)
    return sorted(codes) if codes is not None else []


def _bulk_activity_on(business_date: dt.date) -> dict[str, dict]:
    sql = (
        "select csp_code, txn_count, txn_amount, onus_txn_count, onus_txn_amount "
        "from daily_activity where activity_date = %s"
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, [business_date])
            rows = cursor.fetchall()
    except (ProgrammingError, OperationalError):
        return {}
    return {
        r[0]: {"count": r[1], "amount": r[2], "onus_count": r[3], "onus_amount": r[4]} for r in rows
    }


def _average(values: list[decimal.Decimal]) -> decimal.Decimal | None:
    """The one place a list of balance readings becomes a single average —
    None for an empty list (NO_DATA), never a ZeroDivisionError."""
    if not values:
        return None
    return (sum(values, decimal.Decimal("0")) / len(values)).quantize(decimal.Decimal("0.01"))


# ---------------------------------------------------------------------------
# Rolling window comparisons
# ---------------------------------------------------------------------------


def get_rolling_balance_average(
    csp_code: str, end_date: dt.date, *, window_days: int = 7
) -> decimal.Decimal | None:
    start = end_date - dt.timedelta(days=window_days - 1)
    values = list(
        DailyBalance.objects.filter(
            csp_id=csp_code, balance_date__gte=start, balance_date__lte=end_date
        ).values_list("daily_avg_balance", flat=True)
    )
    return _average(values)


def compare_rolling_balance(
    csp_code: str, end_date: dt.date, *, window_days: int = 7
) -> ComparisonResult:
    """Current N-day average vs. the PRECEDING N-day window (not vs. the
    same window last month) — reduces one-day-fluctuation noise, per
    Phase 2C. E.g. window_days=7: [end-6..end] vs [end-13..end-7]."""
    current_avg = get_rolling_balance_average(csp_code, end_date, window_days=window_days)
    previous_end = end_date - dt.timedelta(days=window_days)
    previous_avg = get_rolling_balance_average(csp_code, previous_end, window_days=window_days)
    return ComparisonResult(end_date, previous_end, current_avg, previous_avg)


def get_mtd_readiness(csp_code: str, as_of_date: dt.date) -> dict:
    """Current MTD-so-far average vs. the previous month's MTD-so-far
    average through the same day-of-month — Phase 2D. "Readiness": this
    reuses the same DailyBalance history bulk_compare draws from, so once
    2 months of real data exist this needs no new plumbing, only callers."""
    month_start = as_of_date.replace(day=1)
    current_values = list(
        DailyBalance.objects.filter(
            csp_id=csp_code, balance_date__gte=month_start, balance_date__lte=as_of_date
        ).values_list("daily_avg_balance", flat=True)
    )
    current_mtd = _average(current_values)

    prev_same_day = resolve_same_date_previous_month(as_of_date)
    previous_mtd = None
    if prev_same_day:
        prev_month_start = prev_same_day.replace(day=1)
        previous_values = list(
            DailyBalance.objects.filter(
                csp_id=csp_code,
                balance_date__gte=prev_month_start,
                balance_date__lte=prev_same_day,
            ).values_list("daily_avg_balance", flat=True)
        )
        previous_mtd = _average(previous_values)

    return {
        "current_mtd": current_mtd,
        "current_days": len(current_values),
        "previous_mtd": previous_mtd,
        "comparison_date": prev_same_day,
        "comparison": ComparisonResult(as_of_date, prev_same_day, current_mtd, previous_mtd),
    }


# ---------------------------------------------------------------------------
# Trend intelligence — streaks, reversals
# ---------------------------------------------------------------------------


def get_trend_intelligence(
    csp_code: str, as_of_date: dt.date, *, lookback_days: int = 45
) -> TrendIntelligence:
    """Consecutive growth/decline days, last growth/decline date, and
    trend-reversal state, walking real DailyBalance history backwards from
    as_of_date. `lookback_days` bounds the walk (streaks longer than this
    are reported as exactly lookback_days, not undercounted — see the
    'incomplete count' branch below) rather than scanning the whole table."""
    window_start = as_of_date - dt.timedelta(days=lookback_days)
    rows = list(
        DailyBalance.objects.filter(
            csp_id=csp_code, balance_date__gte=window_start, balance_date__lte=as_of_date
        )
        .order_by("balance_date")
        .values_list("balance_date", "daily_avg_balance")
    )
    by_date = dict(rows)
    dates_with_data = sorted(by_date)

    current_value = by_date.get(as_of_date)
    previous_value = None
    if len(dates_with_data) >= 2 and dates_with_data[-1] == as_of_date:
        previous_value = by_date[dates_with_data[-2]]
    elif dates_with_data and dates_with_data[-1] != as_of_date:
        current_value = None  # no reading exactly on as_of_date

    avg_7d = get_rolling_balance_average(csp_code, as_of_date, window_days=7)
    prev_avg_7d = get_rolling_balance_average(
        csp_code, as_of_date - dt.timedelta(days=7), window_days=7
    )
    avg_30d = get_rolling_balance_average(csp_code, as_of_date, window_days=30)

    # Day-over-day trend for every consecutive pair of *known* readings —
    # gaps in reporting don't break the streak into a false DECLINE, they
    # just aren't counted either way.
    day_trends: list[tuple[dt.date, str]] = []
    for i in range(1, len(dates_with_data)):
        d, prev_d = dates_with_data[i], dates_with_data[i - 1]
        day_trends.append((d, classify_trend(by_date[d], by_date[prev_d])))

    consecutive_growth = 0
    consecutive_decline = 0
    for _, trend in reversed(day_trends):
        if trend == Trend.GROWTH:
            if consecutive_decline:
                break
            consecutive_growth += 1
        elif trend == Trend.DECLINE:
            if consecutive_growth:
                break
            consecutive_decline += 1
        else:
            break

    last_growth_date = next((d for d, t in reversed(day_trends) if t == Trend.GROWTH), None)
    last_decline_date = next((d for d, t in reversed(day_trends) if t == Trend.DECLINE), None)

    # Reversal = what was happening *immediately before the current streak
    # began*, not just yesterday-vs-today — a streak that started a few
    # days ago (not literally today) is still the reversal in effect as of
    # as_of_date. Walk back past the whole current streak to find it.
    reversal = TrendReversal.NONE
    if consecutive_growth > 0:
        idx_before_streak = len(day_trends) - consecutive_growth - 1
        if idx_before_streak >= 0:
            prior = day_trends[idx_before_streak][1]
            if prior == Trend.DECLINE:
                reversal = TrendReversal.DECLINE_TO_GROWTH
            elif prior == Trend.NO_CHANGE:
                reversal = TrendReversal.STABLE_TO_GROWTH
    elif consecutive_decline > 0:
        idx_before_streak = len(day_trends) - consecutive_decline - 1
        if idx_before_streak >= 0:
            prior = day_trends[idx_before_streak][1]
            if prior == Trend.GROWTH:
                reversal = TrendReversal.GROWTH_TO_DECLINE
            elif prior == Trend.NO_CHANGE:
                reversal = TrendReversal.STABLE_TO_DECLINE

    return TrendIntelligence(
        csp_code=csp_code,
        as_of_date=as_of_date,
        current_value=current_value,
        previous_value=previous_value,
        avg_7d=avg_7d,
        prev_avg_7d=prev_avg_7d,
        avg_30d=avg_30d,
        consecutive_growth_days=consecutive_growth,
        consecutive_decline_days=consecutive_decline,
        last_growth_date=last_growth_date,
        last_decline_date=last_decline_date,
        trend_reversal=reversal,
    )


# ---------------------------------------------------------------------------
# Filter / sort — the CSP Daily Comparison dashboard tab and the
# /comparisons API endpoint both call this rather than each
# re-implementing filter/sort criteria independently.
# ---------------------------------------------------------------------------

_SORT_EXTRACTORS = {
    "pct_change": lambda c: c.balance.pct_change,
    "abs_change": lambda c: c.balance.abs_change,
    "csp_code": lambda c: c.csp_code,
    "csp_name": lambda c: c.csp_name,
    "mtd_avg_so_far": lambda c: c.mtd_avg_so_far,
}
DEFAULT_SORT_BY = "pct_change"


def filter_and_sort_comparisons(
    comparisons: list[CspComparison],
    *,
    trend: str | None = None,
    movement: str | None = None,
    slab: str | None = None,
    search: str | None = None,
    sort_by: str = DEFAULT_SORT_BY,
    sort_dir: str = "desc",
) -> list[CspComparison]:
    """Filters by trend/movement/current slab/csp code-or-name substring,
    then sorts. Rows whose sort field is None (NO_DATA) always sort last
    regardless of sort_dir — a missing value is never displayed as if it
    were the highest or lowest real one."""
    result = list(comparisons)
    if trend:
        result = [c for c in result if c.balance.trend == trend]
    if movement:
        result = [c for c in result if c.balance.movement == movement]
    if slab:
        result = [c for c in result if c.current_slab == slab]
    if search:
        needle = search.strip().lower()
        result = [
            c for c in result if needle in c.csp_code.lower() or needle in c.csp_name.lower()
        ]

    extractor = _SORT_EXTRACTORS.get(sort_by, _SORT_EXTRACTORS[DEFAULT_SORT_BY])
    with_value = [c for c in result if extractor(c) is not None]
    without_value = [c for c in result if extractor(c) is None]
    with_value.sort(key=extractor, reverse=(sort_dir == "desc"))
    return with_value + without_value


@dataclass
class SlabSummary:
    slab: str
    count: int
    avg_balance: decimal.Decimal | None
    avg_pct_change: decimal.Decimal | None
    growing_count: int
    declining_count: int


def summarize_by_slab(comparisons: list[CspComparison]) -> list[SlabSummary]:
    """Per-slab rollup of an already-computed comparison set (average
    balance, average day-over-day change, growth/decline counts) — used by
    the Overview page's slab cards. Groups already-computed CspComparison
    values; never recomputes a balance, a slab, or a trend itself, the
    same "aggregate what the domain layer already produced" pattern
    top_movers()/at_risk_csps() above already use."""
    order = [c.value for c in MonthlySummary.Slab]
    by_slab: dict[str, list[CspComparison]] = {s: [] for s in order}
    for c in comparisons:
        if c.current_slab in by_slab:
            by_slab[c.current_slab].append(c)

    summaries = []
    for slab in order:
        rows = by_slab[slab]
        balances = [c.balance.current_value for c in rows if c.balance.current_value is not None]
        pct_changes = [c.balance.pct_change for c in rows if c.balance.pct_change is not None]
        summaries.append(
            SlabSummary(
                slab=slab,
                count=len(rows),
                avg_balance=_average(balances),
                avg_pct_change=_average(pct_changes),
                growing_count=sum(1 for c in rows if c.balance.trend == Trend.GROWTH),
                declining_count=sum(1 for c in rows if c.balance.trend == Trend.DECLINE),
            )
        )
    return summaries


# ---------------------------------------------------------------------------
# Top movers / at-risk — explainable flags, not an opaque score (Phase 8)
# ---------------------------------------------------------------------------


@dataclass
class AtRiskFlag:
    csp_code: str
    csp_name: str
    reasons: list[str] = field(default_factory=list)


def top_movers(
    comparisons: list[CspComparison], *, by: str = "pct", direction: str = "growth", limit: int = 10
) -> list[CspComparison]:
    """`by`: "pct" or "abs". `direction`: "growth" or "decline". Operates on
    an already-computed bulk_compare_csps() result — never re-queries, so
    the same comparison set can drive multiple "top movers" views for free."""
    key = (lambda c: c.balance.pct_change) if by == "pct" else (lambda c: c.balance.abs_change)
    candidates = [c for c in comparisons if key(c) is not None]
    reverse = direction == "growth"
    candidates.sort(key=lambda c: key(c), reverse=reverse)
    if direction == "growth":
        candidates = [c for c in candidates if key(c) > 0]
    else:
        candidates = [c for c in candidates if key(c) < 0]
    return candidates[:limit]


def at_risk_csps(
    comparisons: list[CspComparison], *, near_threshold: decimal.Decimal | None = None
) -> list[AtRiskFlag]:
    """Explainable at-risk flagging (Phase 8) — every flag names the exact
    reason, no opaque score. `near_threshold` (rupees) controls the "close
    to slipping below the eligibility minimum" sensitivity — checked against
    mtd_avg_so_far - rules.MIN_BALANCE, not gap_to_min (which is only
    nonzero once a CSP has ALREADY fallen below the minimum, so it can never
    catch a CSP that's merely close to it)."""
    near_threshold = near_threshold if near_threshold is not None else decimal.Decimal("200")
    flags: list[AtRiskFlag] = []
    for c in comparisons:
        reasons = []
        if c.balance.movement == MovementBucket.SHARP_DECLINE:
            reasons.append("Sharp decline vs. comparison date")
        if c.slab_movement.downgraded:
            reasons.append(f"Slab downgrade: {c.previous_slab} -> {c.current_slab}")
        if c.mtd_avg_so_far is not None:
            headroom = c.mtd_avg_so_far - rules.MIN_BALANCE
            if 0 <= headroom <= near_threshold:
                reasons.append(f"Only Rs.{headroom} above the eligibility minimum")
        if reasons:
            flags.append(AtRiskFlag(csp_code=c.csp_code, csp_name=c.csp_name, reasons=reasons))
    return flags


def get_csp_slab_history(csp_code: str, *, days: int = 60) -> list[DailyCspSnapshot]:
    """One CSP's daily Rule 19 slab classification over time, ascending by
    date — the one canonical read of `DailyCspSnapshot` history, used by
    the CSP profile's slab-history chart. Empty (not fabricated) for any
    date with no snapshot on file, same "NO_DATA over a fabricated
    number" rule as everywhere else in this module."""
    since = timezone.localdate() - dt.timedelta(days=days)
    return list(
        DailyCspSnapshot.objects.filter(csp_id=csp_code, business_date__gte=since).order_by(
            "business_date"
        )
    )


# ---------------------------------------------------------------------------
# Daily snapshot builder — called by manage.py sync_daily_snapshots
# ---------------------------------------------------------------------------


def _daily_snapshot_fields(
    mtd_avg: decimal.Decimal,
    account_count: int | None,
    latest_balance_date: dt.date | None,
    business_date: dt.date,
) -> dict:
    """The one place MTD-so-far average -> Rule 19 slab/rate/gap/eligibility
    classification happens for a daily snapshot (same csp.rules functions
    MonthlySummary uses) — shared by the single-CSP and bulk builders below
    so there is exactly one implementation of this math, not two."""
    slab = rules.slab_for(mtd_avg, account_count=account_count)
    return {
        "slab": slab,
        "incentive_rate_pa": rules.incentive_rate_for(slab),
        "gap_to_min": rules.gap_to_min(mtd_avg),
        "gap_to_next_slab": rules.gap_to_next_slab(mtd_avg, account_count=account_count),
        "is_eligible": rules.is_eligible(account_count),
        "data_status": classify_freshness(latest_balance_date, business_date),
    }


def build_daily_snapshot(csp_code: str, business_date: dt.date) -> DailyCspSnapshot | None:
    """Computes one CSP's DailyCspSnapshot for one date: the MTD-so-far
    average through business_date, classified via the exact same
    csp.rules functions MonthlySummary uses. Returns None (no snapshot
    written) if there's no balance reading at all this month through this
    date — NO_DATA, not a fabricated zero-balance slab.

    Single-CSP convenience (e.g. an on-demand rebuild for one CSP) — for
    all CSPs on one business day, use build_daily_snapshots_bulk() instead,
    which does it in a handful of queries rather than one round-trip per
    CSP."""
    month_start = business_date.replace(day=1)
    values = list(
        DailyBalance.objects.filter(
            csp_id=csp_code, balance_date__gte=month_start, balance_date__lte=business_date
        ).values_list("daily_avg_balance", flat=True)
    )
    mtd_avg = _average(values)
    if mtd_avg is None:
        return None

    csp = Csp.objects.filter(pk=csp_code).first()
    fields = _daily_snapshot_fields(
        mtd_avg,
        csp.account_count if csp else None,
        _latest_balance_date(csp_code),
        business_date,
    )

    snapshot, _ = DailyCspSnapshot.objects.update_or_create(
        csp_id=csp_code,
        business_date=business_date,
        defaults={
            "mtd_avg_so_far": mtd_avg,
            "days_with_data_mtd": len(values),
            **fields,
        },
    )
    return snapshot


_SNAPSHOT_UPDATE_FIELDS = [
    "mtd_avg_so_far",
    "days_with_data_mtd",
    "slab",
    "incentive_rate_pa",
    "gap_to_min",
    "gap_to_next_slab",
    "is_eligible",
    "data_status",
]


def build_daily_snapshots_bulk(
    business_date: dt.date, *, csp_codes: list[str] | None = None
) -> list[DailyCspSnapshot]:
    """Bulk equivalent of build_daily_snapshot() — builds every CSP's
    DailyCspSnapshot for one business day from a handful of batched queries
    (all CSPs' MTD balance history in one query, all CSPs' latest-balance
    dates in one query) instead of one round-trip per CSP, avoiding N+1
    across 539+ CSPs. Used by manage.py sync_daily_snapshots. Same
    MTD-so-far/slab math as build_daily_snapshot() via
    _daily_snapshot_fields() — not a second implementation."""
    month_start = business_date.replace(day=1)

    csps = Csp.objects.tracked()
    if csp_codes:
        csps = csps.filter(csp_code__in=csp_codes)
    csp_rows = list(csps.values_list("csp_code", "account_count"))
    if not csp_rows:
        return []
    all_codes = [code for code, _ in csp_rows]
    account_count_by_csp = dict(csp_rows)

    values_by_csp: dict[str, list[decimal.Decimal]] = {}
    for csp_id, value in DailyBalance.objects.filter(
        csp_id__in=all_codes, balance_date__gte=month_start, balance_date__lte=business_date
    ).values_list("csp_id", "daily_avg_balance"):
        values_by_csp.setdefault(csp_id, []).append(value)

    latest_date_by_csp = dict(
        DailyBalance.objects.filter(csp_id__in=all_codes)
        .values("csp_id")
        .annotate(latest=Max("balance_date"))
        .values_list("csp_id", "latest")
    )

    snapshots = []
    for csp_code in all_codes:
        values = values_by_csp.get(csp_code)
        if not values:
            continue  # NO_DATA — no row written, same contract as build_daily_snapshot()

        mtd_avg = _average(values)
        if mtd_avg is None:
            continue  # unreachable in practice (values is non-empty) — keeps mypy honest

        fields = _daily_snapshot_fields(
            mtd_avg,
            account_count_by_csp.get(csp_code),
            latest_date_by_csp.get(csp_code),
            business_date,
        )
        snapshots.append(
            DailyCspSnapshot(
                csp_id=csp_code,
                business_date=business_date,
                mtd_avg_so_far=mtd_avg,
                days_with_data_mtd=len(values),
                **fields,
            )
        )

    DailyCspSnapshot.objects.bulk_create(
        snapshots,
        update_conflicts=True,
        unique_fields=["csp", "business_date"],
        update_fields=_SNAPSHOT_UPDATE_FIELDS,
        batch_size=1000,
    )
    return snapshots
