@echo off
set "CODEXHUB_E2E_MATERIALIZER_MODE=unsafe-output"
call "%~dp0run-fixture-python.cmd" "%~dp0fake-managed-client-config.py" %*
exit /b %errorlevel%
