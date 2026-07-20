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
findstr.exe /l /c:"127.0.0.1:19190" "%ZCODE_CATALOG%" >nul || exit /b 24
echo launched>"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%"
ping.exe 127.0.0.1 -t >nul
