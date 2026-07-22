@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo codex-cli 0.145.0
  exit /b 0
)
call "%~dp0fake-client-real-contract.cmd" %*
exit /b %errorlevel%
