# AGENTS.md — working rules for coding agents

Project: Tax Leak (RAMP) — purchase-side sales/use tax review inside a Ramp-shaped spend workflow. Hackathon demo, synthetic data only.

Authoritative docs, in order: `docs/spec/Tax Leak — Design Spec (Ramp Hackathon).md` (product rules), `docs/spec/Tax Leak — Technical Spec.md` **v2 section only** (build plan; the v1.5 section is superseded — never implement it), `docs/architecture.md` (stack and module map), `docs/decisions.md` (why). If an issue or instruction conflicts with the Design Spec, stop and flag it; do not mix old and new instructions.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Local database defaults to SQLite (local scratch only; hosted state is managed PostgreSQL). Set `DATABASE_URL` to override. Blob access uses `DefaultAzureCredential`: run `az login` locally; in Azure it uses the app's managed identity. No keys or connection strings in code. `LLM_MODE=replay` is the default for local runs and CI; `live` needs `MODEL_API_KEY` from your local `.env` (gitignored).

## Run

```bash
uvicorn app.main:app --reload          # local dev
gunicorn -w 1 -k uvicorn.workers.UvicornWorker app.main:app   # hosted (App Service startup command)
```

Exactly one worker process: the background job poller runs in-process.

## Lint / format / type check

```bash
ruff check .
ruff format --check .
mypy app
```

## Test

```bash
pytest                          # everything
pytest tests/unit tests/rules   # fast loop
pytest tests/gates              # never-fail release gates (RG-1..RG-9, H1/H5)
pytest tests/api                # API contract tests
pytest tests/eval               # replay-mode extraction eval (no live model calls)
pytest tests/unit/test_holds.py                  # one file
pytest tests/unit/test_holds.py::test_silent_invoice_holds   # one test
pytest -k "case6"                                  # by name
```

`tests/gates` must stay green on every push; a failing financial gate is never waived.

## Coding conventions

- **Money:** `Decimal` in domain code, decimal strings on the wire (`{"value": "10.00", "currency": "USD"}`), NUMERIC or integer cents in storage. No binary float anywhere on a money path. Engine amount and rule-reference strings are copied exactly; never round, recompute or fill in.
- **Closed enums:** labels, directions, hold/NHR reasons, statuses and modes are fixed sets in `app/contracts/`. Adding a value is a contract change needing both owners (AF and RJ).
- **Hard rules H1–H5:** two labels only; a hold is never a silent zero (no engine call, no amount, no approval); refunds are never netted against liabilities; filing is simulated only (`DRAFT` → `READY_TO_FILE_SIMULATED`); no human-set treatment, label, direction or tax-decision amount — endpoints refuse them.
- **AI boundary:** the extractor proposes source-linked facts only. It never emits rates, tax-decision amounts, treatment, labels or direction; such a field fails with `FORBIDDEN_FIELD`. "No tax line" (null) and "tax line of 0.00" are different values.
- **Tax content:** rule entries, rates, the rounding basis (`rules/rounding.yaml`), the S3 required-fact catalog, golden expected results and filing-basis text (`rules/filing-basis.txt`) activate only through `docs/content-verification.md`: an owner review (`reviewed_by: AF`), or agent verification (`reviewed_by: agent-verified`) with primary-source evidence, a research pass and a blind verification pass by a different lane and model family, and the `make verify-content` gate. Agents never change an owner-reviewed value; they report a contradicting source in a comment and a correction issue. Agents never fill in a value from memory, and never run the app or engine to produce an expected result.
- **Idempotency and versioning:** every mutation takes `Idempotency-Key`; commands carry the expected version; fact changes supersede (never overwrite) prior results and approvals; replaying a webhook or restarting the worker must have no duplicate effects.
- **Synthetic data only** in every environment; every screen keeps the "Synthetic demo data" banner; replayed model output is always labeled replay.
- **Commits/PRs:** one issue, one PR. Match the style of recent commits.

## Repository boundaries

- **Do not edit `.github/` unless the issue asks.** The deploy workflow is live-infrastructure plumbing; changes to it are their own gated work item.
- `rules/` and `fixtures/` expected results change only in issues that follow `docs/content-verification.md`; such PRs are `risk:high`. Contract shapes in `app/contracts/` change only in an issue that asks for the contract change.
- Frontend build output is copied into `app/static/`; edit sources in `web/`, not the built files.
