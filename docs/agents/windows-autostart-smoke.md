# Windows autostart smoke

Run this strict manual check against both an installed package and the supported
portable executable. The harness intentionally sanitizes executable paths from
its output.

Registration uses the Windows Task Scheduler COM API as the current user with
`InteractiveToken` logon and `LeastPrivilege` run level. Run CodexHub normally;
do not elevate it for this check.

```powershell
scripts/Test-WindowsAutostart.ps1 -Executable <path> -Distribution installed -InvokeTask
scripts/Test-WindowsAutostart.ps1 -Executable <path> -Distribution portable -InvokeTask
```

For each distribution, enable autostart, run the deterministic invocation, then
sign out and back in. Confirm Task Manager shows exactly one CodexHub process.
Disable autostart and verify removal:

```powershell
scripts/Test-WindowsAutostart.ps1 -Executable <path> -VerifyDisabled
```

For the portable case, enable autostart, exit CodexHub, move the executable,
and reopen it. The toggle must load as disabled until autostart is enabled again
at the new location. A subsequent real sign-out/sign-in must again produce
exactly one process.
