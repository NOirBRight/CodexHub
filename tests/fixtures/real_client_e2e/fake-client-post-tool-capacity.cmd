@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_MINIMUM_VERSION%
  exit /b 0
)
if not defined CODEXHUB_E2E_CASE exit /b 0
if not "%CODEXHUB_E2E_ATTEMPT%"=="1" (
  call "%~dp0fake-client-real-contract.cmd" %*
  exit /b %errorlevel%
)
echo {"type":"tool_execution_end","toolName":"read","isError":false}
echo {"event":"request_start","request_id":"%CODEXHUB_E2E_CASE%-attempt-1-request-1","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%CODEXHUB_E2E_GATEWAY_MODEL%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
echo {"event":"request_complete","request_id":"%CODEXHUB_E2E_CASE%-attempt-1-request-1","method":"POST","model":"%CODEXHUB_E2E_GATEWAY_MODEL%","model_canonical":"%CODEXHUB_E2E_GATEWAY_MODEL%","status":200,"duration_ms":1,"client_id":"%CODEXHUB_E2E_CLIENT%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
echo {"event":"request_start","request_id":"%CODEXHUB_E2E_CASE%-attempt-1-request-2","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%CODEXHUB_E2E_GATEWAY_MODEL%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
echo {"event":"request_error","request_id":"%CODEXHUB_E2E_CASE%-attempt-1-request-2","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%CODEXHUB_E2E_GATEWAY_MODEL%","status":503}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
exit /b 9
