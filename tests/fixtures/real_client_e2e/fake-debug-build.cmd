@echo off
if not defined CODEXHUB_E2E_CANDIDATE_SHA exit /b 21
if not defined CODEXHUB_RUNTIME_HOME exit /b 22
if not defined CODEXHUB_CODEX_TARGET_HOME exit /b 23
if not defined CODEX_PROXY_GATEWAY_CLIENT_KEY exit /b 24
if not defined VOLCENGINE_API_KEY exit /b 25
if /I not "%HOME%"=="%CD%" exit /b 29
if /I not "%USERPROFILE%"=="%CD%" exit /b 30
if /I not "%APPDATA%"=="%CD%\appdata\roaming" exit /b 31
if /I not "%LOCALAPPDATA%"=="%CD%\appdata\local" exit /b 32
if /I not "%XDG_CONFIG_HOME%"=="%CD%\.config" exit /b 33
if /I not "%TEMP%"=="%CD%\temp" exit /b 34
if /I not "%TMP%"=="%CD%\temp" exit /b 35
if /I not "%CODEX_HOME%"=="%CODEXHUB_CODEX_TARGET_HOME%" exit /b 36
if defined CODEXHUB_HOST_SESSION exit /b 37
if defined OPENAI_API_KEY exit /b 38
if not exist "%CODEXHUB_RUNTIME_HOME%\proxy\settings.json" exit /b 26
if not exist "%CODEXHUB_RUNTIME_HOME%\proxy\config\providers.toml" exit /b 27
if not exist "%CODEXHUB_CODEX_TARGET_HOME%\auth.json" exit /b 28
python.exe "%~dp0fake-debug-gateway.py" --port %CODEXHUB_E2E_GATEWAY_PORT%
