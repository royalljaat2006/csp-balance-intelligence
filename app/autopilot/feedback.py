"""
How far a person's final text is from what the model wrote.

`edit_distance` is the Levenshtein distance (single-character insertions,
deletions and substitutions). `edit_ratio` scales it to 0..1 by the longer of the
two texts, so a 5-character fix to a 280-character SMS and to a 40-character SMS
are comparable. 0 means the person kept the model's text exactly.
"""

from __future__ import annotations

# Drafts are capped at 2,000 characters when stored; the same cap keeps the
# quadratic distance calculation bounded for anything typed into the edit box.
MAX_TEXT_CHARS = 2000


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance between two strings (two-row dynamic programme)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a  # keep the inner row as short as possible
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def edit_ratio(original: str, final: str) -> float:
    """Edit distance as a share of the longer text: 0.0 unchanged, 1.0 fully rewritten."""
    longest = max(len(original), len(final))
    return 0.0 if longest == 0 else edit_distance(original, final) / longest
