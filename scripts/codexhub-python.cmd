@echo off
setlocal
rem Do not inherit a parent's temporary resolver result into a nested launcher.
set "CODEXHUB_RESOLVED_PYTHON="
set "CODEXHUB_RESOLVER_OPTIONS="
if /I "%~1"=="-m" if /I "%~2"=="pytest" set "CODEXHUB_RESOLVER_OPTIONS=-RequirePytest"
where pwsh.exe >nul 2>&1
if %errorlevel% equ 0 (
  for /f "usebackq delims=" %%P in (`pwsh.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0Resolve-CodexHubPython.ps1" -RepoRoot "%~dp0.." -PrintPath %CODEXHUB_RESOLVER_OPTIONS%`) do set "CODEXHUB_RESOLVED_PYTHON=%%P"
) else (
  for /f "usebackq delims=" %%P in (`powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0Resolve-CodexHubPython.ps1" -RepoRoot "%~dp0.." -PrintPath %CODEXHUB_RESOLVER_OPTIONS%`) do set "CODEXHUB_RESOLVED_PYTHON=%%P"
)
if not defined CODEXHUB_RESOLVED_PYTHON exit /b 127
set "CODEXHUB_PYTHON=%CODEXHUB_RESOLVED_PYTHON%"
set "CODEXHUB_PROXY_PYTHON=%CODEXHUB_RESOLVED_PYTHON%"
set "CODEXHUB_E2E_PYTHON=%CODEXHUB_RESOLVED_PYTHON%"
for %%D in ("%CODEXHUB_RESOLVED_PYTHON%") do set "PATH=%%~dpD;%~dp0;%PATH%"
"%CODEXHUB_RESOLVED_PYTHON%" %*
exit /b %errorlevel%
