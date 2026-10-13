"""
manage.py evaluate_bank_flows -- run the event-grouped, chronological evaluation
of flow-based prediction against Calling Sheet Avg Balance history and persist
one EvaluationRun per target. Analysis only; changes no balance data.

Repeat whenever new statements or Calling Sheet observations arrive. Several
horizons / subsets can be requested; horizon 0 is labelled association, not
prediction.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from bankflow import flows
from bankflow.evaluation import EvalConfig, evaluate


class Command(BaseCommand):
    help = (
        "Evaluate bank-flow features against Calling Sheet Avg Balance (held-out, event-grouped)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--horizons", default="1", help="Comma-separated days, e.g. 0,1,3.")
        parser.add_argument(
            "--definition", default=flows.DEFAULT_DEFINITION, choices=sorted(flows.FLOW_DEFINITIONS)
        )
        parser.add_argument("--mode", default=flows.MODE_EXACT, choices=flows.MODES)
        parser.add_argument("--subset", default="all", choices=["all", "acct_unchanged"])
        parser.add_argument("--min-events", type=int, default=8)
        parser.add_argument(
            "--min-change-share",
            type=float,
            default=0.30,
            help="Share of CSPs that must change for a date to count as a refresh event.",
        )
        parser.add_argument(
            "--event-dates",
            default="",
            help="Comma-separated YYYY-MM-DD refresh dates (overrides detection).",
        )

    def handle(self, *args, **opts):
        forced = [d for d in opts["event_dates"].split(",") if d] or None
        horizons = [int(x) for x in opts["horizons"].split(",") if x != ""]
        for h in horizons:
            cfg = EvalConfig(
                batch_configs=len(horizons),
                horizon_days=h,
                flow_definition=opts["definition"],
                resolution_mode=opts["mode"],
                subset=opts["subset"],
                min_events=opts["min_events"],
                min_change_share=opts["min_change_share"],
                forced_event_dates=forced,
            )
            for run in evaluate(cfg):
                self.stdout.write(
                    f"horizon={h} target={run.target} status={run.status} "
                    f"events={run.n_events} test_events={run.n_test_events} :: {run.outcome_detail}"
                )
