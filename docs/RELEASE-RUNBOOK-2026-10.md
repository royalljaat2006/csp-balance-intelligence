# Release runbook: new UI + movement-based shadow estimator (October 2026)

Status: **prepared, not executed.** Nothing here has been run against production. The predictor stays
**shadow-only**; this release does not promote it. Production calculations (Calling Sheet ingest,
Rule #19, the 200-account gate, `DailyBalance` writes) are unchanged.

What was and was not verified is stated per item. "Rehearsed" means run on an isolated scratch
PostgreSQL 16 database with synthetic data, never on production data.

## 1. What this release changes in the database

| Migration | Operation (from `sqlmigrate`) | Locking / cost |
|---|---|---|
| `bankflow 0007` | `ADD COLUMN` x5 on `bankflow_dailyestimate`: four nullable `numeric(16,2)`, one `boolean NOT NULL DEFAULT false` | Metadata-only on PostgreSQL 11+ (nullable columns and a constant default do not rewrite the table). Takes a short `ACCESS EXCLUSIVE` lock; it queues behind any long-running transaction on that table. |
| `bankflow 0008` | `ALTER COLUMN is_final SET DEFAULT false` and `CREATE TABLE bankflow_estimatecorrection` (+ FK, 3 indexes) | Metadata-only plus a new empty table. |

* Both are **additive and backward compatible**. `0008` gives `is_final` a database default so the
  *previous* release's `INSERT` (which does not know the column) stays valid between `migrate` and the
  restart of the old worker. Tested: `test_a_row_inserted_by_the_previous_release_is_valid_during_a_rolling_deploy`.
* **Migrating does not backfill anything.** Rehearsed: legacy rows are untouched after `0007`+`0008`
  (3 of 3 rows kept, new columns empty, `is_final` false); the corrections table starts empty; reversing to
  `0006` keeps the rows; migrating forward twice is a no-op.
* The backfill happens when `worker-bankflow` next runs `compute_daily_estimates` (hourly by default).

## 2. Expected backfill on the first estimator run

On the first run each tracked CSP gets one row **per day from the receipt date of its latest SBI value
to today**. Expected rows = sum over tracked CSPs of `(today - latest_cycle_start + 1)`. Read-only SQL that
computes it (validated: it predicted exactly the stored rows on two scratch databases, 93,000 and 228):

```sql
WITH s AS (
  SELECT csp_id, balance_date, daily_avg_balance,
         lag(daily_avg_balance) OVER (PARTITION BY csp_id ORDER BY balance_date) AS prev
  FROM csp_dailybalance WHERE source NOT IN ('demo','carry_forward')),
runs AS (SELECT csp_id, max(balance_date) FILTER (WHERE prev IS DISTINCT FROM daily_avg_balance)
                AS cycle_start FROM s GROUP BY csp_id)
SELECT count(*) AS csps, sum(current_date - cycle_start + 1) AS expected_rows,
       max(current_date - cycle_start + 1) AS longest_cycle_days
FROM runs r JOIN csp_csp c ON c.csp_code = r.csp_id
WHERE c.status NOT IN ('historical_only','invalid_code');
```

**Run this on production before deploying** (it is a read-only `SELECT`). The production figures are not
known to this report.

Measured on a synthetic scratch database (3,000 CSPs, 30-day cycles, 90,000 flow rows), PostgreSQL 16 on a
developer PC, so treat as an order of magnitude:

| Measure | Result |
|---|---|
| First run (full backfill) | **24.5 s** for 93,000 rows written in one transaction |
| Second run, same day | 1.7 s, 3,000 rows (only today's provisional rows are rewritten; 90,000 final rows untouched) |
| Storage | about **803 bytes per row**; 84 MB for 93,000 rows including indexes (heap 76 MB) |
| Daily growth afterwards | about one row per tracked CSP per day (3,000 CSPs: about 2.4 MB/day) |

Scale roughly linearly with `expected_rows`. The estimator job holds the job lock; the write is a single
`bulk_create` in a transaction, so readers are not blocked and a failure rolls back everything.

## 3. Idempotency and failure behaviour (tested)

* Re-running the same day is idempotent: final rows are not rewritten; provisional rows are recomputed
  from the last final row. (`test_rerunning_the_same_day_is_idempotent`, scale rehearsal second run.)
* A failure mid-write rolls the whole write back; the retry adds no duplicate rows and the chain is intact.
  (`test_a_failed_daily_run_leaves_the_chain_intact_and_the_retry_adds_no_duplicates`.)
* A failed run is **logged** (`poll_failed`, job name) and counted in the job metrics
  (`record_job_run(outcome="failed")`) by the worker loop; the loop keeps running and retries next interval.
  There is no alert beyond that, so check the worker log after the first run (section 6).
* Importing the same statement twice changes nothing (recognised by SHA-256).

## 4. Preconditions (stop if any is false)

1. `origin/main` contains the release commits and CI is green on it (this report could not check the
   GitHub Actions result).
2. A **fresh backup exists and has been restored somewhere** (section 5). The repository documents
   `pg_dump` and a restore command but this report did **not** run or verify either.
3. No import is in progress: do not restart `worker-bankflow` mid-import (it leaves a 10-minute Redis job
   lock). Check `docker compose logs --tail 50 worker-bankflow` first.
4. No long-running transaction on `bankflow_dailyestimate` (it would delay the lock):
   `SELECT pid, now()-xact_start AS age, left(query,80) FROM pg_stat_activity WHERE xact_start IS NOT NULL ORDER BY age DESC LIMIT 5;`
5. The expected-rows query above ran and the number is reasonable (section 2).
6. Disk has room: roughly `expected_rows x 0.9 KB` extra.
7. `/home/shumit/ORCH_NEW` (another team's stack) is not touched by any command below.

## 5. Backup and recovery prerequisites

```bash
# 1. Backup (existing procedure, docs/DEPLOYMENT.md "Backup / restore")
docker compose exec db pg_dump -U ${DATABASE_USER} ${DATABASE_NAME} > backup_$(date +%F)_pre-estimator.sql
# 2. Prove it is usable BEFORE migrating: restore into a throwaway database (never the live one)
docker compose exec db createdb -U ${DATABASE_USER} restore_check
cat backup_YYYY-MM-DD_pre-estimator.sql | docker compose exec -T db psql -U ${DATABASE_USER} restore_check
docker compose exec db psql -U ${DATABASE_USER} restore_check -c "select count(*) from csp_dailybalance"
docker compose exec db dropdb -U ${DATABASE_USER} restore_check
```

Not verified here: that the backup job works on the server, its retention, or restore time.

## 6. Deployment order (when approved)

```bash
cd /home/deepanshu/CSP_BALANCE_INTELLIGENCE && git pull origin main
docker compose build web
# migrate FIRST, with the old containers still running (safe: 0007/0008 are additive and backward compatible)
docker compose run --rm --no-deps --entrypoint sh web -c 'cd /app/app && python manage.py migrate'
docker compose up -d web lb                      # new UI; the web entrypoint also runs migrate (no-op now)
docker compose up -d worker-bankflow             # new estimator; restart only when no import is running
docker compose exec -T -w /app/app worker-bankflow python manage.py build_bank_flows   # once: moves stored `yono withdrawal` rows to unclassified
```

Do not rebuild or restart anything else. The first `compute_daily_estimates` pass (within the hour) does the
backfill. To run it deliberately instead:
`docker compose exec -T -w /app/app worker-bankflow python manage.py compute_daily_estimates`

### Read-only post-deployment checks

```bash
docker compose exec -T -w /app/app web python manage.py showmigrations bankflow | tail -3     # 0007, 0008 applied
docker compose exec -T -w /app/app web python manage.py makemigrations --check --dry-run      # No changes detected
docker compose exec -T -w /app/app web python manage.py correct_estimate --list-drift         # read only
docker compose logs --since 30m worker-bankflow | grep -E "poll_failed|daily_estimates"
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:${APP_PORT}/dashboard/login/        # 200
```

```sql
SELECT method, is_final, count(*) FROM bankflow_dailyestimate GROUP BY 1,2 ORDER BY 1,2;
SELECT count(*) FROM bankflow_dailyestimate WHERE method='movement';          -- close to expected_rows
SELECT csp_id, estimate_date, count(*) FROM bankflow_dailyestimate GROUP BY 1,2 HAVING count(*)>1;  -- must be empty
SELECT count(*) FROM csp_dailybalance;   -- unchanged by this release (compare with the pre-deploy value)
```

**Success:** migrations applied; `daily_estimates` logged without `poll_failed`; no duplicate
(csp, date); `movement` row count within a few percent of `expected_rows`; login and the journey pages
return 200; `csp_dailybalance` count and the production dashboards' figures unchanged.

**Failure indicators:** `poll_failed` for `compute_daily_estimates`; duplicate rows; `makemigrations --check`
reports changes; job lock stuck over 10 minutes; web 5xx; any change in production balances.

## 7. Rollback and recovery

* Schema: additive. The previous release runs unmodified against the migrated schema (database defaults), so
  **stopping `worker-bankflow` and redeploying the previous image is a complete application rollback**; no
  schema change needs to be undone. If a reverse is still wanted: `python manage.py migrate bankflow 0006`
  (rehearsed: rows kept; it drops the new columns and the corrections table, and with them any audit
  records, so export `bankflow_estimatecorrection` first).
* Estimates only: `DailyEstimate` rows are derived data. `TRUNCATE bankflow_dailyestimate` followed by one
  `compute_daily_estimates` run rebuilds the current cycle from the Calling Sheet history and statements; it
  loses `EstimateCorrection` provenance in the rows' details, so do not do this once a correction exists.
* Data corruption of production tables: restore from the section 5 backup.
* **Stop the deployment** if: the backup restore check fails; the expected-rows query is wildly larger than
  anticipated; an import is in progress; migrate errors; or any production figure changes.

## 8. Correcting a final estimate (authorised operators only)

The daily run never rewrites a final row, and an ordinary late statement is only **reported**, never applied
silently:

* Read-only report: `manage.py correct_estimate --list-drift` and the **Review > Finished estimates built on
  older statement data** section (no buttons there).
* Provisional days (missing or partial statements) refill by themselves when the statement arrives; they need
  no correction.
* A correction is explicit: `manage.py correct_estimate --csp C --from-date D --reason "..." --actor USER`
  where USER is an **active superuser**. It recomputes only that CSP's current cycle from D, stores a permanent
  `EstimateCorrection` record (actor, reason, time, days, snapshot of the superseded rows) and marks each
  rewritten row (`details.correction`). Repeating it with unchanged data writes nothing. `--dry-run` shows the
  effect first. MTD averages and the month-end outlook read the stored rows, so they follow.
* **Limitation / blocker:** there is no web or API correction and no narrower approval role, because the
  repository has no approval mechanism for this. A UI action would need one (who approves, a second approver,
  an audit trail visible to them). Until then corrections are a server operation.

## 9. What is needed before real SBI validation can say anything

The evaluation pipeline is implemented and tested on synthetic data only. Real accuracy is **unknown**:
the verdict is `INSUFFICIENT_EVIDENCE` until all of this exists.

| Needed | Why |
|---|---|
| At least 8 SBI refresh events with enough complete statements, and at least 6 distinct monthly cycles | The gate's own minimums. A refresh is a CSP's Avg Balance changing between two Calling Sheet readings. |
| Calling Sheet **history** (the sheet has none before capture began; `scripts/backfill_calling_sheet_history.py` can load dated workbook backups) | Without earlier values there are no refresh pairs. |
| At least 80% of each pair's days covered by **proven-complete** bank statements | Otherwise the pair is counted but not scored (a gap is never read as zero movement). |
| Statement gaps filled where possible | The gaps reported on 2026-10-14 were 2-3, 5-16 and 18-21 September and 3-4 October (verify the current list on Data readiness). Any SBI refresh whose interval falls mostly inside them cannot be scored. |
| A separate, explicit approval | The gate only measures; promotion is a business decision. None is recorded. |

Honest limits of what the pipeline can show even with data: the event date is the day a new value was first
*seen* (the sheet has no measurement date); a refresh that repeats the previous value is not detectable; the
gate's p90 is taken over all held-out comparisons, and the per-CSP table shows each CSP's own p90 only where it
has 8 or more refreshes. Whether the business means the gate's p90 to be pooled or required of each CSP
separately is **not specified in the repository** and needs a decision before the gate can be called met.
