"""
Async dispatch for the CSP Operations Agent (scalability foundation,
2026-09-21) — the "Agent Queue -> Specialized Agent Workers" leg of the
target architecture.

`run_csp_operations_agent()` itself (autopilot/agent.py) is completely
unchanged — every specialist, every verification rule, every tool
permission boundary is identical to before this file existed. All this
module changes is *where* that function runs when triggered from a web
request: enqueued to Redis and executed by the `agent-worker` process
(see compose.yml), instead of inline on the gunicorn thread handling the
dashboard POST. This is exactly the "AI/Nemotron calls must never block
normal dashboard/API requests" requirement — the agent's own Nemotron
calls (via specialists) were, before this change, running synchronously
inside a dashboard request.

Degrades to running synchronously (today's exact prior behaviour) when no
Redis is configured — same "missing integration degrades, never crashes"
convention as NEMOTRON_API_KEY/TELEGRAM_BOT_TOKEN elsewhere in this
project. This is a documented limitation, not a silent one: see the
scalability report's worker/queue design section — a deployment that
wants real async execution must set REDIS_URL *and* run the `agent-worker`
service, not just one of the two.
"""

from __future__ import annotations

import structlog
from django.conf import settings

from autopilot.agent import run_csp_operations_agent
from autopilot.models import AgentTask

logger = structlog.get_logger("autopilot")

AGENT_QUEUE_NAME = "agents"


def _get_queue():
    import redis
    from rq import Queue

    conn = redis.from_url(settings.REDIS_URL)
    return Queue(AGENT_QUEUE_NAME, connection=conn)


def run_agent_task_job(task_description: str) -> int:
    """The actual job body executed by an `agent-worker` process. Returns
    the created AgentTask's pk (not the object itself — RQ pickles the
    return value, and a pk is a stable, tiny result to store/inspect via
    `rq.job.Job.result`)."""
    task = run_csp_operations_agent(task=task_description)
    return task.pk


def enqueue_agent_run(task_description: str) -> AgentTask | None:
    """
    Triggers a CSP Operations Agent run from a web request context.

    Returns the AgentTask if it ran synchronously (no queue configured —
    the caller gets the same immediate result as before this module
    existed), or None if it was handed to the queue (the caller should
    point the user at AI Operations / Agent Monitoring to watch it land,
    same page these have always used to show run history).
    """
    if not settings.REDIS_URL:
        logger.warning("agent_queue_unconfigured", action="running_synchronously")
        return run_csp_operations_agent(task=task_description)

    try:
        queue = _get_queue()
        queue.enqueue(run_agent_task_job, task_description, job_timeout=600)
        return None
    except Exception as exc:  # noqa: BLE001 — queue unavailable must degrade, never 500 the request
        logger.error(
            "agent_enqueue_failed",
            error_type=type(exc).__name__,
            error=str(exc),
            action="falling_back_to_synchronous",
        )
        return run_csp_operations_agent(task=task_description)
