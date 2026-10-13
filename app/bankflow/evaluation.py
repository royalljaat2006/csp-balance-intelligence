"""
Leakage-safe, event-grouped evaluation of flow-based prediction of the next
Calling Sheet Avg Balance.

Design (docs/BANK-FLOW.md):

  * Unit of independence = one REFRESH EVENT (targets.detect_refresh_events),
    never one CSP. Hundreds of CSPs refreshing on one date are one observation
    of "how the sheet changed", not hundreds.
  * Chronological expanding window: to score event k, the model is fitted ONLY
    on events strictly before k. No random splits, no tuning on test events.
  * Baselines on every fold: no-change (predict the previous observation) and
    historical-mean change.
  * Model: transparent through-origin OLS  delta_avg ~ beta * (net flow per account),
    flow aggregated over (previous refresh, event - horizon].
  * horizon_days = 0 is SAME-INTERVAL ASSOCIATION (flows up to the refresh date
    itself): reported but labelled as association, not prediction.
  * Account-count changes are explicit: each row records whether the CSP's
    account_count changed across the event, and a subset run restricts to
    CSPs with an unchanged account count so the denominator effect is removed.
  * Status is decided by pre-registered criteria stored in the run config.
    Too few independent events -> INSUFFICIENT_EVIDENCE, regardless of how good
    the pooled numbers look.

Floats are used for the statistical model only; every monetary value stored in
the database and every reconciliation stays Decimal.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import statistics
from dataclasses import asdict, dataclass, field

from . import flows
from .models import EvaluationRun, ModelVersion, PredictionRecord
from .targets import (
    Observations,
    RefreshEvent,
    detect_refresh_events,
    load_observations,
    month_end_mab_availability,
)

S = EvaluationRun.Status
T = EvaluationRun.Target


@dataclass
class EvalConfig:
    horizon_days: int = 1
    flow_definition: str = flows.DEFAULT_DEFINITION
    resolution_mode: str = flows.MODE_EXACT
    subset: str = "all"  # "all" | "acct_unchanged"
    min_events: int = 8
    min_train_events: int = 3
    min_test_events: int = 3
    min_rows_per_event: int = 30
    min_flow_coverage: float = 0.5
    min_change_share: float = 0.30
    forced_event_dates: list[str] | None = None
    #: how many configurations were evaluated together; >1 means "best of N" picking is
    #: not a valid validation (multiple comparisons) -- recorded and shown with the run.
    batch_configs: int = 1
    # pre-registered pass criteria (recorded with every run)
    pass_win_share: float = 2 / 3
    pass_min_rel_improvement: float = 0.05
    bootstrap_reps: int = 2000
    seed: int = 20261012

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class EventData:
    event: RefreshEvent
    window_start: dt.date
    window_end: dt.date
    coverage: float
    rows: list[dict] = field(default_factory=list)  # csp, prev, new, d, x, acct_changed
    base_date: dt.date | None = None  # date of the "previous refresh" state used as the baseline
    acct_changed_share: float | None = None


def build_datasets(
    obs: Observations,
    events: list[RefreshEvent],
    cfg: EvalConfig,
    *,
    complete_days: set[dt.date] | None = None,
) -> list[EventData]:
    """One EventData per refresh event.

    The baseline ("no change") and the target's origin are the observation on the
    PREVIOUS refresh date (or the earliest observation for the first event) --
    a value that is known strictly before the flow window closes, so neither the
    baseline nor the target can see anything that happened inside the forecast
    horizon. Flows are summed over (previous refresh, event - horizon], using only
    statement days proven complete (flows.proven_complete_dates)."""
    if not obs:
        return []
    earliest = min(obs)
    if complete_days is None:
        complete_days = flows.proven_complete_dates()
    out: list[EventData] = []
    for i, ev in enumerate(events):
        start = events[i - 1].date if i > 0 else earliest
        end = ev.date - dt.timedelta(days=cfg.horizon_days)
        data = EventData(
            event=ev, window_start=start, window_end=end, coverage=0.0, base_date=start
        )
        if end <= start or start not in obs:
            out.append(data)
            continue
        prev, new = obs[start], obs[ev.date]
        common = [c for c in prev if c in new and prev[c][1] and prev[c][1] > 0]
        flow_map, coverage = flows.window_flows(
            common,
            start,
            end,
            definition=cfg.flow_definition,
            mode=cfg.resolution_mode,
            complete_days=complete_days,
        )
        data.coverage = coverage
        if common:
            data.acct_changed_share = sum(prev[c][1] != new[c][1] for c in common) / len(common)
        for c in common:
            acct_changed = prev[c][1] != new[c][1]
            if cfg.subset == "acct_unchanged" and acct_changed:
                continue
            flow = flow_map.get(c, flows.ZERO)
            data.rows.append(
                {
                    "csp": c,
                    "prev": float(prev[c][0]),
                    "new": float(new[c][0]),
                    "d": float(new[c][0] - prev[c][0]),
                    "x": float(flow) / prev[c][1],
                    "acct_changed": acct_changed,
                }
            )
        out.append(data)
    return out


def _usable(e: EventData, cfg: EvalConfig) -> bool:
    return e.coverage >= cfg.min_flow_coverage and len(e.rows) >= cfg.min_rows_per_event


def _fit_beta(train: list[EventData]) -> float:
    sxx = sum(r["x"] ** 2 for e in train for r in e.rows)
    sxy = sum(r["x"] * r["d"] for e in train for r in e.rows)
    return sxy / sxx if sxx > 0 else 0.0


def _mae(errs):
    return sum(abs(e) for e in errs) / len(errs)


def _rmse(errs):
    return math.sqrt(sum(e * e for e in errs) / len(errs))


@dataclass
class Fold:
    event: EventData
    n_train_events: int
    beta: float
    mean_change: float
    majority_sign: int
    n: int
    mae_nochange: float
    mae_meanchg: float
    mae_model: float
    rmse_nochange: float
    rmse_model: float
    bias_model: float
    dir_n: int
    dir_model_acc: float | None
    dir_majority_acc: float | None


def run_folds(usable: list[EventData], cfg: EvalConfig) -> list[Fold]:
    folds: list[Fold] = []
    for j in range(cfg.min_train_events, len(usable)):
        train, test = usable[:j], usable[j]  # strictly earlier events only
        beta = _fit_beta(train)
        event_means = [statistics.fmean(r["d"] for r in e.rows) for e in train]
        mean_change = statistics.fmean(event_means)
        signs = [1 if r["d"] > 0 else -1 for e in train for r in e.rows if r["d"] != 0]
        majority = 1 if sum(signs) >= 0 else -1

        nochange = [r["d"] for r in test.rows]  # predicted change 0
        meanchg = [r["d"] - mean_change for r in test.rows]
        model = [r["d"] - beta * r["x"] for r in test.rows]
        dir_rows = [r for r in test.rows if r["d"] != 0 and r["x"] != 0 and beta != 0]
        dm = da = None
        if dir_rows:
            dm = sum((beta * r["x"] > 0) == (r["d"] > 0) for r in dir_rows) / len(dir_rows)
            da = sum((majority > 0) == (r["d"] > 0) for r in dir_rows) / len(dir_rows)
        folds.append(
            Fold(
                event=test,
                n_train_events=len(train),
                beta=beta,
                mean_change=mean_change,
                majority_sign=majority,
                n=len(test.rows),
                mae_nochange=_mae(nochange),
                mae_meanchg=_mae(meanchg),
                mae_model=_mae(model),
                rmse_nochange=_rmse(nochange),
                rmse_model=_rmse(model),
                bias_model=statistics.fmean(-m for m in model),
                dir_n=len(dir_rows),
                dir_model_acc=dm,
                dir_majority_acc=da,
            )
        )
    return folds


def _bootstrap_ci(values: list[float], reps: int, seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(values)
    means = sorted(statistics.fmean(rng.choice(values) for _ in range(n)) for _ in range(reps))
    return means[int(0.025 * reps)], means[int(0.975 * reps) - 1]


def summarize(folds: list[Fold], cfg: EvalConfig) -> dict:
    if not folds:
        return {}
    imp = [f.mae_nochange - f.mae_model for f in folds]  # >0: model better than no-change
    lo, hi = _bootstrap_ci(imp, cfg.bootstrap_reps, cfg.seed)
    base = statistics.fmean(f.mae_nochange for f in folds)
    dir_pairs = [
        (f.dir_model_acc, f.dir_majority_acc)
        for f in folds
        if f.dir_model_acc is not None and f.dir_majority_acc is not None
    ]
    dir_lift = [m - b for m, b in dir_pairs]
    dir_ci = _bootstrap_ci(dir_lift, cfg.bootstrap_reps, cfg.seed + 1) if dir_lift else (None, None)
    return {
        "mae_nochange": base,
        "mae_mean_change": statistics.fmean(f.mae_meanchg for f in folds),
        "mae_model": statistics.fmean(f.mae_model for f in folds),
        "rmse_nochange": statistics.fmean(f.rmse_nochange for f in folds),
        "rmse_model": statistics.fmean(f.rmse_model for f in folds),
        "bias_model": statistics.fmean(f.bias_model for f in folds),
        "mean_mae_improvement": statistics.fmean(imp),
        "rel_mae_improvement": statistics.fmean(imp) / base if base else None,
        "improvement_ci95_over_events": [lo, hi],
        "events_model_beats_nochange": sum(i > 0 for i in imp),
        "n_test_events": len(folds),
        "win_share": sum(i > 0 for i in imp) / len(imp),
        "direction": {
            "events": len(dir_pairs),
            "model_accuracy": statistics.fmean(m for m, _ in dir_pairs) if dir_pairs else None,
            "majority_accuracy": statistics.fmean(b for _, b in dir_pairs) if dir_pairs else None,
            "lift_ci95_over_events": list(dir_ci),
            "events_model_beats_majority": sum(x > 0 for x in dir_lift),
        },
        "per_event": [
            {
                "event_date": f.event.event.date.isoformat(),
                "n_train_events": f.n_train_events,
                "n_rows": f.n,
                "beta": f.beta,
                "mae_nochange": f.mae_nochange,
                "mae_model": f.mae_model,
                "dir_model_acc": f.dir_model_acc,
                "dir_majority_acc": f.dir_majority_acc,
            }
            for f in folds
        ],
    }


def _model_version(name: str, definition: dict) -> ModelVersion:
    mv, _ = ModelVersion.objects.get_or_create(
        name=name, version="1", defaults={"definition": definition}
    )
    return mv


def evaluate(cfg: EvalConfig | None = None, *, persist: bool = True) -> list[EvaluationRun]:
    """Run the avg-change, direction and month-end-MAB evaluations and (by
    default) persist one EvaluationRun per target. Safe to repeat as new
    statements / Calling Sheet observations arrive."""
    cfg = cfg or EvalConfig()
    notes = [
        "Flow definition is a hypothesis about customer-balance effect, not an established fact.",
        "Pool-account cash movement may not correspond to the CSP's own BSBD balances.",
        f"horizon_days={cfg.horizon_days}: "
        + (
            "same-interval ASSOCIATION, not prediction."
            if cfg.horizon_days == 0
            else "flows end before the refresh date."
        ),
        "Flows use only statement days proven complete by ledger continuity; baseline = "
        "value at the previous refresh.",
    ]
    if cfg.batch_configs > 1:
        notes.append(
            f"{cfg.batch_configs} configurations were evaluated together: picking the best one "
            "is NOT a valid validation (multiple comparisons). Pre-register one configuration."
        )
    runs: list[EvaluationRun] = []
    try:
        obs = load_observations()
        forced = (
            [dt.date.fromisoformat(d) for d in cfg.forced_event_dates]
            if cfg.forced_event_dates
            else None
        )
        events = detect_refresh_events(
            obs, min_change_share=cfg.min_change_share, forced_dates=forced
        )
        if forced:
            unmatched = sorted(d.isoformat() for d in forced if d not in {e.date for e in events})
            if unmatched:
                notes.append(
                    "Forced event dates ignored (not an observation date with a prior "
                    f"observation): {', '.join(unmatched)}."
                )
        datasets = build_datasets(obs, events, cfg)
        usable = [e for e in datasets if _usable(e, cfg)]
        # Performance numbers are only computed once there are enough independent events;
        # below that they would be noise presented as a score, so none are produced.
        folds = run_folds(usable, cfg) if len(usable) >= cfg.min_events else []
        summary = summarize(folds, cfg)
        failure = None
    except Exception as exc:  # noqa: BLE001
        events, datasets, usable, folds, summary = [], [], [], [], {}
        failure = f"{type(exc).__name__}: {exc}"

    meta = {
        "refresh_events_detected": len(events),
        "events_with_usable_flow_data": len(usable),
        "event_coverage": [
            {
                "event": e.event.date.isoformat(),
                "window": f"{e.window_start}..{e.window_end}",
                "flow_coverage": round(e.coverage, 3),
                "rows": len(e.rows),
                "baseline_date": e.base_date.isoformat() if e.base_date else None,
                "account_count_changed_share": (
                    round(e.acct_changed_share, 3) if e.acct_changed_share is not None else None
                ),
                "usable": _usable(e, cfg),
            }
            for e in datasets
        ],
    }

    def decide_avg() -> tuple[str, str]:
        if failure:
            return S.FAILED, f"error: {failure}"
        if len(usable) < cfg.min_events:
            return S.INSUFFICIENT, (
                f"{len(usable)} independent events with usable flow data; need >= {cfg.min_events}"
            )
        if len(folds) < cfg.min_test_events:
            return S.INSUFFICIENT, f"{len(folds)} held-out events; need >= {cfg.min_test_events}"
        lo = summary["improvement_ci95_over_events"][0]
        ok = (
            summary["win_share"] >= cfg.pass_win_share
            and lo > 0
            and (summary["rel_mae_improvement"] or 0) >= cfg.pass_min_rel_improvement
        )
        return (
            (S.PASSED, "criteria met")
            if ok
            else (S.FAILED, "criteria_not_met: model does not reliably beat no-change")
        )

    def decide_dir() -> tuple[str, str]:
        if failure:
            return S.FAILED, f"error: {failure}"
        d = summary.get("direction") if summary else None
        if (
            len(usable) < cfg.min_events
            or len(folds) < cfg.min_test_events
            or not d
            or d["events"] < cfg.min_test_events
        ):
            return (
                S.INSUFFICIENT,
                f"{len(usable)} usable events / {len(folds)} held-out; "
                f"need >= {cfg.min_events} / {cfg.min_test_events}",
            )
        lo = d["lift_ci95_over_events"][0]
        ok = (
            lo is not None
            and lo > 0
            and d["events_model_beats_majority"] / d["events"] >= cfg.pass_win_share
        )
        return (
            (S.PASSED, "criteria met")
            if ok
            else (S.FAILED, "criteria_not_met: direction no better than majority sign")
        )

    def period(evs: list[EventData]) -> str:
        return f"{evs[0].event.date}..{evs[-1].event.date}" if evs else ""

    test_evs = [f.event for f in folds]

    if persist:
        mv_flow = _model_version(
            "flow_ols_through_origin", {"features": ["net_flow_per_account"], "cfg": cfg.as_dict()}
        )
        mv_dir = _model_version("flow_sign_direction", {"rule": "sign(beta*flow)"})
        mv_mab = _model_version("month_end_mab_unavailable", {})
    for target, mv, decide in (
        (T.AVG_CHANGE, "mv_flow", decide_avg),
        (T.DIRECTION, "mv_dir", decide_dir),
    ):
        status, detail = decide()
        run = EvaluationRun(
            target=target,
            status=status,
            outcome_detail=detail,
            config=cfg.as_dict(),
            n_events=len(usable),
            n_test_events=len(folds),
            n_observations=sum(len(e.rows) for e in usable),
            train_period=period(usable[: (folds[0].n_train_events if folds else len(usable))])
            if usable
            else "",
            test_period=period(test_evs),
            metrics={**summary, **meta},
            notes=notes,
        )
        if persist:
            run.model = mv_flow if mv == "mv_flow" else mv_dir
            run.save()
            if target == T.AVG_CHANGE and folds:
                _save_predictions(run, folds, cfg)
        runs.append(run)

    mab = month_end_mab_availability()
    mab_run = EvaluationRun(
        target=T.MONTH_END_MAB,
        status=S.INSUFFICIENT,
        outcome_detail="ground truth unavailable"
        if not mab["available"]
        else "month-end model not implemented",
        config=cfg.as_dict(),
        metrics=mab,
        notes=[
            "Month-end MAB is NOT evaluated: a sparse/stepwise sheet series cannot be turned "
            "into true daily balances or the official month-end MAB.",
            mab["needed"],
        ],
    )
    if persist:
        mab_run.model = mv_mab
        mab_run.save()
    runs.append(mab_run)
    return runs


def _save_predictions(
    run: EvaluationRun, folds: list[Fold], cfg: EvalConfig, limit: int = 5000
) -> None:
    from decimal import Decimal

    recs: list[PredictionRecord] = []
    for f in folds:
        ev = f.event.event
        for r in f.event.rows:
            if len(recs) >= limit:
                break
            recs.append(
                PredictionRecord(
                    run=run,
                    csp_id=r["csp"],
                    event_date=ev.date,
                    previous_date=ev.previous_date,
                    horizon_days=cfg.horizon_days,
                    observed_previous=Decimal(str(round(r["prev"], 2))),
                    observed_new=Decimal(str(round(r["new"], 2))),
                    predicted_baseline=Decimal(str(round(r["prev"], 2))),
                    predicted_model=Decimal(str(round(r["prev"] + f.beta * r["x"], 2))),
                    flow_per_account=Decimal(str(round(r["x"], 2))),
                    account_count_changed=r["acct_changed"],
                )
            )
    PredictionRecord.objects.bulk_create(recs, batch_size=1000)
