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
rem Prefer the prepared 3.13.14 runtime for repository scripts.  Environment
rem/bootstrap modules must stay on the local or host 3.13 runtime because the
rem embedded application runtime intentionally does not contain venv, pip, or
rem pytest.
set "CODEXHUB_RESOLVER_OPTIONS=-PreferBundled"
if /I "%~1"=="-m" (
  if /I "%~2"=="pytest" set "CODEXHUB_RESOLVER_OPTIONS=-RequirePytest"
  if /I "%~2"=="pip" set "CODEXHUB_RESOLVER_OPTIONS="
  if /I "%~2"=="venv" set "CODEXHUB_RESOLVER_OPTIONS="
  if /I "%~2"=="ensurepip" set "CODEXHUB_RESOLVER_OPTIONS="
)
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
