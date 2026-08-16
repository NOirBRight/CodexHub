@echo off
if defined CODEXHUB_E2E_VERSION_PROBE goto delegate
if defined CODEXHUB_E2E_GUI_CLIENT goto delegate
call "%~dp0run-fixture-python.cmd" "%~dp0validate-client-routing.py" "%CD%" "%CODEXHUB_E2E_CLIENT%"
if errorlevel 1 exit /b 26
:delegate
call "%~dp0fake-client-real-contract.cmd" %*
exit /b %errorlevel%
