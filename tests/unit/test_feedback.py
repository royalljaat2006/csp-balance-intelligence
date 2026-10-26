"""Levenshtein distance and the 0..1 edit ratio behind the assistant's correction measure."""

import pytest
from autopilot.feedback import edit_distance, edit_ratio, word_diff


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("", "", 0),
        ("abc", "abc", 0),
        ("", "abc", 3),
        ("abc", "", 3),
        ("kitten", "sitting", 3),
        ("flaw", "lawn", 2),
        ("Hello", "hello", 1),
        ("pay Rs 500", "pay Rs 550", 1),
    ],
)
def test_known_distances(a, b, expected):
    assert edit_distance(a, b) == expected
    assert edit_distance(b, a) == expected  # symmetric


def test_ratio_is_zero_when_unchanged_and_one_when_fully_rewritten():
    assert edit_ratio("same text", "same text") == 0.0
    assert edit_ratio("", "") == 0.0
    assert edit_ratio("abc", "xyz") == 1.0


def test_ratio_makes_short_and_long_messages_comparable():
    short = edit_ratio("a" * 40, "a" * 39 + "b")
    long = edit_ratio("a" * 280, "a" * 279 + "b")
    assert short == pytest.approx(1 / 40) and long == pytest.approx(1 / 280)
    assert 0.0 <= short <= 1.0


def test_word_diff_marks_what_was_removed_and_added():
    assert word_diff("pay Rs 500 now", "pay Rs 400 now") == [
        ("same", "pay Rs"), ("removed", "500"), ("added", "400"), ("same", "now")]
    assert word_diff("same words", "same words") == [("same", "same words")]
    assert word_diff("", "new text") == [("added", "new text")]
    assert word_diff("old text", "") == [("removed", "old text")]
    assert word_diff("", "") == []


def test_word_diff_rebuilds_both_texts():
    original, final = "Dear CSP you are close to S2", "Hi, you are very close to the S2 slab"
    pieces = word_diff(original, final)
    assert " ".join(t for k, t in pieces if k != "added") == original
    assert " ".join(t for k, t in pieces if k != "removed") == final
