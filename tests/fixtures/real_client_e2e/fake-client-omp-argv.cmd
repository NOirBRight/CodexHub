@echo off
if defined CODEXHUB_E2E_VERSION_PROBE goto delegate
if defined CODEXHUB_E2E_GUI_CLIENT goto delegate
if not "%CODEXHUB_E2E_CLIENT%"=="omp" exit /b 20
if not "%~1"=="--print" exit /b 21
if not "%~2"=="--mode" exit /b 22
if not "%~3"=="json" exit /b 23
if not "%~4"=="--model" exit /b 24
if not "%~5"=="%CODEXHUB_E2E_MODEL%" exit /b 25
echo %* | findstr.exe /l /c:"%CODEXHUB_E2E_SENTINEL%" >nul || exit /b 26

:delegate
call "%~dp0fake-client-real-contract.cmd"
exit /b %errorlevel%
