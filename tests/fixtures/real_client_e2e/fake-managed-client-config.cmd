@echo off
call "%~dp0run-fixture-python.cmd" "%~dp0fake-managed-client-config.py" %*
exit /b %errorlevel%
