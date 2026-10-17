"""
The movement-based SHADOW estimator -- pure calculation, no database access.

Agreed rule (product owner, 2026-10-17). All figures are for ONE CSP and ONE predictor cycle:

    combined baseline  = Calling Sheet Avg Balance (a per-account average) x valid account count
    net movement (day) = eligible customer DEPOSITS - eligible customer WITHDRAWALS
                       = pool-account debits - pool-account credits of the eligible labels
    combined balance   = previous combined balance + net movement of the day
    per-account value  = combined balance / the valid account count of that day

A cycle starts on the day a new Avg Balance is first seen (its receipt date). That day uses the new
baseline ALONE; the statement movement of the receipt day is not applied again. Movement applies
from the following day. Earlier cycles are never touched.

An account-count change NEVER scales the combined balance: it only changes the divisor from the
day it is received. Blank, zero and non-numeric counts are invalid: the last valid count stays in
use and the row is flagged.

A day without eligible movement carries the previous combined balance forward. A day whose
statement is missing or incomplete does too (or applies the part of the day that exists), and the
row is marked provisional: a missing statement is never read as "no activity". A day is FINAL only
when its statement data is proven complete and every earlier day of the cycle was final.

Eligible labels are the statement allow-list in normalize.CHANNEL_MAP, exposed here through
flows.FLOW_DEFINITIONS["customer_wd_dep"]; this module keeps no label list of its own.
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from bankflow import flows

ZERO = Decimal("0")
CENT = Decimal("0.01")
METHOD = "movement"
STALE_COUNT_DAYS = 3  # a count older than this (days) is flagged, never silently trusted

# reason codes (DailyEstimate.reason_code is 48 characters at most)
R_BASELINE_DAY = "BASELINE_DAY"
R_APPLIED = "MOVEMENT_APPLIED"
R_NO_MOVEMENT = "NO_ELIGIBLE_MOVEMENT_CARRY_FORWARD"
R_GAP = "STATEMENT_GAP_CARRY_FORWARD"
R_PARTIAL = "PARTIAL_STATEMENT_MOVEMENT"
R_TODAY = "TODAY_PARTIAL_MOVEMENT"

# provisional reasons, in plain words (shown on the explanation page)
P_GAP = "No bank statement covers this day, so the balance is carried forward."
P_PARTIAL = "The bank statement for this day is not proven complete, so its movement is partial."
P_TODAY = "Today is still in progress: its movement is partial until the statement is complete."
P_CHAIN = "An earlier day of this cycle is still provisional."

_CATEGORIES = flows.FLOW_DEFINITIONS[flows.DEFAULT_DEFINITION][1] or ()
WITHDRAWAL, DEPOSIT = "withdrawal", "deposit"


def q(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(CENT)


@dataclass(frozen=True)
class DayMovement:
    """One day of one CSP's statement movement, split into what counts and what does not."""

    deposits: Decimal = ZERO      # pool debits - credits on the deposit labels
    withdrawals: Decimal = ZERO   # pool credits - debits on the withdrawal labels
    counted_txns: int = 0
    excluded_txns: int = 0        # recognised labels that are not customer deposits/withdrawals
    unclassified_txns: int = 0    # labels the allow-list does not know
    duplicate_refs: int = 0
    reversal_amount: Decimal = ZERO

    @property
    def net(self) -> Decimal:
        return self.deposits - self.withdrawals


def split(by_category: Mapping | None, *, duplicate_refs: int = 0,
          reversal_amount: Decimal = ZERO) -> DayMovement:
    """FlowAggregate.by_category -> DayMovement. A reversal (debit on a withdrawal label, credit on
    a deposit label) reduces the figure it reverses, exactly as flows.signed_flow does."""
    deposits = withdrawals = ZERO
    counted = excluded = unclassified = 0
    for cat, v in (by_category or {}).items():
        credit, debit, n = Decimal(v["credit"]), Decimal(v["debit"]), int(v["count"])
        if cat == DEPOSIT and cat in _CATEGORIES:
            deposits += debit - credit
            counted += n
        elif cat == WITHDRAWAL and cat in _CATEGORIES:
            withdrawals += credit - debit
            counted += n
        elif cat == "unclassified":
            unclassified += n
        else:
            excluded += n
    return DayMovement(deposits, withdrawals, counted, excluded, unclassified,
                       duplicate_refs, reversal_amount)


def predict_per_account(
    prev_average: Decimal, prev_count: int, net_movement: Decimal, new_count: int
) -> Decimal:
    """The estimate for a later date from an earlier SBI reading alone: (average x count +
    movement) / new count. Used to score the rule against a held-out SBI refresh; it takes the
    previous value, the two counts and the movement -- never the value it is scored against."""
    return (prev_average * prev_count + net_movement) / new_count


class CountTimeline:
    """A CSP's Calling Sheet account counts over time. `valid_on(d)` is the latest VALID count on
    or before d; blank, zero and negative readings are never valid."""

    def __init__(self, readings):
        by_day: dict[dt.date, int | None] = {}
        for d, n in sorted(readings, key=lambda r: r[0]):
            by_day[d] = n  # the last reading of a day wins
        self._all = sorted(by_day.items())
        self._valid = [(d, n) for d, n in self._all if isinstance(n, int) and n > 0]
        self._valid_days = [d for d, _ in self._valid]
        self._all_days = [d for d, _ in self._all]

    def valid_on(self, day: dt.date) -> tuple[int | None, dt.date | None]:
        i = bisect.bisect_right(self._valid_days, day)
        return (self._valid[i - 1][1], self._valid[i - 1][0]) if i else (None, None)

    def invalid_after_valid(self, day: dt.date) -> bool:
        """Has the sheet since reported a blank/zero count that we are ignoring?"""
        _, vday = self.valid_on(day)
        i = bisect.bisect_right(self._all_days, day)
        return bool(i) and (vday is None or self._all_days[i - 1] > vday) and (
            self._all[i - 1][1] is None or self._all[i - 1][1] <= 0
        )

    def state_on(self, day: dt.date) -> dict:
        count, seen = self.valid_on(day)
        age = (day - seen).days if seen else None
        invalid = self.invalid_after_valid(day)
        return {
            "count": count, "count_read_on": seen, "age_days": age,
            "stale": count is None or invalid or (age or 0) > STALE_COUNT_DAYS,
            "invalid_reading_ignored": invalid,
        }


@dataclass
class Day:
    """One computed day of a cycle."""

    date: dt.date
    combined: Decimal
    count: int | None
    count_state: dict
    movement: DayMovement
    applied: bool
    final: bool
    reason: str
    provisional: list[str] = field(default_factory=list)

    @property
    def per_account(self) -> Decimal | None:
        return q(self.combined / self.count) if self.count else None


def baseline_day(start: dt.date, average: Decimal, count: int, counts: CountTimeline) -> Day:
    """The receipt day: the new baseline alone, no statement movement."""
    return Day(start, average * count, count, counts.state_on(start), DayMovement(), False,
               True, R_BASELINE_DAY)


def roll(
    *, previous: Day, end: dt.date, movements: Mapping[dt.date, DayMovement],
    complete_days: set[dt.date], statement_days: set[dt.date], counts: CountTimeline,
    today: dt.date,
) -> list[Day]:
    """Days previous.date+1 .. end. `previous` is the baseline day or the last FINAL stored day."""
    out: list[Day] = []
    combined, chain_final, day = previous.combined, previous.final, previous.date
    while day < end:
        day += dt.timedelta(days=1)
        mv = movements.get(day)
        complete = day in complete_days
        provisional: list[str] = []
        if complete:
            reason = R_APPLIED if mv and mv.counted_txns else R_NO_MOVEMENT
        elif day in statement_days or mv is not None:
            reason = R_TODAY if day >= today else R_PARTIAL
            provisional.append(P_TODAY if day >= today else P_PARTIAL)
        else:
            reason = R_TODAY if day >= today else R_GAP
            provisional.append(P_TODAY if day >= today else P_GAP)
        if not chain_final and complete:
            provisional.append(P_CHAIN)
        applied = bool(mv and mv.counted_txns)
        movement = mv or DayMovement()
        combined = combined + movement.net
        final = complete and chain_final
        chain_final = final
        st = counts.state_on(day)
        out.append(Day(day, combined, st["count"], st, movement, applied, final, reason,
                       provisional))
    return out
