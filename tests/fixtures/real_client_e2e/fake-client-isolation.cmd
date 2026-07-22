@echo off
if defined CODEXHUB_E2E_VERSION_PROBE goto delegate
if not defined CODEXHUB_E2E_CASE if not defined CODEXHUB_E2E_GUI_CLIENT exit /b 0
if /I not "%HOME%"=="%CD%" goto isolation_error
if /I not "%USERPROFILE%"=="%CD%" goto isolation_error
if /I not "%APPDATA%"=="%CD%\appdata\roaming" goto isolation_error
if /I not "%LOCALAPPDATA%"=="%CD%\appdata\local" goto isolation_error
if /I not "%CODEX_HOME%"=="%CD%\.codex" goto isolation_error
if /I not "%XDG_CONFIG_HOME%"=="%CD%\.config" goto isolation_error
if /I not "%TEMP%"=="%CD%\temp" goto isolation_error
if /I not "%TMP%"=="%CD%\temp" goto isolation_error
if defined CODEXHUB_HOST_SESSION goto isolation_error
if defined OPENAI_API_KEY goto isolation_error
:delegate
call "%~dp0fake-client-real-contract.cmd" %*
exit /b %errorlevel%

:isolation_error
echo {"event":"error","classification":"isolation_path_missing"}
exit /b 11
