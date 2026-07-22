@echo off
setlocal EnableDelayedExpansion
if /I "%~1"=="refresh-models" (
  set "catalog=%CODEXHUB_RUNTIME_HOME%\proxy\model-catalogs"
  if not exist "!catalog!" mkdir "!catalog!"
  python.exe "%~dp0write-catalog.py" "!catalog!\codexhub-model-catalog.json"
  if errorlevel 1 exit /b 37
  exit /b 0
)
if not "%~1"=="" exit /b 39
if not defined CODEXHUB_RUNTIME_HOME exit /b 22
if not exist "%CODEXHUB_RUNTIME_HOME%\proxy\settings.json" exit /b 26
ping.exe 127.0.0.1 -t >nul
