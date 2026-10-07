"""
Rule #19 (SBI CSP balance-maintenance incentive) — PRD §5.1. This is the
canonical Python copy of the slab thresholds; dbt/macros/rule_19.sql
duplicates the same thresholds in SQL for `monthly_summary` (a deterministic
aggregate is cheaper to compute once in the warehouse than to pull every
daily_balance row back into Python). Keep the two in sync — see the test
in tests/unit/test_rules.py, which is the tripwire if they ever drift:
whenever this file's thresholds change, that test's SQL-mirroring cases
must be updated too, and vice versa.

Nothing in this module is a new business rule — it's PRD §5.1's table,
literally transcribed.
"""

from __future__ import annotations

import decimal

Decimal = decimal.Decimal

MIN_BALANCE = Decimal("2501")

# (upper_bound_inclusive, slab_code, incentive_rate_pa, annual_cap) — PRD §5.1.
# upper_bound of None means "and above".
_BANDS: list[tuple[Decimal | None, str, Decimal, Decimal | None]] = [
    (Decimal("2500"), "NIL", Decimal("0"), None),
    (Decimal("4000"), "S1", Decimal("1.10"), Decimal("25000")),
    (Decimal("6000"), "S2", Decimal("1.20"), Decimal("35000")),
    (Decimal("10000"), "S3", Decimal("1.25"), Decimal("40000")),
    (None, "S4", Decimal("1.30"), Decimal("50000")),
]

ELIGIBILITY_MIN_ACCOUNTS = 200


def slab_for(mab: decimal.Decimal, *, account_count: int | None) -> str:
    """PRD §5.1: "Eligibility gate: minimum 200 BSBD accounts" — a CSP under
    that threshold is NIL regardless of balance, not merely flagged
    separately via is_eligible. account_count=None is treated the same as
    "known and under 200" (not eligible), matching is_eligible's own
    existing convention for an unknown count — never optimistically
    assumed eligible just because the Calling Sheet hasn't reported a
    count yet."""
    if not is_eligible(account_count):
        return "NIL"
    for upper, slab, _rate, _cap in _BANDS:
        if upper is None or mab <= upper:
            return slab
    raise AssertionError("unreachable — _BANDS always has a None upper bound")


def incentive_rate_for(slab: str) -> decimal.Decimal:
    for _upper, code, rate, _cap in _BANDS:
        if code == slab:
            return rate
    raise ValueError(f"Unknown slab: {slab!r}")


def annual_cap_for(slab: str) -> decimal.Decimal | None:
    for _upper, code, _rate, cap in _BANDS:
        if code == slab:
            return cap
    raise ValueError(f"Unknown slab: {slab!r}")


def gap_to_min(mab: decimal.Decimal) -> decimal.Decimal:
    return max(MIN_BALANCE - mab, Decimal("0"))


def gap_to_next_slab(mab: decimal.Decimal, *, account_count: int | None) -> decimal.Decimal | None:
    """None if already in the top slab (nothing higher to reach). An
    ineligible CSP (see slab_for) is shown the same gap a genuine NIL CSP
    would see — balance needed to clear the ₹2,501 floor, floored at 0 —
    rather than a gap computed from whatever balance band their real MAB
    happens to fall in, which would contradict the NIL slab they're
    actually shown."""
    if not is_eligible(account_count):
        return max(MIN_BALANCE - mab, Decimal("0"))
    for upper, _slab, _rate, _cap in _BANDS:
        if upper is not None and mab <= upper:
            return upper + 1 - mab
    return None


def is_eligible(account_count: int | None) -> bool:
    return account_count is not None and account_count >= ELIGIBILITY_MIN_ACCOUNTS
