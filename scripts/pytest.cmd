@echo off
setlocal
call "%~dp0codexhub-python.cmd" -m pytest %*
exit /b %errorlevel%
