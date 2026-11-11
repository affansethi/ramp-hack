.PHONY: check lint typecheck test demo-reset demo-local web-check web-build

DEMO_BASE_URL ?= http://127.0.0.1:8000
DEMO_PORT ?= 8000

# One command for lint, type check and tests (AGENTS.md).
check: lint typecheck test

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy app

test:
	pytest

# Frontend loop (needs Node). `make check` stays Python-only; the UI copy
# gate in tests/gates scans web/src and the committed app/static build.
web-check:
	cd web && npm ci && npm test && npm run build

# Rebuild web/ into app/static (npm run build copies dist/ there).
web-build:
	cd web && npm run build

# Wipe and reseed the synthetic demo data on a running app (local or hosted).
# The app must already be running; start it with `make demo-local`.
demo-reset:
	python3 scripts/demo_reset.py --base-url $(DEMO_BASE_URL)

# Local SQLite backup in replay mode: the same app, no Azure dependency.
# Then run `make demo-reset` in another terminal to start clean.
demo-local:
	DATA_MODE=synthetic ENGINE_MODE=mock_reviewed_ruleset DEMO_MODE=true LLM_MODE=replay DATABASE_URL=sqlite:///./taxleak-local.db uvicorn app.main:app --port $(DEMO_PORT)
