@echo off
REM Windows twin of the Makefile. Keep the two in sync: a target that exists in
REM only one is a trap for whoever is on the other platform.
setlocal
if "%~1"=="" goto help
if /I "%~1"=="help" goto help
if /I "%~1"=="install" goto install
if /I "%~1"=="install-dev" goto install-dev
if /I "%~1"=="lint" goto lint
if /I "%~1"=="format" goto format
if /I "%~1"=="type-check" goto type-check
if /I "%~1"=="test" goto test
if /I "%~1"=="test-cov" goto test-cov
if /I "%~1"=="integration" goto integration
if /I "%~1"=="e2e-up" goto e2e-up
if /I "%~1"=="e2e" goto e2e
if /I "%~1"=="e2e-down" goto e2e-down
if /I "%~1"=="docs" goto docs
if /I "%~1"=="build" goto build
if /I "%~1"=="clean" goto clean
if /I "%~1"=="pre-commit" goto pre-commit
if /I "%~1"=="all-checks" goto all-checks
echo Unknown target: %~1 & exit /b 1

:help
echo install      - install the package
echo install-dev  - install with dev extras + pre-commit hooks
echo lint         - ruff check
echo format       - ruff check --fix
echo type-check   - mypy strict
echo test         - pytest (excludes integration + e2e)
echo test-cov     - pytest with coverage (gate: 80%%)
echo integration  - real rsync over real sshd (needs docker)
echo e2e-up       - build the e2e rig: real Coolify + 2 real daemons
echo e2e          - run the e2e suite against the rig
echo e2e-down     - tear the rig down, volumes included
echo docs         - regenerate README.MD / SECURITY.MD from templates
echo build        - build sdist + wheel
echo clean        - remove build/test artefacts
echo all-checks   - lint + type-check + test-cov + docs check
exit /b 0

:install
python -m pip install -e . & exit /b %ERRORLEVEL%
:install-dev
python -m pip install -e ".[dev,docs]" && pre-commit install & exit /b %ERRORLEVEL%
:lint
ruff check src tests & exit /b %ERRORLEVEL%
:format
ruff check src tests --fix & exit /b %ERRORLEVEL%
:type-check
mypy src/bg_coolify_migrate & exit /b %ERRORLEVEL%
:test
pytest -q -m "not integration and not e2e" & exit /b %ERRORLEVEL%
:test-cov
pytest -q -m "not integration and not e2e" --cov=src/bg_coolify_migrate --cov-report=term-missing & exit /b %ERRORLEVEL%
:integration
pytest -q -m integration & exit /b %ERRORLEVEL%
REM The e2e suite runs INSIDE the rig network: Docker Desktop gives Windows no
REM route to container IPs, and container IPs are what Coolify calls the servers.
:e2e-up
python tests/e2e/prepare.py || exit /b 1
docker compose -f tests/e2e/docker-compose.yml up -d --wait || exit /b 1
python tests/e2e/bootstrap.py & exit /b %ERRORLEVEL%
:e2e
docker compose -f tests/e2e/docker-compose.yml --profile test run --rm runner & exit /b %ERRORLEVEL%
:e2e-down
docker compose -f tests/e2e/docker-compose.yml down -v
if exist "tests\e2e\keys" rmdir /s /q "tests\e2e\keys"
if exist "tests\e2e\rig.json" del /q "tests\e2e\rig.json"
if exist "tests\e2e\coolify.env" del /q "tests\e2e\coolify.env"
exit /b 0
:docs
python scripts/generate-docs.py & exit /b %ERRORLEVEL%
:build
python -m build & exit /b %ERRORLEVEL%
:clean
for %%d in (build dist .pytest_cache .mypy_cache .ruff_cache htmlcov site) do if exist %%d rmdir /s /q %%d
if exist .coverage del /q .coverage
exit /b 0
:pre-commit
pre-commit run --all-files & exit /b %ERRORLEVEL%
:all-checks
call :lint || exit /b 1
call :type-check || exit /b 1
call :test-cov || exit /b 1
python scripts/generate-docs.py --check & exit /b %ERRORLEVEL%
