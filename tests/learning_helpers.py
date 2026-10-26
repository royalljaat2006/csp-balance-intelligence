"""Builders for the learning-loop tests. Everything is SYNTHETIC: invented CSPs, balances and
amounts. They show that the matching, scoring and gating behave as designed; none of it is evidence
of accuracy against real SBI figures."""

import datetime as dt
import decimal

from bankflow import flows
from bankflow.models import FlowAggregate
from csp.models import Csp, DailyBalance
from learning import registry
from learning.models import LivePrediction
from learning.outcomes import _Context, score

D = decimal.Decimal
d = dt.date
OCT = d(2026, 10, 1)


def day(n: int) -> dt.date:
    return OCT + dt.timedelta(days=n - 1)


def make_csp(code="1A850001", n=300):
    return Csp.objects.create(csp_code=code, name=f"CSP {code}", account_count=n)


def bal(code, when, value, n=300):
    DailyBalance.objects.create(csp_id=code, balance_date=when, daily_avg_balance=D(str(value)),
                                account_count=n, source="calling_sheet")


def flow(code, when, deposit=0, withdrawal=0):
    cats = {}
    if deposit:
        cats["deposit"] = {"count": 1, "credit": "0", "debit": str(deposit)}
    if withdrawal:
        cats["withdrawal"] = {"count": 1, "credit": str(withdrawal), "debit": "0"}
    FlowAggregate.objects.create(
        csp_id=code, flow_date=when, resolution_mode=flows.MODE_EXACT, txn_count=len(cats),
        credit_amount=withdrawal, debit_amount=deposit, recon_status="RECONCILED",
        by_category=cats)


def balances(code, first, last, value, n=300):
    for k in range(first, last + 1):
        bal(code, day(k), value, n)


def stored_prediction(code="1A850001", *, estimate=None, first_seen=None,
                      baseline="1000", predicted="1000", state="complete", count=300, stale=False,
                      count_stale=False, model=None):
    """A LivePrediction written directly (for scoring and accuracy tests)."""
    estimate = estimate or d(2026, 1, 5)
    first_seen = first_seen or d(2026, 1, 1)
    model = model or registry.active_model() or registry.ensure_registry()[1]
    inputs = {"stale": stale, "count": {"stale": count_stale}}
    return LivePrediction.objects.create(
        csp_id=code, model=model, estimate_date=estimate, period_start=first_seen,
        period_end=estimate, baseline_value=D(baseline), baseline_first_seen=first_seen,
        baseline_account_count=count, account_count=count, predicted_value=D(predicted),
        carry_forward_value=D(baseline), confidence_status="INSUFFICIENT_EVIDENCE",
        statement_state=state, inputs=inputs)


def stored_outcome(prediction, actual, event=None, count=300):
    """Score `prediction` against `actual` seen on `event`, with every statement day complete."""
    event = event or prediction.estimate_date + dt.timedelta(days=1)
    ctx = _Context(prediction.baseline_first_seen, event)
    ctx.complete = [prediction.baseline_first_seen + dt.timedelta(days=i)
                    for i in range(1, (event - prediction.baseline_first_seen).days + 1)]
    o = score(prediction, D(str(actual)), event, count, ctx)
    o.save()
    return o


def synth(months=8, csps=4, model_error=5, carry_gap=50):
    """`csps` CSPs x `months` months, each a separate SBI refresh day. The estimate is
    `model_error` off (alternating sign); carry-forward is `carry_gap` off."""
    made = []
    for c in range(csps):
        code = f"1A85{c + 1:04d}"
        make_csp(code)
        for m in range(months):
            first = d(2026, m + 1, 1)
            est = d(2026, m + 1, 5)
            event = d(2026, m + 1, 10 + c)
            sign = 1 if (m + c) % 2 == 0 else -1
            actual = 1000 + carry_gap
            p = stored_prediction(code, estimate=est, first_seen=first, baseline="1000",
                                  predicted=str(actual + sign * model_error))
            made.append(stored_outcome(p, actual, event=event))
    return made
