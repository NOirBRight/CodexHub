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
if not exist "%CODEXHUB_RUNTIME_HOME%\proxy" mkdir "%CODEXHUB_RUNTIME_HOME%\proxy"
set "budget=%CODEXHUB_RUNTIME_HOME%\proxy\official-context-budget.ready"
set "invocations=%CODEXHUB_RUNTIME_HOME%\proxy\official-bootstrap-invocations.txt"
if /I "%~1"=="refresh-models" goto refresh
if not "%~1"=="" exit /b 30
>>"%invocations%" echo start
if not exist "%budget%" (
  >&2 echo published Official catalog contains no safe resolved context budget
  exit /b 31
)
if exist "%~f0.no-listener" (
  ping.exe 127.0.0.1 -t >nul
  exit /b 36
)
python.exe "%~dp0fake-debug-gateway.py" --port %CODEXHUB_E2E_GATEWAY_PORT%
exit /b %errorlevel%

:refresh
if not "%~2"=="" exit /b 32
>>"%invocations%" echo refresh-models
if exist "%~f0.bootstrap-fail" (
  >&2 echo published Official catalog contains no safe resolved context budget
  exit /b 33
)
if exist "%~f0.bootstrap-slow" (
  ping.exe 127.0.0.1 -n 4 >nul
)
set "catalog=%CODEXHUB_RUNTIME_HOME%\proxy\model-catalogs"
if not exist "%catalog%" mkdir "%catalog%"
python.exe "%~dp0write-catalog.py" "%catalog%\codexhub-model-catalog.json"
if errorlevel 1 exit /b 37
>"%budget%" echo candidate-managed-safe-budget
exit /b 0
