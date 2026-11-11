# Tax Leak — Architecture Decisions (gate A1)

Each entry: the choice, the reason, and the consequence. Sources: Design Spec (product rules) and Technical Spec v2 (authoritative build plan). v1.5 is historical only.

## D1 — Build against Technical Spec v2, ignore v1.5

The technical spec file contains two plans. The v1.5 section is explicitly marked "SUPERSEDED — historical only" and v2 §16 lists every change (hosting, model, region, database, frontend, worker, coverage, period, filing, CI, ingest). Mixing them would produce contradictions (e.g. v1.5's CA/WA-only Container Apps design vs v2's CA/WA/NY single App Service). Consequence: every architecture statement traces to v2 or the Design Spec; where they disagree, the Design Spec wins and v2 is corrected, per v2 §1.

## D2 — Replace the Flask app with FastAPI (and what is preserved)

The existing app (`app.py`, `data.py`, `templates/`, `static/`) is a Flask blob-listing page. The target system needs typed request/response contracts with golden examples, strict JSON-schema AI output validation, and Pydantic-shaped domain payloads — FastAPI + Pydantic is the spec'd stack (v2 §4, §7) and gives that validation for free; Flask would require rebuilding it by hand.

Preserved, not discarded:

- **`data.py`'s storage pattern** (`DefaultAzureCredential`, no keys, account `ramphackaffanandrj26`, container `data`) ports directly to `app/adapters/storage.py`. The account and container already match v2 §9, so no infrastructure change is needed for documents.
- **The deploy workflow** (`.github/workflows/deploy.yml`: OIDC login, `azure/webapps-deploy@v3`, Oryx build, gunicorn) already matches v2's CI/CD model — one workflow, deploy on `main`, no Azure secrets in GitHub. It stays as-is until an issue asks to add the `test`/`gates` jobs.
- **The App Service hosting model** (Linux App Service, West US 2 region group, free-tier-now/B1-for-demo) is exactly what v2 prescribes; the README's operational notes (managed identity, role assignment lag, cold start) carry over.

Consequence: Flask leaves the dependency list when the skeleton lands (work item 1); Jinja templates and the vanilla JS page are superseded by the static React frontend served from `app/static`.

## D3 — One App Service, one process, in-process worker

v1.5's three Container Apps + ACR + Key Vault + per-PR environments cost roughly $158–$720/month against a $75 budget and $100 student credit (v2 §9). v2 collapses this to a single Linux App Service running FastAPI, the static frontend and a background worker thread. Consequence: durability that a separate worker would provide must come from the Postgres jobs table — atomic claim, `lease_until` leases, retry limits, expired-lease recovery — with exactly one gunicorn worker process so leases are never contended by two pollers. F1 is fine while building; B1 with Always On three days before the demo (F1 unloads after 20 idle minutes, which would stop the worker).

## D4 — Managed PostgreSQL hosted, SQLite local-only

App Service `/home` is network storage and SQLite explicitly does not support WAL over network filesystems (v2 §9 cites both vendor docs), so a hosted SQLite file is not a viable system of record. Consequence: hosted state lives in managed PostgreSQL Flexible Server over TLS; durable jobs, idempotency keys and the append-only audit log all live there. Local development may use SQLite as ephemeral scratch, never as the system of record.

## D5 — Static React frontend served by FastAPI

v1.5 used a Next.js server with an `/api` runtime proxy and a six-check proxy gate. v2 replaces it with a static build served from the same origin: one origin means first-party cookies, no proxy to get wrong, and one less service. Consequence: frontend sources live in `web/`; the build output is copied to `app/static`; the UI never contains tax logic and never trusts a browser-supplied amount.

## D6 — Deterministic in-process rule engine over `rules/*.yaml`

Hard rules H1/H5 and the financial invariants require that treatment, tax-decision amounts and direction come only from an owner-reviewed, effective-dated rule set — never from the model and never from a person. Consequence: `TaxEngine` is in-process (no network, fully reproducible by hand from rule and rate tables); engine decimal strings are copied exactly into responses, ledgers and exports; only `reviewed` entries activate; `PLACEHOLDER:` entries and unresolved transitions route NHR with no amount; a missing rule reference in an otherwise supported response is a determination error, never zero.

## D7 — Direct model API behind `DocumentExtractor`, replay by default

v2 drops the Azure OpenAI dependency: a provider interface with `openai` (target, `gpt-4.1` pinned once availability is verified), `anthropic`, `google` and `replay` providers. `LLM_MODE=replay` serves recorded responses and is the mode for CI and the stage demo; `record` captures new ones; `live` calls the API. Consequence: the demo never depends on model availability or quota (the labeled replay fallback is a "never cut" item), extraction output stays strictly schema-bound with the forbidden-field check, and no hosted model credential exists — one `MODEL_API_KEY` app setting, used only in live mode.

## D8 — Money as decimal strings end to end

Engine amounts are copied byte-for-byte; the app never rounds, adjusts, recomputes or fills in. Consequence: `Decimal` in domain code, `{"value": "10.00", "currency": "USD"}` on the wire, NUMERIC/integer-cents in Postgres, and a schema ban (tested in CI) on float money columns and on any net/savings/recovered field — with the explicit carve-outs that sourced invoice line-net facts and the mandatory "not recovered cash" refund label are allowed.

## D9 — Two-phase gating: skeleton first, verified content second

**Superseded 2026-11-10 by D12** (the agent-activation route below no longer applies). Earlier amendment: content activates through owner review **or** agent verification (`docs/content-verification.md`); owner sign-off is no longer required. AF reviewed the rules, rates, rounding basis and filing-basis text on 2026-11-09 (PRs #96, #98); agents audit those with evidence and verify the catalog and goldens. The two-phase gating itself is unchanged.

The S3 catalog, rule entries, rates and golden expected results are owner-authored (RJ drafts, AF signs); agents build structures and evaluators only. To keep parallel work unblocked, signing gates only the dependent stages: skeleton/contracts, extractor scaffolding, the engine evaluator, the simulator and mock UI can all land before sign-off; fact admission, hold checks, end-to-end supported acceptance and the case goldens wait. Consequence: the build order in `docs/architecture.md` §7 follows v2 §15 exactly, and "agents never record expected results" is enforced as a repo rule in `AGENTS.md`.

## D10 — `.github/` is frozen by default

The deploy workflow touches live infrastructure (OIDC identity, the production web app). v2's CI additions (required `test` and `gates` checks, replay eval) belong in that same file eventually, but only as their own gated work item. Consequence: `AGENTS.md` carries the standing rule "do not edit `.github/` unless the issue asks"; gate jobs are added when the corresponding issue lands, not opportunistically.

## D11 — WA Q4 2026 as the demo period

v1.5 used WA Q3 2026; v2 moves to Q4 2026 (2026-10-01 → 2026-12-31) because the Q3 return was already due 2026-11-02. Period assignment is by invoice date, independently of the rule's effective-date selection — which is what makes Case 4 (December 2026 invoice, January 2027 service) a clean timing-isolation test that must route NHR `RULE_TRANSITION_UNRESOLVED`. Consequence: Setup stores the period bounds; boundary invoice dates (2026-09-30, 2026-10-01, 2026-12-31, 2027-01-01) are fixture requirements.


## D12 — Spec §3 governs tax-content activation (2026-11-10, AF)

RJ drafts, AF signs, agents never activate. Agents may draft and verify sources (`docs/content-verification.md`), but only owner values (`reviewed_by: AF`, `signed_off_by: AF`) activate an item. Reason: a single accountable signer for tax content; the agent-verification route contradicted the Design Spec and spec §3. PRs #114 and #117 were closed. `AGENTS.md` and `docs/content-verification.md` updated to match. Supersedes the D9 amendment.

## D13 — VARCHAR decimal-string money storage accepted for the demo (2026-11-10, AF)

Spec §4/§5 call for NUMERIC or integer cents. The demo keeps `String(32)` decimal strings. Condition: no SQL query may sum, average, compare or sort on a money column; money arithmetic happens in Python `Decimal` only. Enforced by `tests/gates/test_money_sql.py`. Reason: changing every money mapping before the demo risks the exact-string invariant (RG-3).

## D14 — West US 3 for the App Service and Postgres (2026-11-10, AF)

Spec §9 names West US 2. The F1 App Service quota there was 0, so the plan and web app run in West US 3; managed Postgres will sit next to them in West US 3, resource group `data-rg`. Storage stays in West US 2.

## D15 — Two workflows (2026-11-10, AF)

Spec §11 describes one workflow. Accepted: `ci.yml` (lint, types, tests, gates on pull requests and `main`) and `deploy.yml` (deploy `main`). The `deploy.yml` sanity step now uses Python 3.12 to match the runtime. This is the item that lifts D10's `.github/` freeze for that one line.

## D16 — Fetch failures and ambiguous dedup stay visible (2026-11-10, AF)

Ren's 2026-11-09 decisions are accepted only with visibility: a fetch failure ends as a FAILED job (no new hold reason) and an ambiguous invoice match keeps both purchases with an audit row, but neither may disappear from what the reviewer sees. Both surface on the review list with a status and reason (brief Task 7), never with a tax amount or zero, and never in a total.

## D17 — Item status enum shrinks to three values (2026-11-10, AF)

Spec §5 lists eight item statuses; the build uses `HELD`, `NHR_PRE_DETERMINATION` and `DETERMINED`. The spec shrinks to those three; the other five are not built.

## D18 — Environment variables (2026-11-10, AF)

`RAMP_API_BASE` and `SESSION_SECRET` are dropped from spec §10 (the simulator is in-process; sessions are server-side rows). `MODEL_PROVIDER`, `MODEL_NAME` and `MODEL_API_KEY` are implemented with the provider adapter (brief Task 14), with no default model in code. The spec's `gpt-4.1` target is treated as deprecated and is not pinned; RJ picks and pins a current model.

## D19 — One record run, replay on stage (2026-11-10, AF)

One `LLM_MODE=record` run over synthetic fixtures only, after RJ supplies the pinned model and key; replay recordings must come from that real run, never written by hand. The demo itself stays on replay.
