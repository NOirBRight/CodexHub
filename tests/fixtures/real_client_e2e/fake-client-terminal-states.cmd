@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_EXPECTED_VERSION%
  exit /b 0
)
if not defined CODEXHUB_E2E_CASE exit /b 0
if not "%CODEXHUB_E2E_CLIENT%"=="pi" if not "%CODEXHUB_E2E_CLIENT%"=="omp" exit /b 20
echo {"type":"tool_execution_end","toolName":"read","isError":false}
if "%CODEXHUB_E2E_CASE%"=="pi-luna" echo {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"%CODEXHUB_E2E_SENTINEL%"}],"stopReason":"error","errorMessage":"fixture-terminal-error"}}
if "%CODEXHUB_E2E_CASE%"=="pi-volc" echo {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"%CODEXHUB_E2E_SENTINEL%"}],"stopReason":"aborted"}}
if "%CODEXHUB_E2E_CASE%"=="omp-luna" echo {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"%CODEXHUB_E2E_SENTINEL%"}],"stopReason":"length"}}
if "%CODEXHUB_E2E_CASE%"=="omp-volc" echo {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"%CODEXHUB_E2E_SENTINEL%"}]}}
echo {"type":"agent_end"}
echo {"event":"request_start","request_id":"%CODEXHUB_E2E_CASE%-attempt-1-request-1","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%CODEXHUB_E2E_GATEWAY_MODEL%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
echo {"event":"request_complete","request_id":"%CODEXHUB_E2E_CASE%-attempt-1-request-1","method":"POST","model":"%CODEXHUB_E2E_GATEWAY_MODEL%","model_canonical":"%CODEXHUB_E2E_GATEWAY_MODEL%","status":200,"duration_ms":1,"client_id":"%CODEXHUB_E2E_CLIENT%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
echo {"event":"request_start","request_id":"%CODEXHUB_E2E_CASE%-attempt-1-request-2","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%CODEXHUB_E2E_GATEWAY_MODEL%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
echo {"event":"request_complete","request_id":"%CODEXHUB_E2E_CASE%-attempt-1-request-2","method":"POST","model":"%CODEXHUB_E2E_GATEWAY_MODEL%","model_canonical":"%CODEXHUB_E2E_GATEWAY_MODEL%","status":200,"duration_ms":1,"client_id":"%CODEXHUB_E2E_CLIENT%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
exit /b 0
