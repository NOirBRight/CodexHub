@echo off
setlocal
if not defined CODEXHUB_RUNTIME_HOME exit /b 21
if not defined CODEXHUB_CODEX_TARGET_HOME exit /b 22
if not defined CODEXHUB_CODEX_PATH exit /b 34
if not exist "%CODEXHUB_CODEX_PATH%" exit /b 35
if /I not "%HOME%"=="%CD%" exit /b 23
if /I not "%USERPROFILE%"=="%CD%" exit /b 24
if /I not "%APPDATA%"=="%CD%\appdata\roaming" exit /b 25
if /I not "%LOCALAPPDATA%"=="%CD%\appdata\local" exit /b 26
if /I not "%CODEX_HOME%"=="%CODEXHUB_CODEX_TARGET_HOME%" exit /b 27
if defined CODEXHUB_HOST_SESSION exit /b 28
if defined OPENAI_API_KEY exit /b 29
set "budget=%CODEXHUB_RUNTIME_HOME%\proxy\official-context-budget.ready"
set "invocations=%CODEXHUB_RUNTIME_HOME%\proxy\official-bootstrap-invocations.txt"
if /I "%~1"=="refresh-models" goto refresh
if not "%~1"=="" exit /b 30
>>"%invocations%" echo start
if not exist "%budget%" exit /b 31
python.exe "%~dp0fake-debug-gateway.py" --port %CODEXHUB_E2E_GATEWAY_PORT%
exit /b %errorlevel%

:refresh
if not "%~2"=="" exit /b 32
>>"%invocations%" echo refresh-models
set "catalog=%CODEXHUB_RUNTIME_HOME%\model-catalogs"
if not exist "%catalog%" mkdir "%catalog%"
>"%budget%" echo candidate-managed-safe-budget
exit /b 0
