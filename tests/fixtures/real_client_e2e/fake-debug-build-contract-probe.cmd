@echo off
setlocal EnableDelayedExpansion
if not defined CODEXHUB_E2E_CONTRACT_PROBE_LOG exit /b 50
if /I "%~1"=="refresh-models" (
  set "catalog=%CODEXHUB_RUNTIME_HOME%\proxy\model-catalogs"
  if not exist "!catalog!" mkdir "!catalog!"
  python.exe "%~dp0write-catalog.py" "!catalog!\codexhub-model-catalog.json"
  if errorlevel 1 exit /b 37
  exit /b 0
)
if not "%~1"=="" exit /b 39
if not defined CODEXHUB_E2E_CANDIDATE_SHA exit /b 21
if not defined CODEXHUB_RUNTIME_HOME exit /b 22
if not defined CODEXHUB_CODEX_TARGET_HOME exit /b 23
if not defined CODEX_PROXY_GATEWAY_CLIENT_KEY exit /b 24
if not defined VOLCENGINE_API_KEY exit /b 25
python.exe "%~dp0validate-managed-client-contract-probe.py" "%CODEXHUB_E2E_CONTRACT_PROBE_LOG%"
if errorlevel 1 exit /b 51
python.exe "%~dp0fake-debug-gateway.py" --port %CODEXHUB_E2E_GATEWAY_PORT%
