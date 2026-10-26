# CSP Balance Intelligence API: integration guide

For developers who want to show CSP-wise data in their own project. Read-only. For the full
endpoint list open the interactive docs at `<base>/docs`; error shapes are in `docs/API-ERRORS.md`.

## 1. Connection

| | |
|---|---|
| Base URL | `https://indev.eko.in/csp-balance-intelligence/api/v1` |
| Auth | header `X-API-Key: <your key>` on every request |
| Format | JSON (`Accept: application/json`) |
| Access | **Read-only.** Every endpoint is `GET`; nothing can be changed through the API |

You get a key from the CSP Balance Intelligence team. It is shown once and cannot be recovered;
if it is lost or leaked, ask for a new one and the old one is revoked.

**Keep the key on your server.** The API does not allow browser (CORS) calls, and a key placed in
front-end code is visible to every visitor. Your backend calls this API; your front end calls your
backend.

```
Browser  ->  your backend (holds the key)  ->  this API
```

## 2. Try it

```bash
export API=https://indev.eko.in/csp-balance-intelligence/api/v1
export KEY='<your key>'

curl -sS "$API/csps?limit=5" -H "X-API-Key: $KEY" | python3 -m json.tool
```

## 3. CSP-wise endpoints

| What you want | Endpoint | Main fields per CSP |
|---|---|---|
| Monthly list | `GET /csps` | `csp_code, name, mtd_mab, projected_mab, slab, trend_flag, is_eligible` |
| One CSP | `GET /csps/{csp_code}` | the above, plus `mobile, account_count, status` |
| Latest balance | `GET /csps/{csp_code}/balance` | `date, daily_avg_balance, source` |
| Balance history | `GET /csps/{csp_code}/balances/history` | `date, daily_avg_balance, source` |
| Today vs yesterday, every CSP | `GET /bank-flow/csp-day-compare` | accounts, withdrawals, deposits, net, transactions, SBI average (`observed_*`), our estimate (`estimate_*`), each for today and yesterday with the change |
| Today vs yesterday, one CSP or the whole network | `GET /bank-flow/day-compare?csp=<code>` | the same figures, one CSP, or the network when `csp` is empty |
| Growth explorer | `GET /bank-flow/csp-growth` | accounts, flows, observed and estimated average balance |

Useful parameters: `limit` (1 to 500, default 50) and `offset` for paging; `month` on `/csps`;
`date_from` and `date_to` (YYYY-MM-DD, at most 366 days apart) on history; `search`, `sort`,
`order` on `csp-day-compare`.

List endpoints answer with:

```json
{ "data": [ { "csp_code": "1A85XXXX", "name": "…" } ],
  "pagination": { "limit": 50, "offset": 0, "total": 527 },
  "request_id": "…" }
```

Single-item endpoints answer `{ "data": { … }, "request_id": "…" }`. Page through a list by
raising `offset` by `limit` until `offset >= total`.

## 4. Rules your front end must follow

- **`null` means "no figure for that day", not zero.** Show "not available" (or similar), never `0`.
- Today's money movement (`withdrawals_today`, `deposits_today`, `net_today`) is **so far** until the
  bank statement for the day arrives. When the response says it is not like-for-like
  (`like_for_like_money` is false), do not show a "more/less than yesterday" comparison.
- `observed_*` is the figure SBI sent. `estimate_*` is our own estimate and is **not** an SBI
  number; label it as an estimate everywhere you show it.
- A CSP code is `1A85` followed by four digits.

## 5. Errors and limits

| Status | Meaning | What to do |
|---|---|---|
| 401 | key missing, wrong or revoked | check the `X-API-Key` header |
| 403 | key valid but lacks the scope for this endpoint | ask for that scope |
| 404 | unknown CSP code, or no data yet for it | check the code |
| 422 | bad parameter (date, `limit`, `sort`) | fix the field named in `errors[].loc` |
| 429 | more than the allowed requests per minute (default 120 per key) | wait and retry with back-off |
| 500 | server error | report the `request_id` from the response |

Errors are JSON in the `application/problem+json` shape. Quote `request_id` when reporting a problem.
Cache what you can: the figures do not change second by second, so polling every few seconds gains nothing.

## 6. Minimal example

Backend (Node), holds the key:

```js
app.get("/api/csps", async (req, res) => {
  const qs = new URLSearchParams({ limit: req.query.limit ?? 50, offset: req.query.offset ?? 0 });
  const r = await fetch(`${process.env.CSP_API_BASE}/bank-flow/csp-day-compare?${qs}`, {
    headers: { "X-API-Key": process.env.CSP_API_KEY },
  });
  res.status(r.status).json(await r.json());
});
```

Front end, calls your backend only:

```js
const { data } = await (await fetch("/api/csps?limit=50")).json();
const rows = data.rows.map((r) => ({
  csp: `${r.name} (${r.csp_code})`,
  accounts: r.accounts_today ?? "not available",
  change: r.accounts_change ?? "not available",
  sbiAverage: r.observed_today ?? "not available",
  ourEstimate: r.estimate_today ?? "not available",
}));
```

## 7. Prompt to give an AI coding assistant

Paste this into your assistant, then add your own stack and what you want to build.

```text
Build a CSP-wise data view that reads from the CSP Balance Intelligence API.

API: https://indev.eko.in/csp-balance-intelligence/api/v1 (read-only, JSON).
Auth: header "X-API-Key" with a key read from the environment variable CSP_API_KEY.
The API has no CORS, so the key must live only on a backend that I write. The browser
must call my backend, never this API, and the key must never appear in front-end code,
logs or the repository.

Use GET /bank-flow/csp-day-compare for the table (one row per CSP, today vs yesterday).
Rows are in response.data.rows; paging uses limit (max 500) and offset, with
response.data.total. Use GET /csps/{csp_code} and GET /csps/{csp_code}/balances/history
(date_from, date_to as YYYY-MM-DD, max 366 days) for a CSP detail page.

Rules:
- A null field means "no figure for that day". Show "not available", never 0.
- Today's money (withdrawals_today, deposits_today, net_today) is "so far" until the bank
  statement arrives. If like_for_like_money is false, do not show a comparison with yesterday.
- observed_* is the SBI figure; estimate_* is our own estimate. Label the estimate as
  "our estimate, not an SBI number".
- Handle 401, 403, 404, 422 and 429 (retry with back-off); show the request_id of failures.
- Cache responses for about a minute on my backend.

My stack: <fill in>. Build: <fill in, for example a searchable CSP table with a detail page>.
```
