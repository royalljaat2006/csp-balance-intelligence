"""manage.py run_csp_operations_agent — one on-demand or scheduled run of
the CSP Operations Agent (see autopilot/agent.py). Wired into
run_autopilot.py's existing daily step sequence; also callable directly for
manual runs/tests, and from AI Operations' "Run Agent Now" button via
django.core.management.call_command.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from autopilot.agent import run_csp_operations_agent


class Command(BaseCommand):
    help = "Run the CSP Operations Agent once (observe -> reason -> plan -> act -> verify)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--task", type=str, default="Daily network check", help="Task description for this run."
        )

    def handle(self, *args, **options):
        agent_task = run_csp_operations_agent(task=options["task"])
        self.stdout.write(
            f"AgentTask #{agent_task.pk}: {agent_task.status} — {agent_task.final_status}"
        )
