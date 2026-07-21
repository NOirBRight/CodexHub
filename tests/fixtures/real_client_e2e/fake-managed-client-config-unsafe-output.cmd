@echo off
set "CODEXHUB_E2E_MATERIALIZER_MODE=unsafe-output"
python.exe "%~dp0fake-managed-client-config.py" %*
exit /b %errorlevel%
