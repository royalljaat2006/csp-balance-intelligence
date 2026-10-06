# PRD — CSP Average Balance Tracker

| | |
|---|---|
| **Status** | Draft v0.1 (for review) |
| **Owner** | biswajit.chacko@gmail.com |
| **Last updated** | 2026-09-10 |
| **Reviewers** | _TBD_ |

---

## 1. Summary

A backend service that tracks the **Monthly Average Balance (MAB)** maintained by each SBI‑kiosk CSP (Customer Service Point), computes where each CSP stands against the SBI incentive rule ("Rule #19"), projects month‑end position, and exposes everything through an **authenticated API** for consumption by other Eko dashboards/agents.

There is **no user‑facing UI and no end‑user login** in this project. Output is an API + an internal Django admin console for the Eko ops team.

---

## 2. Background & context

- Eko operates ~**550 CSPs** under the **SBI kiosk banking** model. Each CSP behaves like a mini bank branch and opens **BSBD accounts** (Basic Savings Bank Deposit) registered under its CSP code.
- SBI pays each CSP a **balance‑maintenance incentive** based on the average balance held across that CSP's accounts. This is **"Rule #19"** in the SBI agreement (see §5).
- Eko wants to actively manage this: know every CSP's current average balance, whether it is growing or declining, who is stuck at **NIL** (below the minimum), and how the overall book is trending — so field teams can intervene.

---

## 3. Problem statement

Today there is no consolidated, historical, queryable view of per‑CSP average balance and its trend. The balance figure lives in a live Google Sheet that is overwritten, so **history and trend are lost**, and there is no programmatic way for other tools to consume per‑CSP status.

---

## 4. Goals / Non‑goals

### Goals
1. Capture a **daily snapshot** of every CSP's daily average balance and retain full history.
2. Compute per CSP, per calendar month: **MTD MAB**, **projected month‑end MAB**, **Rule #19 slab**, **gap to minimum (₹2,501)**, **gap to next slab**, **trend (7‑day + month‑over‑month)**.
3. Compute an **overall rollup**: slab distribution, count at NIL, count improving vs declining, total book, MoM growth.
4. Expose all of the above via a versioned, **API‑key‑authenticated REST API**.
5. Provide an **internal admin console** for the Eko ops team to inspect data and ingestion health.
6. Ingestion must be **reliable and idempotent** — a missed or repeated run must self‑recover.

### Non‑goals

> **Superseded (2026-09-13):** the "no front-end" non-goal below no longer holds — the project
> was extended into a full web app with a staff-facing dashboard (session-auth, `/dashboard/*`),
> including network-wide trend views. See docs/ARCHITECTURE.md "Two front doors, one domain
> layer" for the resulting design: the dashboard and the API are two independent consumers of the
> same `csp.services` layer, so this change didn't touch the API's contract or its own goals
> above. The other non-goals (CSP-facing self-service login, write-back, real-time, payout
> accounting, non-SBI-kiosk programs) are unaffected.

- ~~Front‑end dashboards / charts (a separate team builds these on top of the API).~~
- CSP‑facing login / auth (the per‑CSP view is delivered by an external dashboard that calls this API).
- Write‑back to SBI systems or to the source Google Sheet.
- Real‑time / intraday tracking — **daily batch only** (hour‑wise explicitly dropped).
- Incentive disbursement, payout accounting, or reconciliation.
- Programs other than SBI kiosk.

---

## 5. Business rules

### 5.1 Rule #19 — balance‑maintenance incentive (per CSP)

Source: SBI agreement, item 19 — _"Weekly average balance maintenance fee (each CSP) (Min. 200 no. of BSBD accounts)"_.

| Average balance band | Incentive payable | Maximum |
|---|---|---|
| Up to ₹2,500 | **NIL** | NA |
| ₹2,501 – ₹4,000 | 1.10% p.a. | ₹25,000 |
| ₹4,001 – ₹6,000 | 1.20% p.a. | ₹35,000 |
| ₹6,001 – ₹10,000 | 1.25% p.a. | ₹40,000 |
| Above ₹10,000 | 1.30% p.a. | ₹50,000 |

- **Eligibility gate:** minimum **200 BSBD accounts** per CSP.
- **Primary business objective:** move every CSP out of **NIL** (≥ ₹2,501), then push into higher slabs.

### 5.2 Averaging method (as used by Eko business)

- Each **day** the CSP is expected to maintain a **daily average balance ≥ ₹2,501**.
- At month end, the **Monthly Average Balance (MAB)** = the **average of the daily average balance across every day of the calendar month** (1 → 30/31).
- **MAB is compared to the Rule #19 bands** to determine slab and incentive.

> **Open item:** the SBI document text says *weekly* average; Eko tracks *monthly*. To be confirmed which governs the actual payout (§13, Q4). This PRD implements **monthly** per current business instruction, with the engine written so a weekly window can be added later.

### 5.3 Definitions

| Term | Meaning |
|---|---|
| **CSP** | Customer Service Point — the retail agent operating the SBI kiosk. Identified by CSP code. |
| **BSBD account** | Basic Savings Bank Deposit account opened by the CSP under its code. |
| **Daily average balance** | Per‑CSP average balance figure for a given day, as published in the CALLING SHEET. Not computed by us. |
| **MAB** | Monthly Average Balance = mean of daily average balance over the calendar month. |
| **MTD MAB** | Month‑to‑date MAB — mean over days elapsed so far this month. |
| **Projected MAB** | Estimated month‑end MAB given data so far (§8.4). |
| **Slab** | The Rule #19 band the MAB falls into (`NIL`, `S1`…`S4`). |
| **Pool / settlement account** | The common SBI kiosk account (masked `XXXXXX71556`) that CSP cash flows through. |

---

## 6. Users & stakeholders

| User | Need | Interface |
|---|---|---|
| **External dashboard / agent** (primary consumer) | Per‑CSP status + overall rollup, programmatically | REST API (API key) |
| **Eko ops / field team leads** | Which CSPs are at NIL / declining, drill into one CSP | via the external dashboard (API) |
| **Eko data/ops engineer** | Inspect raw data, verify ingestion ran, debug a CSP | Django admin console |
| **This project's maintainer** | Deploy, monitor, evolve rules | CLI + admin + logs |

---

## 7. Data sources

### 7.1 CALLING SHEET — _primary balance source_
- **Type:** Google Sheet, live (continuously updated, overwritten).
- **Access:** direct access to be granted (Google service account, share the sheet).
- **Provides:** CSP master data (code, name, mobile, …) **and the daily average balance maintained per CSP**.
- **Consumption:** read via Google Sheets API once per day; **snapshot into our DB** to build history.
- **Exact column layout:** _TBD — pending access_ (§13, Q1).

### 7.2 TRANSACTION SHEET — _activity / health context (secondary)_
- **Type:** Excel file (`Transaction <Mon>'<YY> (n).xlsx`), a **new/updated file each day** — same month, re‑exported with latest data.
- **Ingestion:** **watched folder (Option A)** on the internal server. The service picks up the newest matching file and imports it.
- **Scope filter (important):** read **only the `Success` sheet**, and within it **only rows whose `Type of Transaction` is one of the 7 allow‑listed types** below. The `GTV` and `Failed` sheets and every other type (OFFUS, Money Transfer, IMPS, BBPS, ATM DEPOSIT/FUNDSTRANSFER, Initial deposit, Loan Deposit, PPF SSA, etc.) are ignored. Date scope: all days present in the file.

  | # | `Type of Transaction` | Category | Pool direction |
  |---|---|---|---|
  | 1 | `AEPS ONUS Withdrawal` | withdrawal | cash **into** pool |
  | 2 | `ATM Onus Withdrawal` | withdrawal | cash **into** pool |
  | 3 | `Withdrawal` | withdrawal | cash **into** pool |
  | 4 | `YONO WITHDRAWAL` | withdrawal | cash **into** pool |
  | 5 | `AEPS ONUS Deposit` | deposit | cash **out of** pool |
  | 6 | `Deposit` | deposit | cash **out of** pool |
  | 7 | `AEPS ONUS Fund Transfer` | fund_transfer | neither (account→account) |

  - Match on the **exact** type string (case‑sensitive as it appears in the file). Keep this list in config so it can be edited without a code change.
  - Sample `Transaction Aug'26 (4).xlsx`: **220,098** in‑scope rows across **481** CSPs, 31 days (of 306,035 total Success rows).
- **Structure (confirmed from sample):**
  - Sheets: `GTV` (daily pivot), `Success` (306,035 rows), `Failed` (108,367 rows). Only `Success` is used.
  - Columns: `KO ID`, `Transaction Date & Time`, `Reference Number`, `Type of Transaction`, `From Account`, `To Account`, `Amount`, `Status`, `New Date`, `CSP Code New`.
  - `CSP Code New` = normalised `KO ID` (KO ID sometimes carries extra trailing digits) → **use `CSP Code New` as the join key**.
  - `Reference Number` is unique per transaction → **dedupe key** (idempotent re‑import).
  - Pool account `XXXXXX71556`: `To = pool` → withdrawal (cash into pool); `From = pool` → deposit (cash out of pool); fund transfer touches neither.
  - **No account balances in this file** — confirms CALLING SHEET is the balance source.
- **Use:** per‑CSP per‑day **activity aggregates over the 7 types** — transaction **count** and amount, rolled up by category (withdrawal / deposit / fund_transfer) and by pool direction. Context alongside balance; **not** part of the MAB calculation.

### 7.3 History backfill — _one‑time_
- **2–3 months** of per‑CSP daily average balance, provided as files at a location to be shared.
- Loaded once to backfill `daily_balance` so trend/MoM works from day one.
- **Format / location:** _TBD_ (§13, Q5).

---

## 8. Functional requirements

### FR1 — Ingest CALLING SHEET (daily)
- Read the sheet via Google Sheets API.
- Validate with a **Pandera schema** (expected columns, types, non‑null CSP code, balance numeric ≥ 0, CSP code format).
- Upsert CSP master into `csp`; insert/replace today's row per CSP into `daily_balance` (`source = calling_sheet`).
- Rows failing validation are **rejected and logged**, not written. Run recorded in `ingest_log`.
- Re‑running the same day is idempotent (overwrites that day's snapshot).

### FR2 — Ingest TRANSACTION files (daily, watched folder)
- Scan configured folder for the **newest** file matching the filename pattern.
- Parse the **`Success` sheet only**; keep **only rows whose `Type of Transaction` is in the 7‑type allow‑list** (config‑driven — see §7.2); discard everything else. Coerce/validate the kept rows with Pandera (numeric `Amount`, valid date, non‑null CSP code).
- Derive `txn_date` from `New Date`, `category` (withdrawal / deposit / fund_transfer) from the type, `direction` (`in_pool` / `out_pool` / `other`) from pool‑account position, `csp_code` from `CSP Code New`.
- Upsert into `transaction` keyed on `Reference Number` (idempotent re‑import).
- Detect month automatically from row dates (handles month rollover with no code change).
- Record run in `ingest_log`; unmatched CSP codes reported.

### FR3 — History backfill (one‑time, CLI)
- Read provided history files, validate, upsert into `daily_balance` (`source = history`).
- Safe to re‑run; never overwrites a `calling_sheet` snapshot with a `history` value for the same day.

### FR4 — Calculation engine
Builds, per CSP per calendar month, the `monthly_summary` record:

| Field | Definition |
|---|---|
| `days_in_month` | Calendar days in the month |
| `days_with_data` | Days with a balance value (direct or carried forward) |
| `mtd_mab` | Mean daily average balance over days elapsed (carry‑forward on gaps — §8.3) |
| `projected_mab` | §8.4 |
| `slab` / `projected_slab` | Rule #19 band for `mtd_mab` / `projected_mab` |
| `incentive_rate_pa` | 0 / 1.10 / 1.20 / 1.25 / 1.30 |
| `projected_incentive_annual` | `projected_mab × account_count × rate/100`, capped at slab max — **only if `account_count` is available** (§13, Q2) |
| `gap_to_min` | `max(0, 2501 − mtd_mab)` |
| `gap_to_next_slab` | Amount needed to reach the next band (null at top band) |
| `prev_month_mab` | Final MAB of previous calendar month |
| `mom_change_abs`, `mom_change_pct` | `mtd_mab` vs `prev_month_mab` |
| `trend_7d_pct` | Last‑7‑day mean vs prior‑7‑day mean |
| `trend_flag` | `improving` (> +2%), `declining` (< −2%), else `stable` — from `trend_7d_pct` |
| `is_eligible` | `account_count ≥ 200` (if known) |

- Deterministic aggregation, slab lookup, gaps, MoM, trend → **dbt models** (with `dbt test` assertions).
- Projection → small **Python module** (numpy), writes `projected_*` columns.

### FR5 — API (see §9)
Versioned REST, `X-API-Key` auth, JSON, served from precomputed `monthly_summary` / `daily_activity` for low latency.

### FR6 — Admin console (Django admin)
- Browse/search `csp`, `daily_balance`, `transaction`, `monthly_summary`, `ingest_log`.
- Read‑oriented; filters by month, slab, trend, CSP code.
- Surface last successful ingest per source and any rejected‑row counts.

### FR7 — Observability & health
- `GET /health`: DB reachable, last successful CALLING SHEET ingest timestamp, staleness flag.
- Alert if no successful CALLING SHEET ingest in **26 h** (channel TBD — §13, Q12).
- Every ingest writes a structured (JSON) log line + an `ingest_log` row.

---

## 9. API specification

> **Superseded (2026-09-12):** this section is the original design draft. The implemented, authoritative contract is **docs/API-CATALOG.md** (endpoints, scopes, schemas), **docs/API-VERSIONING.md**, **docs/API-ERRORS.md**, and the live OpenAPI at `/api/v1/docs`. Key differences from the draft below: base path is `/api/v1` (not `/v1`); auth is per-consumer with six resource scopes (`csp:read` … `report:read`) instead of read/admin; every response is wrapped in a `data`/`pagination`/`request_id` envelope; errors are RFC 9457. The draft is kept for history only.

- **Base path:** `/v1`
- **Auth:** `X-API-Key: <key>` header. Keys stored hashed; each key has a scope (`read` or `admin`). Missing/invalid → `401`.
- **Errors:** `{ "error": { "code": "...", "message": "..." } }`, standard HTTP status codes.
- **Common query param:** `month=YYYY-MM` (defaults to current IST month).
- All responses include `as_of` (IST date of latest data) and `stale` (bool).

### `GET /health`
Liveness + data freshness. No auth.

### `GET /v1/overview?month=YYYY-MM`
```json
{
  "month": "2026-09",
  "as_of": "2026-09-10",
  "stale": false,
  "csp_total": 550,
  "csp_with_data": 512,
  "slab_distribution": { "NIL": 120, "S1": 200, "S2": 130, "S3": 70, "S4": 22 },
  "in_nil_count": 120,
  "compliant_count": 422,
  "eligible_count": 500,
  "avg_mab": 4210.5,
  "total_book": 3456789.0,
  "mom": { "prev_avg_mab": 3980.0, "change_abs": 230.5, "change_pct": 5.8 },
  "trend": { "improving": 310, "stable": 90, "declining": 112 }
}
```

### `GET /v1/csps`
Query: `month`, `slab`, `trend`, `min_mab`, `max_mab`, `eligible`, `q` (code/name search), `sort` (`mab|mom|trend|gap_to_min`), `order`, `limit`, `offset`.
Returns a paged list of per‑CSP summary rows (same shape as the `summary` block below).

### `GET /v1/csps/{csp_code}?month=YYYY-MM`
```json
{
  "csp": { "code": "1A850583", "name": "…", "mobile": "…", "account_count": 312, "is_eligible": true },
  "month": "2026-09", "as_of": "2026-09-10", "stale": false,
  "summary": {
    "mtd_mab": 2640.0, "projected_mab": 2585.0,
    "slab": "S1", "projected_slab": "S1", "incentive_rate_pa": 1.10,
    "projected_incentive_annual": 10850.0,
    "gap_to_min": 0.0, "gap_to_next_slab": 1360.0,
    "days_with_data": 9, "days_in_month": 30,
    "prev_month": { "mab": 2490.0, "slab": "NIL" },
    "mom": { "change_abs": 150.0, "change_pct": 6.0 },
    "trend_7d_pct": 4.2, "trend_flag": "improving"
  },
  "daily": [ { "date": "2026-09-01", "daily_avg_balance": 2600.0, "source": "calling_sheet" }, "…" ],
  "activity": { "txn_count": 242, "txn_amount": 1620000.0,
                "withdrawal_count": 210, "withdrawal_amount": 1450000.0,
                "deposit_count": 25, "deposit_amount": 140000.0,
                "fund_transfer_count": 7, "fund_transfer_amount": 30000.0,
                "cash_in_pool": 1450000.0, "cash_out_pool": 140000.0, "net_flow": 1310000.0 }
}
```
> `activity` covers the **7 allow‑listed transaction types** (Success sheet only — see §7.2). Descriptive context, not an input to MAB.

### `GET /v1/csps/{csp_code}/history?from=YYYY-MM-DD&to=YYYY-MM-DD&granularity=daily|monthly`
Time series of daily average balance (or monthly MAB) for charting.

### `GET /v1/csps/{csp_code}/activity?month=YYYY-MM`
Daily activity series for the CSP.

### `POST /v1/admin/ingest/run` _(scope: admin)_
Body: `{ "source": "calling_sheet" | "transactions" | "all" }`. Triggers ingest immediately; returns the `ingest_log` id.

### `GET /v1/admin/ingest/log?source=&limit=` _(scope: admin)_
Recent ingestion runs with status, counts, errors.

---

## 10. Data model (PostgreSQL)

| Table | Key columns | Notes |
|---|---|---|
| `csp` | `csp_code` PK | `name`, `mobile`, `account_count`, `zone`, `tl_name`, `status`, `first_seen_date`, `last_seen_date`, `raw_attrs` JSONB, `updated_at` |
| `daily_balance` | `(csp_code, balance_date)` unique | `daily_avg_balance` numeric, `source` (`calling_sheet` / `history` / `carry_forward`), `ingested_at` |
| `transaction` | `ref_number` PK | `csp_code`, `txn_datetime`, `txn_date`, `txn_type`, `category` (withdrawal/deposit/fund_transfer), `direction` (`in_pool`/`out_pool`/`other`), `from_account`, `to_account`, `amount`, `source_file`, `ingested_at`. **7 allow‑listed types, Success sheet only** (see §7.2). |
| `daily_activity` *(dbt)* | `(csp_code, activity_date)` | `txn_count`, `txn_amount`, per‑category counts & amounts (`withdrawal_*`, `deposit_*`, `fund_transfer_*`), per‑type counts (`by_type` JSONB), `cash_in_pool`, `cash_out_pool`, `net_flow` |
| `monthly_summary` *(dbt + projection)* | `(csp_code, month)` | all fields from FR4 |
| `ingest_log` | `id` | `source`, `file_name`, `file_mtime`, `started_at`, `finished_at`, `status`, `rows_read`, `rows_valid`, `rows_rejected`, `rows_upserted`, `error_summary` |
| `api_key` | `key_hash` | `label`, `scope`, `active`, `created_at`, `last_used_at` |

**CSP code normalisation:** replicate the `KO ID → CSP Code New` rule; keep a mapping and report codes seen in transactions but absent from the CALLING SHEET (and vice‑versa).

---

## 11. Architecture

```
 Google Sheet (CALLING) ──gspread──▶ ingest_calling  (mgmt command, cron 1×/day)
                                        │  Pandera validate → reject+log bad rows
                                        ▼
 Watched folder *.xlsx  ──pandas──▶ ingest_transactions (mgmt command, cron 1×/day)
                                        │  Success sheet · 7 allow-listed types · Pandera · dedupe on ref_number
                                        ▼
                              PostgreSQL 16  (raw: csp, daily_balance, transaction)
                                        │
                              dbt run + dbt test   (cron, right after ingest)
                                        ▼
                              marts: daily_activity, monthly_summary
                                        │
                              projection step (Python/numpy) → projected_* columns
                                        ▼
             Django + django‑ninja  REST API  ──X-API-Key──▶  external dashboards / agents
             Django admin  ──────────────────────────────▶  Eko ops (browser)
                                        │
                              container binds 127.0.0.1:${APP_PORT} only
                                        ▼
                    R730 host Nginx (80/443, TLS) ──▶ Internet
```

R730 host, `docker compose` under `/opt/projects/csp-balance-tracker`: `app` (Gunicorn/Django, `127.0.0.1:${APP_PORT}` only) + `db` (Postgres, no host port). The server's existing Nginx does TLS/reverse-proxy — not part of this repo's compose file. Cron on the host invokes management commands inside the `app` container. See §12a.

---

## 12. Tech stack (locked)

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Web + API | Django 5 + **django‑ninja** |
| Admin console | Django admin |
| Calculation layer | **dbt‑core** (+ dbt tests & docs) |
| Projections | Python + numpy |
| Database | PostgreSQL 16 |
| Migrations | Django migrations (app tables) + dbt (marts) |
| Excel parsing | pandas + openpyxl |
| Google Sheets | gspread + google‑auth (service account) |
| Import validation | Pandera |
| Scheduling | host **cron** → Django management commands |
| Logging | structlog (JSON) + `ingest_log` table |
| Config | django‑environ / `.env` (secrets never committed) |
| Packaging / env | **uv** |
| Deployment | Docker Compose (app + postgres) on the **Eko Dell PowerEdge R730**, per the **r730-deployment-standards** house skill — see §12a |
| Tests | pytest + pytest‑django |
| Lint / types | ruff + mypy + pre‑commit |
| Errors (optional) | Sentry |

Rationale summary: data volume is small; the real complexity is **business rules** and **messy spreadsheet input**. Django gives a free admin/ops console + migrations + structure; dbt makes the incentive math **tested, documented, and version‑controlled** (it governs money); everything else is deliberately boring and proven.

### 12a. R730 deployment contract (house standard)

The internal server is a Dell PowerEdge R730, governed by Eko's `r730-deployment-standards` house skill. Confirmed facts (resolves former Q7):

| | |
|---|---|
| Server LAN IP | `192.168.1.4` |
| Project root on server | `/opt/projects/<project-name>` |
| Public traffic | **Nginx** (already on the server) terminates HTTP/HTTPS (80/443) and proxies to `127.0.0.1:<host-port>` — **no Caddy, no TLS in our container** |
| SSH | external `2223` → internal `22` |
| Our port range | **8200–8299** (backend/API) — exact port **assigned by the administrator**, never invented |
| Reserved (never use) | `22, 80, 443, 8000, 9443` |
| Docker | prepared by us, **deployed by an authorized administrator**; no Docker‑socket access for the app |
| Database | Postgres **must not** publish a host port — internal Compose network only |
| Domain | `<project-name>.developer.eko.co.in` convention — exact domain pending admin |

**Project name (kebab‑case, needed for the path/domain/container names):** proposed `csp-balance-tracker` — confirm or rename.

**Consequences for §11/§12:**
- Drop Caddy from the stack; the container binds only to `127.0.0.1:${APP_PORT}` and R730's Nginx does the rest.
- `compose.yml` follows the standard pattern (named volumes, `restart: unless-stopped`, healthcheck, non‑root user, `.env` via `env_file`).
- Deliverables must include `docs/DEPLOYMENT.md` (service list, ports, volumes, health check, domain, rollback) and an **admin handoff** block, in the format the skill specifies, before this project can be called deployment‑ready.
- Still open: exact `APP_PORT` assignment and final domain — both pending the R730 administrator.

---

## 13. Non‑functional requirements

- **Reliability:** all ingestion idempotent; a failed run does not corrupt state and the next run recovers; `/health` + alerting expose staleness.
- **Data quality:** Pandera on every import; `dbt test` must pass before marts are published (build to a staging schema, swap on success); rejected rows logged with reason.
- **Performance:** API p95 < 300 ms (reads from precomputed marts); full ingest + dbt run < 5 min.
- **Security:** API keys hashed at rest; HTTPS via Caddy; DB not exposed off‑host; Google service‑account JSON stored only on the server; `.env` for secrets.
- **Timezone:** `Asia/Kolkata` everywhere; "today" / "as‑of" is IST.
- **Auditability:** rule changes tracked in git via dbt models; `monthly_summary` can be snapshotted per month‑end.
- **Backups:** nightly `pg_dump`, retain 30 days.
- **Portability:** no cloud‑managed services required; runs fully on the internal server.

---

## 14. Scheduling (indicative, IST — tune to source refresh times)

| Time | Job |
|---|---|
| 07:30 | Ingest CALLING SHEET |
| 08:00 | Ingest newest TRANSACTION file from watched folder |
| 08:15 | `dbt run` + `dbt test`, then projection step |
| hourly | `/health` self‑check → log + alert if stale |

---

## 15. Assumptions

1. CALLING SHEET contains exactly one current "daily average balance" figure per CSP, refreshed daily.
2. CSP codes are stable over time and consistent (after normalisation) between the two sources.
3. ~~The internal server allows Docker, an outbound HTTPS connection to Google APIs, and running PostgreSQL.~~ **Confirmed** — R730 host runs Docker via admin, outbound HTTPS is standard; see §12a.
4. The daily transaction export lands in the watched folder reliably once per day.
5. A CSP with no balance row on a given day carries its last known balance forward (standard bank EOD practice) — **to be confirmed** (Q3).
6. The API has a small number of trusted internal consumers; a single shared read key (plus an admin key) is sufficient initially.

---

## 16. Open questions

| # | Question | Blocks |
|---|---|---|
| 1 | ~~CALLING SHEET exact columns~~ **Resolved (2026-09-13)** — worksheet "Calling Sheet New" (row 2 = headers, data from row 3): `CSP ID`, `CSP Name`, `CSP Mail ID`, `CSP Mobile number`, `Total Accounts`, `Total Balance`, `Avg Balance`, `Amount to be deposited (for eligiblity/higher slab)`. Currency cells are Indian-formatted text (`"₹ 34,00,000"`); one current-value snapshot per CSP, no dated columns — we build history by snapshotting daily. See `app/ingestion/calling_sheet_ingest.py`. | DB schema, ingester |
| 2 | ~~Is "daily average balance" per‑account or aggregate?~~ **Resolved** — per-account: sheet's `Avg Balance` = `Total Balance` / `Total Accounts` (confirmed on live data, e.g. an example CSP: ₹34,00,000 / 981 = ₹3,466 — illustrative figures, not a specific real CSP's). `account_count` (`Total Accounts`) is ingested, so `projected_incentive_annual` computes for every CSP with data. | FR4 incentive field |
| 3 | MAB divisor — **calendar days in month** or **days‑with‑data**? Missing‑day handling — carry forward, zero, or skip? | Calculation engine |
| 4 | **Weekly** (per SBI text) vs **monthly** (per Eko practice) averaging — which governs the actual payout? | Rule semantics |
| 5 | History files — format, location, granularity, columns, which months? | Backfill |
| 6 | Transaction file — folder path, exact filename pattern, drop time, owner of the export? (Scope resolved: `Success` sheet, the 7 allow‑listed types.) | Watched‑folder ingester |
| 6b | Activity — is a per‑CSP per‑day **count** (+ amount, by type/category) enough, or does this feed a further calculation? | `daily_activity` shape |
| 7 | ~~Internal server — OS, Docker allowed, dedicated Postgres allowed, outbound internet, TLS/domain?~~ **Resolved** — R730, see §12a. Still pending: exact `APP_PORT` (8200–8299 range) and final domain, both admin‑assigned. | Deployment |
| 8 | ~~Google Sheet — service account + Sheet ID?~~ **Resolved** — sheet shared with `csp-balance-tracker@eko-502406.iam.gserviceaccount.com`, ID `1KHy7ejiUWZg9EQA0x4lm0axnVS5Hpiw1vLruc-0jSOg` (see `.env.example`, kept out of git — the ID itself isn't secret but is deployment config). | CALLING SHEET ingester |
| 9 | "Overall growth" headline metric — avg MAB across CSPs, total book, or % compliant? | `/overview` |
| 10 | Incentive cap is annual — do we pro‑rate monthly for the projection? | FR4 |
| 11 | Raw transaction retention period? | Storage/ops |
| 12 | Alerting channel — email / Slack / Google Chat? | FR7 |
| 13 | How many months of history should the API expose for trend? | API |
| 14 | Any need for per‑CSP API keys, or single read key + caller passes `csp_code`? | Auth |

---

## 17. Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Source sheet schema drift (columns edited) | Ingest breaks / bad data | Pandera fails the run safely, alert; no partial writes |
| Transaction file late/missing | Stale activity data | `/health` staleness alert; API returns last good data with `stale: true` |
| CSP code mismatch between sources | CSP missing from rollup | Normalise on `CSP Code New` rule; mapping table; unmatched‑code report |
| Weekly vs monthly ambiguity | Wrong incentive expectation | Resolve Q4 before go‑live; engine supports both windows |
| Per‑account vs aggregate balance misread | Wrong slab / incentive | Resolve Q2 before go‑live |
| Projection misleading early in month | Ops over/under‑reacts | Label clearly as projection; expose `days_with_data`; widen method after more data |

---

## 18. Milestones (AI‑assisted solo dev, indicative)

| Milestone | Scope | Est. |
|---|---|---|
| **M0** | Repo scaffold (uv, Django, Docker Compose, Postgres, ruff/mypy/pytest), config, base schema + migrations, `/health` | 1–2 d |
| **M1** | CALLING SHEET ingester + Pandera + history backfill → `csp`, `daily_balance` populated | 2–3 d |
| **M2** | Transaction watched‑folder ingester + dedupe → `daily_activity` (dbt) | 2–3 d |
| **M3** | Calculation engine — `monthly_summary` (dbt) + projection module + slab/gap/trend, with unit tests | 3–4 d |
| **M4** | API — overview, csps, csp detail, history, activity, admin endpoints; API‑key auth; OpenAPI docs | 3–4 d |
| **M5** | Django admin polish, observability, alerting | 1–2 d |
| **M6** | Deploy to internal server, smoke test, handover doc + API contract to dashboard team | 1–2 d |

---

## 19. Out of scope (restated)

Front‑end/charts · CSP login/auth · write‑back to SBI or the sheet · real‑time/intraday · payout accounting · non‑SBI‑kiosk programs.

---

## Appendix A — sample transaction file analysis

`Transaction Aug'26 (4).xlsx`:
- Sheets: `GTV` (daily pivot, Jul + Aug side by side), `Success` (306,035 rows), `Failed` (108,367 rows).
- Columns: `KO ID`, `Transaction Date & Time`, `Reference Number`, `Type of Transaction`, `From Account`, `To Account`, `Amount`, `Status`, `New Date`, `CSP Code New`.
- **493** distinct CSP codes; dates 01–31 Aug 2026.
- `CSP Code New` = normalised `KO ID` (KO ID sometimes has extra trailing digits).
- Pool/settlement account `XXXXXX71556` in ~99% of rows.
- 21 transaction types total (AEPS ONUS/OFFUS Withdrawal & Deposit, Money Transfer, ATM On/Off‑us, BBPS, PPF/SSA, Initial/Loan deposit, IMPS, YONO, …).
- **In‑scope subset used by the tracker** — the 7 allow‑listed types (Success sheet): **220,098 rows · 481 CSPs · 31 days** (of 306,035 Success rows).

  | Type | Count | Amount | Direction |
  |---|--:|--:|---|
  | AEPS ONUS Withdrawal | 189,586 | ₹119.14 Cr | into pool |
  | ATM Onus Withdrawal | 6,665 | ₹4.11 Cr | into pool |
  | Withdrawal | 8,306 | ₹2.26 Cr | into pool |
  | YONO WITHDRAWAL | 10 | ₹0.07 Cr | into pool |
  | AEPS ONUS Deposit | 13,219 | ₹14.14 Cr | out of pool |
  | Deposit | 411 | ₹0.34 Cr | out of pool |
  | AEPS ONUS Fund Transfer | 1,901 | ₹4.09 Cr | neither |
  | **Total** | **220,098** | **₹144.08 Cr** | in ₹125.5 Cr · out ₹14.5 Cr · neither ₹4.1 Cr |

- `Failed` sheet has stray `@ State Bank of India …` header rows and some non‑numeric `Amount` — **not used** (Success sheet only).
- **No account balances present** → CALLING SHEET is the sole balance source.
