"""
Safe KO-code -> CSP-code resolution.

Rules, deliberately conservative:

  CANONICAL  A KO code is "1A85" followed by four digits: 8 characters, never more
             (owner-confirmed 2026-10-13). Anything after the 8th character is not part of the
             code and is dropped (1A8502957 -> 1A850295, 1A850536N -> 1A850536, 1A85135510 ->
             1A851355). Independent check on the September Transaction sheets: 11,269 of 11,269
             matched rows agree with the trimmed code.
  EXACT      the canonical 8-character code is a known csp_code. -> assigned (method "exact"
             when the code was already 8 characters, "first_8_chars" when it was trimmed).
  UNMATCHED  a well-formed canonical code that is not in the master list (e.g. a deleted CSP).
  VARIANT    only for codes that do NOT follow the 1A85 format: ko_raw = exactly one known code
             + trailing chars -> NOT assigned unless BANKFLOW_CONFIRM_TRAILING_VARIANTS (with a
             confirmation reference) is switched on. Never triggered by 1A85 codes any more.
  AMBIGUOUS  ko_raw is a truncation of one-or-more known codes,
             or is both a truncation and an extension.          -> never assigned.
  UNMATCHED  a code is present but nothing relates to it.       -> never assigned.
  MISSING    no KO code in the description.                     -> never assigned.

Nothing here ever creates a placeholder CSP. Only well-formed codes (7-10
alphanumeric characters) take part in prefix/variant inference, so a junk
short code in the master table cannot swallow other codes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EXACT = "exact"
VARIANT = "variant_provisional"
AMBIGUOUS = "ambiguous_truncated"
UNMATCHED = "unmatched"
MISSING = "missing"

_WELL_FORMED = re.compile(r"^[0-9A-Za-z]{7,10}$")
#: "1A85" + 4 digits = the whole code; whatever follows it is dropped.
CANONICAL_CODE = re.compile(r"^(1A85\d{4})", re.IGNORECASE)
MAX_CANDIDATES = 10


@dataclass(frozen=True)
class Resolution:
    status: str
    method: str
    ko_raw: str
    resolved_code: str | None = None
    candidates: list[str] = field(default_factory=list)


class CodeResolver:
    def __init__(self, known_codes: set[str], *, confirm_trailing_variants: bool = False):
        self.known = set(known_codes)
        self.long_codes = sorted(c for c in self.known if _WELL_FORMED.match(c))
        self.confirm_variants = confirm_trailing_variants

    def resolve(self, ko_raw: str) -> Resolution:
        ko = (ko_raw or "").strip()
        if not ko:
            return Resolution(MISSING, "no_ko_code", "")
        canonical = CANONICAL_CODE.match(ko)
        if canonical:
            code = canonical.group(1).upper()
            if code in self.known:
                method = "exact" if len(ko) == len(code) else "first_8_chars"
                return Resolution(EXACT, method, ko, resolved_code=code)
            return Resolution(UNMATCHED, "canonical_code_not_in_master_list", ko, candidates=[code])
        if ko in self.known:
            return Resolution(EXACT, "exact", ko, resolved_code=ko)

        extensions = [c for c in self.long_codes if len(c) < len(ko) and ko.startswith(c)]
        truncations = [c for c in self.long_codes if len(c) > len(ko) and c.startswith(ko)]

        if len(extensions) == 1 and not truncations:
            code = extensions[0]
            return Resolution(
                VARIANT,
                "trailing_chars_confirmed"
                if self.confirm_variants
                else "trailing_chars_unconfirmed",
                ko,
                resolved_code=code if self.confirm_variants else None,
                candidates=[code],
            )
        if extensions or truncations:
            return Resolution(
                AMBIGUOUS,
                "truncated_or_multiple_candidates",
                ko,
                candidates=(extensions + truncations)[:MAX_CANDIDATES],
            )
        return Resolution(UNMATCHED, "no_candidate", ko)

    def attributable_code(self, res: Resolution, *, include_provisional: bool) -> str | None:
        """Which csp_code (if any) this resolution may be counted under."""
        if res.status == EXACT:
            return res.resolved_code
        if res.status == VARIANT and include_provisional and res.candidates:
            return res.candidates[0]
        return None
