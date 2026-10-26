"""
The assistant's activity, made visible: every model call with what it was shown and what it
answered, and what people then did with the drafts that came out of it.

Read-only views over AgentInteraction and DraftMessage, shared by the dashboard page and the API.
`call_log` carries the full text for staff; `call_metadata` is the same list without any prompt,
context or reply text, for API consumers.
"""

from __future__ import annotations

import datetime as dt
import json

from django.db.models import Count, Prefetch
from django.utils import timezone

from .feedback import word_diff
from .models import AgentInteraction, DraftMessage

# Long prompts and replies are shortened for the page only; the stored row is never changed.
SHOW_CHARS = 8000


def _since(days: int, now: dt.datetime | None = None) -> dt.datetime:
    return (now or timezone.now()) - dt.timedelta(days=max(1, min(int(days), 365)))


def _pretty(text: str) -> tuple[str, bool]:
    """JSON re-indented for reading when it parses, otherwise as stored; shortened to SHOW_CHARS."""
    try:
        text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    return (text[:SHOW_CHARS], len(text) > SHOW_CHARS)


def _filtered(purpose: str, outcome: str, days: int):
    qs = AgentInteraction.objects.filter(created_at__gte=_since(days))
    if purpose:
        qs = qs.filter(purpose=purpose)
    if outcome:
        qs = qs.filter(outcome=outcome)
    return qs


def _total_tokens(row: AgentInteraction) -> int | None:
    if row.prompt_tokens is None or row.completion_tokens is None:
        return None
    return row.prompt_tokens + row.completion_tokens


def call_log(*, purpose: str = "", outcome: str = "", days: int = 30, limit: int = 25,
             offset: int = 0) -> dict:
    """One page of model calls, newest first, each with its text and the drafts it produced."""
    qs = _filtered(purpose, outcome, days)
    total = qs.count()
    rows = qs.prefetch_related(
        Prefetch("drafts", queryset=DraftMessage.objects.order_by("id"))
    )[offset:offset + limit]
    out = []
    for r in rows:
        context, context_cut = _pretty(r.context)
        reply, reply_cut = _pretty(r.raw_output)
        out.append({
            "id": r.id, "created_at": r.created_at, "purpose": r.purpose or "(unlabelled)",
            "model": r.model, "outcome": r.outcome, "outcome_label": r.get_outcome_display(),
            "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
            "total_tokens": _total_tokens(r), "finish_reason": r.finish_reason,
            "latency_ms": r.latency_ms, "error": r.error,
            "system_prompt": r.system_prompt, "context": context, "context_cut": context_cut,
            "reply": reply, "reply_cut": reply_cut,
            "drafts": [{"csp_id": d.csp_id, "status": d.get_status_display(), "text": d.text,
                        "edit_distance": d.edit_distance} for d in r.drafts.all()],
        })
    return {"rows": out, "total": total}


def purposes(days: int = 365) -> list[str]:
    return sorted(set(AgentInteraction.objects.filter(created_at__gte=_since(days))
                      .values_list("purpose", flat=True)))


def call_metadata(*, purpose: str = "", outcome: str = "", days: int = 30, limit: int = 50,
                  offset: int = 0) -> dict:
    """The same calls without any prompt, context or reply text."""
    qs = _filtered(purpose, outcome, days)
    rows = qs[offset:offset + limit]
    return {"total": qs.count(), "rows": [{
        "id": r.id, "created_at": r.created_at.isoformat(), "purpose": r.purpose,
        "model": r.model, "outcome": r.outcome, "prompt_tokens": r.prompt_tokens,
        "completion_tokens": r.completion_tokens, "total_tokens": _total_tokens(r),
        "finish_reason": r.finish_reason, "latency_ms": r.latency_ms,
    } for r in rows]}


def summary(days: int = 30) -> dict:
    """Headline counts for the activity page."""
    qs = AgentInteraction.objects.filter(created_at__gte=_since(days))
    by_outcome = dict(qs.values_list("outcome").annotate(n=Count("id")))
    calls = sum(by_outcome.values())
    answered = by_outcome.get(AgentInteraction.Outcome.OK, 0)
    tokens = [t for t in (_total_tokens(r) for r in qs.filter(outcome="ok")) if t is not None]
    drafts = DraftMessage.objects.filter(interaction__in=qs)
    return {
        "days": days, "calls": calls, "answered": answered,
        "unavailable": by_outcome.get(AgentInteraction.Outcome.UNAVAILABLE, 0),
        "bad_json": by_outcome.get(AgentInteraction.Outcome.BAD_JSON, 0),
        "mean_tokens": sum(tokens) / len(tokens) if tokens else None,
        "drafts_written": drafts.count(),
        "drafts_edited": drafts.filter(edit_distance__gt=0).count(),
        "last_call": qs.order_by("-created_at").values_list("created_at", flat=True).first(),
    }


def recent_edits(days: int = 30, limit: int = 15) -> list[dict]:
    """Drafts a person reworded before approving, newest first, with the word-level change."""
    rows = (DraftMessage.objects.filter(edit_distance__gt=0, edited_at__gte=_since(days))
            .exclude(original_text="").select_related("csp", "reviewed_by")
            .order_by("-edited_at")[:limit])
    return [{
        "csp": r.csp.name or r.csp_id, "csp_code": r.csp_id, "edited_at": r.edited_at,
        "by": r.reviewed_by.get_username() if r.reviewed_by else "",
        "distance": r.edit_distance, "diff": word_diff(r.original_text, r.text),
    } for r in rows]
