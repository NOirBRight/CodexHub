@echo off
setlocal

if defined CODEXHUB_E2E_PYTHON goto run_explicit
if defined CODEXHUB_PYTHON goto run_repository
if defined CODEXHUB_PROXY_PYTHON goto run_proxy

rem Prefer the materialized candidate runtime over a launcher whose 3.13
rem registration may have disappeared from the machine between runs.
if exist "%~dp0python\python.exe" goto run_bundled

where py.exe >nul 2>&1
if not errorlevel 1 goto run_launcher

echo CodexHub E2E fixture requires Python 3.13 or newer. 1>&2
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

:run_launcher
py.exe -3.13 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>&1
if errorlevel 1 (
  echo CodexHub E2E fixture requires Python 3.13 or newer. 1>&2
  exit /b 126
)
py.exe -3.13 %*
set "status=%errorlevel%"
goto exit_status

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
