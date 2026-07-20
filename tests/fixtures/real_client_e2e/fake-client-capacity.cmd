@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_EXPECTED_VERSION%
  exit /b 0
)
if not defined CODEXHUB_E2E_CASE exit /b 0
if "%CODEXHUB_E2E_ATTEMPT%"=="1" (
  echo {"event":"request_error","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%CODEXHUB_E2E_GATEWAY_MODEL%","status":429}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
  exit /b 9
)
call "%~dp0fake-client-real-contract.cmd" %*
exit /b %errorlevel%
