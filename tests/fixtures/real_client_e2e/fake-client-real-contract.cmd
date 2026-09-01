@echo off
rem This script-like fixture may spawn Python helpers.  The E2E runner must
rem bind it to the repository-selected interpreter instead of ambient PATH.
if not defined CODEXHUB_E2E_PYTHON exit /b 41
call "%~dp0run-fixture-python.cmd" -c "import os,sys; from pathlib import Path; raise SystemExit(0 if sys.version_info >= (3,13) and Path(sys.executable).resolve() == Path(os.environ['CODEXHUB_E2E_PYTHON']).resolve() else 1)" >nul 2>&1
if errorlevel 1 exit /b 42
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_MINIMUM_VERSION%
  exit /b 0
)
if defined CODEXHUB_E2E_GUI_CLIENT (
  set "OPEN_PROJECT="
  for %%A in (%*) do if /I "%%~A"=="--open-project" set "OPEN_PROJECT=1"
  if /I "%CODEXHUB_E2E_GUI_CLIENT%"=="desktop" if defined OPEN_PROJECT exit /b 0
  echo launched>"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%"
  ping.exe 127.0.0.1 -t >nul
)
if not defined CODEXHUB_E2E_CASE exit /b 0
if not defined CODEXHUB_E2E_CLIENT exit /b 12
if not defined CODEXHUB_E2E_DIAGNOSTICS_PATH exit /b 13
set "GATEWAY_MODEL=%CODEXHUB_E2E_MODEL%"
if /I "%CODEXHUB_E2E_MODEL%"=="codexhub-openai/gpt-5.6-luna" set "GATEWAY_MODEL=openai/gpt-5.6-luna"
if /I "%CODEXHUB_E2E_MODEL%"=="codexhub-opencode-go/muse-spark-1.2-contributor" set "GATEWAY_MODEL=opencode-go/muse-spark-1.2-contributor"
if /I not "%GATEWAY_MODEL%"=="%CODEXHUB_E2E_GATEWAY_MODEL%" exit /b 17
set "PROVIDER_ID=official"
set "UPSTREAM_FORMAT=responses"
set "INBOUND_FORMAT=responses"
if /I "%GATEWAY_MODEL%"=="opencode-go/muse-spark-1.2-contributor" set "PROVIDER_ID=opencode_go"
if defined CODEXHUB_E2E_FORCE_ROUTE_CONTRADICTION (
  set "PROVIDER_ID=wrong-provider"
  set "UPSTREAM_FORMAT=chat_completions"
  set "INBOUND_FORMAT=chat_completions"
)
set "IS_STREAM=true"
if defined CODEXHUB_E2E_FORCE_NONSTREAM set "IS_STREAM=false"
if "%CODEXHUB_E2E_CLIENT%"=="codex-cli" set "CLIENT_CONFIG=%CD%\.codex\config.toml"
if "%CODEXHUB_E2E_CLIENT%"=="opencode" set "CLIENT_CONFIG=%CD%\.config\opencode\opencode.json"
if "%CODEXHUB_E2E_CLIENT%"=="pi" set "CLIENT_CONFIG=%CD%\.pi\agent\models.json"
if "%CODEXHUB_E2E_CLIENT%"=="omp" set "CLIENT_CONFIG=%CD%\.omp\agent\models.yml"
if not exist "%CLIENT_CONFIG%" exit /b 15
findstr /c:"127.0.0.1:19190" "%CLIENT_CONFIG%" >nul || exit /b 16

if "%CODEXHUB_E2E_CLIENT%"=="codex-cli" (
  echo {"type":"thread.started"}
  if "%CODEXHUB_E2E_CODEX_TOOL_RESULT%"=="missing" (
    echo {"type":"item.completed","item":{"type":"command_execution","command":"read sentinel.txt"}}
  ) else if "%CODEXHUB_E2E_CODEX_TOOL_RESULT%"=="failed" (
    echo {"type":"item.completed","item":{"type":"command_execution","command":"read sentinel.txt","status":"completed","exit_code":7}}
  ) else (
    echo {"type":"item.completed","item":{"type":"command_execution","command":"Get-Content -Raw -LiteralPath ./sentinel.txt","status":"completed","exit_code":0}}
  )
  echo {"type":"item.completed","item":{"type":"agent_message","text":"%CODEXHUB_E2E_SENTINEL%"}}
  echo {"type":"turn.completed"}
) else if "%CODEXHUB_E2E_CLIENT%"=="opencode" (
  echo {"type":"step_start","part":{"type":"step-start"}}
  echo {"type":"tool_use","part":{"type":"tool","tool":"read","state":{"status":"completed"}}}
  echo {"type":"step_finish","part":{"type":"step-finish","reason":"tool-calls"}}
  echo {"type":"text","part":{"type":"text","text":"%CODEXHUB_E2E_SENTINEL%"}}
  echo {"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}
) else if "%CODEXHUB_E2E_CLIENT%"=="pi" (
  echo {"type":"tool_execution_end","toolName":"read","isError":false}
  echo {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"%CODEXHUB_E2E_SENTINEL%"}],"stopReason":"stop"}}
  echo {"type":"agent_end"}
) else if "%CODEXHUB_E2E_CLIENT%"=="omp" (
  echo {"type":"tool_execution_end","toolName":"read","isError":false}
  echo {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"%CODEXHUB_E2E_SENTINEL%"}],"stopReason":"stop"}}
  echo {"type":"agent_end"}
) else (
  exit /b 14
)

echo {"event":"request_start","request_id":"%CODEXHUB_E2E_CASE%-attempt-%CODEXHUB_E2E_ATTEMPT%-request-1","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%GATEWAY_MODEL%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
echo {"event":"request_complete","request_id":"%CODEXHUB_E2E_CASE%-attempt-%CODEXHUB_E2E_ATTEMPT%-request-1","method":"POST","model":"%GATEWAY_MODEL%","model_requested":"%CODEXHUB_E2E_MODEL%","model_canonical":"%GATEWAY_MODEL%","upstream":"%PROVIDER_ID%","provider_id":"%PROVIDER_ID%","provider_hint":"%PROVIDER_ID%","upstream_format":"%UPSTREAM_FORMAT%","behavior_profile":"third_party_app_transparent_metered","inbound_format":"%INBOUND_FORMAT%","route_reason":"provider_path","route_mode":"codexhub","is_stream":%IS_STREAM%,"status":200,"duration_ms":1,"client_id":"%CODEXHUB_E2E_CLIENT%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
echo {"event":"request_start","request_id":"%CODEXHUB_E2E_CASE%-attempt-%CODEXHUB_E2E_ATTEMPT%-request-2","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%GATEWAY_MODEL%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
echo {"event":"request_complete","request_id":"%CODEXHUB_E2E_CASE%-attempt-%CODEXHUB_E2E_ATTEMPT%-request-2","method":"POST","model":"%GATEWAY_MODEL%","model_requested":"%CODEXHUB_E2E_MODEL%","model_canonical":"%GATEWAY_MODEL%","upstream":"%PROVIDER_ID%","provider_id":"%PROVIDER_ID%","provider_hint":"%PROVIDER_ID%","upstream_format":"%UPSTREAM_FORMAT%","behavior_profile":"third_party_app_transparent_metered","inbound_format":"%INBOUND_FORMAT%","route_reason":"provider_path","route_mode":"codexhub","is_stream":%IS_STREAM%,"status":200,"duration_ms":1,"client_id":"%CODEXHUB_E2E_CLIENT%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
