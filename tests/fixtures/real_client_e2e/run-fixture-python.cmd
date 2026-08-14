@echo off
setlocal

if defined CODEXHUB_E2E_PYTHON goto run_explicit
if defined CODEXHUB_PYTHON goto run_repository
if defined CODEXHUB_PROXY_PYTHON goto run_proxy

rem Prefer the materialized candidate runtime only when the parent did not
rem provide an explicit binding. Do not fall back to py.exe: that can select
rem a different 3.13 installation (or Hermes Python 3.11 through PATH).
if exist "%~dp0python\python.exe" goto run_bundled

echo CodexHub E2E fixture requires an explicit CODEXHUB_E2E_PYTHON binding or a bundled Python 3.13 runtime. 1>&2
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

:run_bundled
set "FIXTURE_PYTHON=%~dp0python\python.exe"

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
