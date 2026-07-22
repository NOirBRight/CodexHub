@echo off
python.exe "%~dp0fake-managed-client-config.py" %*
exit /b %errorlevel%
