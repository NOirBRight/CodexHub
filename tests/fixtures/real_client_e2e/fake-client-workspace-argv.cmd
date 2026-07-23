@echo off
setlocal EnableDelayedExpansion
if defined CODEXHUB_E2E_VERSION_PROBE goto delegate
if defined CODEXHUB_E2E_GUI_CLIENT goto delegate
if not defined CODEXHUB_E2E_CASE exit /b 30

if "%CODEXHUB_E2E_CLIENT%"=="codex-cli" (
  set /p "PROMPT_INPUT="
  echo(!PROMPT_INPUT! | findstr.exe /l /c:"./sentinel.txt" >nul || exit /b 31
  echo %* | findstr.exe /l /c:"-C %CD%" >nul || exit /b 32
  for %%A in (%*) do set "LAST_ARG=%%~A"
  if not "!LAST_ARG!"=="-" exit /b 33
) else if "%CODEXHUB_E2E_CLIENT%"=="opencode" (
  echo %* | findstr.exe /l /c:"./sentinel.txt" >nul || exit /b 31
  echo %* | findstr.exe /l /c:"--dir %CD%" >nul || exit /b 34
  echo %* | findstr.exe /l /c:"--title codexhub-real-client-e2e" >nul || exit /b 35
  echo %* | findstr.exe /l /c:"--pure" >nul || exit /b 36
) else if "%CODEXHUB_E2E_CLIENT%"=="pi" (
  echo %* | findstr.exe /l /c:"./sentinel.txt" >nul || exit /b 31
  echo %* | findstr.exe /l /c:"--tools read" >nul || exit /b 37
  echo %* | findstr.exe /l /c:"--no-context-files" >nul || exit /b 38
  echo %* | findstr.exe /l /c:"--no-extensions" >nul || exit /b 39
  echo %* | findstr.exe /l /c:"--no-skills" >nul || exit /b 40
  echo %* | findstr.exe /l /c:"--no-prompt-templates" >nul || exit /b 41
) else if "%CODEXHUB_E2E_CLIENT%"=="omp" (
  echo %* | findstr.exe /l /c:"./sentinel.txt" >nul || exit /b 31
  echo %* | findstr.exe /l /c:"--cwd %CD%" >nul || exit /b 42
  echo %* | findstr.exe /l /c:"--tools read" >nul || exit /b 43
  echo %* | findstr.exe /l /c:"--no-title" >nul || exit /b 44
  echo %* | findstr.exe /l /c:"--no-extensions" >nul || exit /b 45
  echo %* | findstr.exe /l /c:"--no-skills" >nul || exit /b 46
  echo %* | findstr.exe /l /c:"--no-rules" >nul || exit /b 47
) else (
  exit /b 48
)

:delegate
call "%~dp0fake-client-real-contract.cmd"
exit /b %errorlevel%
