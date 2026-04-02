.PHONY: test lint typecheck check deps-lock

test:
	python -m pytest tests/ -v

lint:
	ruff check .

typecheck:
	mypy . --ignore-missing-imports

# Run all checks — use this in QA / PR review
check: lint typecheck test

deps-lock:
	pip install -r requirements.txt
	pip freeze > requirements-lock.txt
