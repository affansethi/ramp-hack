# Tax Leak (RAMP)

Purchase-side sales/use tax review inside a Ramp-shaped spend workflow. FastAPI backend with an in-process worker, a deterministic rule engine over owner-reviewed `rules/*.yaml`, and replay-mode document extraction. **Hackathon demo — synthetic data only; every screen keeps the "Synthetic demo data" banner.**

Authoritative docs:

- [docs/spec/](docs/spec/) — Design Spec (product rules) and Technical Spec **v2** (build plan; the v1.5 section is superseded — never implement it)
- [docs/architecture.md](docs/architecture.md) — stack and module map
- [docs/glossary.md](docs/glossary.md) — domain terms used in both specs
- [docs/decisions.md](docs/decisions.md) — why

**Live:** https://ramphack-affan.azurewebsites.net (free tier — the first request after idle can take 30+ seconds to wake up.)

## Project layout

```
app/
  main.py            FastAPI app factory; serves API + static frontend
  contracts/         money type, closed enums, AI/engine schemas
  api/               routers (health today; commands/read models to come)
  domain/            pipeline stages (pure-ish; adapters injected)
  worker/            in-process job poller (leases, retries, recovery)
  adapters/          storage.py, extractor.py, engine.py, ramp.py
  static/            built frontend (copied from web/ build output)
rules/               rule entries, S3 catalog, rate tables (owner-authored)
fixtures/            synthetic documents, expected results, recordings
tests/               unit/, rules/, gates/, api/, eval/
docs/                specs, glossary, architecture, decisions
```

## Local setup

```bash
git clone https://github.com/affansethi/ramp-hack.git
cd ramp-hack

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Startup mode guards refuse anything but the synthetic modes — set them in your local `.env` (gitignored) or shell:

```bash
export DATA_MODE=synthetic ENGINE_MODE=mock_reviewed_ruleset DEMO_MODE=true
# LLM_MODE=replay is the default; `live` needs MODEL_API_KEY from .env
```

Local database defaults to SQLite (scratch only; hosted state is managed PostgreSQL). Set `DATABASE_URL` to override.

## Run

```bash
uvicorn app.main:app --reload          # local dev
gunicorn -w 1 -k uvicorn.workers.UvicornWorker app.main:app   # hosted (App Service startup command)
```

Exactly one worker process: the background job poller runs in-process. Health: `GET /health` (also `/api/v1/health`) reports modes and never calls the model.

## Checks

```bash
make check      # ruff check, ruff format --check, mypy app, pytest
```

Or individually per [AGENTS.md](AGENTS.md): `ruff check .`, `ruff format --check .`, `mypy app`, `pytest` (fast loop: `pytest tests/unit tests/rules`).

CI runs the same command in [.github/workflows/ci.yml](.github/workflows/ci.yml) on pull requests and pushes to `main`.

## Storage

Documents live in Azure Blob Storage (`ramphackaffanandrj26` / container `data`), accessed via `DefaultAzureCredential` from `app/adapters/storage.py` — locally your `az login`, in Azure the app's managed identity. No keys or connection strings anywhere. If blob access returns a 403/auth error, ask Affan to give your Azure account the **Storage Blob Data Contributor** role on the storage account (role changes can take a few minutes to apply).

## Deployment

Every push to `main` runs [.github/workflows/deploy.yml](.github/workflows/deploy.yml), which deploys to Azure App Service (OIDC login, no Azure secrets in GitHub). Watch it in the repo's **Actions** tab (or `gh run watch`).

## Team rules

See [AGENTS.md](AGENTS.md): money is `Decimal`/decimal strings end to end; closed enums in `app/contracts/` are owner-gated; hard rules H1–H5; agents never author rule entries, rates, catalog substance or expected results; every mutation takes `Idempotency-Key`; synthetic data only.

## Contributing

One issue, one PR. Before committing, run the checks from [AGENTS.md](AGENTS.md):

```bash
make check
```

That runs `ruff check .`, `ruff format --check .`, `mypy app`, and `pytest`.
