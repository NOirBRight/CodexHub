@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  if /I "%CODEXHUB_E2E_CLIENT%"=="codex-cli" echo codex-cli 0.144.4
  if /I "%CODEXHUB_E2E_CLIENT%"=="opencode" echo opencode 1.18.3
  if /I "%CODEXHUB_E2E_CLIENT%"=="pi" echo pi 0.80.5
  if /I "%CODEXHUB_E2E_CLIENT%"=="omp" echo omp 17.0.2
  exit /b 0
)
exit /b 0
