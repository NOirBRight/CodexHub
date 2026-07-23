@echo off
if /I "%CODEXHUB_E2E_GUI_CLIENT%"=="desktop" (
  <nul set /p "=%*">"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%.argv"
)
call "%~dp0fake-client-real-contract.cmd" %*
