"""
Month-end MAB projection — PRD FR4. Deliberately NOT a dbt model: this is a
forward-looking *estimate*, not a deterministic aggregate of rows that
already exist, so it belongs in Python next to the rest of the domain logic
(csp/rules.py), not in SQL. `monthly_summary` (dbt mart) computes the
deterministic fields (mtd_mab, slab, gap_to_min, gap_to_next_slab, trend_7d_pct,
...); `sync_monthly_summary` (management command) copies those across
unchanged and calls this module only for the fields that are genuinely a
projection: projected_mab, projected_slab, projected_incentive_annual, and
the human-readable trend_flag derived from dbt's trend_7d_pct.

Method (PRD §8 FR4, "default (a)"): assume the CSP's most recent known
daily balance holds for the rest of the month.

    projected_mab = (sum_of_known_days + last_known_balance * remaining_days)
                    / days_in_month

This is a documented simplification, not a statistical model — it is
optimistic/pessimistic exactly to the extent the CSP's balance keeps
trending the way it already was on its last known day. Revisit if a
smoothed (e.g. 7-day-average-based) estimate proves more accurate once
enough historical data exists to compare against.
"""

from __future__ import annotations

import decimal

from .rules import annual_cap_for, incentive_rate_for, slab_for

TREND_THRESHOLD_PCT = decimal.Decimal("2")  # PRD §8: >+2% improving, <-2% declining


def project_month_end_mab(
    *,
    mtd_mab: decimal.Decimal,
    days_with_data: int,
    days_in_month: int,
    last_known_balance: decimal.Decimal,
) -> decimal.Decimal | None:
    if days_with_data <= 0 or days_in_month <= 0:
        return None
    if days_with_data >= days_in_month:
        return mtd_mab  # month is already complete; MTD is final
    remaining_days = days_in_month - days_with_data
    sum_so_far = mtd_mab * days_with_data
    projected_sum = sum_so_far + last_known_balance * remaining_days
    return projected_sum / days_in_month


def trend_flag_for(trend_7d_pct: decimal.Decimal | None) -> str:
    if trend_7d_pct is None:
        return "stable"
    if trend_7d_pct > TREND_THRESHOLD_PCT:
        return "improving"
    if trend_7d_pct < -TREND_THRESHOLD_PCT:
        return "declining"
    return "stable"


def project_incentive_annual(
    *, projected_mab: decimal.Decimal, account_count: int | None
) -> decimal.Decimal | None:
    """PRD Q2/§16: incentive is paid on the CSP's total portfolio balance,
    which needs account_count (per-account average * account_count). Null
    until account_count is known for that CSP — never guessed."""
    if account_count is None:
        return None
    slab = slab_for(projected_mab, account_count=account_count)
    rate = incentive_rate_for(slab)
    if rate == 0:
        return decimal.Decimal("0")
    total_balance = projected_mab * account_count
    annual = total_balance * rate / decimal.Decimal("100")
    cap = annual_cap_for(slab)
    return min(annual, cap) if cap is not None else annual


def build_projection(
    *,
    mtd_mab: decimal.Decimal,
    days_with_data: int,
    days_in_month: int,
    last_known_balance: decimal.Decimal,
    trend_7d_pct: decimal.Decimal | None,
    account_count: int | None,
) -> dict:
    """The subset of MonthlySummary's columns that are genuinely a
    projection, not a deterministic aggregate (those come from the dbt
    mart as-is — see module docstring)."""
    projected_mab = project_month_end_mab(
        mtd_mab=mtd_mab,
        days_with_data=days_with_data,
        days_in_month=days_in_month,
        last_known_balance=last_known_balance,
    )
    return {
        "projected_mab": projected_mab,
        "projected_slab": (
            slab_for(projected_mab, account_count=account_count)
            if projected_mab is not None
            else ""
        ),
        "projected_incentive_annual": (
            project_incentive_annual(projected_mab=projected_mab, account_count=account_count)
            if projected_mab is not None
            else None
        ),
        "trend_flag": trend_flag_for(trend_7d_pct),
    }
