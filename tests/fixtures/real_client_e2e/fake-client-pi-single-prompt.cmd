@echo off
setlocal
if defined CODEXHUB_E2E_VERSION_PROBE goto delegate
if defined CODEXHUB_E2E_GUI_CLIENT goto delegate
if /I not "%CODEXHUB_E2E_CLIENT%"=="pi" exit /b 51
call :check_prompt %*
if errorlevel 1 exit /b %errorlevel%

:delegate
call "%~dp0fake-client-real-contract.cmd" %*
exit /b %errorlevel%

:check_prompt
for /L %%N in (1,1,12) do shift /1
if not "%~2"=="" exit /b 52
if not "%~1"=="Use exactly one read-only tool call to read ./sentinel.txt. Then reply with only this exact line and no other text: %CODEXHUB_E2E_SENTINEL%" exit /b 53
exit /b 0
