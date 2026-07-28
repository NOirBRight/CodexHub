@echo off
set "OPEN_PROJECT="
for %%A in (%*) do if /I "%%~A"=="--open-project" set "OPEN_PROJECT=1"
if not "%CODEXHUB_E2E_GUI_CLIENT%"=="" (
  if /I "%CODEXHUB_E2E_GUI_CLIENT%"=="desktop" if defined OPEN_PROJECT (
    <nul set /p "=%*">"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%.project.argv"
    <nul set /p "=%~f0">"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%.project.executable"
    exit /b 0
  )
  <nul set /p "=%*">"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%.argv"
  <nul set /p "=%~f0">"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%.executable"
  <nul set /p "=%USERNAME%|%USERDOMAIN%">"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%.identity"
)
call "%~dp0fake-client-real-contract.cmd" %*
