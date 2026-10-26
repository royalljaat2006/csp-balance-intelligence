"""
How well the assistant is doing, from what people actually did with its output.

One small function per measure, all reading the rows the app already writes
(AgentInteraction, DraftMessage, AgentTask). `agent_quality` puts them together for the
dashboard page and the API, so the two can never disagree. Nothing here calls the model,
and nothing is estimated: a measure with no rows behind it is reported as "no data", not 0.

Measures that need machinery this app does not have (retrieval precision and recall need a
retrieval step; the assistant is handed pre-computed figures) are listed as not measured
rather than invented.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from django.db.models import Count
from django.utils import timezone

from .feedback import edit_distance
from .models import AgentInteraction, AgentTask, DraftMessage

TARGETS = {
    "implicit_acceptance_rate": 0.75,
    "task_completion_rate": 0.95,
}
SMALL_SAMPLE = 10  # below this many rows a rate is shown but flagged as a small sample

_KEPT = (DraftMessage.Status.APPROVED, DraftMessage.Status.SENT)
_DECIDED = (*_KEPT, DraftMessage.Status.REJECTED)


def _status(value: float | None, n: int, target: float | None, *, higher_is_better=True) -> str:
    if value is None or n == 0:
        return "no_data"
    if target is None:
        return "info"
    ok = value >= target if higher_is_better else value <= target
    return ("met" if ok else "below") + ("_small_sample" if n < SMALL_SAMPLE else "")


def draft_feedback(since: dt.datetime) -> dict:
    """What people did with drafted messages decided since `since`: kept as written, edited
    then kept, or rejected. Drafts from before the feedback trail existed are left out."""
    rows = list(
        DraftMessage.objects.filter(status__in=_DECIDED, reviewed_at__gte=since)
        .exclude(original_text="")
        .values_list("status", "original_text", "text", "edit_distance")
    )
    unchanged = edited = rejected = 0
    ratios: list[float] = []
    chars: list[int] = []
    for status, original, final, stored_distance in rows:
        if status == DraftMessage.Status.REJECTED:
            rejected += 1
            continue
        distance = (stored_distance if stored_distance is not None
                    else edit_distance(original, final))
        longest = max(len(original), len(final))
        ratio = distance / longest if longest else 0.0
        ratios.append(ratio)
        chars.append(distance)
        if distance == 0:
            unchanged += 1
        else:
            edited += 1
    decided = len(rows)
    rate = unchanged / decided if decided else None
    return {
        "decided": decided, "kept_as_written": unchanged, "edited_then_kept": edited,
        "rejected": rejected,
        "implicit_acceptance_rate": rate,
        "correction_delta": sum(ratios) / len(ratios) if ratios else None,
        "mean_characters_changed": sum(chars) / len(chars) if chars else None,
    }


def call_reliability(since: dt.datetime) -> dict:
    """Share of model calls that came back with usable JSON."""
    counts = dict(
        AgentInteraction.objects.filter(created_at__gte=since)
        .values_list("outcome").annotate(n=Count("id"))
    )
    ok = counts.get(AgentInteraction.Outcome.OK, 0)
    total = sum(counts.values())
    return {
        "calls": total, "answered": ok,
        "unavailable": counts.get(AgentInteraction.Outcome.UNAVAILABLE, 0),
        "bad_json": counts.get(AgentInteraction.Outcome.BAD_JSON, 0),
        "rate": ok / total if total else None,
    }


def investigation_completion(since: dt.datetime) -> dict:
    """Share of finished agent investigations that reached a result (not 'needs data' or failed)."""
    counts = dict(
        AgentTask.objects.filter(created_at__gte=since)
        .exclude(status=AgentTask.Status.RUNNING)
        .values_list("status").annotate(n=Count("id"))
    )
    done = counts.get(AgentTask.Status.COMPLETED, 0)
    finished = sum(counts.values())
    return {
        "finished": finished, "completed": done,
        "needs_data": counts.get(AgentTask.Status.NEEDS_DATA, 0),
        "failed": counts.get(AgentTask.Status.FAILED, 0),
        "rate": done / finished if finished else None,
    }


def _trend(by_week: list[dict]) -> str | None:
    if len(by_week) < 2:
        return None
    first, last = by_week[0]["mean_tokens"], by_week[-1]["mean_tokens"]
    return "down" if last < first else "up" if last > first else "flat"


def context_efficiency(since: dt.datetime) -> dict:
    """Average tokens spent per answered call, overall and by week (a falling line is good)."""
    rows = AgentInteraction.objects.filter(
        created_at__gte=since, outcome=AgentInteraction.Outcome.OK,
        prompt_tokens__isnull=False, completion_tokens__isnull=False,
    ).values_list("created_at", "prompt_tokens", "completion_tokens")
    weeks: dict[str, list[int]] = defaultdict(list)
    all_tokens: list[int] = []
    for created, prompt, completion in rows:
        if prompt is None or completion is None:
            continue
        total = prompt + completion
        all_tokens.append(total)
        year, week, _ = created.isocalendar()
        weeks[f"{year}-W{week:02d}"].append(total)
    by_week = [{"week": w, "calls": len(v), "mean_tokens": sum(v) / len(v)}
               for w, v in sorted(weeks.items())]
    return {
        "calls": len(all_tokens),
        "mean_tokens_per_call": sum(all_tokens) / len(all_tokens) if all_tokens else None,
        "by_week": by_week,
        "trend": _trend(by_week),
    }


def agent_quality(days: int = 30, *, now: dt.datetime | None = None) -> dict:
    """Every measure for the last `days` days, each with its target and a plain status."""
    days = max(1, min(int(days), 365))
    since = (now or timezone.now()) - dt.timedelta(days=days)
    drafts = draft_feedback(since)
    calls = call_reliability(since)
    runs = investigation_completion(since)
    tokens = context_efficiency(since)
    t_acc, t_tcr = TARGETS["implicit_acceptance_rate"], TARGETS["task_completion_rate"]
    return {
        "days": days, "since": since.isoformat(),
        "implicit_acceptance": {
            **drafts, "target": t_acc,
            "status": _status(drafts["implicit_acceptance_rate"], drafts["decided"], t_acc),
        },
        "correction_delta": {
            "value": drafts["correction_delta"], "n": drafts["decided"] - drafts["rejected"],
            "target": 0.0, "status": _status(drafts["correction_delta"],
                                             drafts["decided"] - drafts["rejected"], None),
            "note": "Share of each kept message that a person changed (0 = kept as written, "
                    "1 = fully rewritten). Lower is better.",
        },
        "call_reliability": {**calls, "target": t_tcr,
                             "status": _status(calls["rate"], calls["calls"], t_tcr)},
        "investigation_completion": {**runs, "target": t_tcr,
                                     "status": _status(runs["rate"], runs["finished"], t_tcr)},
        "context_efficiency": {**tokens, "status": _status(
            tokens["mean_tokens_per_call"], tokens["calls"], None)},
        "not_measured": [
            {"name": "Retrieval precision and recall",
             "why": "The assistant is given pre-computed figures, not documents it searches, "
                    "so there is no retrieval step to score."},
            {"name": "Confidence highlighting",
             "why": "The model is not asked for token probabilities, so no per-word confidence "
                    "exists to show."},
        ],
    }
