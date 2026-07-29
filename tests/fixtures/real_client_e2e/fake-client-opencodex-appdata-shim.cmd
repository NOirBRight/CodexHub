@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_MINIMUM_VERSION%
  exit /b 0
)
if not defined CODEXHUB_E2E_CASE exit /b 0
if exist "%APPDATA%\OpenCodex\codex.exe" exit /b 19
echo isolated>"%CD%\opencodex-appdata-isolated.marker"
exit /b 77
