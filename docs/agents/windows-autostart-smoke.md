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

## Packaged uninstall cleanup

Run the focused uninstall harness in the clean Windows smoke VM for each
packaged flavor. It installs and enables autostart, verifies owned cleanup while
preserving an unrelated control task, reinstalls and checks one valid
registration, then proves that an overwritten same-name mismatch is preserved:

```powershell
scripts/Test-WindowsAutostartUninstall.ps1 -Installer <path> -Flavor normal
scripts/Test-WindowsAutostartUninstall.ps1 -Installer <path> -Flavor debug
```

The normal and debug packages currently share the merged runtime identity
(`CodexHub`, `CodexHubProxy`, and the CodexHub per-user install directory); the
debug boundary is distinguished by its `_debug` installer artifact and compiled
diagnostics capability. The harness selects and validates those flavor contracts
explicitly rather than inferring identity from an environment variable.

The harness and installer diagnostics deliberately report only task disposition;
they do not print the executable path or user identity.
