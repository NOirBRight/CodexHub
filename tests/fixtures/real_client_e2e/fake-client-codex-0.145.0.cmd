@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  echo codex-cli 0.145.0
  exit /b 0
)
echo(%*| findstr /C:"-a never" >nul && exit /b 21
echo(%*| findstr /C:"--skip-git-repo-check" >nul || exit /b 22
call "%~dp0fake-client-real-contract.cmd" %*
exit /b %errorlevel%
