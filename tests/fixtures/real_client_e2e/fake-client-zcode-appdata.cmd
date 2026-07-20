@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_EXPECTED_VERSION%
  exit /b 0
)
if not "%CODEXHUB_E2E_GUI_CLIENT%"=="zcode" exit /b 20
if /I not "%APPDATA%"=="%CD%\appdata\roaming" exit /b 21
set "ZCODE_CATALOG=%APPDATA%\ZCode\model-providers\codexhub.json"
if not exist "%ZCODE_CATALOG%" exit /b 22
if exist "%CD%\appdata\ZCode\model-providers\codexhub.json" exit /b 23
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0validate-zcode-structures.ps1" -CaseRoot "%CD%"
if errorlevel 1 exit /b 24
echo launched>"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%"
ping.exe 127.0.0.1 -t >nul
