@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_MINIMUM_VERSION%
  exit /b 0
)
if not defined CODEXHUB_E2E_CASE exit /b 0
if "%CODEXHUB_E2E_CASE%"=="codex-cli-volc" (
  echo Authorization: Bearer fixture-private-token C:\Users\private-account 1>&2
  exit /b 7
)
start "" /b "%ComSpec%" /d /s /c "echo started>child-started ^& ping.exe 127.0.0.1 -n 6 ^>nul ^& echo survived^>child-survived"
ping.exe 127.0.0.1 -t >nul
