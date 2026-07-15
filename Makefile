# POSIX twin of Make.cmd. Keep the two in sync: contributors run whichever their
# platform gives them, and a target that exists in only one is a trap.
.PHONY: help install install-dev lint format type-check test test-cov integration e2e e2e-up e2e-down docs build clean pre-commit all-checks

PY ?= python

help:
	@echo "install      - install the package"
	@echo "install-dev  - install with dev extras + pre-commit hooks"
	@echo "lint         - ruff check"
	@echo "format       - ruff check --fix"
	@echo "type-check   - mypy strict"
	@echo "test         - pytest (excludes integration + e2e)"
	@echo "test-cov     - pytest with coverage (gate: 80%)"
	@echo "integration  - real rsync over real sshd (needs docker)"
	@echo "e2e-up       - build the e2e rig: real Coolify + 2 real daemons"
	@echo "e2e          - run the e2e suite against the rig"
	@echo "e2e-down     - tear the rig down, volumes included"
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
	pytest -q -m "not integration and not e2e"

test-cov:
	pytest -q -m "not integration and not e2e" --cov=src/bg_coolify_migrate --cov-report=term-missing

integration:
	pytest -q -m integration

# The e2e suite runs INSIDE the rig's network: Docker Desktop gives the host no
# route to container IPs, and container IPs are what Coolify hands out as the
# servers' addresses.
E2E := docker compose -f tests/e2e/docker-compose.yml

e2e-up:
	$(PY) tests/e2e/prepare.py
	$(E2E) up -d --wait
	$(PY) tests/e2e/bootstrap.py

e2e:
	$(E2E) --profile test run --rm runner

e2e-down:
	$(E2E) down -v
	rm -rf tests/e2e/keys tests/e2e/rig.json tests/e2e/coolify.env

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
