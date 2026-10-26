"""
Thin wrapper around NVIDIA's OpenAI-compatible Nemotron endpoint. Every
autopilot feature calls through chat_json() below, not its own HTTP/SDK
code — the API key, base URL, model name, and error handling live in
exactly one place, and every caller gets the same "unavailable this run,
never crash the caller" behaviour.
"""

from __future__ import annotations

import contextvars
import functools
import json
import re
import time

import structlog
from common import metrics
from django.conf import settings
from openai import OpenAI

logger = structlog.get_logger("autopilot")

_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


class NemotronUnavailable(Exception):
    """Raised when the API key isn't configured, the call fails, or the
    response can't be parsed as JSON. Every caller in this app treats this
    as "produce nothing this run," never as a reason to crash a management
    command or a page render — see each service function's try/except."""


_REQUEST_TIMEOUT_SECONDS = 30.0

# Bounds on what one telemetry row keeps, so a runaway prompt or reply cannot bloat the table.
_MAX_CONTEXT_CHARS = 200_000
_MAX_OUTPUT_CHARS = 50_000

# The id of the AgentInteraction written by this thread's most recent chat_json call, so the
# caller can link what it produces (e.g. a DraftMessage) back to what the model was shown.
_last_interaction_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "autopilot_last_interaction_id", default=None
)


def last_interaction_id() -> int | None:
    """The AgentInteraction recorded by the latest chat_json call in this context, if any."""
    return _last_interaction_id.get()


def forget_last_interaction() -> None:
    """Call just before chat_json when the result will be linked to its interaction, so an id
    left by an earlier call can never be attached to something this call did not produce."""
    _last_interaction_id.set(None)


def _record_interaction(
    *, purpose: str, system_prompt: str, user_prompt: str, outcome: str, started: float,
    raw_output: str = "", error: str = "", response=None,
) -> None:
    """Write one immutable AgentInteraction row. Telemetry must never break the call it
    describes, so any failure here is logged and swallowed."""
    try:
        from .models import AgentInteraction

        usage = getattr(response, "usage", None)
        choice = response.choices[0] if response is not None and response.choices else None
        row = AgentInteraction.objects.create(
            purpose=purpose[:60], model=str(settings.NEMOTRON_MODEL)[:100], outcome=outcome,
            system_prompt=system_prompt, context=user_prompt[:_MAX_CONTEXT_CHARS],
            raw_output=raw_output[:_MAX_OUTPUT_CHARS], error=error[:300],
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            finish_reason=str(getattr(choice, "finish_reason", "") or "")[:30],
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        _last_interaction_id.set(row.pk)
    except Exception as exc:  # noqa: BLE001 — telemetry is best effort
        logger.warning("interaction_not_recorded", error_type=type(exc).__name__, error=str(exc))


def _client() -> OpenAI:
    if not settings.NEMOTRON_API_KEY:
        raise NemotronUnavailable("NEMOTRON_API_KEY is not configured.")
    # Explicit timeout (scalability foundation, 2026-09-21) — the SDK's own
    # default is much longer, which would otherwise let one slow/hung
    # Nemotron call tie up a request-handling or worker thread indefinitely.
    # See the "external API timeout" failure-handling requirement.
    return OpenAI(
        base_url=_NVIDIA_BASE_URL,
        api_key=settings.NEMOTRON_API_KEY,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )


def chat_json(
    system_prompt: str, user_prompt: str, *, max_tokens: int = 3072, purpose: str = ""
) -> dict:
    """
    Sends a chat completion and parses the response as JSON.

    Passes chat_template_kwargs={"thinking": False} — Nemotron Super/Ultra
    are reasoning models that otherwise spend a large, visible
    chain-of-thought budget *before* the final answer (confirmed against
    the real API: with thinking on, a 50-candidate prompt hit
    finish_reason="length" at 2048 tokens and produced malformed/truncated
    JSON even at 8192; with it off, the same prompt finishes in ~2000
    output tokens with finish_reason="stop" and valid JSON). Every
    autopilot task here wants a direct structured answer, not visible
    reasoning, so this is strictly better: faster, cheaper, and reliable.

    Also deliberately does NOT pass response_format={"type": "json_object"}
    — NIM-hosted models don't uniformly support OpenAI's JSON mode, and a
    silently-unsupported parameter is worse than just asking clearly in the
    prompt and parsing defensively (strips a markdown code fence if the
    model wraps its answer in one, which instruction-tuned models often do
    even when told not to).

    Callers must still validate the *shape* of the parsed dict — this only
    guarantees valid JSON, not that it matches the schema they asked for.

    Every call, successful or not, is recorded as one AgentInteraction (prompt, injected
    context, raw reply, token usage, outcome); `purpose` labels which feature made it.
    """
    started = time.monotonic()
    _last_interaction_id.set(None)
    record = functools.partial(
        _record_interaction, purpose=purpose, system_prompt=system_prompt,
        user_prompt=user_prompt, started=started,
    )
    try:
        response = _client().chat.completions.create(
            model=settings.NEMOTRON_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.2,
            extra_body={"chat_template_kwargs": {"thinking": False}},
        )
    except NemotronUnavailable:
        metrics.record_external_api_call(
            service="nemotron", outcome="unconfigured", duration_seconds=time.monotonic() - started
        )
        record(outcome="unavailable", error="NEMOTRON_API_KEY is not set")
        raise
    except Exception as exc:  # noqa: BLE001 — any SDK/network failure is "unavailable this run"
        metrics.record_external_api_call(
            service="nemotron", outcome="error", duration_seconds=time.monotonic() - started
        )
        logger.error("nemotron_call_failed", error_type=type(exc).__name__, error=str(exc))
        record(outcome="unavailable", error=f"{type(exc).__name__}: {exc}")
        raise NemotronUnavailable(f"Nemotron call failed: {type(exc).__name__}: {exc}") from exc
    metrics.record_external_api_call(
        service="nemotron", outcome="ok", duration_seconds=time.monotonic() - started
    )

    content = (response.choices[0].message.content or "").strip()
    cleaned = _FENCE_RE.sub("", content).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("nemotron_bad_json", raw=content[:500])
        record(outcome="bad_json", raw_output=content, error=str(exc),
                            response=response)
        raise NemotronUnavailable(f"Nemotron returned non-JSON output: {exc}") from exc
    record(outcome="ok", raw_output=content, response=response)
    return parsed
