# POSIX twin of Make.cmd. Keep the two in sync: contributors run whichever their
# platform gives them, and a target that exists in only one is a trap.
.PHONY: help install install-dev lint format type-check test test-cov docs build clean pre-commit all-checks

PY ?= python

help:
	@echo "install      - install the package"
	@echo "install-dev  - install with dev extras + pre-commit hooks"
	@echo "lint         - ruff check"
	@echo "format       - ruff check --fix"
	@echo "type-check   - mypy strict"
	@echo "test         - pytest (excludes integration)"
	@echo "test-cov     - pytest with coverage (gate: 80%)"
	@echo "docs         - regenerate README.MD / SECURITY.MD from templates"
	@echo "build        - build sdist + wheel"
	@echo "clean        - remove build/test artefacts"
	@echo "all-checks   - lint + type-check + test-cov + docs check"

install:
	$(PY) -m pip install -e .

install-dev:
	$(PY) -m pip install -e ".[dev,docs]" && pre-commit install

lint:
	ruff check src tests

format:
	ruff check src tests --fix

type-check:
	mypy src/bg_coolify_migrate

test:
	pytest -q -m "not integration"

test-cov:
	pytest -q -m "not integration" --cov=src/bg_coolify_migrate --cov-report=term-missing

docs:
	$(PY) scripts/generate-docs.py

build:
	$(PY) -m build

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov site

pre-commit:
	pre-commit run --all-files

all-checks: lint type-check test-cov
	$(PY) scripts/generate-docs.py --check
