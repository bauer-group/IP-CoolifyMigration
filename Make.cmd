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
echo test         - pytest (excludes integration)
echo test-cov     - pytest with coverage (gate: 80%%)
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
pytest -q -m "not integration" & exit /b %ERRORLEVEL%
:test-cov
pytest -q -m "not integration" --cov=src/bg_coolify_migrate --cov-report=term-missing & exit /b %ERRORLEVEL%
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
