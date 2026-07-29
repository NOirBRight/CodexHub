@echo off
set "OPEN_PROJECT="
for %%A in (%*) do (
  if /I "%%~A"=="--open-project" set "OPEN_PROJECT=1"
)
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_MINIMUM_VERSION%
  exit /b 0
)
if defined CODEXHUB_E2E_GUI_CLIENT (
  if /I "%CODEXHUB_E2E_GUI_CLIENT%"=="desktop" if defined OPEN_PROJECT exit /b 0
  echo launched>"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%"
  python.exe "%~dp0fake-gui-expanding-tree.py" "%CODEXHUB_E2E_GUI_LAUNCH_MARKER%.orphan"
)
exit /b 0
