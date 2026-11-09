.PHONY: check lint typecheck test

# One command for lint, type check and tests (AGENTS.md).
check: lint typecheck test

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy app

test:
	pytest
