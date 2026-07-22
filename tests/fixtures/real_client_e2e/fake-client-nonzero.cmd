@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_MINIMUM_VERSION%
  exit /b 0
)
if not defined CODEXHUB_E2E_CASE exit /b 0
echo Authorization: Bearer fixture-private-token C:\Users\private-account 1>&2
exit /b 7
