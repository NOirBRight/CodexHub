@echo off
if not defined CODEXHUB_E2E_CASE exit /b 0
if "%CODEXHUB_E2E_ATTEMPT%"=="1" (
  echo {"event":"provider_capacity","status":429,"output_seen":false}
  exit /b 9
)
echo {"event":"model_selected","model":"%CODEXHUB_E2E_MODEL%"}
echo {"event":"tool_call","tool":"read_file","read_only":true}
echo {"event":"stream_delta","text":"%CODEXHUB_E2E_SENTINEL%"}
echo {"event":"request_complete","status":200}
echo {"event":"terminal","classification":"completed"}
