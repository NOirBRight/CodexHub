@echo off
setlocal

rem Never let the fixture inherit a host Python prefix or module search path.
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONSTARTUP="
set "PYTHONUSERBASE="
set "VIRTUAL_ENV="
set "CONDA_PREFIX="
set "CONDA_DEFAULT_ENV="
set "CONDA_PROMPT_MODIFIER="
set "PIPENV_ACTIVE="

if defined CODEXHUB_E2E_PYTHON goto run_explicit
if defined CODEXHUB_PYTHON goto run_repository
if defined CODEXHUB_PROXY_PYTHON goto run_proxy

rem A fixture has no independent runtime contract.  Never select a copied
rem candidate runtime or ambient PATH here: the parent runner must bind the
rem exact repository-selected interpreter explicitly.
echo CodexHub E2E fixture requires an explicit CODEXHUB_E2E_PYTHON binding. 1>&2
exit /b 127

:run_explicit
set "FIXTURE_PYTHON=%CODEXHUB_E2E_PYTHON%"
goto validate_python

:run_repository
set "FIXTURE_PYTHON=%CODEXHUB_PYTHON%"
goto validate_python

:run_proxy
set "FIXTURE_PYTHON=%CODEXHUB_PROXY_PYTHON%"
goto validate_python

:validate_python
call "%FIXTURE_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>&1
if errorlevel 1 (
  echo CodexHub E2E fixture requires Python 3.13 or newer. 1>&2
  exit /b 126
)
call "%FIXTURE_PYTHON%" %*
set "status=%errorlevel%"

:exit_status
exit /b %status%
