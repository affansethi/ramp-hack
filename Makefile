.PHONY: check lint typecheck test verify-content

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
