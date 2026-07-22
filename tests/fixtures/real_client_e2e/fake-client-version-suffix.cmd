@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_MINIMUM_VERSION%-beta
  exit /b 0
)
call "%~dp0fake-client-real-contract.cmd" %*
exit /b %errorlevel%
