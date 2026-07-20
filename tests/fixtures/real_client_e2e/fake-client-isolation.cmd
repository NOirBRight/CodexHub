@echo off
if not defined CODEXHUB_E2E_CASE exit /b 0
if /I not "%HOME%"=="%CD%" goto isolation_error
if /I not "%USERPROFILE%"=="%CD%" goto isolation_error
if /I not "%APPDATA%"=="%CD%\appdata\roaming" goto isolation_error
if /I not "%LOCALAPPDATA%"=="%CD%\appdata\local" goto isolation_error
if /I not "%CODEX_HOME%"=="%CD%\.codex" goto isolation_error
if /I not "%XDG_CONFIG_HOME%"=="%CD%\.config" goto isolation_error
if /I not "%TEMP%"=="%CD%\temp" goto isolation_error
if /I not "%TMP%"=="%CD%\temp" goto isolation_error
echo {"event":"model_selected","model":"%CODEXHUB_E2E_MODEL%"}
echo {"event":"tool_call","tool":"read_file","read_only":true}
echo {"event":"stream_delta","text":"%CODEXHUB_E2E_SENTINEL%"}
echo {"event":"request_complete","status":200}
echo {"event":"terminal","classification":"completed"}
exit /b 0

:isolation_error
echo {"event":"error","classification":"isolation_path_missing"}
exit /b 11
