# The learning loop for the shadow Avg Balance estimate

What it is, what is built, what it will not do, and what is still waiting for real history.
Code: `app/learning/`. Page: **Agent learning centre** (`/dashboard/learning/`, staff only).
API: `/api/v1/learning/...`. Nothing here changes how an estimate is computed or what Rule #19, the
200-account gate or `DailyBalance` do.

## The idea in one paragraph

Every day's estimate is saved, once, before SBI reports its next Avg Balance for that CSP. When SBI
does, the saved estimate is scored against the figure SBI reported. Scores are summarised, split
into "the model was off" and "the data was bad", compared with simply carrying SBI's last figure
forward, and with other ways of estimating. People can say an estimate looks wrong; that is recorded
as an opinion and never as a value. A different method can become the active one only through an
audited, human-approved step. The estimate is never treated as the truth.

## Records (all in `app/learning/models.py`)

| Record | Written by | Rule |
|---|---|---|
| `LivePrediction` | `freeze_predictions` | Insert-only. Holds the CSP, creation time, the observation period, the SBI baseline and when it was first seen, the Calling Sheet count used and the PMJDY snapshot (null if none), the eligible deposits, withdrawals and net movement, flow coverage, the estimate, the carry-forward value, the method and its definition, confidence status and reason |
| `PredictionOutcome` | `record_outcomes` | Insert-only, one per prediction. The SBI value that followed, when it was first seen, horizon, signed error (estimate minus SBI), direction, the carry-forward error, and a data-quality label |
| `PredictionFeedback` + `FeedbackEvent` | people / API | Opinion only: no numeric field. Status OPEN, IN_REVIEW, RESOLVED, DISMISSED; every change is an insert-only event with who, when and a note |
| `CandidateEvaluation` | `evaluate_candidates` | Insert-only. One per method per run: verdict, the pre-declared criteria and whether each was met, limits |
| `ModelEvent` | registry | Insert-only. Registered, promoted, rolled back: who, why, on which evaluation |
| `bankflow.ModelVersion` (extended) | registry | The method registry: stage (BASELINE, CANDIDATE, ACTIVE, RETIRED), `live_supported`, `blocked_reason`, `training_cutoff` |

"Insert-only" is enforced twice: the model refuses to update or delete, and migration
`learning 0002` adds a PostgreSQL trigger so a queryset `update()`, a `delete()` or hand-written SQL
fails too. A later change to SBI's figure is just the next refresh for the next cycle; it never
rewrites an earlier outcome.

## How a prediction is matched to its outcome (`outcomes.py`)

* The CSP's Calling Sheet series is collapsed to refreshes: the first day each successive distinct
  Avg Balance was seen. A prediction belongs to the cycle that began with its baseline value and is
  scored against **the next refresh of the same CSP**. CSPs are matched independently, so SBI
  updating different CSPs on different days needs no special case.
* A prediction is scored only if its estimate day is **strictly before** the day the new value was
  first seen. The baseline must be found in that CSP's own history with the same first-seen day and
  value, otherwise the prediction stays unmatched (counted, never guessed).
* Dates are the day the system **first saw** a value. The sheet has no measurement-date column, so
  SBI's own effective date is unknown and is not invented. The download time is not used as one.
* Only FINAL estimates (the day's bank statement proven complete) are frozen by default.

## Data problems are kept apart from model error

Each outcome gets a label. Only `OK` outcomes measure the method; the rest are counted and shown
separately: `STATEMENT_GAP` (under 80% of the interval's days have complete statements),
`UNMATCHED_CODES` (more than 10% of the interval's pool movement carried a code that could not be
matched to a CSP; the 10% is an operational limit and the measured share is stored), `PROVISIONAL_DATA`,
`COUNT_UNRELIABLE` (blank or stale account count), `STALE_SOURCE`. CSP/KO matching stays exact-only.

## Accuracy (`accuracy.py`)

MAE, RMSE, mean signed error (positive = estimate too high), over/under counts, and a percentage only
where SBI's figure is at least ₹100. Broken down by days ahead, account-count group, CSP and method.
One outcome per CSP per refresh enters the headline (the prediction made closest to it).

**It refuses to claim accuracy** until the pre-declared bar the validation gate already uses is met:
at least 8 separate SBI refresh days, 6 monthly cycles and 30 checks, a win in two thirds of events,
a gain of at least 5% over carry-forward, and a confidence interval above zero. Before that the
verdict is `INSUFFICIENT_EVIDENCE`, `metrics` is null, and no figure is shown, not even a
descriptive one. A slice with too few checks says what it is missing instead of a number.

## Candidate methods (`candidates.py`) and promotion (`registry.py`)

Compared in date order on SBI refreshes, each predicting the next value from the previous reading,
the account counts and the statement movement only:

| Method | Rule | Notes |
|---|---|---|
| carry-forward | last SBI value unchanged | the reference |
| account count only | value x new count / old count | **blocked**: an account-count change never scales the combined balance. Scored for evidence only |
| flow only | value + movement / old count | cannot run live yet |
| movement (account growth + flow) | (value x old count + movement) / new count | the active shadow method |

No method has fitted parameters, so nothing is trained and every usable refresh on or after the
hold-out date is a held-out test. A method is `RECOMMENDED` only when every criterion is met and,
if another method is active, it also beats that one in two thirds of events with a confidence
interval above zero. Too little evidence is `INSUFFICIENT_EVIDENCE`.

Changing the active method (`manage.py promote_model`) needs **all** of: an active superuser, a
written reason, a registered candidate that the live estimator can really run, no business-rule
block, and a `RECOMMENDED` evaluation less than 7 days old. Nothing promotes automatically and one
wrong prediction never reaches this path. `promote_model --rollback` restores the method that was
active before the last promotion and records what its own evaluation said. Today only the movement
rule can run in the live estimator and it is already active, so **no promotion is possible yet**; a
new method needs its own reviewed inference code first.

## Commands (none is scheduled)

```bash
manage.py freeze_predictions [--date D] [--since-days 14] [--include-provisional]
manage.py record_outcomes
manage.py evaluate_candidates [--holdout-from D] [--dry-run]
manage.py promote_model <name> --actor <superuser> --reason "..."   |   --rollback
```
Run them after estimates are final and after SBI refreshes. Wiring them into the existing hourly
bank-flow job is a one-line follow-up that needs approval; it has not been done.

## API

Reads need scope `report:read`; submitting feedback needs the new `feedback:write` scope, so a
read-only key can never write. `create_api_key` offers the new scope automatically.

## Implemented, synthetic-tested, and waiting for real history

* **Implemented and tested on synthetic data:** everything above, with 100+ tests covering matching,
  asynchronous per-CSP refreshes, no leakage, insufficient evidence, the error arithmetic, feedback
  versus observed values, candidate evaluation, promotion criteria, rollback, permissions and audit.
* **Waiting for real history:** no real prediction has been frozen or scored. Accuracy is
  `INSUFFICIENT_EVIDENCE` and will stay so until real SBI refreshes accumulate: 8 refresh days over
  6 months at the very least, with complete statements around each. Bank-statement gaps already seen
  (2-3, 5-16 and 18-21 Sep, 3-4 Oct) lower the usable share.
* **Not built:** automatic promotion (by design), scheduling, an exported training set for any
  fine-tuning, and methods beyond the four above (no data yet justifies more).
