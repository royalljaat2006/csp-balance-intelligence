# Assistant feedback trail and quality measures

What the AI assistant (the `autopilot` app) records about itself, and how its quality is measured
from what people do with its output. Scope: the narrative assistant and its draft messages. It does
not touch the balance, estimate or incentive calculations.

## What is recorded

| Record | Written by | Holds |
|---|---|---|
| `AgentInteraction` | `nemotron_client.chat_json`, on **every** model call | purpose, model, system prompt, the exact context JSON sent, raw reply, token counts, finish reason, latency, outcome (`ok`, `unavailable`, `bad_json`). Written once, never edited |
| `DraftMessage` (new fields) | `draft_csp_nudges`, `edit_and_approve_draft` | `original_text` (what the model wrote, never changes), `text` (what was approved), `context` (the figures for that CSP), `interaction` (the call), `edit_distance` (Levenshtein), `edited_at` |

A failed or unreadable reply is recorded too, so reliability is measured on every attempt. Recording
is best effort: if the insert fails, the call still returns and a warning is logged.

## What people can do

On **Draft messages** (`/dashboard/messaging/`): approve as written, reject, or *Edit, then approve*
(change the wording in place). *Why this message?* shows the figures the assistant was given for that
CSP, saved when it wrote the message.

## Measures (`autopilot/quality.py`, page `/dashboard/agent-quality/`, API `GET /api/v1/reports/agent-quality?days=30`)

| Measure | Definition | Target |
|---|---|---|
| Kept as written | drafts approved with no change / drafts decided (approved + rejected) | 75% or more |
| Correction delta | mean of edit distance / longer text, over kept drafts (0 = unchanged, 1 = rewritten) | towards 0 |
| Model calls that worked | `ok` calls / all calls | 95% or more |
| Investigations finished | completed agent tasks / finished tasks (running excluded) | 95% or more |
| Tokens per answered call | mean prompt + completion tokens, by ISO week | falling |

A measure with no rows is `no_data` (null), never 0. Fewer than 10 rows is flagged as a small sample.
Drafts written before this trail existed have no original text and are left out.

## Golden cases

`tests/golden/autopilot_nudge_cases.json` lists model replies the draft writer must keep or drop (an
invented CSP code, empty text, wrong shape, and so on). `tests/integration/test_agent_feedback.py`
runs every case on each test run. Add a case whenever a new failure is seen. It holds 16 cases today.

## Not built

- **Retrieval precision and recall**: the assistant is given pre-computed figures, not documents it
  searches, so there is no retrieval to score.
- **Confidence highlighting**: the model is not asked for token probabilities.
- **Copy and rewrite tracking, context on/off toggles, token-budget bar, prompt A/B playground**: the
  assistant has no chat box or free-text surface to attach them to.
- **LangSmith / Phoenix**: sending prompts to a third party is a separate decision; the trail above
  keeps the same fields so an export is possible later.
- **Fine-tuning pipeline**: no export of high-edit examples yet.
- **Pruning**: interaction rows are kept indefinitely. They contain CSP names and figures that were
  sent to the model provider, so agree a retention period before they grow large.

## Rollout

Migration `autopilot` 0004 adds one table and five `DraftMessage` columns. `original_text` and
`context` have database defaults, so the previous release can keep inserting drafts between `migrate`
and the restart. Rolling back the code leaves the extra columns and table unused.
