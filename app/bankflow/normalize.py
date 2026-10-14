"""
Description normalisation and transaction-semantics classification.

SEMANTICS -- CONFIRMED by the business seniors on 2026-10-14 (see docs/BANK-FLOW.md):

The statement is of Eko's POOL account. A statement CREDIT raises the pool balance and a DEBIT
lowers it (proved by the running balance on every row).

  * Withdrawal channels (AEPSWDL, RupayWDL, Withdrawal)  -> the customer took cash out at the CSP,
    pool CREDITED.  Customer-balance effect: negative.
  * Deposit channels (AEPSDeposit, RupayDeposit, Deposit, and INITIAL DEPOSIT) -> the customer
    gave cash to the CSP, pool DEBITED.   Customer-balance effect: positive.
  * Money transfer       -> not a change to the customers' balances. EXCLUDED.
  * Off-us withdrawal    -> customer of ANOTHER bank; no effect on SBI customer
    balances. EXCLUDED.
  * Loan / SSA deposits  -> other products. EXCLUDED. (Initial deposit is NOT one of these: the
    seniors confirmed it counts as a deposit.)
  * CDM cash deposit / EKO deductions -> Eko's own pool entries, no CSP. EXCLUDED.
  * Labels not in the map below (outimps, ppf, yono fi wdl, ...) are NOT customer withdrawals or
    deposits (seniors, 2026-10-14) and stay unclassified.

For every withdrawal- or deposit-category row the customer-balance effect is
`debit - credit` (a pool credit on a withdrawal is -x; a pool debit on a deposit
is +x; a debit on a withdrawal channel, i.e. a reversal, is +x; a credit on a
deposit channel, i.e. a reversal, is -x).

Every definition stays configurable and is evaluated offline (flows.FLOW_DEFINITIONS); the
default, `customer_wd_dep`, is the confirmed one above.

Privacy: the trailing `-TRANSFER FROM/TO <customer account> <customer name>`
segment is dropped from `description_redacted`; the full original is retained
only as a SHA-256 hash.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# channel (lower-cased, whitespace-collapsed) -> (normalized label, category)
CHANNEL_MAP: dict[str, tuple[str, str]] = {
    "aepswdl": ("aeps_withdrawal", "withdrawal"),
    "withdrawal": ("withdrawal", "withdrawal"),
    "rupaywdl": ("rupay_withdrawal", "withdrawal"),
    "yono withdrawal": ("yono_withdrawal", "withdrawal"),
    "aepsdeposit": ("aeps_deposit", "deposit"),
    "deposit": ("deposit", "deposit"),
    "rupaydeposit": ("rupay_deposit", "deposit"),
    "aepsoffuswdl": ("aeps_offus_withdrawal", "offus"),
    "rupayoffuswdl": ("rupay_offus_withdrawal", "offus"),
    "moneytrf": ("money_transfer", "transfer"),
    "loan deposit": ("loan_deposit", "other_product"),
    "initial deposit": ("initial_deposit", "deposit"),
    "ssa": ("ssa_deposit", "other_product"),
}

_REF_RE = re.compile(r"(?:BY|TO) TRANSFER-(\d{12,})")
_CHANNEL_RE = re.compile(r"TRANSFER-\d{12,}\s*(.*?)\s*(?:TXN)?@KO", re.IGNORECASE)
_KO_RE = re.compile(r"@KO\s*([0-9A-Za-z]+)")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Normalized:
    description_hash: str
    description_redacted: str
    ref_number: str
    channel_raw: str
    channel_normalized: str
    category: str
    side: str
    ko_raw: str
    is_reversal_candidate: bool
    reversal_reason: str


def description_hash(description: str, ref_column: str) -> str:
    """SHA-256 of the whitespace-collapsed original text. Collapsing makes the same
    ledger row hash identically across exports whose column padding differs
    (e.g. a statement re-saved from a spreadsheet), while any real text change
    still changes the hash."""
    h = hashlib.sha256()
    h.update(_WS_RE.sub(" ", description).strip().encode("utf-8"))
    h.update(b"")
    h.update(_WS_RE.sub(" ", ref_column).strip().encode("utf-8"))
    return h.hexdigest()


_LONG_DIGITS_RE = re.compile(r"\d{9,}")
_SAFE_CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z ]{0,39}$")


def redact(description: str, *, ref: str, channel_raw: str, ko: str, category: str) -> str:
    """Allow-list redaction -- the stored text is BUILT from fields we parsed, never
    copied from the free-text part of the bank description, so an unexpected
    description layout cannot leak a customer's name or account number.

      * rows with a reference/KO code -> `<BY|TO> TRANSFER ref=.. channel=.. ko=..`
      * pool funding (CDM / EKO deductions) -> first 60 chars, long digit runs masked
      * anything else                    -> a fixed placeholder (hash still verifies it)
    """
    if ref or ko:
        side = (
            "BY" if description.startswith("BY") else "TO" if description.startswith("TO") else "?"
        )
        return f"{side} TRANSFER ref={ref or '-'} channel={channel_raw or '-'} ko={ko or '-'}"
    if category == "pool_operations":
        text = _WS_RE.sub(" ", description).strip()[:60]
        return _LONG_DIGITS_RE.sub("#", text)
    return "[redacted: unrecognised description layout]"


def classify(description: str, *, debit, credit) -> tuple[str, str, str, str, str]:
    """-> (ref_number, channel_raw, channel_normalized, category, ko_raw)"""
    ref = ""
    m = _REF_RE.search(description)
    if m:
        ref = m.group(1)
    channel_raw = ""
    cm = _CHANNEL_RE.search(description)
    if cm:
        candidate = _WS_RE.sub(" ", cm.group(1)).strip()
        # Only a short, letters-and-spaces label is accepted as a channel; anything
        # else is dropped rather than stored (it could contain free text).
        channel_raw = candidate if _SAFE_CHANNEL_RE.match(candidate) else ""
    km = _KO_RE.search(description)
    ko = km.group(1) if km else ""

    key = channel_raw.lower()
    if key in CHANNEL_MAP:
        normalized, category = CHANNEL_MAP[key]
    elif description.startswith(("CSH DEP (CDM)", "CASH DEPOSIT")):
        normalized, category = "cash_deposit_funding", "pool_operations"
    elif "INB EKO DEDUC" in description:
        normalized, category = "eko_deduction", "pool_operations"
    elif channel_raw:
        normalized, category = f"unknown:{key}", "unclassified"
    else:
        normalized, category = "unclassified", "unclassified"
    return ref, channel_raw, normalized, category, ko


def side_of(debit, credit) -> str:
    if credit and not debit:
        return "credit"
    if debit and not credit:
        return "debit"
    return "zero"


def normalize_row(description: str, ref_column: str, *, debit, credit) -> Normalized:
    ref, channel_raw, normalized, category, ko = classify(description, debit=debit, credit=credit)
    side = side_of(debit, credit)
    reversal, reason = False, ""
    # A withdrawal-channel row that DEBITS the pool, or a deposit-channel row
    # that CREDITS it, runs against the channel's normal direction.
    if category == "withdrawal" and side == "debit":
        reversal, reason = True, "withdrawal_channel_debit"
    elif category == "deposit" and side == "credit":
        reversal, reason = True, "deposit_channel_credit"
    return Normalized(
        description_hash=description_hash(description, ref_column),
        description_redacted=redact(
            description, ref=ref, channel_raw=channel_raw, ko=ko, category=category
        ),
        ref_number=ref,
        channel_raw=channel_raw,
        channel_normalized=normalized,
        category=category,
        side=side,
        ko_raw=ko,
        is_reversal_candidate=reversal,
        reversal_reason=reason,
    )
