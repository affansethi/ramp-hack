.PHONY: check lint typecheck test verify-content demo-reset demo-local

DEMO_BASE_URL ?= http://127.0.0.1:8000
DEMO_PORT ?= 8000

# One command for lint, type check, the content gate and tests (AGENTS.md).
check: lint typecheck verify-content test

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy app

verify-content:
	python -m scripts.verify_content

test:
	pytest

# Wipe and reseed the synthetic demo data on a running app (local or hosted).
# The app must already be running; start it with `make demo-local`.
demo-reset:
	python3 scripts/demo_reset.py --base-url $(DEMO_BASE_URL)

# Local SQLite backup in replay mode: the same app, no Azure dependency.
# Then run `make demo-reset` in another terminal to start clean.
demo-local:
	DATA_MODE=synthetic ENGINE_MODE=mock_reviewed_ruleset DEMO_MODE=true LLM_MODE=replay DATABASE_URL=sqlite:///./taxleak-local.db uvicorn app.main:app --port $(DEMO_PORT)
