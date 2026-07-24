@echo off
if not "%CODEXHUB_E2E_GUI_CLIENT%"=="" (
  <nul set /p "=%*">"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%.argv"
  <nul set /p "=%~f0">"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%.executable"
  <nul set /p "=%USERNAME%|%USERDOMAIN%">"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%.identity"
)
call "%~dp0fake-client-real-contract.cmd" %*
