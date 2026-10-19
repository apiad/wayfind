.PHONY: check test lint format-check typecheck

check: format-check lint typecheck test

test:
	uv run pytest -q

lint:
	uv run ruff check src/ tests/

format-check:
	uv run ruff format --check src/ tests/

typecheck:
	uv run ty check src/
