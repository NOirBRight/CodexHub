@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo %CODEXHUB_E2E_EXPECTED_VERSION%
  exit /b 0
)
if defined CODEXHUB_E2E_GUI_CLIENT (
  echo launched>"%CODEXHUB_E2E_GUI_LAUNCH_MARKER%"
  ping.exe 127.0.0.1 -n 2 >nul
  for /L %%I in (1,1,6) do start "" /b cmd.exe /d /s /c "ping.exe 127.0.0.1 -t ^>nul"
  ping.exe 127.0.0.1 -t >nul
)
exit /b 0
