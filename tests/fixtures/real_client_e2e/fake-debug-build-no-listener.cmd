@echo off
if /I "%~1"=="refresh-models" exit /b 0
if not "%~1"=="" exit /b 39
if not defined CODEXHUB_RUNTIME_HOME exit /b 22
if not exist "%CODEXHUB_RUNTIME_HOME%\proxy\settings.json" exit /b 26
ping.exe 127.0.0.1 -t >nul
