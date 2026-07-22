@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_MINIMUM_VERSION%
  exit /b 0
)
if not defined CODEXHUB_E2E_CASE exit /b 0
call "%~dp0fake-client-real-contract.cmd" %*
echo {"type":"agent_end"}
echo {"event":"request_start","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%CODEXHUB_E2E_GATEWAY_MODEL%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
echo {"event":"upstream_protocol_fallback","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%CODEXHUB_E2E_GATEWAY_MODEL%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
