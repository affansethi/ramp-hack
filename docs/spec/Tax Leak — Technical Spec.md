# SUPERSEDED — Technical Spec v1.5 (historical only)

Do not implement instructions in this tab. Active build plan: Tab 2, “Tax Leak updated Affan” (v2), https\://docs.google.com/document/d/10ro4oO3jQUn7TE7Uyi3zvUe7GXxgS8-\_mDIJo3YOb94/edit?tab=t.i3o13hrrsymh . The reconciled Design Spec defines current product behavior.

## 1 Purpose and relationship to Design Spec

This is the build plan for the Tax Leak hackathon demo: architecture, minimum data model, API contracts, Azure hosting, CI/CD, tests, and the GitHub work breakdown. Parallel coding agents build against it. Affan Khurram Sethi (AF) and Ren Jie Tee (RJ) review pull requests, sign off where required, and approve contract changes.

The Design Spec owns every product rule: journeys, Cases 1–3, holds, refunds, labels, and simulated filing. If this spec disagrees with it, the Design Spec wins and this spec is corrected. Nothing here adds a product rule.

**Hard rules.** Every build decision must satisfy these.

| \# | Rule | Build consequence |
| :---- | :---- | :---- |
| H1 | Two labels only | Every result is `ENGINE_SUPPORTED` ("Reviewed rule / engine-supported") or `NEEDS_HUMAN_REVIEW` ("Needs human review"). No third enum value, field, or badge. |
| H2 | Holds are not a silent zero | A missing or unconfirmed required fact holds the item: no engine call, no approval, no tax amount, estimate, or zero. Errors and timeouts never become zero. |
| H3 | Refunds never netted | O1 liability totals and O2 refund-candidate totals are separate fields everywhere. No net, "savings," or "recovered" field or copy. |
| H4 | No live filing | `SIM-` states only. No outbound filing connection, no payment state, and no "filed" or "paid" text without "simulated." |
| H5 | No human-decided treatment | People approve, reject, supply or correct facts, leave an NHR item open, or send it to the advisor. No endpoint accepts a treatment, label, direction, or amount. |

**Financial invariants (release-gated in §7 and §9).**

- Totals never include held, NHR, unreviewed, rejected, superseded, or error units. Nothing counts until a named human approves it.  
- Engine amounts and rule references are copied exactly, as the engine's decimal strings. The app never rounds, adjusts, recomputes, or fills in a value.  
- Rule references are real reviewed-rule-set entry IDs or carry a visible `PLACEHOLDER:` prefix. An unmarked fake ID is a defect.  
- The model never determines tax. The mock engine produces every treatment and amount.

**Locked demo decisions.**

| Topic | Decision |
| :---- | :---- |
| Engine | Mock reviewed rule set: remote-access SaaS, CA and WA. Runs in-process. Returns UNSUPPORTED outside exact coverage. |
| S3 required-fact catalog | RJ drafts candidates; **Affan signs**. Entries stay candidates until signed. AI never authors S3 or rule entries. |
| Location evidence | Accepted: seat-assignment export or a named person's answer. Never accepted: bill-to, HQ, office, or entity address. |
| Direction | Copy the engine's direction. If absent, derive it from engine expected tax versus admitted tax charged and set `derived_direction=true`. |
| Period | WA, 2026-07-01 through 2026-09-30 inclusive, assigned by invoice date. Affan sets both in Setup. |
| Hold and NHR routing | Whole-item hold. Whole-item NHR when any confirmed location, or any state where tax was charged, is undeclared. Confidence never holds (HR-6 off). |
| Data | Synthetic documents only. They may be sent to the hosted vision model. |
| Unreviewed refund amounts | Visible on the review list and item view; never in any total. |

Stage codes used below: TG1 fact admission; W3 hold check; W5 engine call; W7 review; O1 approved-liability ledger; O2 refund-candidate register; O3 close; O4 simulated filing; HR-1 to HR-6 hold sub-reasons (HR-6 \= low confidence, off); IC-n adapter contract IDs; RG-n release gates (§7); EG-n eval gates (§9).

**P0 scope.** Synthetic document upload, fixed-schema CSV, and simulated Ramp events; one entity, USD; AI proposals only; holds, NHR, two labels, direction; versioned approval (no bulk); replay protection; WA workpaper with a separate refund list; simulated filing; 30 fixtures and a recorded fallback demo. **Out:** live Ramp (P1), partner or live engines, more states or categories, allocation, exemptions, partial refunds, FX, refund requests, live filing or payment.

## 2 Architecture

Browser ──HTTPS──▶ web: Next.js server (external ingress)

                      │  /api/\* runtime proxy (one origin; session \+ CSRF cookies first-party)

                      ▼

                   api: FastAPI (environment-internal ingress) ──TLS──▶ Postgres Flexible Server

                      │  auth, org scoping, commands, read models,        ▲  facts, results, ledgers,

                      │  audit, SAS minting                                │  jobs table (leases)

                      ▼                                                    │

                   Blob (private) ◀────────────────── worker: single poller (no ingress, exactly 1 replica)

                   evidence \+ frozen exports              ├─ DocumentExtractor ──▶ Azure OpenAI vision (managed identity)

                                                          ├─ TaxEngine ──▶ mock reviewed rule set (in-process)

                                                          └─ FilingAdapter ──▶ simulator (in-process, no egress)

migrate job (alembic upgrade \+ seed) ──TLS──▶ Postgres

All apps and jobs: images from ACR · secrets from Key Vault via managed identity · telemetry → App Insights / Log Analytics

| Component | Technology | Responsibility | Never |
| :---- | :---- | :---- | :---- |
| web | Next.js, React, TypeScript; standalone server | Views, two-second status polling, runtime `/api` proxy | Tax logic; trusting a browser-supplied amount |
| api | Python FastAPI, Pydantic | Authentication, org scoping, validation, commands, read models, audit writes, download SAS | Model calls or long work inside a request |
| worker | Same backend image, `worker` command; polls the Postgres jobs table with leases | Extraction, admission, reconciliation, hold check, engine calls, simulated filing steps, exports | Approvals; running more than one replica |
| Database | PostgreSQL Flexible Server; SQLAlchemy and Alembic | System of record, jobs, idempotency keys, append-only audit | Binary-float money |
| Blob | Private container | Original documents (SHA-256 identity), frozen exports | Public access; shared-key access |
| Vision adapter | `DocumentExtractor` → Azure OpenAI deployment | Fact candidates with evidence, category proposal, question drafts | Rates, amounts, labels, direction, approvals, tool use |
| Mock engine | `TaxEngine` over `rules/` | Treatment, expected tax, adjustment amount, rule reference | Coverage beyond CA/WA remote-access SaaS |
| Filing simulator | `FilingAdapter` | `SIM-` lifecycle | Network egress; any payment state |

**Vision model.** TARGET `gpt-4.1` with strict structured output; version pinned on the deployment at kickoff. Fallback: `gpt-4o`, then the GPT-5 family (a pin change only).

**Pipeline.** Each stage writes audit events and keys its output by immutable input version, so retries never duplicate output.

| Stage | Output | Stop condition |
| :---- | :---- | :---- |
| Ingest | Purchase, document hash, job | Invalid file, missing entity, non-USD |
| Deduplicate | Canonical purchase | Ambiguous match goes to review, never a destructive merge |
| Extract (AI) | Candidate values with evidence | Still invalid after one repair retry: human fact entry |
| Admit facts (TG1) | Versioned admitted facts | No resolvable source; a location from a non-accepted source stays unconfirmed |
| Reconcile | Lines \+ tax \+ charges − discounts vs invoice total | Difference over one cent: fact stays unconfirmed (hold) |
| Classify | Category accepted | Not remote-access SaaS: NHR `NO_SUPPORTED_CATEGORY` |
| Hold check (W3) | Held or complete | Any missing or unconfirmed S3 fact: whole-item hold |
| Jurisdiction set | Candidate states from confirmed locations plus states where tax was charged | Empty set: hold, never "no tax." Any undeclared state: whole-item NHR `UNDECLARED_JURISDICTION`, no engine call for any state |
| Engine call (W5) | One request per line × jurisdiction, confirmed facts only | Never sent for held or NHR items |
| Response check | Labeled determination | Missing rule reference, malformed, or timeout: determination error (retry, no label, no amount). UNSUPPORTED: NHR `ENGINE_UNSUPPORTED` |
| Review (W7) | Approved, rejected, or approved for pursuit | Held, NHR, error, stale version, unnamed actor |
| Close (O3) | Workpaper draft from O1 only | Period, mapping, or reconciliation gap |
| Simulated filing (O4) | `SIM-` events | Duplicate key, stale lock, integrity check |

**AI contract.** Two bounded calls per new document.

- **Call A, extraction (IC-2):** invoice identity and date, vendor, service period, currency, per-line quantity/unit price/gross/discount/net, tax charged as printed, document tax total, shipping, product and delivery terms, location statements with their printed label, and evidence references (document, page, exact passage, optional validated bounding box, provenance). Values exactly as printed, never computed; nulls for missing. "No tax line" (null) and "tax line of zero" (`0.00` with evidence) are different values.  
- **Call B, category and questions (IC-4, IC-8):** `REMOTE_ACCESS_SAAS` or `NO_SUPPORTED_CATEGORY` with rationale, evidence IDs, and unresolved S3 keys; question drafts asking for a seat-assignment export or a named person's answer, by state, for the invoice period. Any finer category hint is diagnostic only and never routes.  
- **Forbidden output:** no field for rate, expected tax, amount owed, label, direction, hold, approval, jurisdiction, or legal conclusion; any such key fails with `FORBIDDEN_FIELD`. Document text is data, not instructions. No tools, no browsing.  
- **Limits and failure:** PDF, PNG, JPEG; ≤ 10 MB and ≤ 10 pages; no encrypted or active content. Timeout 45 s; two retries honoring retry-after; then human fact entry with visible provenance. Never an invented value, estimate, or zero.  
- **Records:** cache by document hash \+ model \+ prompt \+ schema version; store model and prompt/schema versions, input hashes, output, latency, tokens. Confidence may be displayed but never routes.

**Amounts and totals.** API money is `{"value": "10.00", "currency": "USD"}`; storage is NUMERIC or integer cents; no binary float anywhere on a money path. Every SUPPORTED mock result carries its own adjustment amount: tax owed (liability), amount in question (refund candidate), or none (no adjustment). Derived direction: expected tax above admitted tax charged → `LIABILITY`; below → `REFUND_CANDIDATE`; equal → `CHARGED_CORRECTLY`. W5 never subtracts to create an amount. A zero tax charged is sent only when a sourced fact shows zero; a silent invoice holds when S3 requires the field.

| Figure (shown side by side, never combined) | Includes | Never includes |
| :---- | :---- | :---- |
| O1 approved liabilities, by jurisdiction and period | Approved, current liability units | Held, NHR, unreviewed, rejected, superseded, error, no-adjustment, or refund units |
| O2 refund candidates ("not recovered cash") | Units approved for pursuit | Liabilities; unreviewed refund results (count only) |
| Held | Count and "purchase amount, not tax" | Any tax figure |
| NHR; engine errors | Lists with reasons | Any amount |

A filing key with no approved lines shows "No approved liabilities," never a bare zero.

## 3 Minimum data model

Conventions: every organization-owned row carries `org_id`; IDs are UUIDs; timestamps are UTC, while invoice and period dates keep business meaning; supersedable records carry a monotonically increasing `version`; document, fact, and determination history is frozen; audit tables are append-only for the application role through grants.

| Area | Entity | Key fields |
| :---- | :---- | :---- |
| Platform | Organization, Membership | name; `person_id`; role `viewer`, `reviewer`, or `setup` |
|  | Setup | declared jurisdictions \[CA, WA\]; period bounds; basis `invoice_date`; scripted SIM outcome; `set_by` |
|  | Job | kind; input version; status; `lease_until`; attempts; error code |
|  | IdempotencyKey | org, operation, key, request hash, stored response |
|  | AuditEvent | actor (`person_id` or `component_id`); command; object and version; before/after hashes; correlation ID |
| Intake | SourceEvent | source; external event ID; payload hash |
|  | Purchase, PurchaseSourceLink | entity; vendor; normalized invoice number; invoice date; item status; card/bill/import links |
|  | PurchaseLine | purchase; line number; `batch_ref`; description; quantity, unit price, gross, discount, net |
|  | Document | blob key; SHA-256; MIME type; page count |
| Facts and evidence | Fact | subject (purchase or line); field key; value; origin `AI_EXTRACTED` or `HUMAN_SUPPLIED`; source `DOCUMENT_SPAN` or `PERSON_ANSWER`; confirmation `CONFIRMED` or `UNCONFIRMED`; version; supersedes |
|  | Evidence | document; page; exact passage; optional bounding box; provenance |
| Catalogs | RequiredFactCatalog (S3) | category; required keys; accepted source types; status `candidate` or `signed`; `signed_off_by` |
|  | RuleSetEntry | ID (real or `PLACEHOLDER:`); jurisdiction; category; effective interval; sources; dated rate record |
| Holds and questions | Hold | purchase; reason; sub-reason (HR-n); named fact; active flag; releasing fact version |
|  | Question | hold; fact key; recipient `person_id`; asked at; answer reference; state |
| Results and labels | Determination | line × jurisdiction; facts version; rule-set version; label; NHR reason; direction; `derived_direction`; expected tax; engine amount; rule reference; engine ID and mode; request/response hashes; unit status; supersedes |
|  | ReviewDecision | determination and version; action; approval path; actor; reason |
| Ledgers | LiabilityLine (O1) | determination and version; jurisdiction; period; amount (copied); approver; approved at; `in_totals` |
|  | RefundCandidate (O2 refund register) | determination and version; amount in question (copied); status `APPROVED_FOR_PURSUIT` (the only status; never cash); approver; approved at |
| Workpapers | TaxPeriod, CloseRecord | jurisdiction; start and end; basis; close state; confirmed-figures fingerprint |
|  | WorkpaperVersion, WorkpaperLine | filing key (entity \+ jurisdiction \+ period); version; sections; fingerprint; `locked_at`; line → LiabilityLine |
|  | CorrectionRecord | locked version affected; changed items; reason |
| Filing SIM-states | SimulatedFilingRun | locked workpaper version; idempotency key; SIM state; simulated acknowledgment ID; event timeline |

**Closed enums.** Adding a value is a contract change.

| Enum | Values |
| :---- | :---- |
| Label | `ENGINE_SUPPORTED`, `NEEDS_HUMAN_REVIEW` |
| Direction (`ENGINE_SUPPORTED` only) | `LIABILITY`, `REFUND_CANDIDATE`, `CHARGED_CORRECTLY` |
| Hold reason | `MISSING_REQUIRED_FACT`, `UNCONFIRMED_REQUIRED_FACT` (HR-1 to HR-6 are sub-reasons only; HR-6 off) |
| NHR reason | `NO_SUPPORTED_CATEGORY`, `UNDECLARED_JURISDICTION`, `ENGINE_UNSUPPORTED` |
| Item status | `INTAKEN`, `OUT_OF_SCOPE`, `FACTS_ADMITTED`, `CLASSIFIED`, `HELD`, `NHR_PRE_DETERMINATION`, `READY_FOR_DETERMINATION`, `DETERMINED` (`OUT_OF_SCOPE` \= failed an ingest stop: invalid file, missing entity, or non-USD; never used for category or state, which route to NHR) |
| Unit status | `REQUESTED`, `DETERMINATION_ERROR`, `NHR_ENGINE`, `SENT_TO_ADVISOR`, `READY_FOR_REVIEW`, `APPROVED`, `APPROVED_FOR_PURSUIT`, `REJECTED`, `SUPERSEDED` |
| Approval path | `LIABILITY`, `REFUND_PURSUIT`, `CHARGED_CORRECTLY` (must match direction) |
| Period close | `P-OPEN`, `P-PRECLOSE`, `P-CONFIRMED`, `P-FILING-SIM`, `P-CLOSED-SIM`, `P-CORRECTION` |
| Workpaper version | Draft → Locked. Lock and simulated-run start are one action; no transition unlocks |
| SIM run | `SIM-QUEUED` → `SIM-PREFLIGHT` → `SIM-SUBMITTED` → `SIM-ACKNOWLEDGED` → `SIM-ACCEPTED`; terminal `SIM-REJECTED` (scripted), `SIM-PREFLIGHT-FAILED`, `SIM-CANCELLED`, `SIM-HALTED` |
| Job status | `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED` |
| Modes | `data_mode=synthetic`, `engine_mode=mock_reviewed_ruleset`; the app refuses to start with anything else |

**Unique constraints.** (org, source, external\_event\_id); (org, source, external\_object\_id); (org, purchase\_line\_id, jurisdiction, facts\_version, ruleset\_version) for determination identity; (org, workpaper fingerprint, submission\_revision) for simulated runs. Invoice identity matches on entity, vendor, and normalized invoice number; date and amount corroborate.

**Schema bans (tested).** No column, field, or label meaning net, savings, or recovered. No float money column. No human-writable treatment, label, direction, or amount column.

**Invalidation.** A fact, rule-set, or determination version change supersedes the old approval; the UI shows why and requires approval of the new version. Changes after lock create a CorrectionRecord and a flag; a correction run is LATER.

## 4 API contracts (parallel-agent ready)

**Conventions.** Base path `/api/v1`; JSON except uploads and exports. Server-side session; every lookup scoped to the session organization; actor `person_id` always comes from the session, never the body. Every mutation takes `Idempotency-Key` (same key and body returns the original response; same key with a different body returns 409). Commands on facts, determinations, closes, and workpapers carry the expected version. Every response carries IDs, versions, `data_mode`, `engine_mode`, and timestamps. Shapes live in `contracts/` (Pydantic plus exported JSON Schema and golden examples), tagged `contracts-v1`.

| Method and path | Request | Response / effect | Stream |
| :---- | :---- | :---- | :---- |
| POST /imports | Multipart document or CSV; entity ID | 202 with import and job IDs; rejected-row report | W-ING-01 |
| GET /jobs/{id} | — | Status, stage, retryable, error code | W-ING-01 |
| POST /demo/events, /demo/reset | Allowlisted scenario; workspace | Simulated Ramp event or synthetic reset; demo mode only | W-API-01 |
| GET /purchases | Cursor, period, label, direction, hold, review status | Page of at most 100 | W-UI-01 |
| GET /purchases/{id} | — | Lines, facts, evidence, holds, current and past determinations, available actions | W-FACT-01 |
| GET /documents/{id}/access | — | Single-blob read SAS, minutes-long, after ownership check | W-ING-01 |
| POST /purchases/{id}/facts | Field, value, source, reason, expected version | New fact version; supersedes approvals; re-check job. Rejects category, treatment, label, direction, or amount | W-FACT-01 |
| POST /questions/{id}/answers | Answer, evidence reference | Admitted fact or still held; re-check job. "I don't know" or redirect keeps the hold | W-HOLD-01 |
| POST /purchases/{id}/evaluate | Current fact version | 202 job or existing result; 422 `ITEM_HELD` | W-ENG-01 |
| POST /determinations/{id}/review | `approve` (with approval path) or `reject`; reason; expected version | Decision and O1/O2 write; 409 stale; 422 held, NHR, or error | W-REV-01 |
| POST /determinations/{id}/refer | `leave_open` or `send_to_advisor`; note | NHR only; no treatment recorded | W-REV-01 |
| GET /totals | Entity, period | Three figures plus NHR, error, and unreviewed counts; no net | W-TOT-01 |
| GET, PUT /setup | Declared list, period bounds, basis, scripted outcome | Setup role only | W-API-01 |
| POST /closes; GET /closes/{id} | Period ID, expected revision | Close record, draft workpaper sections, blockers, reconciliation | W-INT-01 |
| POST /closes/{id}/confirm | Figures fingerprint | Confirmed close or validation error | W-INT-01 |
| POST /workpapers/{id}/mark-ready | Fingerprint, reason | Locks the version and starts `SIM-QUEUED` in one action. Refused: `PERIOD_NOT_CONFIRMED`, `CLOSE_RECORD_STALE`, `NO_APPROVED_LIABILITY_LINES`, `RUN_ALREADY_ACTIVE` | W-INT-02 |
| GET /workpapers/{id}/export | `csv` or `json` | Frozen export of a locked version, marked DRAFT/SIMULATED; formula-escaped CSV | W-INT-01 |
| GET /filing-runs/{id}; POST …/advance, …/cancel | — | `SIM-` timeline; pacing and cancel only, never the outcome | W-INT-02 |
| GET /audit-events | Object ID, cursor | Permission-scoped history | W-API-01 |
| GET /health/live, /health/ready | — | Database, storage, schema head; never a model call | W-API-01 |

**Errors.** Body: `error.code`, `message`, `field_errors`, `retryable`, `correlation_id`. HTTP: 401, 403 wrong role, 404, 409 stale version or key conflict, 413, 422 domain refusal, 429, 503\. Domain codes: `ITEM_HELD`, `STALE_VERSION`, `NEEDS_HUMAN_REVIEW_NOT_APPROVABLE`, `PATH_MISMATCH`, `NOT_NAMED_APPROVER`, `ALREADY_DECIDED`, `WORKPAPER_LOCKED`, the `mark-ready` refusals listed above, and fact-admission failures such as `FORBIDDEN_FIELD`. No refusal or error ever produces a result, an amount, or a zero. No command exists to set treatment, label, or amount, release a hold, approve all, net totals, or override a category.

**Determination payload (shape only, not a tax assertion):**

{

  "id": "det\_example", "line\_id": "line\_example", "jurisdiction": "WA",

  "batch\_ref": "batch\_case1", "facts\_version": 3, "version": 1,

  "data\_mode": "synthetic", "engine\_mode": "mock\_reviewed\_ruleset",

  "label": "ENGINE\_SUPPORTED", "nhr\_reason": null,

  "direction": "LIABILITY", "derived\_direction": true,

  "expected\_tax": { "value": "10.00", "currency": "USD" },

  "engine\_amount": { "value": "10.00", "currency": "USD" },

  "rule\_reference": "PLACEHOLDER:WA-RAS-001",

  "evidence\_ids": \["ev\_example"\], "unit\_status": "READY\_FOR\_REVIEW"

}

**Totals payload (no net field, no combined total):**

{

  "period\_id": "wa\_2026q3",

  "approved\_liabilities": { "by\_jurisdiction": \[ { "jurisdiction": "WA", "total": { "value": "...", "currency": "USD" }, "line\_count": 3 } \] },

  "refund\_candidates": { "label": "not recovered cash", "total": { "value": "...", "currency": "USD" }, "count": 1 },

  "held": { "count": 1, "purchase\_amount": { "value": "...", "currency": "USD" }, "label": "purchase amount, not tax" },

  "open\_needs\_human\_review": { "count": 1 },

  "determination\_errors": { "count": 0 },

  "unreviewed": { "by\_direction": \[ { "direction": "REFUND\_CANDIDATE", "count": 1 } \] }

}

**Adapter interfaces.**

| Adapter | Contract |
| :---- | :---- |
| SpendSource (IC-1) | List changes from a cursor; fetch canonical purchase and evidence; acknowledge only after persistence. P0 backs it with simulated events; a card charge and later bill for one invoice make one purchase. |
| DocumentExtractor (IC-2) | Bytes or blob reference plus schema version → candidates, evidence, diagnostics, model metadata. Cannot admit or approve. Providers: `azure_openai`, `replay` (labeled "Recorded extraction replay"). |
| TaxEngine (IC-10) | One request per line × jurisdiction: jurisdiction, category, invoice date, service period, line amount, tax charged, S3 location facts. No names, free text, spans, or confidence. Responses: SUPPORTED (treatment, expected tax, adjustment amount, optional direction, rule reference, engine ID), UNSUPPORTED (reason, no amount), ERROR (no result). For Case 1, a batch check confirms the two requests differ only in line, jurisdiction, and location, or the comparison view is withheld. |
| FilingAdapter | Prepare, validate, submit with idempotency key, get status. Simulated only; no network. |

## 5 Azure deployment

Everything runs on Azure in one region, `eastus2` (if kickoff verification fails, the whole stack moves together to `westus2`).

| Component | Azure service | Settings |
| :---- | :---- | :---- |
| web | Container Apps, Consumption | External HTTPS ingress; Next.js standalone image; route handler proxies `/api/*` to `API_INTERNAL_URL` at runtime; same shape in every hosted environment |
| api | Container Apps | Environment-internal ingress only; reached only through the web proxy; min 1 replica (preview min 0, max 1\) |
| worker | Container Apps | No ingress; `minReplicas=1`, `maxReplicas=1`; job leases cover revision swaps |
| Migrate, seed, preview DB admin | Container Apps Jobs (manual trigger) | Run inside the environment; no GitHub runner connects to a database |
| Database | PostgreSQL Flexible Server, Burstable (B1ms preview/staging, B2s demo) | TLS required; firewall allows only its own Container Apps environment; database per PR on the shared preview server |
| Evidence | Blob Storage GPv2, LRS | Private; public and shared-key access disabled; user-delegation SAS for downloads |
| Model | Azure OpenAI | Separate deployments `vision-staging`, `vision-demo`, optional low-quota `vision-preview`; keyless through managed identity |
| Images | Azure Container Registry, Basic | Admin user off; two images (`taxleak-backend`, `taxleak-web`); pulled by managed identity |
| Secrets | Key Vault, RBAC mode | Container Apps Key Vault references through user-assigned identities |
| Observability | Application Insights \+ Log Analytics | Daily ingestion cap; 30-day retention |
| IaC / CI | Bicep; GitHub Actions with OIDC | §6, §7 |

**Environments.**

|  | local | preview (per PR) | staging | demo |
| :---- | :---- | :---- | :---- | :---- |
| Trigger | `docker compose up` | Non-draft same-repo PR | Push to `main` | Manual dispatch \+ reviewer approval |
| Apps | Compose: Postgres, Azurite, api, worker, web, seed | `ca-tl-{web,api,wkr}-pr-{n}` | `ca-tl-*-staging` | `ca-tl-*-demo` |
| Database | Compose Postgres | `tl_pr_{n}` on `psql-tl-preview` | `psql-tl-staging` | `psql-tl-demo` |
| Model | `replay` | `replay`; live with label `preview:live-model` | `vision-staging`, daily cap | `vision-demo`; labeled replay fallback |

**Identities (no credentials in code; one user-assigned identity per app per environment).**

| Identity | Roles |
| :---- | :---- |
| `id-tl-web-{env}` | AcrPull only. Holds no secrets; never touches Postgres, Blob, Key Vault, or the model |
| `id-tl-api-{env}` | AcrPull; Key Vault Secrets User; Storage Blob Data Contributor and Blob Delegator (scoped to `evidence`) |
| `id-tl-worker-{env}` | AcrPull; Key Vault Secrets User; Storage Blob Data Contributor; Cognitive Services OpenAI User (the only model role) |

Postgres uses a password-based app role whose URL lives only in Key Vault. P0 networking is public endpoints with TLS, Entra RBAC, and app login; VNet and private endpoints are LATER.

**Foundation gate: `/api` proxy passthrough.** The web route handler (Node.js runtime, dynamic, uncached, streaming) must pass six checks on the W-FND-04 skeleton preview, re-run in `preview.yml` on every PR: (1) a 10 MB multipart upload arrives intact and 10 MB \+ 1 byte gets the API's 413; (2) session and CSRF `Set-Cookie` headers arrive separately with attributes unchanged; (3) the browser `Origin` arrives unchanged and a foreign one is rejected; (4) a mutation without the CSRF header is rejected; (5) no Next.js caching, and 409/422/413 pass through; (6) the check is automated. G-FND stays closed until it passes. Fallback is an AF decision.

**Bicep and naming.** `infra/bootstrap.bicep` (run once by an Owner) creates resource groups, ACR, Azure OpenAI deployments, identities, Key Vaults, all role assignments, and deployer federated credentials. `infra/main.bicep` (`envName`, optional `prNumber`, image digests) deploys the preview base, a per-PR stack, staging, or demo from `modules/`. What-if runs before every deploy; demo fails on any delete or replace. Names follow `{prefix}-tl-{component}-{env}`; tags `app=taxleak`, `env`, `data=synthetic`, plus `taxleak-pr={n}` for per-PR teardown.

**Kickoff verification.** Before the first deploy: budget alert at the \$75 planning ceiling (soft, owner review); `eastus2` allows every service; the TARGET model has quota on `vision-staging` and `vision-demo` and retires after the demo; the GitHub plan supports required reviewers and merge queue; otherwise escalate to AF.

## 6 Repo structure and environment variables

apps/web/            Next.js UI \+ runtime /api proxy

services/api/        FastAPI routes, domain layer, adapters

services/worker/     job poller (same backend image, different command)

contracts/           Pydantic models, JSON Schema, golden examples (tag contracts-v1)

rules/               mock reviewed rule set \+ S3 catalog (owner-authored content)

fixtures/            synthetic documents, expected results, recordings (20 dev; held-out stored outside repo)

tests/               domain, contracts, api, gates, eval, e2e (Playwright)

infra/               bootstrap.bicep, main.bicep, modules/, params/

.github/workflows/   ci, preview, deploy-staging, deploy-demo, teardown-preview, eval-smoke, eval-nightly

.github/CODEOWNERS   owners for workflows, infra, rules, contracts, fixtures, tests/gates, tests/eval thresholds

compose.yml          local stack; compose.ci.yml for CI end-to-end fallback

docs/                specs and runbooks

The mock engine and simulated filing adapter ship inside the backend image and need no network. Long extraction never runs in a web request. Period bounds and declared states live in Setup, not environment variables.

| Variable | Used by | Source | Value |
| :---- | :---- | :---- | :---- |
| `DATABASE_URL`, `SESSION_SECRET` | api, worker, jobs | Key Vault ref | Flexible Server URL (TLS); per-environment session secret (api) |
| `AZURE_STORAGE_ACCOUNT_URL`, `DOCUMENT_STORAGE_BUCKET` | api, worker | Plain | Blob endpoint (no keys); `evidence` or `pr-{n}-evidence` |
| `EVIDENCE_LINK_TTL_SECONDS` | api | Plain | `300` |
| `AZURE_CLIENT_ID` | api, worker, jobs | Plain | App's user-assigned identity |
| `MODEL_PROVIDER` | worker | Plain | `azure_openai` (staging, demo) or `replay` (CI, preview, local) |
| `MODEL_NAME`, `MODEL_ENDPOINT`, `MODEL_API_VERSION`, `MODEL_AUTH` | worker | Plain | `vision-{env}`, endpoint, pinned API version, `managed_identity` |
| `MAX_MODEL_CONCURRENCY` | worker | Plain | `2` (preview `1`) |
| `ENGINE_MODE`, `DATA_MODE`, `DEMO_MODE` | api, worker | Plain | `mock_reviewed_ruleset`, `synthetic` (startup refuses others), `true` |
| `APP_ENV`, `PR_NUMBER` | api, worker, jobs | Plain | Seed refuses a mismatched database |
| `ALLOWED_ORIGINS`, `API_INTERNAL_URL` | api; web server only | Bicep output | The environment's web origin; internal API address (no `NEXT_PUBLIC_`) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING`, `OTEL_SERVICE_NAME` | web, api, worker | Plain | Telemetry; never in the browser bundle |

**Secrets.** GitHub holds no Azure secret. Runtime secrets are generated in bootstrap into Key Vault and resolved by managed identity; Bicep reads the Postgres admin password with `getSecret()`. Azure OpenAI, Blob, and ACR use managed identity with no keys. No secret enters the browser bundle, logs, or test artifacts, and no human or agent holds a hosted database credential.

## 7 CI/CD and release gates

**OIDC deployers.** Three Entra app registrations with one federated credential each and no client secret.

| Deployer | Subject | Roles |
| :---- | :---- | :---- |
| `gh-taxleak-preview` | `environment:preview` | Contributor on `rg-taxleak-preview`; AcrPush; Key Vault Secrets Officer (preview vault) |
| `gh-taxleak-staging` | `environment:staging` (`main` only) | Contributor on `rg-taxleak-staging`; AcrPush; read on `heldout-fixtures` |
| `gh-taxleak-demo` | `environment:demo` (`main` only, required reviewer, no self-review) | Contributor on `rg-taxleak-demo`; AcrPush (tag existing digests); read on `heldout-fixtures` |

**Workflows.**

| Workflow | Trigger | Jobs |
| :---- | :---- | :---- |
| `ci.yml` | PR, merge group | No Azure login or model call. Lint, Bicep lint, unit and contract tests, migrations (one head, upgrade from merge base), release gates, E0 eval replay, Playwright e2e (preview, else Compose) |
| `preview.yml` | Non-draft same-repo PR | Build `pr-{n}-{sha7}` images → fresh database → deploy per-PR apps → migrate and seed → health and proxy checks → PR comment |
| `teardown-preview.yml` | PR closed; daily janitor | Delete everything tagged `taxleak-pr={n}`; expire idle previews; `PREVIEW_CAP` 8 |
| `deploy-staging.yml` | Push to `main` | CI → build → migrate job → roll API and worker, then web → smoke, gates, E3 → tag `staging-passed-{sha}` |
| `deploy-demo.yml` | Manual `sha` \+ reviewer | Check staging-passed and latest E2 → promote same digests (never rebuild) → restore marker → migrate → roll → read-only smoke |
| `eval-smoke.yml`, `eval-nightly.yml` | Watched-path PRs; nightly | E1 in the PR preview; E2 on staging with `eval-regression` issue on failure |

**Branch ruleset on `main`.** One human approval; stale approvals dismissed; required checks `ci / lint`, `ci / infra-lint`, `ci / unit-contracts`, `ci / migrations`, `ci / release-gates`, `ci / e2e`, `ci / eval-replay`, `eval / smoke-live`; linear history; merge queue on. CODEOWNERS covers `.github/workflows/**`, `infra/**`, `rules/**`, `contracts/**`, `fixtures/**`, `tests/gates/**`, and eval thresholds and baselines. Third-party actions are pinned to commit SHAs; never `pull_request_target` with PR code.

**Release gates.** A failing gate blocks merge; on staging it blocks `staging-passed` and therefore the demo. A gate once on cannot be switched off by a feature PR. A failing financial gate removes the affected feature from the demo; it is never waived.

| Gate | Test | Check |
| :---- | :---- | :---- |
| RG-1 No held, NHR, unreviewed, or error unit in any total; held never shown as zero | Property tests on totals and close builders; API tests on `/totals`; Playwright asserts totals equal approved liability lines | `release-gates`, `e2e` |
| RG-2 No net, savings, or recovered field | Scans of OpenAPI, Pydantic models, migrated columns, built web copy | `release-gates` |
| RG-3 Engine amounts copied exactly | Byte-for-byte decimal comparison with mock output; no float on money paths | `release-gates` |
| RG-4 Rule reference real or `PLACEHOLDER:` | Scan of `rules/`, determinations, exports | `release-gates` |
| RG-5 Cases 1–3 pass (§9) | Playwright case specs with must-not-happen lists | `e2e`; staging smoke |
| RG-6 Every citation resolves | Evidence ID → uploaded document and page | `release-gates` |
| RG-7 No duplicate effects | Duplicate import, card \+ bill, duplicate simulated submit, replay journey | `release-gates`, `e2e` |
| RG-8 No "filed"/"paid" without "simulated" | Copy scans of web output and export templates | `release-gates` |
| H1/H5 Two labels; no human-set treatment | Enum tests; fact, review, refer endpoints reject treatment, label, direction, amount | `release-gates` |
| RG-R Extraction report | Per-field match and correction counts on held-out set | Staging E3 |
| Schema and infra | Single Alembic head; upgrade from merge base; Bicep lint; preview what-if | `migrations`, `infra-lint` |

**Images, migrations, rollback.** Staging and demo reference images by digest; demo promotes staging-tested digests. Migrations are forward-only and expand-then-contract, run by Container Apps Jobs before new revisions take traffic; a failed migration stops the workflow. No automatic rollback: fix forward or revert on `main`; demo rollback re-dispatches an earlier eligible SHA; data recovery is a point-in-time restore to a new server.

**Contract freeze.** After W-FND-01 merges, `contracts/` is tagged `contracts-v1` and read-only. A change needs a `contract-change` issue, both owners' approval (AF for engine, review, ledger, close, filing shapes; RJ for fact, evidence, AI shapes), a version bump, and regenerated golden examples. CI fails any PR that edits `contracts/` without that label.

## 8 Synthetic data and security

**Synthetic-only rule.** Only synthetic documents exist in any environment and only synthetic documents reach the hosted vision model. Data: fictional entity Acme Startup LLC, a fixed registry of fictional vendors checked against real company names, fictional addresses with real state names, visibly fake tax IDs and invoice numbers, a "Synthetic document — Tax Leak evaluation" footer, and `data_mode=synthetic` on every record. No real customer, vendor, employee, or Ramp document is used, including as a template or few-shot example. Any pilot with real documents needs retention and no-training terms first and is out of scope.

**Isolation and seed.**

- Seed comes only from repo `fixtures/` and `rules/` and refuses a target that mismatches `APP_ENV` or carries a non-synthetic marker.  
- No environment reads another's database, storage, or Key Vault (identity-enforced); no workflow copies data across environments.  
- Held-out fixtures live in staging Blob `heldout-fixtures`, readable only by staging/demo deployers and the staging eval job, never by PRs, previews, or agents. GitHub sees aggregates only.

**Application security.**

- Check organization ownership on every read, download, export, and write. Protected demo login with seeded named identities; every action attributed.  
- HTTP-only secure session cookies, CSRF tokens, exact origin allowlist, TLS; web and API share one origin.  
- Validate file signature, size, pages, parse time, and raster size; no URL ingestion. Treat document text and model output as untrusted; escape rendered content and CSV formulas.  
- Rate-limit uploads and model calls. No card numbers, bank credentials, or real tax identifiers.

**Telemetry hygiene.** Logs carry IDs, hashes, durations, modes, result codes, and retry counts; never bodies, document bytes, passages, prompts, model outputs, credentials, or SQL parameters. Each model call emits one `ai.call` record (versions, outcome, latency, tokens). The only Azure alert is the budget alert.

## 9 Tests, eval, and thresholds

**Canonical cases (CA/WA remote-access SaaS).** Demo amounts and outcomes come from actual mock output; no script asserts an outcome the rule set does not return.

| Case | Setup | Expected behavior | Pass gate | Fails if |
| :---- | :---- | :---- | :---- | :---- |
| 1 CA vs WA | One purchase, two subscription lines sharing `batch_ref`, identical terms; seat export or named answer puts one line in CA, one in WA; vendor charged no tax | Two engine requests identical apart from line, jurisdiction, and location; two labeled results side by side with evidence, label, rule reference, and direction exactly as returned | Batch check passes; both results shown; same treatment for both or an honest NHR still passes | Requests differ beyond location; any amount altered; unmarked rule reference |
| 2 WA workpaper | 2–4 WA SaaS liability items, one held WA item, one WA refund candidate (vendor charged WA tax); invoice dates in 2026-07-01..09-30 | Workpaper total equals approved WA liability lines only; held item listed as excluded with no amount; refund candidate approved for pursuit on its own list with its own total; lifecycle runs to `SIM-ACCEPTED` marked SIMULATED | Totals match O1 lines exactly; O2 list nonzero and separate; "No return transmitted. No payment made." shown | Held item has any amount; refund netted; empty refund list; unmarked "filed" |
| 3 Missing multistate evidence | Distributed-team SaaS invoice with only an HQ bill-to; no location breakdown | Item held for "user location by state"; recipient and time named; purchase amount shown as "purchase amount, not tax" | No engine call; absent from every total; question asks for export or named answer | Any tax figure or zero; HQ used as location; engine called |
| ENG-R Replay | Re-import or re-send an event with identical source identity | Existing object returned | Counts and totals unchanged | Any duplicate purchase, line, or amount |

**Fixtures.** 30 synthetic fixtures with frozen inputs and expected results recorded independently by the owners (RJ: evidence and AI expectations; AF: engine outputs, deduplication, state transitions; each cross-checks the other). Split: 20 development in the repo, 10 held out until the release check. Agents never record expected results.

| Group | Count | Covers |
| :---- | :---- | :---- |
| FG-SUP supported treatment | 6 | Case 1 pair; liability; refund candidate; no adjustment; mock-returned direction; derived direction |
| FG-HOLD hold and NHR | 7 | Missing location; bill-to only; conflicting facts; silent vs zero tax; undeclared state; unsupported category; engine unsupported |
| FG-DOC documents | 4 | No tax line vs explicit zero; unreadable page; total mismatch; prompt injection |
| FG-DUP dedup and period | 4 | Duplicate import; card \+ bill; line in locked workpaper; invoice dates 06-30, 07-01, 09-30, 10-01 |
| FG-LIFE lifecycle | 6 | Stale approval; double approve; fact edit supersedes; duplicate simulated submit; scripted `SIM-REJECTED`; post-lock correction record |
| FG-DEP dependencies | 3 | Model timeout; engine timeout and missing rule reference (error, no zero); worker restart |

**Browser journeys (Playwright):** BJ-1 import → evidence → approval; BJ-2 held → answer → re-run → approval; BJ-3 Case 1 comparison; BJ-4 approved close → mark ready → export; BJ-5 replay → unchanged totals. **API tests:** AT-1 cross-organization denial; AT-2 stale-version refusal.

**Model evaluation.** Evaluation measures the vision model only and never widens what it may output. Model-layer scoring and pipeline-layer scoring (expected stop class after TG1, W3, and jurisdiction set) stay separate; engine outcomes are out of scope.

- **Dataset:** `tl-eval-core`, TARGET about 40 documents: the fixtures above plus owner-recorded model-only stress cases, each degraded fixture naming its clean twin.  
- **Smoke set (E1):** 9 documents covering Cases 1–3, silent and zero tax, injection, a category near-miss, and worst blur.  
- **Graders:** deterministic graders decide every gate and reuse product code. An LLM judge may score only description similarity and question clarity; it is diagnostic and never sees money.

**Gate thresholds (TARGET; fail the check).**

| ID | Threshold | Anchor |
| :---- | :---- | :---- |
| EG-01 | 0 unflagged amount errors in any O1/O2 line or total (golden facts vs model facts through the real pipeline) | RG-1, RG-3, H2, H3 |
| EG-02 | 0 silent zeros; 0 amounts downstream of a missed field | H2 |
| EG-03 | 0 forbidden fields; 100% invalid output routed to human entry | H1, H5 |
| EG-04 / EG-05 | 100% admitted evidence resolves; 0 admitted hallucinated financial or location facts | RG-6, TG1 |
| EG-06 / EG-07 | 100% injection fixtures match clean twin; 0 false `REMOTE_ACCESS_SAAS` | FG-DOC |
| EG-08 / EG-09 | 100% required stops with reason and named fact; 100% S3 fields correct on Case fixtures | H2, RG-5 |
| EG-10 | 0 wrong invoice dates on period-boundary fixtures | Period |
| EG-11 – EG-17 | Stability, coverage, and reporting floors (owner-set; lowering needs both owners) | RG-R, H2 |

Any fixture that passed a gate in the committed baseline and now fails it blocks merge.

| Tier | Trigger | Where | Blocks |
| :---- | :---- | :---- | :---- |
| E0 replay | Every PR | `ci.yml`, committed recordings, no Azure login; missing recording fails | `ci / eval-replay` |
| E1 live smoke | PR touching prompts, extractor, AI schemas, or pins | PR preview eval job, `vision-preview`, 30-call cap | `eval / smoke-live` |
| E2 full | Nightly on `main` after model-path changes | Staging eval job, `vision-staging` | Demo eligibility |
| E3 held-out | Staging deploy | Staging, `heldout-fixtures` | `staging-passed` |

**Pin changes.** Model, prompt, schema, or parameter changes need an `ai-change` (or `contract-change`) issue, a green full E1 run, a new baseline, and refreshed recordings in the same PR. Held-out data never tunes prompts. Evals never use `vision-demo`.

**Performance targets (measured, not guaranteed):** reads under 500 ms p95 warm; extraction usually under 30 s.

## 10 Work breakdown (dependency order → GitHub issues)

**Rules for parallel work.**

1. Feature PRs may open after W-FND-01 merges; none merge until G-FND passes.  
2. `contracts-v1` is frozen (§7). Hard dependencies merge first; soft ones are stubbed with golden examples.  
3. One issue, one PR, one preview.  
4. If an issue conflicts with the Design Spec, the agent stops and flags it.  
5. Agents never author S3 entries, rule entries, rule references, expected results, Setup values, or the scripted filing outcome, and never close a sign-off issue.

**Dependency order.**

L0  W-FND-01 contracts ──┬── W-FND-01.S3 (Affan signs)      W-FND-03 Azure infra \+ OIDC

L1  W-FND-02 Postgres \+ jobs ───────────────────────────── W-FND-04 CI \+ previews \+ proxy gate

    ═══════════════ G-FND foundation exit gate ═══════════════

L2  W-API-01  W-RULES-01  W-AI-01  W-UI-01  W-EVAL-01  W-EVAL-02.1

L3  W-ING-01  W-FACT-01  W-ENG-01\*  W-AI-02  W-TOT-01  W-OBS-01  W-EVAL-03  W-EVAL-02.2\*

L4  W-HOLD-01\*  W-REV-01  W-EVAL-04

L5  W-UI-02

    ═══════════════ Integration (serial) ═══════════════

L6  W-INT-01 → W-INT-02 → W-INT-03 → W-EVAL-05 → W-INT-04

\* cannot merge until W-FND-01.S3 is signed by Affan

Critical path: W-FND-01 → W-FND-02 → W-API-01 → W-FACT-01 → W-HOLD-01 → W-UI-02 → W-INT-01 → W-INT-02 → W-INT-03 → W-EVAL-05 → W-INT-04. W-ENG-01 → W-REV-01 runs in parallel and must merge before W-UI-02; W-ENG-01 stubs W-HOLD-01 through golden examples.

**G-FND (all must hold):** `contracts-v1` tagged and golden examples validate in CI; Alembic baseline migrates an empty database and job-lease tests pass; shared Azure stack deployed from GitHub Actions through OIDC; a skeleton PR completes preview deploy, smoke, and teardown; the six-check `/api` proxy test passes (fail closed).

**Epic 1 — Foundation (land first).**

| Issue | Scope | Depends | Acceptance | Sign-off |
| :---- | :---- | :---- | :---- | :---- |
| W-FND-01 | Contracts for §3–§4: money type, error body, modes, closed enums, AI schemas without forbidden fields, TaxEngine variants, S3/RuleSetEntry/Setup; golden examples (Case 1–3 traces, determination and totals payloads); fixture manifest (30 IDs, 20/10) | — | Goldens validate; no net/savings/recovered field; no third label; AI schemas reject amount, label, direction | AF engine shapes; RJ fact/AI shapes |
| W-FND-01.S3 | RJ drafts the remote-access SaaS catalog; Affan signs; Setup values recorded | W-FND-01 | `signed_off_by=AF`, status signed | **Affan only** |
| W-FND-02 | Alembic baseline for §3; constraints; NUMERIC money; append-only audit grants; idempotency table; jobs with transactional claim, lease reclaim, retries; worker loop; seed hooks | W-FND-01 | Two workers never claim one job; expired lease reclaimed; float-money test; app role cannot alter audit | AF |
| W-FND-03 | Bicep bootstrap and main (§5); identities, roles, Key Vault secrets; model deployments; worker `minReplicas=1`; budget alert | — | Deploy via OIDC; storage private; secrets only in Key Vault | AF |
| W-FND-04 | Deploy workflows (§7); skeleton web/api/worker with health routes; preview defaults (synthetic, mock, replay); proxy gate test | W-FND-02, \-03 | No-op PR gets green checks, preview URL, isolated DB; proxy checks pass; teardown leaves no orphans | AF |

**Epic 2 — Platform and intake.**

| Issue | Scope | Depends | Acceptance | Sign-off |
| :---- | :---- | :---- | :---- | :---- |
| W-API-01 | Demo login with seeded roles, sessions, CSRF, org scoping, error body, idempotency, expected-version helper, audit writer, `/setup`, `/demo/*`, health | W-FND-01, \-02 | AT-1, AT-2; 403 on `/setup` for non-setup roles | AF |
| W-ING-01 | Imports, jobs, file checks, Blob upload with SHA-256, download SAS, simulated Ramp events, invoice identity, card \+ bill merge | W-API-01, W-FND-03 | ENG-R; FG-DUP; RG-7 | AF |
| W-OBS-01 | Structured logs, counters, `ai.call`, diagnostics view with failed-job retry and worker heartbeat | W-API-01, W-FND-03 | No document text or prompts in telemetry | AF |

**Epic 3 — Facts, AI, and holds.**

| Issue | Scope | Depends (soft) | Acceptance | Sign-off |
| :---- | :---- | :---- | :---- | :---- |
| W-AI-01 | DocumentExtractor (Azure OpenAI via managed identity, plus replay); Call A; validation, one repair, human-entry fallback; timeout, retries, cache | W-FND-02 (W-ING-01) | FG-DOC; model timeout gives no zero; RG-6 | RJ; AF on TG1 boundary |
| W-AI-02 | Call B: category proposal, unresolved keys, question drafts | W-AI-01 | Non-SaaS → `NO_SUPPORTED_CATEGORY`; questions ask for export or named answer | RJ |
| W-FACT-01 | TG1 admission, fact versions, `/facts`, supersession, human entry, reconciliation | W-API-01 (W-AI-01) | Bill-to and conflicts stay unconfirmed; silent ≠ zero; mismatch held | RJ design; AF enforcement |
| W-HOLD-01 | W3 hold check from signed S3, category check, jurisdiction set, undeclared-state NHR, `/answers` | W-FACT-01, W-AI-02, **S3 signed** | Case 3 hold; FG-HOLD; RG-1 at unit level | AF; RJ wording |

**Epic 4 — Engine, review, and totals.**

| Issue | Scope | Depends (soft) | Acceptance | Sign-off |
| :---- | :---- | :---- | :---- | :---- |
| W-RULES-01 | Mock rule set and evaluator: CA/WA SaaS, dated entries, real or `PLACEHOLDER:` IDs, own adjustment amount per result, UNSUPPORTED elsewhere. RJ drafts entries; agents write the evaluator only | W-FND-01 | RG-4; unit example (\$100 at a fictional 10%: charged \$0 → liability \$10; \$12 → refund \$2; \$10 → no adjustment); every amount emitted as a two-decimal USD string | AF interface; RJ entries |
| W-ENG-01 | W5: requests per line × jurisdiction, never for held/NHR; response checks; copy amounts; copy or derive direction; supersedes chain; Case 1 batch check | W-RULES-01, **S3 signed** (W-HOLD-01) | RG-3; FG-SUP; timeout or missing reference → error, never zero | AF |
| W-REV-01 | Review and refer commands; path must match direction; no bulk; O1 and O2 writes | W-ENG-01, W-FACT-01 | FG-LIFE; NHR has no treatment action; H5 | AF |
| W-TOT-01 | `GET /totals` per §4 | W-API-01 (W-REV-01) | RG-1, RG-2; "No approved liabilities" | AF |

**Epic 5 — UI.**

| Issue | Scope | Depends (soft) | Acceptance | Sign-off |
| :---- | :---- | :---- | :---- | :---- |
| W-UI-01 | Shell and read views from typed `contracts/` client; "Synthetic data • simulated filing" banner; two-second polling; exact label wording; accessible, right-aligned two-decimal amounts (rendered from the engine string; never reformatted or rounded) | W-FND-04 (W-TOT-01) | RG-2 and RG-8 scans of rendered pages | AF; RJ evidence view |
| W-UI-02 | Review queue, approve/reject/approve for pursuit, NHR "Leave open"/"Send to advisor" only, fact correction, answers, Case 1 side-by-side | W-UI-01, W-REV-01, W-HOLD-01 | BJ-1 to BJ-3 on preview | AF |

**Epic 6 — Evaluation.**

| Issue | Scope | Depends | Acceptance | Sign-off |
| :---- | :---- | :---- | :---- | :---- |
| W-EVAL-01 | Harness, replay/live providers, pin capture, metrics JSON, baselines, `ci / eval-replay`, RG-2/4/8 scanners | W-FND-04 | E0 needs no Azure login; unpinned run fails | RJ |
| W-EVAL-02 | .1 generator and model-layer goldens; .2 pipeline-layer goldens and expected results | .1 W-FND-01; .2 W-RULES-01, **S3 signed** | Goldens validate; owner-recorded; all synthetic | RJ (.1); AF (.2) |
| W-EVAL-03 | Graders, differential pipeline run, thresholds file, first baselines, RG-R report | W-EVAL-01, \-02.1, W-AI-01 | Seeded negative test per gate; EG-01 \= 0 in E0 | RJ; AF amount gates |
| W-EVAL-04 | E1/E2 workflows, eval jobs, caps, demo eligibility, eval checks required | W-EVAL-03, W-OBS-01 | Cap hit fails; no runner holds a model role | AF |

**Epic 7 — Integration and demo (serial).**

| Issue | Scope | Depends | Acceptance | Sign-off |
| :---- | :---- | :---- | :---- | :---- |
| W-INT-01 | Close and WA workpaper: open items, confirm figures, draft from O1 only, excluded items without amounts, informational count of approved no-adjustment lines (no amount), O2 list "Not part of this workpaper total. Not netted.", fingerprint, WA form fields `PLACEHOLDER:` until verified, DRAFT export | W-REV-01, W-TOT-01, W-UI-02 | Case 2 workpaper checks; boundary dates | AF |
| W-INT-02 | `mark-ready` lock \+ `SIM-QUEUED`; SIM machine; scripted outcome; duplicate submit blocked; correction record; timeline UI | W-INT-01 | FG-LIFE; RG-8; H4 | AF |
| W-INT-03 | Playwright BJ-1..5, Cases 1–3, ENG-R; staging and demo smoke | W-INT-02, W-AI-01 | RG-5 without manual DB edits | AF and RJ |
| W-EVAL-05 | Held-out split, E3, human spot check, final run with demo pins, RG-R report | W-EVAL-04, W-INT-03 | EG-01..EG-17 green on held out | RJ and AF |
| W-INT-04 | Demo hardening: deploy staging-passed SHA, synthetic baseline, restore drill, worker restart, replay fallback, narration from mock output, backup recording | W-EVAL-05 | Every RG green on demo | AF and RJ |

**Never cut:** H1–H5; the Case 3 real hold; the Case 2 refund candidate in full (approve-for-pursuit action, own O2 total, workpaper reference list); RG-1 to RG-8; DRAFT and SIMULATED markers. If the live model is unavailable, show the labeled recorded replay or human-supplied facts.

**Issue conventions.** Title `[<ID>] <verb phrase> (<gate or contract IDs>)`, for example `[W-FND-01.S3] Sign remote-access SaaS S3 catalog`. Labels: `workstream:<ID>`, `level:<0–6>`, plus `contract-change`, `needs-signoff:AF`, `needs-signoff:RJ`, `release-gate`, `ai-change`, or `eval-change` where they apply.

Status: lean Tech Spec — build plan only.

# Tax Leak Technical Spec v2 (Lean)

Updated November 7, 2026\. Owners: Affan Khurram Sethi (AF) and Ren Jie Tee (RJ). This spec replaces v1.5 (Tab 1\) as the build plan. Tab 1 stays as the record of the earlier design, and section 16 lists every change.

## 1 Purpose and scope

This is the build plan for the Tax Leak demo for the BBS x Ramp Hackathon (Build it lane): architecture, data model, rule set, API, Ramp integration, Azure hosting, CI/CD, tests and the work breakdown. Coding agents build against it. AF and RJ review every pull request. Rule, rate, catalog, and golden content activates only when verified under `docs/content-verification.md`; there is no owner signing step.

Tax Leak reviews Ramp card and bill spend for US sales and use tax errors in both directions: use tax the company owes when a vendor charged none, and tax a vendor charged on something that was not taxable. The model reads receipts and explains. Effective-dated rules in code decide treatment and do every calculation. A named person approves every action.

**Design goals for v2.**

* One deployable app on one Azure App Service, inside the \$75 budget and the \$100 student credit.  
* A live demo that works end to end: a Ramp-shaped webhook arrives, the item is flagged within seconds, the fix is drafted, a person approves it.  
* Correct tax content: every rule effective-dated and sourced, every amount reproducible by hand from the rule and rate tables.  
* No dependency on Azure OpenAI.

**P0 scope.** Synthetic receipts and invoices; simulated Ramp webhooks that use Ramp's real field names (the live Ramp sandbox if access is approved); one entity, USD; California, Washington and New York; remote-access SaaS, plus computer hardware for one New York case; holds, two labels and direction; versioned approval (no bulk); use tax journal entry draft, vendor refund letter draft, and a Washington Q4 2026 workpaper with a simulated "ready to file" status; 20 to 30 fixtures; replay mode and a recorded fallback demo.

**Out.** Live filing or payment, sending refund letters, other states and categories, exemption certificates (other than flagging Washington multiple points of use), allocation beyond user counts, FX, per-PR environments.

## 2 Hard rules and financial invariants

The hard rules are unchanged from v1.5. Every build decision must satisfy them.

| \# | Rule | Build consequence |
| :---- | :---- | :---- |
| H1 | Two labels only | Every result is `ENGINE_SUPPORTED` ("Reviewed rule / engine-supported") or `NEEDS_HUMAN_REVIEW` ("Needs human review"). No third label or tax-result confidence score. Fact confidence and evidence provenance may be shown; never use them alone to decide tax. |
| H2 | Holds are not a silent zero | A missing or unconfirmed required fact holds the item: no engine call, no approval, no tax amount, estimate or zero. Errors and timeouts never become zero. |
| H3 | Refunds never netted | Liability totals and refund-candidate totals are separate fields everywhere. No net, "savings" or "recovered" field or copy. The refund comes from a vendor and the liability goes to a state, so they never offset. |
| H4 | No live filing | The workpaper reaches a simulated "ready to file" status only. No outbound filing connection, no payment state, no "filed" or "paid" text without "simulated". |
| H5 | No human-decided treatment | People approve, reject, supply or correct facts, leave an NHR item open, or send it to an advisor. No endpoint accepts a tax-treatment, label, direction or tax-decision amount override. Sourced invoice amounts are facts and may be corrected with versioning/re-evaluation. |

**Financial invariants (release-gated in sections 11 and 13).**

* Totals never include held, NHR, unreviewed, rejected, superseded or error units. Nothing counts until a named person approves it.  
* Engine amounts and rule references are copied exactly, as the engine's decimal strings. The app never rounds, adjusts, recomputes or fills in a value.  
* SUPPORTED results require real owner-reviewed, effective-dated rule and rate references. PLACEHOLDER: metadata/fixtures are visibly illustrative only and cannot support approval or totals. Unreviewed applicability or unresolved transition routes NHR with no amount; a missing reference in an otherwise supported response is a determination error.  
* The model may extract printed invoice amounts with evidence, but never generates tax-decision amounts, rates, treatment or direction. The deterministic rule engine produces every tax treatment and tax-decision amount; copy its decimal strings exactly.

## 3 Locked decisions

These decisions are fixed for the demo. Changing one needs both owners.

| Topic | Decision |
| :---- | :---- |
| Engine | In-process rule engine over `rules/` covering CA, WA and NY. Every entry has `effective_from`, `effective_to` and a source URL. Returns UNSUPPORTED outside its coverage. |
| Effective dating | Reporting period is assigned by invoice date, independently of the tax rule's effective-date selection. Demo SaaS uses monthly service periods with the rule timing basis explicitly verified under `docs/content-verification.md`; no annual/cross-year timing is inferred. Unverified annual/cross-year timing and unresolved CA January 2027 transition handling route to NHR RULE\_TRANSITION\_UNRESOLVED, with no amount or approval. A source title or PLACEHOLDER reference alone never establishes supported treatment. Hardware uses its separately verified delivery-date rule. |
| Location evidence | SaaS ladder, strongest first: seat export/roster → named answer → SSO/HRIS business location → order-form ship-to → matched office address. Last two require a named single-office confirmation; low-confidence proposals always require recorded confirmation. Keep source, rung, confidence, confirmed and confirmed\_by; conflicts/partial seat coverage hold. Bill-to never confirms use and is explanatory only, not an admissible rung. NY hardware separately needs confirmed delivery evidence. |
| Direction | Copy the engine's direction. If absent, derive it from expected tax versus admitted tax charged and set `derived_direction=true`. |
| Period | Washington Q4 2026 (2026-10-01 to 2026-12-31), assigned by invoice date. Return due 2027-02-01. Q3 2026 is not used because its return was due 2026-11-02. |
| Hold and NHR routing | Whole-item hold for a missing or unconfirmed required fact. Whole-item NHR for an undeclared jurisdiction, an unsupported category or an engine UNSUPPORTED. Confidence alone never decides tax or releases a hold; low-confidence location requires recorded confirmation under S3. |
| Required-fact catalog (S3) | Verified under `docs/content-verification.md`. Entries stay candidates until verified. This gates only dependent fact-admission/hold work and end-to-end supported acceptance, not skeleton/contracts, extractor scaffolding, engine evaluator, simulator or mock UI. Agents implement structures/evaluator, never author authoritative entries, rates or golden expected results. |
| Model | Direct API behind DocumentExtractor; target provider OpenAI, target model gpt-4.1 with strict JSON schema output, UNVERIFIED for this account. Confirm current availability/support and pin an available model before live/record use; substitution requires owner approval and replay/golden revalidation. No model account, quota or key is asserted provisioned. Azure OpenAI is not used; replay remains the stage default. |
| Data | Synthetic documents and fictional vendors only. "Synthetic demo data" banner on every screen. |
| Unreviewed refund amounts | Visible on the review list and item view, never in any total. |

## 4 Architecture

One Linux App Service runs everything: a FastAPI app that serves the API under `/api`, the static frontend from the same origin, and an in-process worker that drains a jobs table. There is one hosted environment plus local.

`Browser -> App Service (West US 2): FastAPI + static frontend + in-process worker`  
`Ramp webhook (sandbox or simulator) -> POST /api/v1/webhooks/ramp`  
`Worker -> DocumentExtractor -> model API (live / record / replay)`  
`Worker -> TaxEngine (in-process, rules/) -> rate tables and WA DOR rate lookup (cached)`  
`App -> managed PostgreSQL Flexible Server over TLS (system of record, durable jobs, audit)`  
`App -> Blob container "data" (documents, exports) through managed identity`

Components:

| Component | Technology | Responsibility | Never |
| :---- | :---- | :---- | :---- |
| Frontend | Static build (React), served by FastAPI from `app/static` | Review queue, item view, totals, workpaper view, two-second polling | Tax logic; trusting a browser-supplied amount |
| API | FastAPI, Pydantic | Demo login, validation, commands, read models, audit writes, webhook intake | Model calls or long work inside a request |
| Worker | Background thread in the same process; polls the jobs table with leases | Fetch, extraction, fact admission, cross-check, hold check, engine calls, exports | Approvals |
| Database | Managed PostgreSQL Flexible Server for hosted data, SQLAlchemy and Alembic. SQLite is optional for local development only; never place a hosted SQLite database on App Service /home network storage. | System of record, jobs, idempotency keys, append-only audit | Binary-float money |
| Documents | Existing Blob account `ramphackaffanandrj26`, container `data` | Original documents (SHA-256 identity), frozen exports | Public access; account keys |
| Extractor | `DocumentExtractor` with providers `openai`, `anthropic`, `google`, `replay` | Fact candidates with evidence, category proposal, question drafts | Tax rates or tax-decision amounts, treatment, labels, direction, approvals (printed invoice amounts may be extracted with evidence) |
| Rule engine | `TaxEngine` over `rules/*.yaml` | Treatment, expected tax, adjustment amount, rule reference, rate and location code | Anything outside `rules/` |
| Rate source | Static rate tables; WA DOR address rate lookup, cached per address and quarter | Rate and location code | Live lookups on stage (cache only) |
| Workpaper | Workpaper builder in the API | Draft, then simulated "ready to file" | Network calls; any payment state |

Run exactly one App Service instance and one gunicorn worker process. The background worker uses durable PostgreSQL jobs with atomic claim, leases, retry limits and expired-lease recovery after restart; result writes and job completion are transactional and idempotent. Leases and unique keys prevent duplicate effects during restarts/deployment overlap. SQLite is local-only. Verify PostgreSQL availability, subscription eligibility and cost before provisioning; no free entitlement or deployment is asserted.

**Pipeline.** Each stage writes audit events and keys its output by immutable input version, so retries never duplicate output.

| Stage | Output | Stop condition |
| :---- | :---- | :---- |
| Ingest | SourceEvent, purchase, job | Bad signature; duplicate event id (return the existing object); invalid file; non-USD |
| Fetch | Ramp transaction and receipt records | Fetch error: retry, then hold |
| Deduplicate | Canonical purchase (a card charge and a later bill for one invoice make one purchase) | Ambiguous match goes to review, never a destructive merge |
| Extract (model) | Candidate values with evidence | Still invalid after one repair retry: human fact entry |
| Cross-check | Extracted totals and tax vs `entity_amount` and Ramp's `ocr.taxes` | Difference over one cent: fact stays unconfirmed (hold) |
| Admit facts | Versioned admitted facts | No resolvable source; a location from a rung that cannot confirm stays unconfirmed |
| Classify | Category | Outside coverage: NHR `NO_SUPPORTED_CATEGORY` |
| Hold check | Held or complete | Any missing or unconfirmed S3 fact: whole-item hold |
| Jurisdiction set | States from confirmed locations plus states where tax was charged | Empty set: hold. Undeclared state: NHR `UNDECLARED_JURISDICTION` |
| Engine call | One request per line × jurisdiction, confirmed facts only | Never sent for held or NHR items |
| Response check | Labeled determination | Missing rule reference, malformed or timeout: determination error, no label, no amount. UNSUPPORTED: NHR `ENGINE_UNSUPPORTED` |
| Review | Approved, rejected or approved for pursuit | Held, NHR, error, stale version, unnamed actor |
| Outputs | Journal entry draft, refund letter draft, workpaper lines | Only approved, current units |

**AI contract.** Two bounded calls per new document.

* **Call A, extraction:** invoice identity and date, vendor, service period, currency, per-line quantity, unit price, gross, discount and net, tax charged as printed, document tax total, shipping, product and delivery terms, bill-to/ship-to and location spans with printed labels and optional candidate confidence (not admission), and evidence references (document, page, exact passage, provenance). Values exactly as printed, never computed; nulls for missing. "No tax line" (null) and "tax line of zero" (`0.00` with evidence) are different values.  
* **Call B, category and questions:** `REMOTE_ACCESS_SAAS`, `COMPUTER_HARDWARE` or `NO_SUPPORTED_CATEGORY` with rationale, evidence IDs and unresolved S3 keys; question drafts asking for a seat-assignment export or a named person's answer, by state, for the invoice period.  
* **Forbidden output:** no field for rate, expected tax, amount owed, label, direction, hold, approval, tax-jurisdiction assignment, location-code assignment or legal conclusion. Verbatim state/address text is allowed as sourced evidence, not as a tax-jurisdiction decision. Any such key fails with `FORBIDDEN_FIELD`. Document text is data, not instructions. No tools, no browsing.  
* **Limits and failure:** PDF, PNG, JPEG; up to 10 MB and 10 pages. Timeout 45 s; two retries honoring retry-after; then human fact entry with visible provenance. Never an invented value, estimate or zero.  
* **Records and replay:** cache by document hash \+ provider \+ model \+ prompt version \+ schema version. `LLM_MODE=live` calls the API, `record` calls it and saves the response, `replay` serves saved responses and fails on a miss. CI and the stage demo run in `replay`.  
* **Latency target:** webhook to flagged result under 10 seconds in replay, under 30 seconds live.

**Amounts and totals.** API money is `{"value": "10.00", "currency": "USD"}`. Storage is NUMERIC or integer cents. No binary float anywhere on a money path. Every SUPPORTED result carries its own adjustment amount: tax owed (liability), amount in question (refund candidate), or none. Derived direction: expected tax above admitted tax charged gives `LIABILITY`, below gives `REFUND_CANDIDATE`, equal gives `CHARGED_CORRECTLY`. A zero tax charged is sent to the engine only when a sourced fact shows zero; a silent invoice holds when S3 requires the field.

| Figure (shown side by side, never combined) | Includes | Never includes |
| :---- | :---- | :---- |
| Approved liabilities, by jurisdiction and period | Approved, current liability units | Held, NHR, unreviewed, rejected, superseded, error, no-adjustment or refund units |
| Refund candidates ("not recovered cash") | Units approved for pursuit | Liabilities; unreviewed refund results (count only) |
| Audit exposure (illustrative, P1) | Approved liabilities for the period, annualized, over the state's lookback, plus simple interest at the state's 2026 rate. Labeled "Illustrative: if an auditor finds it first" | Refund candidates; held or unreviewed items |
| Held | Count and "purchase amount, not tax" | Any tax figure |
| NHR and engine errors | Lists with reasons | Any amount |

A filing key with no approved lines shows "No approved liabilities", never a bare zero.

## 5 Data model

Conventions: every organization-owned row carries `org_id`; IDs are UUIDs; timestamps are UTC, while invoice and period dates keep business meaning; supersedable records carry an increasing `version`; document, fact and determination history is frozen; audit rows are append-only.

| Area | Entities and key fields |
| :---- | :---- |
| Platform | Organization; Person (seeded demo users with role viewer, reviewer or setup); Setup (declared jurisdictions CA, WA, NY; period bounds; basis `invoice_date`); Job (kind, input version, status, `lease_until`, attempts, error code); IdempotencyKey; AuditEvent (actor, command, object and version, before and after hashes) |
| Intake | SourceEvent (source, external event id, payload hash, signature result); Purchase (entity, vendor, normalized invoice number, invoice date, item status, Ramp transaction and bill ids); PurchaseLine (line number, description, quantity, unit price, gross, discount, net, service period start and end); Document (blob key, SHA-256, MIME type, pages) |
| Facts and evidence | Fact (subject, field key, value, origin `AI_EXTRACTED` or `HUMAN_SUPPLIED` or `RAMP_RECORD`, source, evidence rung 1 to 5, confirmation, version, supersedes); Evidence (document, page, exact passage, provenance) |
| Catalogs | RequiredFactCatalog (category, required keys, accepted rungs, status candidate or verified, evidence file under `rules/evidence/`); RuleSetEntry (id, jurisdiction, category, treatment, effective\_from, effective\_to, timing\_basis, primary\_source\_url, review\_status, reviewed\_by, reviewed\_at); RateRecord (jurisdiction, location, location code, rate, quarter, source URL, review\_status, reviewer). Only verified entries activate. |
| Holds and questions | Hold (purchase, reason, named fact, active, releasing fact version); Question (hold, fact key, recipient, asked at, answer, state) |
| Results | Determination (line, jurisdiction, facts version, rule-set version, label, NHR reason, direction, `derived_direction`, rate, location code, expected tax, engine amount, rule reference, unit status, supersedes); ReviewDecision (determination and version, action, approval path, actor, reason) |
| Ledgers and outputs | LiabilityLine (determination, jurisdiction, period, amount copied, approver, approved at); RefundCandidate (determination, amount in question copied, status `APPROVED_FOR_PURSUIT` only, approver); JournalEntryDraft (debit Use Tax Expense, credit Use Tax Payable, lines); RefundLetterDraft (vendor, invoice, amount, legal basis text) |
| Workpaper | Workpaper (filing key entity \+ jurisdiction \+ period, version, status, fingerprint, frozen at); WorkpaperLine (links to LiabilityLine) |

**Closed enums.** Adding a value is a contract change that needs both owners.

| Enum | Values |
| :---- | :---- |
| Label | `ENGINE_SUPPORTED`, `NEEDS_HUMAN_REVIEW` |
| Direction (ENGINE\_SUPPORTED only) | `LIABILITY`, `REFUND_CANDIDATE`, `CHARGED_CORRECTLY` |
| Hold reason | `MISSING_REQUIRED_FACT`, `UNCONFIRMED_REQUIRED_FACT` |
| NHR reason | `NO_SUPPORTED_CATEGORY`, `UNDECLARED_JURISDICTION`, `ENGINE_UNSUPPORTED`, `RULE_TRANSITION_UNRESOLVED` |
| Item status | `INTAKEN`, `OUT_OF_SCOPE`, `FACTS_ADMITTED`, `CLASSIFIED`, `HELD`, `NHR_PRE_DETERMINATION`, `READY_FOR_DETERMINATION`, `DETERMINED` |
| Unit status | `REQUESTED`, `DETERMINATION_ERROR`, `NHR_ENGINE`, `SENT_TO_ADVISOR`, `READY_FOR_REVIEW`, `APPROVED`, `APPROVED_FOR_PURSUIT`, `REJECTED`, `SUPERSEDED` |
| Approval path | `LIABILITY`, `REFUND_PURSUIT`, `CHARGED_CORRECTLY` (must match direction) |
| Workpaper status | `DRAFT`, `READY_TO_FILE_SIMULATED` (no transition back; a later change creates a new draft version) |
| Job status | `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED` |
| Modes | `data_mode=synthetic`, `engine_mode=mock_reviewed_ruleset`, `llm_mode` live, record or replay. The app refuses to start with other data or engine modes. |

**Unique constraints.** (org, source, external\_event\_id); (org, purchase\_line\_id, jurisdiction, facts\_version, ruleset\_version) for determination identity; (org, workpaper fingerprint) for ready-to-file versions. Invoice identity matches on entity, vendor and normalized invoice number; date and amount corroborate.

**Schema bans (tested).** No combined liability-minus-refund net, savings or recovered column/field/label. Printed invoice line net is an allowed sourced invoice fact, never a net tax benefit. No float money column. No human-writable tax treatment, label, direction or tax-decision amount column. Sourced printed invoice amounts may be entered/corrected as versioned facts, never as determination amounts.

**Invalidation.** A fact, rule-set or determination version change supersedes the old approval; the UI shows why and asks for approval of the new version. A workpaper marked ready is frozen; a later change creates a new draft version and a visible flag.

## 6 Rule set v2 (seed content)

Each rule, rate, timing basis and expected result activates only when verified under `docs/content-verification.md` (primary sources, an evidence file, a research pass and a blind verification pass by a different lane and model family, and the `make verify-content` gate). These are candidate seed entries, not verified legal conclusions; prose or an ID alone is not approval. Catalog status is candidate (inactive) or verified; active entries record evidence and a real source URL. PLACEHOLDER: is an identifier prefix, not a status or third result label. PLACEHOLDER:CA-DP-001 remains candidate/inactive and NHR until replaced by a verified real rule. Only verified applicable entries may yield ENGINE\_SUPPORTED. Placeholder/unreviewed entries route NHR (transition: RULE\_TRANSITION\_UNRESOLVED; otherwise ENGINE\_UNSUPPORTED) with no amount. Source titles below are research pointers until verified and linked. Agents implement evaluator structures only.

| ID | Jurisdiction and category | Treatment | Effective | Source |
| :---- | :---- | :---- | :---- | :---- |
| WA-RAS-001 | WA, remote-access prewritten software (SaaS) | Retail sale. Buyer owes use tax when the vendor charged none | 2009-07-26 onward | RCW 82.04.050(6)(b); WAC 458-20-15503 |
| WA-MPU-001 | WA, SaaS used in several states | Apportion by users in WA over all users, only if supported by records (seat export) | Current | WAC 458-20-15503; RCW 82.08.0208 |
| CA-RAS-001 | CA, remote-access prewritten software, no tangible copy | Not taxable | Service periods through 2026-12-31 | Reg. 1502(f)(1)(D); Audit Manual 0421.03 (rev. April 2014\) |
| PLACEHOLDER:CA-DP-001 | INACTIVE PROPOSAL — CA remote software transition. Adjacent treatment/date/source text is unverified research, not an active rule; NHR RULE\_TRANSITION\_UNRESOLVED until reviewed. | Taxable | Service periods starting 2027-01-01 onward | SB 122 (Stats. 2026, ch. 23); CDTFA Special Notice L-1036. Placeholder until the emergency regulations are approved |
| NY-SW-001 | NY, prewritten software including remote access | Taxable, sourced to where users are | Current | TB-ST-128 |
| NY-TPP-001 | NY, computer hardware delivered to NY | Taxable at the delivery location's rate, including shipping | Current | NY DTF, Use Tax for Businesses; Pub 718 |

ESSB 5814 (effective 2025-10-01) added IT services, custom software and similar services to Washington's sales tax. It does not change SaaS, which was already a retail sale, so no rule cites it.

**Rates.**

| Location | Rate | Location code | Effective | Source |
| :---- | :---- | :---- | :---- | :---- |
| Seattle, WA | 10.55% (6.5% state, 4.05% local) | 1726 | 2026 Q1 to Q4 (10.35% in Q4 2025\) | WA DOR local rate tables |
| San Francisco, CA | 8.625% | n/a | From 2026-10-01 | CDTFA rates |
| New York City, NY | 8.875% | 8081 | Current | NY Pub 718 |

**Refund and reporting routes (owner-review research pointers; only owner-approved basis text enters DRAFT outputs, never transmitted or presented as verified filing advice).**

| Situation | Output | Basis |
| :---- | :---- | :---- |
| CA vendor charged tax on non-taxable SaaS | Refund letter to the vendor | RTC 6901.5; Reg. 1700; McClain v. Sav-On Drugs (2019): the customer has no claim against CDTFA directly |
| WA use tax owed | Journal entry; line on the Q4 2026 excise tax return ("Use Tax/Deferred Sales Tax") | WA DOR filing rules; quarterly returns due the end of the month after the quarter |
| NY use tax, registered business | Journal entry; line on the sales tax return | NY DTF |
| NY use tax, unregistered business | Journal entry; Form ST-130 within 20 days | NY DTF |
| CA use tax from 2027, no seller's permit | Note: register and report directly once untaxed purchases pass \$10,000 a year | CDTFA (threshold applies 2024 to 2028\) |

**Audit exposure inputs (P1 candidate research only; off until owners review/sign all relevant sources and assumptions; placeholder rates never produce an estimate).**

| State | Lookback | Late payment penalty | 2026 interest |
| :---- | :---- | :---- | :---- |
| CA | 3 years; 8 if no return was filed (RTC 6487\) | 10% (RTC 6591\) | 10% |
| WA | 4 years after the tax year (RCW 82.32.050) | 9%, then 19%, then 29%; 5% to 25% on an assessed underpayment (RCW 82.32.090) | 6% |
| NY | 3 years | PLACEHOLDER: verify with NY DTF | PLACEHOLDER: verify with NY DTF |

## 7 API

**Conventions.** Base path `/api/v1`; JSON except uploads and exports. Server-side session; every lookup scoped to the session organization; actor `person_id` always comes from the session, never the body. Every mutation takes `Idempotency-Key`. Commands on facts, determinations and workpapers carry the expected version. Every response carries IDs, versions, `data_mode`, `engine_mode` and timestamps. Shapes live in `app/contracts/` as Pydantic models with golden examples.

| Method and path | Request | Response or effect |
| :---- | :---- | :---- |
| POST /webhooks/ramp | Ramp event (IDs only) with `X-Ramp-Signature` | 200 after the SourceEvent is stored; a duplicate event id returns 200 with no new work |
| POST /imports | Multipart document or CSV; entity ID | 202 with import and job IDs |
| GET /jobs/{id} | None | Status, stage, retryable, error code |
| POST /demo/events, /demo/reset | Allowlisted scenario | Simulated Ramp event or synthetic reset; demo mode only |
| GET /purchases | Cursor, period, label, direction, hold, review status | Page of at most 100 |
| GET /purchases/{id} | None | Lines, facts, evidence, holds, current and past determinations, available actions |
| POST /purchases/{id}/facts | Field, value, source, reason, expected version | New fact version; supersedes approvals; re-check job. Rejects tax treatment, label, direction and tax-decision amount overrides; allows sourced printed invoice-amount facts only. Category follows the signed classifier/catalog contract. |
| POST /questions/{id}/answers | Answer, evidence reference | Admitted fact or still held. "I don't know" keeps the hold |
| POST /determinations/{id}/review | `approve` (with approval path) or `reject`; reason; expected version | Decision and ledger write; 409 stale; 422 held, NHR or error |
| POST /determinations/{id}/refer | `leave_open` or `send_to_advisor`; note | NHR only; no treatment recorded |
| GET /totals | Entity, period | Separate figures plus NHR, error and unreviewed counts; no net |
| GET /drafts/journal-entries; GET /drafts/refund-letters/{id} | Period or determination | Drafts for approved units only |
| POST /workpapers | Period | Draft workpaper from approved liabilities only |
| POST /workpapers/{id}/mark-ready | Fingerprint, reason | Freezes the version with status `READY_TO_FILE_SIMULATED`. Refused with `NO_APPROVED_LIABILITY_LINES` or `STALE_VERSION` |
| GET /workpapers/{id}/export | `csv` or `json` | Frozen export marked DRAFT / SIMULATED; formula-escaped CSV |
| GET /audit-events | Object ID, cursor | History |
| GET /health | None | Database, storage and modes; never a model call |

**Errors.** Body: `error.code`, `message`, `field_errors`, `retryable`, `correlation_id`. HTTP 401, 403, 404, 409 (stale version or key conflict), 413, 422 (domain refusal), 429, 503\. Domain codes include `ITEM_HELD`, `STALE_VERSION`, `NEEDS_HUMAN_REVIEW_NOT_APPROVABLE`, `PATH_MISMATCH`, `ALREADY_DECIDED`, `BAD_SIGNATURE` and `FORBIDDEN_FIELD`. No refusal or error ever produces a result, an amount or a zero.

**Determination payload (case 1):**

`{`  
  `"id": "det_case1", "line_id": "line_case1", "jurisdiction": "WA",`  
  `"service_period_start": "2026-11-01", "facts_version": 3, "version": 1,`  
  `"data_mode": "synthetic", "engine_mode": "mock_reviewed_ruleset",`  
  `"label": "ENGINE_SUPPORTED", "nhr_reason": null,`  
  `"direction": "LIABILITY", "derived_direction": true,`  
  `"rate": "0.1055", "location_code": "1726",`  
  `"expected_tax": { "value": "151.92", "currency": "USD" },`  
  `"engine_amount": { "value": "151.92", "currency": "USD" },`  
  `"rule_reference": "WA-RAS-001",`  
  `"evidence_ids": ["ev_seat_export_1"], "unit_status": "READY_FOR_REVIEW"`  
`}`

**Totals payload (no net field, no combined total):**

`{`  
  `"period_id": "wa_2026q4",`  
  `"approved_liabilities": { "by_jurisdiction": [ { "jurisdiction": "WA", "total": {...}, "line_count": 3 } ] },`  
  `"refund_candidates": { "label": "not recovered cash", "total": {...}, "count": 1 },`  
  `"audit_exposure": { "label": "Illustrative: if an auditor finds it first", "total": {...} },`  
  `"held": { "count": 1, "purchase_amount": {...}, "label": "purchase amount, not tax" },`  
  `"open_needs_human_review": { "count": 1 }, "determination_errors": { "count": 0 }`  
`}`

## 8 Ramp integration

The demo uses a simulator that emits Ramp's real event and record shapes. If the Ramp sandbox (`demo-api.ramp.com`, requested through Ramp's self-serve form) is approved, the same code points at it by changing `RAMP_API_BASE`. Whatever runs on stage is labeled "simulated Ramp webhook" unless it is the sandbox.

* Subscribe to `transactions.receipt_added`, `transactions.cleared` and `bills.created`. Payloads carry IDs only.  
* Verify the HMAC-SHA256 signature in `X-Ramp-Signature` and reject a mismatch with `BAD_SIGNATURE`.  
* Deduplicate on the event id. Events can arrive out of order and are retried.  
* Fetch `GET /developer/v1/transactions/{id}` and `GET /developer/v1/receipts/{id}` after storing the event.

Fields used:

| Ramp field | Used for |
| :---- | :---- |
| `entity_amount` (`amount` is deprecated) | Charge total for the cross-check |
| `merchant_location` {city, state, postal\_code, country} | Explaining how the vendor sourced tax; mismatch flag |
| `merchant_category_code`, `sk_category_name` | Category hint (never routes) |
| `card_holder` {location\_id, location\_name, department\_id} | Location ladder rung 5 (matched office), low confidence; requires named single-office confirmation |
| `merchant_data.receipt.items[]` {description, quantity, unit\_cost, tax, total} | Line items and tax charged |
| `receipts[]`, then `ocr.line_items[]` and `ocr.taxes[]` {tax\_amount, tax\_name, tax\_rate} | Cross-check of the model's extraction |
| Accounting field type `SALES_TAX_CODE` | Later: write the tax code back to Ramp |

The model fills gaps in Ramp's receipt OCR and cross-checks it. It does not replace it, and the demo says so. The production path, which is not built, is an agent on Ramp's MCP (`mcp.ramp.com`) or a Stack skill. Ramp's MCP has no tax or journal-entry tool today, so Tax Leak would supply that layer.

## 9 Azure deployment

Everything runs in the existing resource group in West US 2\.

| Item | Setting |
| :---- | :---- |
| Resource group | `data-rg`, West US 2 (already exists, so the student subscription allows the region). Do not use eastus2. |
| App Service plan | Linux. F1 while building; B1 with Always On from three days before the final |
| Web app | Python 3.12 or later. Startup command `gunicorn -w 1 -k uvicorn.workers.UvicornWorker app.main:app` |
| Identity | System-assigned managed identity with Storage Blob Data Contributor on the storage account |
| Storage | Existing account `ramphackaffanandrj26`, container `data` |
| Database | Managed PostgreSQL Flexible Server (smallest suitable supported tier; B1ms target only if available). TLS and a restricted app role; connection string in App Settings. Verify actual student-offer eligibility and current cost. SQLite optional local-only. |
| Model key | App setting `MODEL_API_KEY`. No Key Vault for one key |
| Monitoring | App Service log stream. No Log Analytics workspace |
| Budget | Cost Management budget of \$75 with alerts at 50%, 75% and 90% |

Monthly cost, West US 2 list prices:

| Design | Monthly | Per day |
| :---- | :---- | :---- |
| v1.5 at list price, workers idle | about \$187 | about \$6.20 |
| v1.5 after student free offers | about \$158 | about \$5.20 |
| v1.5 with active workers and the new Container Apps environment fee | about \$645 to \$720 | about \$21 to \$24 |
| v2: App Service B1 \+ managed Postgres \+ Blob \+ direct model API (revised architecture) | Reprice current region/tier and verified credit; prior SQLite estimate is withdrawn | Unverified; calculate before provisioning |

Budget target remains \$75; subscription credit and offer terms are unverified here. Recalculate hosted Postgres, App Service, Blob and model costs before provisioning and set alerts. This document is a plan, not evidence that infrastructure, access or budget has been verified.

**Setup notes for the coding agents.**

* Confirm the subscription offer under Subscriptions \> Overview (MS-AZR-0170P is Azure for Students) and run `az policy assignment list` to see allowed regions.  
* Set `SCM_DO_BUILD_DURING_DEPLOYMENT=true` and list `gunicorn` and `uvicorn` in `requirements.txt`.  
* Hosted state goes to managed PostgreSQL; documents/exports go to Blob, never an on-disk SQLite file. Local scratch may be ephemeral and cannot be the system of record. Rationale: App Service /home is persistent shared storage (https\://learn.microsoft.com/en-us/azure/app-service/configure-custom-container\#use-persistent-shared-storage); SQLite documents that WAL does not work over a network filesystem (https\://sqlite.org/wal.html).  
* F1 unloads the app after 20 idle minutes and caps CPU at 60 minutes a day, and the in-process worker only runs while the app is loaded. Use B1 with Always On for the demo.  
* There are no deployment slots below the Standard tier. Test with replay fixtures locally instead of a staging slot.

## 10 Repo structure and environment variables

`app/            FastAPI app: api/, domain/, worker/, adapters/, contracts/, static/`  
`web/            frontend source; build output copied to app/static`  
`rules/          rule entries (yaml), S3 catalog, rate tables (owner-authored)`  
`fixtures/       synthetic documents, expected results, model recordings`  
`tests/          unit, rules, gates (never-fail), api, eval`  
`scripts/        az setup script, seed, demo reset`  
`.github/workflows/deploy.yml`

| Variable | Value |
| :---- | :---- |
| `DATABASE_URL` | `Hosted: PostgreSQL connection URL with TLS (secret App Setting, never printed here). Local: optional SQLite URL.` |
| `AZURE_STORAGE_ACCOUNT_URL` | `https://ramphackaffanandrj26.blob.core.windows.net` |
| `DOCUMENT_CONTAINER` | `data` |
| `MODEL_PROVIDER`, `MODEL_NAME` | `Target MODEL_PROVIDER=openai, MODEL_NAME=gpt-4.1 (unverified; confirm and pin before live use). Alternate adapters are optional; LLM_MODE=replay controls stage/CI replay.` |
| `MODEL_API_KEY` | App setting only, never in the repo |
| `LLM_MODE` | `live`, `record` or `replay` (replay in CI and on stage) |
| `RAMP_API_BASE`, `RAMP_WEBHOOK_SECRET` | Sandbox URL or the simulator; webhook signing secret (app setting) |
| `DATA_MODE`, `ENGINE_MODE`, `DEMO_MODE` | `synthetic`, `mock_reviewed_ruleset`, `true` (startup refuses other values) |
| `SESSION_SECRET` | App setting |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` |

## 11 CI/CD and release gates

One workflow, `.github/workflows/deploy.yml`, runs on every push and pull request: lint, unit and rule tests, never-fail gates and the replay eval. On `main` it deploys with `azure/webapps-deploy@v3`, authenticated through the Deployment Center's user-assigned identity (OIDC), so GitHub holds no Azure secret. Required checks on `main`: `test` and `gates`. No merge queue, no per-PR environments, no Bicep; a short `az` script in `scripts/` creates the plan and the app.

| Gate | Check | Test |
| :---- | :---- | :---- |
| RG-1 | No held, NHR, unreviewed or error unit in any total; held never shown as zero | Property tests on totals and the workpaper builder |
| RG-2 | No net, savings or recovered field or copy | Semantic scan of models/API/UI for combined-liability-minus-refund net or savings/recovered claims; do not reject sourced invoice line net facts or the mandatory not-recovered-cash disclaimer. |
| RG-3 | Engine amounts copied exactly | Decimal string comparison; no float on money paths |
| RG-4 | Every SUPPORTED result has a real reviewed applicable rule/rate reference; placeholder-only/unreviewed entries produce NHR with no amount/approval | Scan of `rules/` and determinations |
| RG-5 | Demo cases 1 to 7 pass | Case tests against the API |
| RG-6 | Every fact citation resolves to its admitted source: document/page/span, named answer/confirmation, or SSO/HRIS record with provenance; every active tax rule/rate resolves to reviewed primary-source URL | Evidence check |
| RG-7 | No duplicate effects | Duplicate webhook event, card plus bill, duplicate mark-ready |
| RG-8 | No "filed" or "paid" without "simulated" | Scan of frontend text and export templates |
| RG-9 | Effective dating | Verified December 2026 CA case selects its verified rule; unresolved January 2027 case stops at NHR RULE\_TRANSITION\_UNRESOLVED with no amount. A placeholder never satisfies SUPPORTED acceptance. |
| H1 and H5 | Two labels; no human-set treatment | Enum tests; reject tax treatment/label/direction/tax-decision amount overrides. Sourced invoice-amount corrections must supersede results and require fresh approval. |

A failing financial gate removes the affected feature from the demo. It is never waived.

## 12 Synthetic data and security

**Synthetic-only rule.** Only synthetic documents exist in any environment and only synthetic documents reach the model API. Data: fictional entity Acme Startup LLC, a fixed list of fictional vendors checked against real company names, fictional addresses with real state names, visibly fake tax IDs and invoice numbers, a "Synthetic document, Tax Leak evaluation" footer, and `data_mode=synthetic` on every record. No real customer, vendor, employee or Ramp document is used, including as a template or example. A pilot with real data needs retention and no-training terms first and is out of scope.

* Check organization ownership on every read, download, export and write. Demo login with seeded named people; every action attributed.  
* HTTP-only secure session cookies, CSRF tokens, exact origin allowlist, TLS. Web and API share one origin.  
* Verify the Ramp webhook signature before storing anything.  
* Validate file signature, size, pages and parse time; no URL ingestion. Treat document text and model output as untrusted; escape rendered content and CSV formulas.  
* Rate-limit uploads and model calls. No card numbers, bank credentials or real tax identifiers.  
* Logs carry IDs, hashes, durations, modes and result codes, never document text, prompts, model output or secrets.

## 13 Demo cases, fixtures and evaluation

Demo amounts and outcomes come from engine output. No script asserts an outcome the rule set does not return.

| Case | Setup | Expected result | Fails if |
| :---- | :---- | :---- | :---- |
| 1 WA liability | Design-tool monthly SaaS service 2026-11-01 to 2026-11-30, synthetic price \$1,440; confirmed Seattle seat export; explicit sourced \$0 vendor tax; invoice date 2026-11-15. Illustrative rate/amount until verified golden under `docs/content-verification.md`. | After the rule/rate and golden are verified: ENGINE\_SUPPORTED, LIABILITY, \$151.92 (illustrative 10.55%, code 1726), WA-RAS-001. Only after verification and approval enter WA Q4 totals/workpaper; before verification, do not publish a supported tax verdict. | Any other amount; rate, code or source missing |
| 2 CA vs WA | Same purchase with a second subscription line, same \$1,440 monthly terms/explicit \$0 vendor tax as Case 1, but confirmed users in San Francisco; November 2026\. | Pending verification of CA-RAS-001 under `docs/content-verification.md`, fixture target CHARGED\_CORRECTLY with no liability, beside Case 1; without a verified rule, show NHR rather than force target. | The two engine requests differ beyond line, jurisdiction and location |
| 3 CA overcharge | Docs-tool monthly subscription \$962.32; San Francisco seat export; service 2026-12-01 to 2026-12-31; invoice date 2026-12-15; sourced vendor tax \$83.00. | After CA-RAS-001 and the golden are verified under `docs/content-verification.md`: target REFUND\_CANDIDATE \$83.00, separate approved-for-pursuit total and DRAFT letter with verified basis text. Without a verified rule, NHR; no letter sent. | Refund netted or labeled recovered |
| 4 Unresolved CA transition | Case 3 with service 2027-01-01 to 2027-01-31; keep invoice date 2026-12-15 to isolate rule timing from reporting period. | NHR RULE\_TRANSITION\_UNRESOLVED; no tax amount, approval, refund or total contribution. PLACEHOLDER:CA-DP-001 is illustrative metadata only until an applicable rule, rate and new golden are verified under `docs/content-verification.md`. | Supported tax verdict or any amount/approval based only on PLACEHOLDER; old approval/total retained after fact version changes |
| 5 NY hardware | Synthetic laptop \$2,100, confirmed NYC delivery, sourced \$0 vendor tax and \$0 shipping. Positive shipping requires a separately verified base and golden. | After the rule/rate/golden are verified under `docs/content-verification.md`: illustrative LIABILITY \$186.38 (8.875%, code 8081), NY-TPP-001, zero shipping fixture. Unconfirmed delivery holds; form notes require verified basis and stay DRAFT. | Use tax computed from bill-to; missing verified source/rate/location code; unconfirmed delivery treated as admitted |
| 6 Missing evidence | Distributed-team SaaS invoice with only an HQ bill-to; users in several states | Held for "user location by state"; question asks for a seat export or a named person's answer; amount labeled "purchase amount, not tax" | Any tax figure or zero; engine called |
| 7 Replay | The same webhook event sent twice | Existing object returned; totals unchanged. Unfinished durable jobs recover after restart via expired lease; atomic claims, unique input/version keys and transactional completion prevent duplicate effects. | Any duplicate purchase, line or amount |

**Fixtures.** One golden set of 20 to 30 synthetic fixtures with expected results recorded by hand (RJ: extraction expectations; AF: engine outputs and state transitions; each checks the other). Coverage: cases 1 to 7, no tax line vs explicit zero, total mismatch, unreadable page, prompt injection, unsupported category, undeclared state, period boundary invoice dates (2026-09-30, 2026-10-01, 2026-12-31, 2027-01-01), model timeout, and a missing rule reference. Agents never record expected results.

**Never-fail cases.** These run in CI on every push and block merge:

* A held item never shows a tax amount or zero.  
* A refund candidate never reduces a liability total.  
* Model output never contains a tax rate, tax-decision amount, treatment, label, direction or verdict; printed invoice amounts with source evidence are permitted.  
* Bill-to alone never confirms use; matched office address needs a named single-office confirmation and recorded provenance. NY hardware requires confirmed delivery; conflicting or incomplete evidence holds.  
* A duplicate webhook never creates a second purchase or amount.  
* December 2026 CA service selects the verified rule; unresolved January 2027 service routes NHR with no amount. Placeholder-only entries never produce supported determinations.

**Eval table for round 2\.** Per-field extraction accuracy; precision and recall for LIABILITY and REFUND\_CANDIDATE; hold rate; never-fail pass count; cost and latency per transaction. Replay runs in CI. A live run on the golden set happens before each round, and its results go in the round 2 submission with two or three real failures and the regression test each one produced.

## 14 Demo requirements

* **Simulated swipe. POST /demo/events emits a Ramp-shaped transactions.receipt\_added. Flag within 10 seconds using labeled recorded extraction; deterministic rules run on the current admitted facts. Flow is flag → review → named human approval → approved liability total updates. A webhook or flag alone never changes an approved total.**  
* **Judge interaction. A sourced demo fact edit changes Case 3's service month to January 2027, supersedes any old approval and reruns admission plus deterministic rules. Case 4 shows NHR RULE\_TRANSITION\_UNRESOLVED, not a supported placeholder tax verdict. Recorded model extraction stays visibly labeled replay; live rule recalculation is not live model inference.**  
* **Trace view.** Every result shows the receipt, the extracted JSON, the rule ID with its source link, the rate, the location code and the math.  
* **Hold on purpose.** Case 6 is shown, and the hold rate is on the dashboard.  
* **Figures. Approved liabilities and refund candidates (not recovered cash) stay separate. Optional P1 audit exposure is off by default; enable only after owners sign source-reviewed lookback/interest/assumptions, clearly illustrative, approved current liabilities only. No held/NHR/unreviewed/error inputs or unsupported savings/net claims.**  
* **Stage mode.** `LLM_MODE=replay`, B1 with Always On, `main` frozen 24 hours before, the page loaded once before going on stage, and a 60-second backup recording on the laptop.

## 15 Work breakdown

Ordered by dependency. One issue, one pull request.

| \# | Item | Owner | Depends on | Done when |
| :---- | :---- | :---- | :---- | :---- |
| 1 | Skeleton and contracts: FastAPI, static frontend, managed Postgres hosted (SQLite local optional), durable jobs/worker, closed schemas and golden shapes, deploy workflow, App Service | AF | None | A push to main deploys; `/health` is green |
| 2 | Rule and rate tables plus engine (CA, WA, NY, effective dates) with unit tests | AF | 1 | Evaluator passes contract tests and synthetic unit fixtures, including no-adjustment and unsupported/NHR paths; authoritative case goldens/signoff are not prerequisites for scaffolding. |
| 3 | S3 catalog: draft, then verify under `docs/content-verification.md` | RJ | 2 | Status verified |
| 4 | DocumentExtractor: provider interface, strict schema, forbidden-field check, replay cache | RJ | 1 | Replay extraction for synthetic contract examples passes the extraction schema; final verified golden set follows in item 5\. |
| 5 | Fixtures and golden set with expected results | RJ (extraction), AF (engine) | 2, 4 | 20–30 fixtures with verified expected outputs for Cases 1–7, including NHR/no-amount paths, committed; engine rounding basis is verified under `docs/content-verification.md`. |
| 6 | Ramp simulator and webhook intake: signature, dedup, fetch | AF | 1 | Case 7 passes |
| 7 | Fact admission, Ramp OCR cross-check, hold check, jurisdiction set, questions | AF (RJ on question wording) | 3, 4 | Case 6 holds correctly |
| 8 | Review queue, approvals, totals, journal entry and refund letter drafts | AF | 2, 5, 7 | Cases 1 to 5 run end to end in the UI |
| 9 | Workpaper (WA Q4 2026), mark ready, export | AF | 8 | RG-1 and RG-8 pass |
| 10 | Never-fail gates and the eval table | RJ | 5, 8, 9 | Cases 1–7 match verified outputs and all release gates are green in CI. |
| 11 | Demo hardening: replay mode, judge interaction, backup recording, round 2 video | Both | 9, 10 | Two full run-throughs without an error |

Dependency order: 1 (skeleton \+ frozen contracts/golden shapes) first; then 2 (engine evaluator) and 4 (extractor) in parallel, with 6 (simulator/intake). 3 follows 2 for verification under `docs/content-verification.md` and gates 7 only. 5 follows 2 \+ 4 with verified expectations, before final UI integration and gates. Mock UI may start after 1; integrated 8 requires 2 \+ 5 \+ 7\. Then 9; final 10 requires 5 \+ 8 \+ 9; 11 requires 9 \+ 10\. Gate scaffolding may start early, but fixture-dependent checks cannot run before their inputs exist.

**Never cut: H1 to H5; Case 6 hold; Case 3 approved refund candidate with separate total and DRAFT letter; Case 4 safe unresolved-transition NHR; RG-1 to RG-9; DRAFT and SIMULATED labels; labeled replay fallback.** If the model API is unavailable, show the labeled replay or human-supplied facts.

## 16 Changes from v1.5

What changed from the design in Tab 1, and why.

| Area | v1.5 | v2 | Reason |
| :---- | :---- | :---- | :---- |
| Hosting | Three Container Apps, container registry, Key Vault, App Insights; per-PR, staging and demo environments | One App Service | v1.5 costs about \$158 to \$720 a month against a \$75 ceiling and a \$100 credit |
| Model | Azure OpenAI gpt-4.1, fallback gpt-4o, then GPT-5 | Direct API behind a provider interface | Affan prefers direct API and no Azure OpenAI dependency. Actual provider entitlement/model availability is unverified; confirm target before live use, otherwise use labeled replay. |
| Region | eastus2 | westus2 | Student subscriptions were blocked from eastus2 in several 2026 reports; data-rg already works in westus2 |
| Database | Postgres per environment and per PR | Managed Postgres hosted; optional SQLite local-only | App Service persistent /home is shared storage; SQLite WAL is not a supported network-filesystem design. One app still uses durable managed Postgres. |
| Frontend | Next.js server with an /api proxy and a six-check proxy gate | Static frontend served by FastAPI | One origin with no proxy |
| Worker | Separate Container App, one replica | Thread in the same process | Same lease logic with no extra resource |
| Coverage | CA and WA SaaS | CA, WA and NY; SaaS plus one hardware case | Matches the pitch examples |
| California | Not taxable | 2026 verified-rule fixture only; January 2027 transition unresolved → NHR, no amount | Prior transition claim is unverified; applicable primary sources, dates and rule must be verified under `docs/content-verification.md` before activation. |
| Period | WA Q3 2026 | WA Q4 2026 | The Q3 return was due November 2, 2026 |
| Location evidence | Seat export or named answer only | Confirmed five-rung admissible SaaS ladder; bill-to explanatory only, never confirms; hardware delivery evidence separate | Explains vendor charges and matches state sourcing rules |
| Filing | Lock, nine SIM states, correction records | DRAFT, then READY\_TO\_FILE\_SIMULATED | Judges never see the state machine |
| CI | Nine required checks, merge queue, four eval tiers, contract freeze | One workflow, two required checks, golden set and never-fail cases | Two people |
| Ingest | Simulated events | Ramp webhook shapes, sandbox when available | Makes the live swipe possible |

## 17 Sources

* [RCW 82.04.050](https://app.leg.wa.gov/RCW/default.aspx?cite=82.04.050)  
* [WAC 458-20-15503](https://app.leg.wa.gov/wac/default.aspx?cite=458-20-15503&full=true)  
* [WA DOR interim guidance, ESSB 5814 IT services](https://www.dor.wa.gov/laws-rules/interim_guidance_statements/interim-guidance-statement-regarding-changes-made-essb-5814-information-technology-services)  
* [WA DOR local sales and use tax rate table](https://dor.wa.gov/taxes-rates/sales-use-tax-rates/local-sales-use-tax/local-sales-use-tax-rate-table?page=3)  
* [WA DOR rate lookup URL interface](https://dor.wa.gov/wa-sales-tax-rate-lookup-url-interface)  
* [WA DOR filing frequencies](https://dor.wa.gov/node/512)  
* [RCW 82.32.050](https://lawfilesext.leg.wa.gov/Law/RCW/RCW%20%2082%20%20TITLE/RCW%20%2082%20.%2032%20%20CHAPTER/RCW%20%2082%20.%2032%20.050.htm) and [RCW 82.32.090](https://lawfilesext.leg.wa.gov/Law/RCW/RCW%20%2082%20%20TITLE/RCW%20%2082%20.%2032%20%20CHAPTER/RCW%20%2082%20.%2032%20.090.htm)  
* [CDTFA Regulation 1502](https://www.cdtfa.ca.gov/lawguides/vol1/sutr/1502.html)  
* [CDTFA Special Notice L-1036 (SB 122\)](https://cdtfa.ca.gov/formspubs/l1036.pdf)  
* [RTC 6901.5](https://cdtfa.ca.gov/lawguides/vol1/sutl/6901-5.html) and [McClain v. Sav-On Drugs (2019)](https://law.justia.com/cases/california/supreme-court/2019/s241471.html)  
* [CDTFA use tax for business use](https://www.cdtfa.ca.gov/taxes-and-fees/use-tax/business-use.htm)  
* [NY TB-ST-128](https://www.tax.ny.gov/pdf/tg_bulletins/sales/b14_128s.pdf), [NY use tax for businesses](https://tax.ny.gov/pubs_and_bulls/tg_bulletins/st/use_tax_for_businesses.htm) and [NY Pub 718](https://www.tax.ny.gov/pdf/publications/sales/pub718.pdf)  
* [Ramp webhooks guide](https://docs.ramp.com/llms-guides/webhooks.txt), [Ramp API reference](https://docs.ramp.com/llms-api.txt) and [Ramp sandbox guide](https://docs.ramp.com/llms-guides/sandbox.txt)  
* [Azure OpenAI quotas and limits](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/quotas-limits) and [Azure for Students FAQ](https://learn.microsoft.com/en-us/azure/education-hub/faq)  
* [Configure Python on App Service](https://learn.microsoft.com/en-us/azure/app-service/configure-language-python) and [Deploy to App Service with GitHub Actions](https://learn.microsoft.com/en-us/azure/app-service/deploy-github-actions)

