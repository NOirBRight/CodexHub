@echo off
if defined CODEXHUB_E2E_CASE (
  if "%CODEXHUB_E2E_CASE%"=="codex-cli-luna" set "CODEXHUB_E2E_CODEX_TOOL_RESULT=missing"
  if "%CODEXHUB_E2E_CASE%"=="codex-cli-volc" set "CODEXHUB_E2E_CODEX_TOOL_RESULT=failed"
)
call "%~dp0fake-client-real-contract.cmd" %*
exit /b %errorlevel%
