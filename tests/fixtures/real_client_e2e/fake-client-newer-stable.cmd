@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  if /I "%CODEXHUB_E2E_CLIENT%"=="codex-cli" echo codex-cli 0.145.0
  if /I "%CODEXHUB_E2E_CLIENT%"=="opencode" echo opencode 1.19.0
  if /I "%CODEXHUB_E2E_CLIENT%"=="pi" echo pi 0.81.0
  if /I "%CODEXHUB_E2E_CLIENT%"=="omp" echo omp 17.1.0
  exit /b 0
)
call "%~dp0fake-client-real-contract.cmd" %*
exit /b %errorlevel%
