"""
How far a person's final text is from what the model wrote.

`edit_distance` is the Levenshtein distance (single-character insertions,
deletions and substitutions). `edit_ratio` scales it to 0..1 by the longer of the
two texts, so a 5-character fix to a 280-character SMS and to a 40-character SMS
are comparable. 0 means the person kept the model's text exactly.
"""

from __future__ import annotations

import difflib

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


def word_diff(original: str, final: str) -> list[tuple[str, str]]:
    """What a person changed, word by word, as (kind, text) pieces in reading order, where kind is
    "same", "removed" (in the original, not in the final) or "added" (in the final only)."""
    a, b = original.split(), final.split()
    pieces: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            pieces.append(("same", " ".join(a[i1:i2])))
            continue
        if i2 > i1:
            pieces.append(("removed", " ".join(a[i1:i2])))
        if j2 > j1:
            pieces.append(("added", " ".join(b[j1:j2])))
    return pieces
