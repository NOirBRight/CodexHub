@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_EXPECTED_VERSION%
  exit /b 0
)
if defined CODEXHUB_E2E_GUI_CLIENT (
  echo launched>"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%"
  ping.exe 127.0.0.1 -t >nul
)
if not defined CODEXHUB_E2E_CASE exit /b 0
if not defined CODEXHUB_E2E_CLIENT exit /b 12
if not defined CODEXHUB_E2E_DIAGNOSTICS_PATH exit /b 13
if "%CODEXHUB_E2E_CLIENT%"=="codex-cli" set "CLIENT_CONFIG=%CD%\.codex\config.toml"
if "%CODEXHUB_E2E_CLIENT%"=="opencode" set "CLIENT_CONFIG=%CD%\.config\opencode\opencode.json"
if "%CODEXHUB_E2E_CLIENT%"=="pi" set "CLIENT_CONFIG=%CD%\.pi\agent\models.json"
if "%CODEXHUB_E2E_CLIENT%"=="omp" set "CLIENT_CONFIG=%CD%\.omp\agent\models.yml"
if not exist "%CLIENT_CONFIG%" exit /b 15
findstr /c:"127.0.0.1:19190" "%CLIENT_CONFIG%" >nul || exit /b 16

if "%CODEXHUB_E2E_CLIENT%"=="codex-cli" (
  echo {"type":"thread.started"}
  echo {"type":"item.completed","item":{"type":"command_execution","command":"read sentinel.txt","status":"completed"}}
  echo {"type":"item.completed","item":{"type":"agent_message","text":"%CODEXHUB_E2E_SENTINEL%"}}
  echo {"type":"turn.completed"}
) else if "%CODEXHUB_E2E_CLIENT%"=="opencode" (
  echo {"type":"step_start","part":{"type":"step-start"}}
  echo {"type":"tool_use","part":{"type":"tool","tool":"read","state":{"status":"completed"}}}
  echo {"type":"text","part":{"type":"text","text":"%CODEXHUB_E2E_SENTINEL%"}}
  echo {"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}
) else if "%CODEXHUB_E2E_CLIENT%"=="pi" (
  echo {"type":"tool_execution_end","toolName":"read","isError":false}
  echo {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"%CODEXHUB_E2E_SENTINEL%"}]}}
  echo {"type":"agent_end"}
) else if "%CODEXHUB_E2E_CLIENT%"=="omp" (
  echo {"type":"tool_execution_end","toolName":"read","isError":false}
  echo {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"%CODEXHUB_E2E_SENTINEL%"}]}}
  echo {"type":"agent_end"}
) else (
  exit /b 14
)

echo {"event":"request_start","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%CODEXHUB_E2E_MODEL%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
echo {"event":"request_complete","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%CODEXHUB_E2E_MODEL%","status":200,"terminal_count":1}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
echo {"event":"request_start","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%CODEXHUB_E2E_MODEL%"}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
echo {"event":"request_complete","client_id":"%CODEXHUB_E2E_CLIENT%","model_canonical":"%CODEXHUB_E2E_MODEL%","status":200,"terminal_count":1}>>"%CODEXHUB_E2E_DIAGNOSTICS_PATH%"
