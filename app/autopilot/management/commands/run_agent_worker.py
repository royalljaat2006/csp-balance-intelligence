"""
manage.py run_agent_worker — the `agent-worker` service's process
(scalability foundation, 2026-09-21).

Deliberately NOT the bare `rq worker` CLI: that entrypoint never calls
django.setup(), so the moment it imports the job function
(autopilot.tasks.run_agent_task_job), which imports autopilot.agent ->
autopilot.agents -> csp.comparison -> csp.models, Django raises
AppRegistryNotReady — confirmed live (this is how the gap was actually
found: the first real queued job crash-looped the container with exactly
that traceback). A management command runs through manage.py's own
django.setup() first, exactly like every other worker role in this
project (watch_transactions, poll_telegram, ...), so this is the
consistent fix rather than a one-off workaround.
"""

from __future__ import annotations

import structlog
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

logger = structlog.get_logger("autopilot")


class Command(BaseCommand):
    help = "Run the RQ worker that consumes the CSP Operations Agent's async queue."

    def handle(self, *args, **options):
        if not settings.REDIS_URL:
            raise CommandError(
                "REDIS_URL is not configured — nothing for this worker to consume. "
                "Set REDIS_URL (see .env.example) before running run_agent_worker."
            )
        import redis
        from rq import Queue, Worker

        from autopilot.tasks import AGENT_QUEUE_NAME

        conn = redis.from_url(settings.REDIS_URL)
        logger.info("started", stage="AGENT_WORKER", queue=AGENT_QUEUE_NAME)
        worker = Worker([Queue(AGENT_QUEUE_NAME, connection=conn)], connection=conn)
        worker.work()
