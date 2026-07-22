@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_MINIMUM_VERSION%
  exit /b 0
)
if defined CODEXHUB_E2E_GUI_CLIENT (
  echo launched>"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%"
  python.exe "%~dp0fake-gui-expanding-tree.py" "%CODEXHUB_E2E_GUI_LAUNCH_MARKER%.orphan"
)
exit /b 0
