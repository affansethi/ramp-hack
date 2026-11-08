# Tax Leak — Architecture (gate A1)

Authoritative inputs: the reconciled Design Spec (product rules) and Technical Spec v2 "Tax Leak updated Affan" (build plan). Technical Spec v1.5 in the same file is superseded and must not be implemented. Where the two active documents disagree, the Design Spec wins.

## 1 Stack

| Layer | Choice | Notes |
| :---- | :---- | :---- |
| Language | Python 3.12+ | Single backend codebase; API and worker share one image/process model |
| API | FastAPI + Pydantic v2 | Typed contracts, request validation, JSON Schema export for golden examples |
| Frontend | Static React build, served by FastAPI from `app/static` | One origin; no server-side proxy |
| Worker | Background thread in the same process, polling the Postgres jobs table with leases | Exactly one gunicorn worker process, so one poller |
| Database | Managed PostgreSQL Flexible Server (hosted), SQLAlchemy + Alembic; SQLite optional local-only | SQLite never hosts shared state; App Service `/home` is network storage and SQLite WAL is unsupported there |
| Documents | Existing Azure Blob account `ramphackaffanandrj26`, container `data`, managed identity | SHA-256 document identity; frozen exports |
| Model | Direct provider API behind `DocumentExtractor` (`openai`, `anthropic`, `google`, `replay`); target `gpt-4.1` with strict JSON schema, pinned and verified before live use | `LLM_MODE=replay` on stage and in CI; no Azure OpenAI dependency |
| Engine | In-process `TaxEngine` over owner-authored `rules/*.yaml` + rate tables; WA DOR rate lookup cached per address/quarter | The only source of treatment, tax-decision amounts and direction |
| Hosting | One Linux App Service in `data-rg`, West US 2; F1 while building, B1 with Always On for the demo | Startup: `gunicorn -w 1 -k uvicorn.workers.UvicornWorker app.main:app` |
| CI/CD | One GitHub Actions workflow (`.github/workflows/deploy.yml`): lint, unit/rule tests, never-fail gates, replay eval; deploy to App Service on `main` via OIDC (`azure/webapps-deploy@v3`) | GitHub holds no Azure secret |

Money is never a binary float. API money is `{"value": "10.00", "currency": "USD"}`; storage is NUMERIC or integer cents; engine decimal strings are copied byte-for-byte into ledgers, totals and exports.

Startup mode guards: the app refuses to start unless `DATA_MODE=synthetic` and `ENGINE_MODE=mock_reviewed_ruleset`; `LLM_MODE` is `live`, `record` or `replay`.

## 2 Modules

| Module | Responsibility | Hard boundaries |
| :---- | :---- | :---- |
| `app/contracts/` | Pydantic shapes for every API body, the money type, closed enums, AI output schemas, engine request/response; golden examples | No contract field may mean net, savings or recovered; AI schemas contain no field for rate, tax-decision amount, treatment, label, direction or verdict (violation fails with `FORBIDDEN_FIELD`) |
| `app/api/` | FastAPI routers: demo login/sessions/CSRF, org scoping, idempotency keys, expected-version checks, commands, read models, audit writes, webhook intake | No model calls or long work inside a request; no endpoint accepts a tax treatment, label, direction or tax-decision amount |
| `app/domain/` | Pipeline stages as pure-ish functions: ingest, deduplicate, admit facts (TG1), cross-check, classify, hold check, jurisdiction set, engine orchestration, review/approve, ledgers, totals, close, workpaper | Domain code never performs I/O directly; adapters are injected |
| `app/worker/` | Job poller: atomic claim, `lease_until`, retry limits, expired-lease recovery, transactional result write + completion | Never approves anything; survives restart without duplicate effects |
| `app/adapters/storage.py` | Blob reads/writes via `DefaultAzureCredential` (ported from the existing `data.py`) | No account keys |
| `app/adapters/extractor.py` | `DocumentExtractor`: Call A (extraction) and Call B (category + question drafts); providers `openai`/`anthropic`/`google`/`replay`; cache keyed by document hash + provider + model + prompt + schema version | Two bounded calls per document; 45 s timeout, two retries, then human fact entry; values exactly as printed, nulls for missing; "no tax line" ≠ "tax line of 0.00" |
| `app/adapters/engine.py` | `TaxEngine`: one request per line × jurisdiction; effective-date rule selection from `rules/`; returns SUPPORTED (treatment, expected tax, adjustment amount, optional direction, rule reference, rate, location code), UNSUPPORTED (no amount) or ERROR | Coverage only what reviewed `rules/` entries state; never emits an amount without a reviewed rule/rate reference; placeholder/unreviewed entries route NHR |
| `app/adapters/ramp.py` | Ramp-shaped simulator now, sandbox later via `RAMP_API_BASE`; HMAC-SHA256 verification of `X-Ramp-Signature`; event dedup; fetch transaction/receipt records | Webhook intake stores the event and returns 200; all work happens in the worker |
| `rules/` | Owner-authored YAML: rule entries (`effective_from/to`, `timing_basis`, `primary_source_url`, `review_status`, `reviewed_by/at`), the S3 required-fact catalog, rate tables | Agents implement the evaluator only; agents never author entries, rates or expected results. Only `reviewed` entries activate; `PLACEHOLDER:` entries stay inactive and route NHR |
| `fixtures/` | Synthetic documents, owner-signed expected results, model recordings | Synthetic only; agents never record expected results |

Pipeline (each stage writes audit events and keys output by immutable input version):
webhook/import → ingest → fetch → deduplicate → extract (model) → cross-check against Ramp `entity_amount`/`ocr.taxes` (over one cent: unconfirmed) → admit facts (location ladder rungs 1–5, bill-to never confirms use) → classify → hold check against signed S3 → jurisdiction set (undeclared state: whole-item NHR `UNDECLARED_JURISDICTION`) → engine call per line × jurisdiction (never for held/NHR) → response check (missing rule reference/malformed/timeout: determination error, never zero, never NHR-by-default) → review → outputs (journal entry draft, refund letter draft, workpaper lines) from approved current units only.

## 3 Data model

Conventions: every org-owned row carries `org_id`; UUID ids; UTC timestamps; supersedable records carry an increasing `version`; document/fact/determination history is frozen; audit rows are append-only.

- **Platform:** Organization; Person (seeded demo users, roles viewer/reviewer/setup); Setup (declared jurisdictions CA/WA/NY, period bounds WA 2026-10-01→2026-12-31, basis `invoice_date`); Job (kind, input version, status, `lease_until`, attempts, error code); IdempotencyKey; AuditEvent (actor, command, object + version, before/after hashes).
- **Intake:** SourceEvent (source, external event id, payload hash, signature result); Purchase (entity, vendor, normalized invoice number, invoice date, item status, Ramp transaction/bill ids — a card charge and a later bill for one invoice make one purchase); PurchaseLine (description, quantity, unit price, gross, discount, net, service period); Document (blob key, SHA-256, MIME, pages).
- **Facts/evidence:** Fact (subject, field key, value, origin `AI_EXTRACTED`/`HUMAN_SUPPLIED`/`RAMP_RECORD`, source, evidence rung 1–5, confirmation `CONFIRMED`/`UNCONFIRMED`, version, supersedes); Evidence (document, page, exact passage, provenance).
- **Catalogs:** RequiredFactCatalog (category, required keys, accepted rungs, status candidate/signed, `signed_off_by`); RuleSetEntry; RateRecord (jurisdiction, location, location code, rate, quarter, source URL, review status).
- **Holds/questions:** Hold (purchase, reason `MISSING_REQUIRED_FACT`/`UNCONFIRMED_REQUIRED_FACT`, named fact, active, releasing fact version); Question (hold, fact key, recipient, asked at, answer, state).
- **Results:** Determination (line, jurisdiction, facts version, rule-set version, label, NHR reason, direction, `derived_direction`, rate, location code, expected tax, engine amount, rule reference, unit status, supersedes); ReviewDecision (determination + version, action, approval path, actor, reason).
- **Ledgers/outputs:** LiabilityLine (amount copied from engine, approver, approved at); RefundCandidate (amount in question copied; only status `APPROVED_FOR_PURSUIT`, never cash); JournalEntryDraft; RefundLetterDraft (owner-approved basis text; never transmitted).
- **Workpaper:** Workpaper (filing key entity + jurisdiction + period, version, status `DRAFT`/`READY_TO_FILE_SIMULATED`, fingerprint, frozen at); WorkpaperLine → LiabilityLine. No transition back from ready; a later change creates a new draft version with a visible flag.

Closed enums (adding a value is a contract change needing both owners): Label (`ENGINE_SUPPORTED`, `NEEDS_HUMAN_REVIEW`); Direction (`LIABILITY`, `REFUND_CANDIDATE`, `CHARGED_CORRECTLY`); NHR reason (`NO_SUPPORTED_CATEGORY`, `UNDECLARED_JURISDICTION`, `ENGINE_UNSUPPORTED`, `RULE_TRANSITION_UNRESOLVED`); item status, unit status, approval path (must match direction), workpaper status, job status, modes.

Unique constraints: (org, source, external_event_id); (org, purchase_line_id, jurisdiction, facts_version, ruleset_version); (org, workpaper fingerprint) for ready versions. Invoice identity: entity + vendor + normalized invoice number; date and amount corroborate; ambiguous matches go to review, never a destructive merge.

Schema bans (tested in CI): no net/savings/recovered column or field (sourced invoice line-net facts and the mandatory "not recovered cash" disclaimer are allowed); no float money column; no human-writable treatment, label, direction or tax-decision amount column.

Invalidation: a fact, rule-set or determination version change supersedes the old approval; the reviewer approves the new version, never the old one. Earlier results are kept in the trail, never overwritten.

## 4 API contracts

Base `/api/v1`; JSON except uploads/exports. Server-side session; every lookup scoped to the session org; actor `person_id` from the session, never the body. Every mutation takes `Idempotency-Key` (same key + body returns the original response; same key + different body → 409). Commands on facts, determinations and workpapers carry the expected version. Every response carries ids, versions, `data_mode`, `engine_mode`, timestamps.

| Endpoint | Effect |
| :---- | :---- |
| `POST /webhooks/ramp` | Verify `X-Ramp-Signature` (mismatch → `BAD_SIGNATURE`); store SourceEvent; duplicate event id returns 200 with no new work |
| `POST /imports` | Multipart document or CSV → 202 with import + job ids |
| `GET /jobs/{id}` | Status, stage, retryable, error code |
| `POST /demo/events`, `POST /demo/reset` | Allowlisted simulated Ramp event / synthetic reset; demo mode only |
| `GET /purchases`, `GET /purchases/{id}` | Queues and item view (lines, facts + evidence links, holds, current and past determinations, available actions) |
| `POST /purchases/{id}/facts` | New fact version; supersedes approvals; re-check job. Rejects treatment/label/direction/tax-decision amount; allows sourced printed invoice-amount facts only |
| `POST /questions/{id}/answers` | Sourced answer → admitted fact + re-check; "I don't know"/redirect keeps the hold |
| `POST /determinations/{id}/review` | `approve` (with approval path matching direction) or `reject`; writes O1/O2 ledger; 409 stale; 422 held/NHR/error; refused for superseded or already-decided versions |
| `POST /determinations/{id}/refer` | `leave_open` or `send_to_advisor`; NHR only; no treatment recorded |
| `GET /totals` | Side-by-side figures: approved liabilities by jurisdiction; refund candidates ("not recovered cash"); held (count + "purchase amount, not tax"); NHR and error counts; unreviewed counts by direction. No net field |
| `GET /drafts/journal-entries`, `GET /drafts/refund-letters/{id}` | Drafts for approved units only |
| `POST /workpapers`; `POST /workpapers/{id}/mark-ready`; `GET /workpapers/{id}/export` | Draft from approved liabilities only (excluded section has no amounts; refund candidates on a separate list); mark-ready freezes with fingerprint (`NO_APPROVED_LIABILITY_LINES`, `STALE_VERSION` refusals); frozen export marked DRAFT/SIMULATED, formula-escaped CSV |
| `GET /audit-events`, `GET /health` | Permission-scoped history; health checks DB/storage/modes, never the model |

Error body: `error.code`, `message`, `field_errors`, `retryable`, `correlation_id`. Domain codes include `ITEM_HELD`, `STALE_VERSION`, `NEEDS_HUMAN_REVIEW_NOT_APPROVABLE`, `PATH_MISMATCH`, `ALREADY_DECIDED`, `BAD_SIGNATURE`, `FORBIDDEN_FIELD`. No refusal or error ever produces a result, an amount or a zero. No command exists to set treatment/label/amount, release a hold, approve in bulk, or net totals.

## 5 Folder layout (target)

```
app/
  main.py            FastAPI app factory; serves API under /api and static frontend
  api/               routers, deps (session, org scope, idempotency, versions)
  domain/            pipeline stages, totals, workpaper builder
  worker/            job poller (leases, retries, recovery)
  adapters/          storage.py, extractor.py, engine.py, ramp.py
  contracts/         Pydantic models + golden examples
  static/            built frontend (copied from web/ build output)
web/                 frontend source (React)
rules/               rule entries (yaml), S3 catalog, rate tables (owner-authored)
fixtures/            synthetic documents, expected results, recordings
tests/               unit/, rules/, gates/, api/, eval/
scripts/             az setup, seed, demo reset
docs/                specs, architecture, decisions
.github/workflows/   deploy.yml (existing; test/gate jobs added only when an issue asks)
```

## 6 Testing strategy

- **Unit tests** (`tests/unit`): domain stages, fact admission (bill-to never confirms; silent ≠ zero; partial seat coverage holds), dedup, supersession.
- **Rule tests** (`tests/rules`): evaluator against synthetic unit fixtures — liability, refund candidate, no adjustment, UNSUPPORTED/NHR, effective-date selection (Dec 2026 CA selects its signed rule; Jan 2027 routes NHR `RULE_TRANSITION_UNRESOLVED`), two-decimal USD string output.
- **Gate tests** (`tests/gates`, never-fail, block merge): RG-1 no held/NHR/unreviewed/error unit in any total and held never shown as zero; RG-2 no net/savings/recovered field or copy; RG-3 engine amounts copied exactly (decimal string comparison, no float on money paths); RG-4 every SUPPORTED result has a real reviewed applicable rule/rate reference, placeholders never support approval; RG-5 demo cases 1–7 against the API; RG-6 every citation resolves to an admitted source and every active rule to a reviewed primary-source URL; RG-7 no duplicate effects (duplicate webhook, card + bill, duplicate mark-ready, worker restart with unfinished job); RG-8 no "filed"/"paid" without "simulated"; RG-9 effective dating; H1/H5 enum and endpoint-refusal tests.
- **API tests** (`tests/api`): cross-org denial, stale-version refusal, idempotency-key behavior.
- **Eval** (`tests/eval`): replay-mode extraction against golden fixtures; model output never contains rate, tax-decision amount, treatment, label, direction or verdict.
- **Fixtures:** 20–30 synthetic goldens with owner-signed expected results (RJ extraction, AF engine/state transitions). Agents never record expected results.
- **CI:** one workflow; required checks `test` and `gates`; deploy to App Service only on `main` via OIDC.

## 7 What to build first

Dependency order from Technical Spec v2 §15 (one issue, one PR):

1. **Skeleton + contracts:** FastAPI app serving API + static frontend, managed Postgres (SQLite local-only), durable jobs/worker, closed schemas + golden shapes, deploy workflow green on `/health`. Migrate the existing blob helper into `app/adapters/storage.py`.
2. **Rule/rate tables + engine** (CA/WA/NY, effective dates) with unit tests — evaluator only; owner signs entries and goldens separately.
3. **S3 catalog:** RJ drafts, AF signs (gates fact admission and end-to-end supported acceptance only).
4. **DocumentExtractor:** provider interface, strict schema, forbidden-field check, replay cache.
5. **Fixtures + golden set** (owner-signed expected results for Cases 1–7).
6. **Ramp simulator + webhook intake** (signature, dedup, fetch) → Case 7 passes.
7. **Fact admission, OCR cross-check, hold check, jurisdiction set, questions** → Case 6 holds correctly.
8. **Review queue, approvals, totals, journal-entry and refund-letter drafts** → Cases 1–5 end to end.
9. **WA Q4 2026 workpaper, mark-ready, export** → RG-1, RG-8.
10. **Never-fail gates + eval table** → Cases 1–7 match owner-signed outputs in CI.
11. **Demo hardening:** replay mode, judge interaction (Case 3 → Case 4 fact edit), backup recording.

Never cut: H1–H5; the Case 6 hold; Case 3 approved refund candidate with separate total and DRAFT letter; Case 4 safe unresolved-transition NHR; RG-1–RG-9; DRAFT and SIMULATED labels; labeled replay fallback.
