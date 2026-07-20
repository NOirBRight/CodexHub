@echo off
if not defined CODEXHUB_E2E_CANDIDATE_SHA exit /b 21
if not defined CODEXHUB_RUNTIME_HOME exit /b 22
if not defined CODEXHUB_CODEX_TARGET_HOME exit /b 23
if not defined CODEX_PROXY_GATEWAY_CLIENT_KEY exit /b 24
if not defined VOLCENGINE_API_KEY exit /b 25
if not exist "%CODEXHUB_RUNTIME_HOME%\proxy\settings.json" exit /b 26
if not exist "%CODEXHUB_RUNTIME_HOME%\proxy\config\providers.toml" exit /b 27
if not exist "%CODEXHUB_CODEX_TARGET_HOME%\auth.json" exit /b 28
ping.exe 127.0.0.1 -t >nul
