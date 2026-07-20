@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_EXPECTED_VERSION%
  exit /b 0
)
if not defined CODEXHUB_E2E_CASE exit /b 0
echo {"type":"error"}
echo {"event":"request_error","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%CODEXHUB_E2E_MODEL%","status":500}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
for /l %%i in (1,1,1500) do echo Authorization: Bearer fixture-private-token C:\Users\private-account
echo api_key=fixture-private-token 1>&2
