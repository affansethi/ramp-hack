# Demo runbook — Tax Leak (RAMP)

Tonight's path through cases 1–7, what each step must show, the exact
talking-point caveats, and recovery when something goes wrong. The live
stage recording itself stays with the owners; this file is the repeatable
part.

Every screen keeps the **"Synthetic demo data"** banner, and replayed model
output is always labeled **replay** — point at both before anything else.

> UI note: the served frontend is still the placeholder page
> (`app/static/index.html`). Until the React build lands, the demo runs
> through the API below. Each step gives the exact request and the response
> fields to show — treat each `GET` response as the "screen".

## 0. Preconditions (2 minutes)

Base URL: `https://ramphack-affan.azurewebsites.net` hosted, or
`http://127.0.0.1:8000` for the local backup (§7).

```bash
BASE=https://ramphack-affan.azurewebsites.net   # or http://127.0.0.1:8000
curl -s $BASE/health   # status ok; first hosted hit can take 30+ s (free tier)
```

Every request sends `X-Requested-With: taxleak`. Every mutation also sends a
unique `Idempotency-Key` plus the CSRF token from login (cookie session).

## 1. Start clean

```bash
make demo-reset DEMO_BASE_URL=$BASE   # local: make demo-reset
```

This wipes every demo row but keeps the seed org/reviewer, then verifies
zero purchases remain and all seven scenarios still answer. If the wipe is
interrupted, just run it again — the reset key replays safely.

Log in and keep the token (the cookie jar matters — the session cookie is
`HttpOnly` and never appears in the JSON):

```bash
LOGIN_JSON=$(curl -s -c jar.txt -H 'Idempotency-Key: login' \
  -H 'X-Requested-With: taxleak' \
  -H 'Content-Type: application/json' -d '{}' \
  $BASE/api/v1/demo/login)
TOKEN=$(LOGIN_JSON="$LOGIN_JSON" python3 -c 'import json,os; print(json.loads(os.environ["LOGIN_JSON"])["csrf_token"])')
```

Mutations from here use `-b jar.txt -H "X-CSRF-Token: $TOKEN"` and a fresh
`-H 'Idempotency-Key: <unique>'` per call.

## 2. Pre-demo check — which catalog state is live?

Ingest case 1 and read its queue:

```bash
curl -s -b jar.txt -H "X-CSRF-Token: $TOKEN" -H 'Idempotency-Key: ev-1' \
  -H 'X-Requested-With: taxleak' -H 'Content-Type: application/json' \
  -d '{"scenario":"case1"}' $BASE/api/v1/demo/events
PID=<purchase_id from above>
curl -s -b jar.txt $BASE/api/v1/purchases/$PID | python3 -m json.tool
```

If `queue` is briefly `processing`, wait a few seconds and re-GET — the
in-process worker is still advancing the job. Then:

- `queue == "held"` with `holds[0].named_fact == "required_fact_catalog"`:
  the catalog is still candidate (its current state in `rules/`). Every
  case below will hold at that same gate. Narrate §3 as the intended flow
  and demo the hold/replay mechanics (§4 cases 6–7 still work as written).
- `queue == "ready"` with a determination: the catalog is signed. Demo the
  full per-case screens in §3.

Never work around a hold by signing, editing, or copying rule files during
the demo — review signoff belongs to AF, and agents never author it.

## 3. Cases 1–5: the click-by-click path

Same three calls for every case — ingest, show the purchase, approve where
the rules allow it:

```bash
curl -s -b jar.txt -H "X-CSRF-Token: $TOKEN" -H 'Idempotency-Key: ev-N' \
  -H 'X-Requested-With: taxleak' -H 'Content-Type: application/json' \
  -d '{"scenario":"caseN"}' $BASE/api/v1/demo/events
curl -s -b jar.txt $BASE/api/v1/purchases/$PID
```

The table is the signed-catalog screen for each case (proven by
`tests/api/test_admit_replay.py`, `test_flow.py`, `test_totals.py`,
`test_workpaper.py`). "Tonight" is the candidate-catalog column until AF
signs.

| Case | Signed screen (demo target) | Tonight (candidate catalog) |
| :--- | :--- | :--- |
| 1 WA liability (SYN-1001, $1,440 SaaS, Seattle, $0 vendor tax) | `ready`. Determination: `ENGINE_SUPPORTED`, `LIABILITY`, `engine_amount`/`expected_tax` `151.92 USD`, `WA-RAS-001`, rate `0.1055`, location `1726`. Facts: `tax_charged 0.00 CONFIRMED`, `user_location WA` rung 1. Approve (`approval_path LIABILITY` + reason) → `GET /totals` shows WA `2026Q4` liability `151.92`; refund candidates stay `count 0`. | `held`, catalog gate, no determinations. |
| 2 CA vs WA (same terms, San Francisco) | `ready`. `ENGINE_SUPPORTED`, `CHARGED_CORRECTLY`, `expected_tax 0.00`, `engine_amount` null, `CA-RAS-001`. Approve with `CHARGED_CORRECTLY` — no amount enters any total. Contrast with case 1: same request shape, different jurisdiction/location evidence. | `held`, catalog gate. |
| 3 CA overcharge (SYN-1003, $962.32, $83 vendor tax) | `ready`. `ENGINE_SUPPORTED`, `REFUND_CANDIDATE`, `engine_amount 83.00`, `expected_tax 0.00`, `CA-RAS-001`. Before approval, refund candidates are `count 0`/`total` null. Approve with `REFUND_PURSUIT` → separate refund total `83.00` plus a DRAFT vendor letter that is never sent; liabilities unchanged; no journal entry for this purchase. | `held`, catalog gate. |
| 4 CA transition (same invoice, service Jan 2027) | `needs_human_review`. `RULE_TRANSITION_UNRESOLVED`; amount, direction and rule fields all null. `review` → `422 NEEDS_HUMAN_REVIEW_NOT_APPROVABLE`. Show `refer` (`leave_open` / `send_to_advisor`) instead — no treatment recorded. | `held`, catalog gate. |
| 5 NY hardware (SYN-1005, $2,100 laptop, NYC delivery, $0 tax, $0 shipping sourced) | `ready`. `ENGINE_SUPPORTED`, `LIABILITY`, `186.38 USD`, `NY-TPP-001`, rate `0.08875`, location `8081`. Approve with `LIABILITY`. | `held`, catalog gate. |

Approve call (cases 1, 2, 3, 5 only — 4 refuses, see above):

```bash
curl -s -b jar.txt -H "X-CSRF-Token: $TOKEN" -H 'Idempotency-Key: ap-N' \
  -H 'X-Requested-With: taxleak' -H 'Content-Type: application/json' \
  -d '{"action":"approve","approval_path":"LIABILITY","reason":"Reviewed the engine result","expected_version":1}' \
  $BASE/api/v1/determinations/$DET_ID/review
```

(`approval_path` must match the direction: `LIABILITY`, `CHARGED_CORRECTLY`,
or `REFUND_PURSUIT`. A wrong path is refused with `PATH_MISMATCH`.)

## 4. Cases 6–7: holds and replay (work in both catalog states)

**Case 6 — missing evidence.** HQ bill-to only, no confirmed user split:

- Screen: `queue == "held"`, `holds[0]` is
  (`MISSING_REQUIRED_FACT`, `user_location`), `determinations == []`,
  `purchase_amount_note == "purchase amount, not tax"`.
- Say: no engine call, no tax figure or zero, no approval, no total
  contribution. "I don't know" and silence keep the hold; only a sourced
  answer re-runs the full check.

**Case 7 — replay and recovery.** Same webhook identity as case 1:

```bash
# returns duplicate:true and the SAME purchase_id as case 1
```

- Screen: `duplicate: true`, `purchase_id` identical to case 1, totals
  unchanged. Restarting the worker with an unfinished job recovers its lease
  the same way — records and totals never double.

## 5. Finale — totals and the WA Q4 2026 workpaper

```bash
curl -s -b jar.txt $BASE/api/v1/totals | python3 -m json.tool
```

Show the side-by-side figures: `approved_liabilities.by_jurisdiction`
(WA `2026Q4`), `refund_candidates` (separate count/total — never netted),
`held` (count plus `purchase_amount` labeled "purchase amount, not tax"),
`open_needs_human_review` / `determination_errors` counts, and `unreviewed`
by direction with no amounts. No `net`, `savings`, or `recovered` field
exists anywhere.

```bash
curl -s -b jar.txt -H "X-CSRF-Token: $TOKEN" -H 'Idempotency-Key: wp-1' \
  -H 'X-Requested-With: taxleak' -H 'Content-Type: application/json' \
  -d '{"jurisdiction":"WA","period":"2026Q4"}' $BASE/api/v1/workpapers
```

Show: DRAFT from approved WA liabilities only (SYN-1001), an `excluded`
section with reasons and no amounts, and the refund list kept separate.
Then `POST /workpapers/{id}/mark-ready` freezes it as
`READY_TO_FILE_SIMULATED`; `GET /workpapers/{id}/export?format=csv`
downloads the frozen export, still marked simulated.

## 6. Talking-point caveats (say all three, verbatim in spirit)

1. **Synthetic demo data.** Every fixture, amount, and screen is synthetic;
   nothing here is tax advice or a filing position.
2. **Refund candidates are not recovered cash.** `APPROVED_FOR_PURSUIT`
   means a human approved pursuing the refund — it is not money back, and
   it is never netted against liabilities.
3. **Filing is simulated.** The workpaper moves DRAFT →
   `READY_TO_FILE_SIMULATED` only. Nothing is transmitted, no money moves,
   and no filed/paid state exists.

Also true if asked: extraction is replay-labeled on stage; rule evaluation
is deterministic; nobody in the demo sets a treatment, label, direction, or
amount — the engine (reviewed rules) is the only source of those, and the
endpoints refuse human-set values.

## 7. Local backup — demo without Azure

If App Service or Blob storage is unreachable, serve the same app locally on
SQLite in replay mode (no Azure credential is constructed):

```bash
make demo-local          # terminal 1: DATA_MODE=synthetic ... SQLite ... :8000
make demo-reset          # terminal 2, once the server is up: clean start
BASE=http://127.0.0.1:8000   # then follow §1+ as normal
```

`make demo-local` honors `DEMO_PORT` (default 8000). The local database file
is `taxleak-local.db` (gitignored scratch; hosted state stays Postgres).

## 8. Recovery cheat sheet

| Symptom | Recovery |
| :--- | :--- |
| Wrong step / confused state mid-demo | Re-GET the purchase (transient `processing`), or re-ingest with a fresh `Idempotency-Key`. Full restart: `make demo-reset` + §1 login (~1 min). |
| Suspected double-submit | Re-send with the same key: the stored response replays. Or run case 7 live to prove dedup. |
| Worker/app restarted mid-job | Do nothing on purpose: leases recover the job with no duplicate effects; totals prove it. |
| Azure down | §7 local backup; keep going on `http://127.0.0.1:8000`. |
| First hosted request hangs | Free-tier wake-up (30+ s): warm with `GET /health`, then start. |
| A case does not match §3 | Read the queue: `held` → name the missing fact (that IS the hold story); `needs_human_review` → name the NHR reason (that IS the safe stop); `error` → read `error_code`, never a zero. Do not force the target. |
