@echo off
setlocal
rem Do not inherit a parent's temporary resolver result into a nested launcher.
set "CODEXHUB_RESOLVED_PYTHON="
set "CODEXHUB_RESOLVER_OPTIONS="
rem Prevent an activated 3.11/Conda/Pipenv environment from changing the
rem selected interpreter after the resolver has validated its executable.
set "PYTHONHOME="
set "PYTHONSTARTUP="
set "PYTHONUSERBASE="
set "VIRTUAL_ENV="
set "CONDA_PREFIX="
set "CONDA_DEFAULT_ENV="
set "CONDA_PROMPT_MODIFIER="
set "PIPENV_ACTIVE="
rem This is the source/development launcher, not the packaged-runtime
rem launcher.  Prefer the checkout/host 3.13 environment so scripts that
rem invoke pytest through sys.executable keep their development modules.
rem Production and packaging entrypoints opt into -PreferBundled directly.
if /I "%~1"=="-m" if /I "%~2"=="pytest" set "CODEXHUB_RESOLVER_OPTIONS=-RequirePytest"
rem The watchdog is a test entrypoint: its child command is normally pytest.
rem Select a pytest-capable development runtime before exporting the binding so
rem the nested launcher cannot inherit a valid 3.13 interpreter without pytest.
if /I "%~nx1"=="run-with-windows-watchdog.py" set "CODEXHUB_RESOLVER_OPTIONS=-RequirePytest"
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
set "PYTHONPATH=%~dp0..\src-python"
for %%D in ("%CODEXHUB_RESOLVED_PYTHON%") do set "PATH=%%~dpD;%~dp0;%PATH%"
"%CODEXHUB_RESOLVED_PYTHON%" %*
exit /b %errorlevel%
