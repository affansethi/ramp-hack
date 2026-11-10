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

**Amended 2026-11-10:** owner sign-off is replaced by the agent verification protocol in `docs/content-verification.md` (primary sources, evidence files, research plus blind verification by a different lane and model family, `make verify-content` gate). Read "owner-authored" and "signs" below as "verified under that protocol". The two-phase gating itself is unchanged.

The S3 catalog, rule entries, rates and golden expected results are owner-authored (RJ drafts, AF signs); agents build structures and evaluators only. To keep parallel work unblocked, signing gates only the dependent stages: skeleton/contracts, extractor scaffolding, the engine evaluator, the simulator and mock UI can all land before sign-off; fact admission, hold checks, end-to-end supported acceptance and the case goldens wait. Consequence: the build order in `docs/architecture.md` §7 follows v2 §15 exactly, and "agents never record expected results" is enforced as a repo rule in `AGENTS.md`.

## D10 — `.github/` is frozen by default

The deploy workflow touches live infrastructure (OIDC identity, the production web app). v2's CI additions (required `test` and `gates` checks, replay eval) belong in that same file eventually, but only as their own gated work item. Consequence: `AGENTS.md` carries the standing rule "do not edit `.github/` unless the issue asks"; gate jobs are added when the corresponding issue lands, not opportunistically.

## D11 — WA Q4 2026 as the demo period

v1.5 used WA Q3 2026; v2 moves to Q4 2026 (2026-10-01 → 2026-12-31) because the Q3 return was already due 2026-11-02. Period assignment is by invoice date, independently of the rule's effective-date selection — which is what makes Case 4 (December 2026 invoice, January 2027 service) a clean timing-isolation test that must route NHR `RULE_TRANSITION_UNRESOLVED`. Consequence: Setup stores the period bounds; boundary invoice dates (2026-09-30, 2026-10-01, 2026-12-31, 2027-01-01) are fixture requirements.
