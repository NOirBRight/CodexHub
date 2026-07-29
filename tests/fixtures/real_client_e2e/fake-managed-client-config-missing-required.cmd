@echo off
set "CODEXHUB_E2E_MATERIALIZER_MODE=missing-required"
python.exe "%~dp0fake-managed-client-config.py" %*
exit /b %errorlevel%
