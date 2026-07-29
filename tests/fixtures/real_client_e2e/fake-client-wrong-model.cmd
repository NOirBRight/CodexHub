@echo off
call "%~dp0fake-client-real-contract.cmd" %*
if errorlevel 1 exit /b %errorlevel%
if not defined CODEXHUB_E2E_CASE exit /b 0
echo {"event":"upstream_protocol_fallback","request_id":"%CODEXHUB_E2E_CASE%-attempt-%CODEXHUB_E2E_ATTEMPT%-request-1","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"codexhub-openai/wrong-route"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
exit /b 0
