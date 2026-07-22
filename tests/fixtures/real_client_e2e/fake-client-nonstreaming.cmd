@echo off
set "CODEXHUB_E2E_FORCE_NONSTREAM=1"
call "%~dp0fake-client-real-contract.cmd" %*
exit /b %errorlevel%
