@echo off
if not defined CODEXHUB_E2E_CASE exit /b 0
echo {"event":"error","status":500}
for /l %%i in (1,1,1500) do echo Authorization: Bearer fixture-private-token C:\Users\private-account
echo api_key=fixture-private-token 1>&2
