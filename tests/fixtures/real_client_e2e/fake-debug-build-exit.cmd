@echo off
if /I "%~1"=="refresh-models" exit /b 0
if not "%~1"=="" exit /b 39
exit /b 31
