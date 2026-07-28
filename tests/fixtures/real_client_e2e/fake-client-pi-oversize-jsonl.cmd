@echo off
if defined CODEXHUB_E2E_VERSION_PROBE (
  call "%~dp0fake-client-real-contract.cmd" %*
  exit /b %ERRORLEVEL%
)
if /I "%CODEXHUB_E2E_CLIENT%"=="pi" if /I "%CODEXHUB_E2E_GATEWAY_MODEL%"=="openai/gpt-5.6-luna" (
  for /L %%I in (1,1,8000) do echo {"type":"message_update","assistantMessageEvent":{"type":"thinking_delta","delta":"bounded-padding-bounded-padding-bounded-padding-bounded-padding-bounded-padding"}}
)
call "%~dp0fake-client-real-contract.cmd" %*
exit /b %ERRORLEVEL%
