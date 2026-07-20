@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_EXPECTED_VERSION%
  exit /b 0
)
if defined CODEXHUB_E2E_GUI_CLIENT (
  echo launched>"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%"
  exit /b 32
)
exit /b 0
