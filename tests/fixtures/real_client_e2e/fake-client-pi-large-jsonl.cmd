@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  call "%~dp0fake-client-real-contract.cmd" %*
  exit /b %ERRORLEVEL%
)
if /I "%CODEXHUB_E2E_CLIENT%"=="pi" (
  for /L %%I in (1,1,600) do echo {"type":"message_update","assistantMessageEvent":{"type":"thinking_delta","delta":"bounded-padding-bounded-padding-bounded-padding-bounded-padding-bounded-padding"}}
)
call "%~dp0fake-client-real-contract.cmd" %*
exit /b %ERRORLEVEL%
