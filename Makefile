.PHONY: test lint typecheck check

test:
	python -m pytest tests/ -v

lint:
	ruff check .

typecheck:
	mypy . --ignore-missing-imports

# Run all checks — use this in QA / PR review
check: lint typecheck test
