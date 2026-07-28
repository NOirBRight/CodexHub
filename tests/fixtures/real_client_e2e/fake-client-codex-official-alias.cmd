@echo off
setlocal
if defined CODEXHUB_E2E_VERSION_PROBE goto delegate
if defined CODEXHUB_E2E_GUI_CLIENT goto delegate
if /I "%CODEXHUB_E2E_CLIENT%"=="codex-cli" if /I "%CODEXHUB_E2E_CASE%"=="codex-cli-luna" (
  set "CODEXHUB_E2E_MODEL=openai/gpt-5.6-luna"
  set "CODEXHUB_E2E_GATEWAY_MODEL=openai/gpt-5.6-luna"
)

:delegate
call "%~dp0fake-client-real-contract.cmd" %*
exit /b %errorlevel%
