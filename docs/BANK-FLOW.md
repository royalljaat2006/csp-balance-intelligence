# Bank-Flow Validation (`app/bankflow`)

**Question being tested:** can CSP-wise transaction flows from the SBI *pool-account* statement
predict the existing Calling Sheet `Avg Balance` (and, if ground truth ever exists, month-end MAB)?

**Status: analysis only.** The Calling Sheet remains the source for CSP master data, account counts,
eligibility and every balance figure the app shows. Nothing in `bankflow` writes `DailyBalance`,
`Csp`, `MonthlySummary` or any Calling Sheet table, and it is not scheduled by the production
workers. Rule #19 and the 200-account gate are untouched.

## What the statement is (verified on the 2026-10-12 sample)

* The `.xls` is **tab-delimited text** (header block, `Txn Date…Balance` table, `**` footer), not an
  Excel workbook. `parser.detect_format()` sniffs bytes; extensions are never trusted. Genuine
  `.xlsx` with the same layout also parses. Binary OLE2 `.xls` and HTML are recognised and recorded
  as FAILED (no `xlrd` dependency).
* It is the statement of the **pool account** (ends `71556`), not of any CSP. The Balance column is
  the pool's balance. CSPs appear only through `…TXN@KO<code>` / `…@KO<code>` in the description.
* No time of day, only dates. One file covers one date.

## Pipeline

```
watch folder (read-only) ─▶ run_import ─▶ parse ─▶ normalise ─▶ reconcile ─┬─▶ IMPORTED ─▶ resolve KO ─▶ FlowAggregate
                                                                          └─▶ QUARANTINED (kept as audit record, no rows used)
                                  ─▶ evaluate_bank_flows ─▶ EvaluationRun / PredictionRecord ─▶ dashboard + API
```

| Module | Role |
|---|---|
| `parser.py` | format detection, Decimal money (Indian grouping), header + rows, malformed-row capture |
| `normalize.py` | ref/channel/KO extraction, channel→category map (**provisional**), reversal flags, customer-data redaction |
| `reconcile.py` | opening/running-balance/closing proof, header-gap check → `RECONCILED` / `PARTIAL` / `FAILED` |
| `resolution.py` | KO → CSP: `exact`, `variant_provisional`, `ambiguous_truncated`, `unmatched`, `missing` |
| `ingest.py` | hash-dedupe, overlap/conflict handling, quarantine, persistence, `reresolve_all` |
| `flows.py` | CSP-day aggregates, configurable signed-flow definitions, window sums |
| `targets.py` | Avg Balance history, refresh-event detection (cadence **not assumed**), month-end-MAB availability |
| `evaluation.py` | chronological, event-grouped evaluation vs baselines |
| `services.py` | read layer shared by `dashboard.views.bank_flow` and `api/v1/bankflow.py` |

Tables (migration `bankflow/0001`): `ImportRun`, `StatementFile`, `StatementReconciliation`,
`StatementTransaction`, `CodeResolution`, `FlowAggregate`, `UnresolvedFlowDaily`, `ModelVersion`,
`EvaluationRun`, `PredictionRecord`.

## Reconciliation rules

A statement is **RECONCILED** only if: no malformed rows; `opening − debits + credits` reproduces
*every* running balance and the last row (Decimal, exact); **and** header Book Balance equals the last
row balance; footer present. Internally consistent but header gap ≠ 0 ⇒ **PARTIAL** (never
"reconciled"). Any running-balance break, malformed row, missing opening balance, unexpected account
(`BANKFLOW_EXPECTED_ACCOUNT_SUFFIX`, default `71556`) or conflict with already-stored rows ⇒
**QUARANTINED**: a `StatementFile` + `StatementReconciliation` record is kept, no transactions are used.

*Observed:* the sample is internally consistent (5,399 rows, 0 mismatches) but its Book Balance is
₹22,15,947 higher than the last row → **PARTIAL**, cause unknown (see blockers).

Idempotency: file identity = SHA-256 of content. Overlapping hourly exports add only unseen rows
(row identity = account + date + description-hash + debit + credit + running balance). A row with
the same description/amount but a *different* balance, or a same-period file with a different opening
balance, is a conflict → quarantine. Source files are never moved, renamed or deleted.

## Privacy

Descriptions contain customer account numbers and names. They are **not stored**:
`description_redacted` keeps reference/channel/KO; `description_hash` (SHA-256 of the full original)
keeps rows verifiable and de-duplicable. No customer account → CSP mapping exists or is attempted.

## Flow semantics (CONFIRMED by the business seniors, 2026-10-14; see `normalize.py`)

The statement is Eko's pool account. A **credit** raises the pool balance and a **debit** lowers it
(the running balance proves it on every row). The seniors confirmed, row type by row type, that:

| Statement row | Meaning | Pool side | Counted |
|---|---|---|---|
| `AEPSWDL`, `RupayWDL`, `Withdrawal` | customer withdrew cash at the CSP | credit | yes, as **withdrawals** |
| `AEPSDeposit`, `RupayDeposit`, `Deposit`, **`Initial Deposit`** | customer deposited cash with the CSP | debit | yes, as **deposits** |
| `MoneyTRF` | money transfer | debit | no |
| `AEPSOffUsWDL`, `RupayOffUsWDL` | customer of another bank | credit | no |
| `Loan Deposit`, `SSA` | other products | debit | no |
| `EKO DEDUCTION`, `CSH DEP (CDM)` | Eko's own pool entries, no CSP | debit / credit | no |
| `outimps`, `ppf`, `yono fi wdl` and anything unrecognised | not customer withdrawals or deposits | - | no |
| `yono withdrawal` | **held out** until independently verified and approved (a different label from `yono fi wdl`); stored as unclassified | - | no |

Customer-balance effect of a counted row = `debit − credit` (a withdrawal credited to the pool is
negative, a deposit debited from it is positive; a reversal flips the sign). The dashboard and API
therefore report `withdrawals`, `deposits` and `net_customer_flow = deposits − withdrawals`
(positive = customers put in more than they took out), and the "customers withdrew more than they
deposited" alert fires when the net is negative and material. The raw `pool_credits`, `pool_debits`
and `net_pool_flow = credits − debits` fields are still returned. Alternatives
(`customer_wd_dep_xfer`, `pool_net`) remain selectable offline.

**KO code lengths (seniors, 2026-10-14).** A CSP code is exactly 8 characters (`1A85XXXX`). A
**7-character code is impossible**: such a row is a cut-off/corrupt description and stays unattributed.
A **9-character code** (`1A85XXXX1`) is a **sub-CSP** of the CSP `1A85XXXX` and rolls up to it (the
ninth character is the sub-CSP's number), which is what the "first 8 characters" rule already does.

**Stored rows.** Rows imported before this confirmation carry the old category for `initial deposit`.
`build_bank_flows` (run by `worker-bankflow` daily, or by hand) first re-applies the channel map to
stored rows (`ingest.reclassify_stored`, idempotent) and then rebuilds the flows.

**The dashboard counts the same types.** The headline withdrawals and deposits (Home, Money Movement, the
per-CSP flow, the alerts and `/api/v1/bank-flow/dashboard-summary`) is the sum of the `withdrawal` and
`deposit` categories only (`intelligence.COUNTED_CATEGORIES`, taken from the default definition). Every
other category is reported separately as `flows.other_types` ("not counted here") and is never hidden. On
the first production import (18 statement days) the picture was: withdrawals + deposits ≈ ₹64.1 Cr in /
₹8.0 Cr out, while money transfers alone were ₹39.0 Cr out and EKO deductions (no KO code, so unattributed)
₹20.3 Cr out; counting every type had made "money out" about six times too large. The unattributed share
still compares all types with all types.

## KO-code resolution

Only **exact** matches are attributed to a CSP. A code that is a known code plus trailing characters
(e.g. `1A8502957` vs `1A850295`) is recorded as `variant_provisional` with its candidate but is
**not** attributed. Attribution needs **both** `BANKFLOW_CONFIRM_TRAILING_VARIANTS=true` **and** a
non-empty `BANKFLOW_VARIANT_RULE_CONFIRMATION_REF` naming where the rule was confirmed — the flag alone
is ignored (and logged). Truncated/multi-candidate codes are `ambiguous_truncated` and never assigned. No
placeholder `Csp` rows are ever created. Rates are reported by count **and** by amount, because they
differ materially (sample: exact = 62.4% of rows but 51.8% of money; uncoded pool funding = 1.2% of
rows but 21.3% of money).

## Evaluation design

* **Target:** next Calling Sheet `Avg Balance` change per CSP (per-account average). Refresh events are
  *detected* (≥30% of CSPs change on a date), not assumed; `--event-dates` overrides with confirmed dates.
* **Independence:** the unit is the refresh event, not the CSP. Metrics are per event, then averaged;
  uncertainty is a bootstrap over events.
* **Chronology:** expanding window — event *k* is scored by a model fitted only on events before *k*.
  Minimum 8 usable events, 3 training events, 3 held-out events (all configurable and recorded).
* **Baselines:** no-change; historical-mean change. Model: through-origin OLS on net flow per account.
* **Horizon:** flows end `horizon_days` before the refresh. `0` = same-interval *association*, labelled so.
* **Account counts:** each row records whether `account_count` changed; `--subset acct_unchanged` removes the denominator effect.
* **Pass criteria (pre-registered in each run's config):** model beats no-change in ≥ 2/3 of held-out
  events, the 95% bootstrap CI of the MAE gain excludes 0, and the relative MAE gain ≥ 5%. Direction:
  beats the majority-sign rate with a CI above 0.
* **Statuses:** `NOT EVALUATED`, `INSUFFICIENT EVIDENCE` (too few independent events/coverage —
  regardless of pooled numbers), `EVALUATION FAILED` (enough evidence, criteria not met, or run error),
  `VALIDATION PASSED`.
* **Month-end MAB:** reported as unavailable unless whole months of near-daily *varying* observations
  exist. A sparse/stepwise sheet series is never converted into daily balances or MAB.

## Evaluation integrity (hardened 2026-10-12)

* **Completeness is proven, not assumed.** A statement day counts only if the *next* statement's opening
  balance equals that day's last stored running balance (`flows.proven_complete_dates`). Hourly/intra-day
  exports with no continuity proof are partial and are excluded from every evaluation window; coverage is
  the share of window days proven complete.
* **Baseline origin = value at the previous refresh** (earliest observation for the first event), known
  strictly before the flow window closes. Neither the baseline nor the target origin can see anything
  inside the forecast horizon.
* **Insufficient history publishes no numbers.** Below `min_events` usable events no folds are run, no
  MAE/accuracy is stored and no predictions are saved. Predictions from a run that did not pass are
  labelled "model NOT validated, audit only" on the dashboard.
* **Multiple configurations:** evaluating several horizons in one command records `batch_configs` and a
  warning; picking the best of N is not a validation.
* Refresh events record how many CSPs changed *without* an account-count change and the gap between the
  two observations (the true refresh lies inside the gap).

## Operational safety (hardened 2026-10-12)

* One database transaction per file; failure rolls the whole file back and writes a `FAILED` audit record.
  Counters on `ImportRun` are saved after every file, so an interrupted run is still auditable.
* Concurrent importers are serialised with a PostgreSQL advisory lock and re-check the file hash / overlap
  inside it. (With the lock disabled the same scenario hits `IntegrityError` in 3/3 trials — the unique
  constraints still prevent duplicate rows, the lock prevents failed imports.)
* Queries are chunked (`QUERY_CHUNK`), inserts and resolutions stream in batches, aggregates are rebuilt
  from a streaming cursor. Measured on PostgreSQL 17: 100,000 rows / 19 MB → 100,000 rows stored, 1,000
  CSP-day aggregates, ~246 MB peak Python memory (tracemalloc), re-run recognised as duplicate in 0.04 s.
  Files over 50 MB are refused with an audit record.
* A folder import considers only `.xls/.xlsx/.txt/.csv/.tsv` files; hidden and partial-download files are ignored.
* Redaction is allow-list based: stored text is built from parsed fields (ref/channel/KO); unrecognised
  layouts are replaced by a placeholder, so free text can never reach the database, API or dashboard.

## Historical-data workflow

1. **Statements** (administrator-approved exports only): copy them into one dedicated folder and run
   `ingest_bank_statements --path <folder>`. Re-running is safe. Files are only read.
2. **Calling Sheet history:** use `scripts/backfill_calling_sheet_history.py` (archived workbooks). It writes
   `source=history` rows and **never overwrites a live `calling_sheet` row** for the same CSP and day.
3. `build_bank_flows` (after new CSPs appear), then `bank_flow_coverage` to see date coverage, missing
   statement dates, proven-complete days, overlap, refresh events with usable flow data, and attribution
   coverage by count and amount.
4. `evaluate_bank_flows` only when `bank_flow_coverage` says READY (≥ 8 usable events). If the real refresh
   dates are confirmed by the sheet owner, pass them with `--event-dates`.
5. Provenance is preserved: file name, SHA-256, size, modified time, import run, statement date.

## PostgreSQL verification (local, isolated)

```bash
docker run -d --rm --name bankflow-pg-test -p 127.0.0.1:55432:5432   -e POSTGRES_PASSWORD=<throwaway> -e POSTGRES_DB=csp_test postgres:17
DATABASE_URL=postgres://postgres:<throwaway>@127.0.0.1:55432/csp_test uv run pytest tests
docker stop bankflow-pg-test
```
The suite creates its own `test_*` database. Never point this at a shared or production database.

## Commands

```bash
cd app
uv run python manage.py migrate
uv run python manage.py ingest_bank_statements --path "<folder or single file>"   # default: BANKFLOW_WATCH_DIR
uv run python manage.py build_bank_flows            # re-resolve KO codes + rebuild aggregates
uv run python manage.py bank_flow_coverage          # read-only readiness report
uv run python manage.py evaluate_bank_flows --horizons 1 [--subset acct_unchanged] [--event-dates 2026-09-22,...]
```

Surfaces: dashboard **CSP Intelligence → Bank-Flow Validation** (`/dashboard/bank-flow/`, staff only);
API `GET /api/v1/bank-flow/{summary,code-resolution,flows,evaluations,target}` (scope `report:read`).

## Open blockers / unresolved assumptions

1. Avg Balance refresh cadence and effective measurement date are unknown (local data: one refresh in 13 days).
2. Header Book Balance gap on the sample is unexplained → statements stay `PARTIAL`.
3. ~~Trailing-character KO rule and the channel→category map are unconfirmed~~ -- confirmed by the
   seniors on 2026-10-14 (see "Flow semantics").
4. Only one statement date exists; evaluation needs ≥ 8 independent refresh events with overlapping flow data.
5. Pool flows may not correspond to a CSP's own BSBD balances.

## Running on the server — read-only on other teams' folders

The statement folder (`/home/adarsh/login_automation/yonosbi-automation/downloads`) belongs to another
team. This project only **reads** it. Nothing here creates, moves, renames, locks or deletes anything in
it; every write goes to this project's own database. Mount it **read-only** so that is enforced, not just promised.

**1. Look first, with plain shell tools (no project code needed, read-only):**

```bash
D=/home/adarsh/login_automation/yonosbi-automation/downloads
ls -la --time-style=full-iso "$D"
find "$D" -type f \( -iname '*.xls' -o -iname '*.xlsx' -o -iname '*.txt' -o -iname '*.csv' \) \
     -printf '%TY-%Tm-%Td %TH:%TM  %10s bytes  %p\n' | sort
# real format + reporting period of ONE file (header lines only; prints no transactions):
head -c 3000 "$D/<file>" | tr -d '\r' | grep -E '^(Account Number|Opening Balance|Start Date|End Date)'
file "$D/<file>"            # 'ASCII text' means tab-delimited text, whatever the extension says
sha256sum "$D"/*            # reads only; use to spot byte-identical copies
```
If a file is not readable by your user, **do not change its permissions** — ask its owner for read access.

**2. Inventory with the project (read-only mount; needs the image that contains `bankflow`):**

```bash
cd /opt/projects/csp-balance-tracker          # this project's directory only
docker compose run --rm --no-deps --entrypoint sh \
  -v "$D":/bank_statements:ro worker-ingestion \
  -c 'cd /app/app && python manage.py inventory_bank_statements --path /bank_statements --recursive'
```
Lists every statement-looking file with real format, account suffix, reporting period, SHA-256 and
processing status. `--no-db` skips the status lookup; `--csv /tmp/inventory.csv` writes a report (refused
if inside the scanned folder).

**3. Import (writes to this project's database only; requires an approved schema migration):**

```bash
docker compose run --rm --no-deps --entrypoint sh -v "$D":/bank_statements:ro worker-ingestion -c '
  cd /app/app && python manage.py migrate bankflow &&
  python manage.py ingest_bank_statements --path /bank_statements --recursive --max-file-mb 100 &&
  python manage.py build_bank_flows && python manage.py bank_flow_coverage'
```
Safe to re-run: identical files are recognised by SHA-256 and overlapping exports add only unseen rows.
Do not schedule it, and do not enable the variant rule, without a separate decision.

**No project image on the server yet?** Copy the files out with a read-only pull into this project's own
folder and import elsewhere: `rsync -rt "$D"/ /opt/projects/csp-balance-tracker/data/bank_statements/`
(the source is only read). Remember the files contain customer names/account numbers — keep access tight.

## Findings from the real history (local validation, 2026-10-12)

* Files found on the dev machine: the 12 Oct pool statement; the **August 2026 statement** (1–31 Aug,
  308,651 rows) and a spreadsheet-re-saved copy of it. The server folder itself was not reachable here.
* August ledger: internally consistent, 0 running-balance mismatches, 30 days proven complete (31 Aug is
  the export boundary). Header Book Balance ≠ closing in both files: it is consistent with an
  export-time snapshot (file dated 16 Sep) but that is **not proven** — status stays `PARTIAL`.
* **Independent completeness check:** the stored Transaction sheet's August withdrawals + deposits
  (218,197 rows) are **100%** present in the statement by 20-digit reference, with identical amounts;
  fund transfers (0%) do not pass through the pool.
* **KO-code rule check** (same reference → authoritative CSP): exact codes 135,268 rows, 0 contradictions;
  trailing-character variants 82,929 rows / 222 codes, **222/222 equal the known-code prefix**. This is
  independent supporting evidence; applying the rule still needs the operator's explicit confirmation.
* Calling Sheet `Avg Balance` states seen in the archived exports: one refresh between 16 and 21 Jul, none
  between 21 Jul and 21 Sep, another on 22 Sep (~91–96% of CSPs change at each event; ~81% change account count).
  No refresh event overlaps the August statement → no usable event → **INSUFFICIENT EVIDENCE**.

### Server folder layout (seen 2026-10-12) and what to import

```
downloads/<YYYY-MM-DD>/statement_1556_<DD-MM-YYYY>.xls   daily statement, 0.2-3.4 MB  -> IMPORT THESE
downloads/<YYYY-MM-DD>/direct_1556_<HHMMSS>.xls          hourly snapshot, <= ~57 KB   -> skip (rolling window, not a ledger)
downloads/<YYYY-MM-DD>/provisional/direct_1556_*.xls     same, provisional           -> skip
downloads/credential_health.json, credential_latch.json, run_state.json              -> NEVER read / import
```
Use `--pattern 'statement_1556_*'` with `--recursive`. A statement generated on the SAME day it covers
(e.g. 18:31) is a day-to-date cut; it is only treated as complete if the next day's statement opens at
exactly its last running balance (ledger continuity), otherwise its last day stays "partial".

## Complete, partial and unverified days (exports are used as they are)

Statements are consumed exactly as the other team produces them, including same-day cuts. Each statement
day is classified (`services.day_coverage`) and the class is shown in `bank_flow_coverage`, the API summary
and the dashboard:

| Class | Meaning | Used for evaluation? |
|---|---|---|
| `COMPLETE_DAY` | proven by ledger continuity (inside a verified multi-day statement, or the next statement opens at this day's closing balance) | yes |
| `PARTIAL_DAY` | newest export of that day was taken on that day and no proof exists: a day-to-date snapshot | **no** — analysed separately |
| `UNVERIFIED` | rows exist but neither proof applies (e.g. exported later, next statement missing) | no |

Partial snapshots are reported with an explicit window: from the start of the day (the statement's opening
balance) to **at most** the export timestamp (`file_modified_at`, preserved by `tar`). Ledger rows carry
dates only, so the true last-transaction time is unknown. Snapshots with different cut times are not
comparable; `cut_hour` supports like-with-like grouping. Nothing here ever presents a snapshot as a daily total.

Reproduce: `ingest_bank_statements --path <folder> --recursive --pattern 'statement_1556_*'`, then `bank_flow_coverage`.

## Daily Avg Balance estimates (added 2026-10-13)

Separate from `DailyBalance` (never written to it). Model `DailyEstimate`, one row per CSP per day;
service `bankflow/estimates.py`; API `GET /api/v1/bank-flow/estimates[/summary]`,
`GET /api/v1/bank-flow/account-count-hypothesis` (scope `report:read`); dashboard section on
`/dashboard/bank-flow/`. Each row separates **observed** (SBI, Calling Sheet) from **estimate** (model).

**The estimator (updated 2026-10-17): movement, shadow only.** Per CSP and per cycle:

```
combined baseline  = Calling Sheet Avg Balance (per account) x valid account count
net movement (day) = eligible deposits (pool debits) - eligible withdrawals (pool credits)
combined balance   = previous combined balance + net movement
estimate           = combined balance / valid account count of that day
```

* A cycle starts on the day a new Avg Balance is first seen. That day is the baseline alone; the day's own
  statement movement is **not** applied. Movement applies from the next day. Earlier cycles are kept.
* **An account-count change never scales the combined balance**; it only changes the divisor from the day it is
  received. Blank, zero and non-numeric counts are invalid: the last valid count stays in use and the row is flagged.
  (The retired rule "Avg Balance rises in proportion to the count" is kept only as a stored diagnostic.)
* Eligible labels are the statement allow-list in `normalize.CHANNEL_MAP`; everything else, including `yono
  withdrawal`, never changes the estimate. The transaction workbook keeps its own seven-type list.
* No eligible movement: carry the combined balance forward. Missing or incomplete statement: carry forward (or apply
  the part of the day that exists) and mark the row **provisional** -- a missing statement is never read as no activity.
  Today is partial until its statement is complete.
* A row is **final** when its day is proven complete and every earlier day of the cycle was final. Final rows are never
  rewritten (late transactions go through review); provisional rows are recomputed from the last final row until
  complete. Rows written by the retired rules are replaced by the movement row of the same day with the old values kept
  in `details.superseded_row`. Changes to an already-final day are never applied silently: `correct_estimate
  --list-drift` and Review report them, and an active superuser can run `manage.py correct_estimate` (reason required;
  audit in `EstimateCorrection`). See `docs/RELEASE-RUNBOOK-2026-10.md`.
* Columns added to `DailyEstimate`: `estimated_combined`, `net_movement`, `eligible_deposits`,
  `eligible_withdrawals`, `is_final`. Account counts come from the Calling Sheet only (PMJDY snapshots are not used).

| Status | Meaning |
|---|---|
| `VALIDATED` | the movement estimator met the pre-registered criteria on held-out SBI refreshes (>= 8 refresh events, >= 30 pairs, >= 6 monthly cycles, beats carry-forward in >= 2/3 of events, >= 5% MAE gain, bootstrap CI above 0). Only this status carries `expected_error_pct`. A separate approval is still required before production use; none is recorded. |
| `PROVISIONAL` | ahead of carry-forward on >= 3 events / >= 10 pairs but below the validation bar. No error figure. |
| `INSUFFICIENT_EVIDENCE` | a shadow estimate not shown to beat carry-forward. Default. |
| `UNAVAILABLE` | no baseline, no valid count, or below the 200-account gate (`csp.rules.is_eligible`, unchanged). |

Held-out evaluation (`evaluate_account_count_hypothesis`, `bankflow/model_comparison.py`): for each SBI refresh the rule
predicts the new value from the previous reading only, `(a0 x n0 + net movement over the interval) / n1`, against
carry-forward. There are no fitted parameters, so every refresh with at least 80 % of its days covered by
proven-complete statements is a true held-out test; the rest are counted as not comparable, never scored as zero
movement. Verdict thresholds are fixed in code (`account_hypothesis.classify`). Real SBI accuracy is unknown until
enough refreshes and monthly cycles accumulate: until then the verdict is `INSUFFICIENT_EVIDENCE`.

(`COUNT_SOURCE_MISMATCH`), never treated as growth.

**Estimator selection** (superseded 2026-10-17): the four-way comparison (`growth_only`, `flow_only`, `growth_and_flow`
with fitted coefficients) was replaced by the single movement rule described above, scored against carry-forward.

### Automation (the `worker-bankflow` service)

The pipeline runs by itself on a dedicated worker (`compose.yml`, `WATCH_JOBS`); a plain `worker` never runs it.
Schedule (all settings, see `.env.example`):

| Job | When | Why |
|---|---|---|
| `ingest_bank_statements` | hourly | picks up new statement files (read-only mount; skipped, not failed, if the folder is absent) |
| `ingest_pmjdy` | **hourly** | this month's new-account block from the Calling Sheet |
| `compute_daily_estimates` | **hourly** | the estimates, from the latest observations and flows |
| `snapshot_calling_sheet_baseline` | **once a day, from 06:00 IST** | preserves the day's starting value of every CSP (first capture wins; a worker that was down at 06:00 takes it on return and the log says how late) |
| `warm_dashboard_cache` | every minute (a no-op unless the data changed) | pre-computes the cached dashboard figures so the first visitor after an ingest is not slow (see docs/DEPLOYMENT.md, "Fast loading") |
| `build_bank_flows` | daily | re-attributes statement rows when new CSPs appeared in the sheet |
| `evaluate_account_count_hypothesis` | daily | re-runs the estimator comparison as history accumulates |

Every job is idempotent and logged (`IngestLog`), a failing job never stops the pass, and replicas are
serialised by the existing job lock. Full rollout commands: docs/DEPLOYMENT.md, "Bank-flow and estimate rollout".

**KO codes** are `1A85` + 4 digits; anything after the 8th character is dropped, and a code that is not in that
format is never a CSP (`flag_invalid_csp_codes`).

**Needs attention** flags a CSP for more money going out than coming in only when the outflow is at least
`ALERT_MIN_OUTFLOW` (default Rs 1,00,000) **and** at least `ALERT_MIN_IMBALANCE` (25 %) of that CSP's own activity
(high: Rs 5,00,000 and 40 %). On the 2026-10-13 real data that is 29 CSPs instead of 183. Alerts pause, with a
visible notice, when statements cover under `ALERT_MIN_NETWORK_COVERAGE` (50 %) of the period.

**Refreshes** happen at any moment, per CSP; no refresh dates are assumed. A refresh is a change in a CSP's value
between two readings, and evidence is counted in such changes and in calendar-month cycles.

### Getting Calling Sheet history when there are no archived workbooks (added 2026-10-13)

The deployed tracker polls the sheet every minute and stores one reading per CSP per day in
`csp_dailybalance` (plus any rows the 2026-10 archive backfill loaded, `source = history`). That table
*is* the history, including each day's account count. The per-CSP API does not return account counts, so
export two small tables (read-only SELECTs; no names, mobiles or e-mails) on the host that runs the tracker:

```bash
docker compose exec -T db psql -U "$DATABASE_USER" -d "$DATABASE_NAME" -c \
  "\copy (select csp_code,status,account_count,zone,first_seen_date,last_seen_date from csp_csp order by 1) to stdout with csv header" > csp.csv
docker compose exec -T db psql -U "$DATABASE_USER" -d "$DATABASE_NAME" -c \
  "\copy (select csp_id,balance_date,daily_avg_balance,account_count,source from csp_dailybalance order by 1,2) to stdout with csv header" > dailybalance.csv
```

Copy both files to the analysis machine and load them into a LOCAL database only:
`python scripts/load_balance_export.py --csps csp.csv --balances dailybalance.csv`
(refuses non-localhost databases; skips demo / carry-forward rows; idempotent). Then
`build_bank_flows`, `bank_flow_coverage` and `evaluate_account_count_hypothesis` give the real verdict.

### New PMJDY accounts: the monthly blocks in the main tab (corrected 2026-10-13)

There is no separate PMJDY tab. Every month the main tab ("Calling Sheet New") gains column blocks whose row-1
label names the month: `Targets Oct 2026` (header `PMJDY Monthly`), `MTD Achievement October'26` and
`FTD Achievement October'26` (header `PMJDY` in each). They count PMJDY accounts **opened** (target, month-to-date,
for-the-day), not a CSP's total account base -- that is the `Total Accounts` column.

`ingest_pmjdy` reads the main tab, finds the current IST month's blocks (`bankflow.pmjdy_monthly`) and keeps an
insert-only history in `PmjdyMonthly`. It refuses rather than guess: no MTD block for this month (the sheet owner has
not added next month's yet -- last month's numbers are never reused), two blocks for the same month, or two `PMJDY`
columns inside one block. Blank / non-numeric cells are "no reading", never zero. Rate-limited by
`PMJDY_MIN_INTERVAL_SECONDS`; the block used is stored in `IngestLog.file_name`. On 2026-10-13 the October blocks of
the workbook copy gave MTD 1,246, FTD 130, target 5,205 across 535 CSPs.

The dashboard says "new customer accounts" (the agreed plain wording). `AccountCountObservation` (total-count
snapshots) is still supported but nothing feeds it: total accounts come from `DailyBalance.account_count`.

## Data progress and "today vs yesterday" (added 2026-10-14)

**Today is always the present IST day and yesterday the day before.** This applies to every page,
API response and comparison. Where the data for today has not arrived yet (for example the
transaction batch), the figure is shown as a dash / `null`, never as zero and never replaced by an
older day: the Transactions and Trends pages used to anchor "today" on the newest day in the mart and
now follow the wall clock like everything else.

* **Data progress** (`/dashboard/progress/`; tab "Data progress" in Money Movement, entry in More,
  and the freshness badge in the top bar links to it). `bankflow/progress.py::pipeline_progress` only
  reads what the pipeline already writes (`IngestLog`, `ImportRun`, `StatementFile`,
  `BaselineSnapshot`, `PmjdyMonthly`, `FlowAggregate`, `HypothesisRun`, `DailyEstimate`). Per step:
  schedule, last run, next due, health (On time / Late / Waiting / Not run yet) and what happened
  today and yesterday, including when the morning snapshot was captured and when each bank statement
  was produced and ingested. Late means no run for two cycles plus one 5-minute worker tick; the
  06:00 IST snapshot is "Waiting" before 06:00 and "Late" after it if nothing was captured. Error text
  is shown to staff only. API: `GET /api/v1/bank-flow/progress` (scope `report:read`).
* **Today vs yesterday strip** on every data page (`navigation.COMPARE_PAGES`) and a per-CSP version
  on each CSP page. One function, `intelligence.day_compare(today=None, csp_codes=None)`, feeds both
  and `GET /api/v1/bank-flow/day-compare[?csp=CODE]`. Each figure is what the stored data says *as of*
  that day: account counts and the SBI average balance are the newest reading on or before the day
  (the change counts only CSPs with both readings), new accounts are the newest monthly reading taken
  by the end of the day, money in/out is that day's own statement (withdrawals and deposits only) and
  the estimate is that day's `DailyEstimate` when it is validated or provisional. Today's money
  movement is "so far" until a statement proves the day complete, so the money rows carry
  `like_for_like: false` and a plain-English note instead of being presented as a trend. On a phone the
  strip starts folded; the choice is remembered in the browser.
* Money movement figures on the dashboard now cover **tracked CSPs only** everywhere, so the strip,
  the summary cards and the daily table agree.

* **Every CSP, today vs yesterday** (added 2026-10-14). Home, "Your CSPs", has two views: the last
  7/30/90 days (as before) and **Today vs yesterday**, one row per tracked CSP with customer accounts,
  new accounts that day, withdrawn, deposited, deposits minus withdrawals, the SBI average balance and
  our estimate, each for today and yesterday with the change. It is `intelligence.csp_day_compare_rows`
  (batch queries, same definitions as `day_compare`; the rows add up to the network strip) and
  `GET /api/v1/bank-flow/csp-day-compare` (`search`, `sort`, `order`, `limit`, `offset`). A CSP with no
  stored figure for a day shows a dash and no change; CSPs without a figure sort last whichever way the
  list is ordered. Money for today is "so far", and `like_for_like_money` / `money_note` say when the
  two days cannot be compared fairly.
