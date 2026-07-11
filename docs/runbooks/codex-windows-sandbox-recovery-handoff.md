# Codex Windows Sandbox Recovery Handoff

This document is for an external recovery agent who has no access to the original
conversation. Its first priority is to restore ChatGPT/Codex without destroying user
configuration, login state, task history, package data, or repository changes.

## Current checkpoint

- Date verified: 2026-07-11.
- User config: `%USERPROFILE%\.codex\config.toml`.
- Current native Windows sandbox: `elevated`.
- Pre-elevated backup: `%USERPROFILE%\.codex\config.toml.before-elevated-20260711-090359.bak`.
- The current CodexHub fix baseline is commit `90c2b482` or later. Older 0.1.4 Beta
  portables must not be used for recovery testing.
- At this checkpoint ChatGPT/Codex opens normally, local commands run, the official
  account is available, and elevated sandbox commands complete in both the active
  CodexHub worktree and the repository root.

The current config hash is only a checkpoint, not a permanent invariant:

```text
SHA256 0AE13594FF30FBFFF5E97F1D6A6BCB3D4FCB9375DAC9151BFC4BB0C1C9EC7DA1
```

Always make a fresh safety backup and calculate a fresh hash before recovery work.

## Last action

The elevated sandbox was restored after removing obsolete clean CodexHub and Paseo
worktrees. A controlled App-managed CLI check completed in about 4 seconds in the
active worktree and about 8 seconds at the repository root. CodexHub Beta was then
changed so startup history preflight is read-only and cannot modify an unowned Codex
target.

## Next action if the incident recurs

Do not start by repairing ACLs or reinstalling the app. First capture the current
config hash, sandbox mode, process tree, package state, sandbox log, registered Git
worktrees, and the exact command that hangs. Then use the decision tree below.

## Why

Two different failures occurred and require different treatment:

1. Windows App repair/registration failed with `0x80073D02` while OpenAI.Codex
   processes were still alive.
2. Sandboxed commands hung before `pwsh.exe` or `git.exe` started because
   `codex-windows-sandbox-setup.exe` was spending high kernel CPU in an elevated
   `refresh_only` permission refresh. The captured payload contained 259 read roots.

The second failure stopped after obsolete worktrees were removed. This is strong
evidence that the oversized/stale workspace permission-refresh scope was the trigger.
The exact internal helper function is not known because OpenAI helper symbols and a
stack trace were not available.

## Safety boundary

### Do not perform these actions during first-line recovery

- Do not use Windows **Reset** for the app.
- Do not uninstall or reinstall ChatGPT/Codex.
- Do not delete `%USERPROFILE%\.codex`.
- Do not delete the AppX package-data directory.
- Do not delete `auth.json`, cookies, databases, or rollout JSONL files.
- Do not run `takeown`, `icacls /reset`, or recursive ACL rewrites.
- Do not modify Codex sandbox users, local groups, logon rights, or firewall rules
  without evidence that one of those layers is the failing layer.
- Do not directly launch `ChatGPT.exe` from `C:\Program Files\WindowsApps`.
- Do not run old CodexHub 0.1.4 Beta builds against the real Codex home.
- Do not remove dirty or active Git worktrees, and do not delete worktree directories
  with raw filesystem deletion.
- Do not rewrite SQLite merely because some rollout files lack index rows. Run
  integrity checks first.

Force-stopping ChatGPT/Codex is allowed only when the UI cannot close normally, the
user confirms no task must be preserved, and a normal close attempt has already
failed.

## Phase 1: read-only snapshot

Run from an external PowerShell session, not from the broken Codex task.

```powershell
$ErrorActionPreference = "Stop"
$codexHome = Join-Path $env:USERPROFILE ".codex"
$config = Join-Path $codexHome "config.toml"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = "$config.sandbox-recovery-$stamp.bak"

Copy-Item -LiteralPath $config -Destination $backup
$configHash = (Get-FileHash -LiteralPath $config -Algorithm SHA256).Hash

Get-Process ChatGPT, codex, codex-windows-sandbox-setup, CodexHub, CodexHubBeta `
  -ErrorAction SilentlyContinue |
  Select-Object Id, ProcessName, CPU, StartTime

Get-AppxPackage -Name OpenAI.Codex |
  Select-Object Name, PackageFullName, PackageFamilyName, InstallLocation, Status

Write-Host "Config backup: $backup"
Write-Host "Config SHA256: $configHash"
```

Also record:

```powershell
git -C D:\Workstation\CodexHub worktree list --porcelain
Get-ChildItem "$codexHome\.sandbox" -Force -ErrorAction SilentlyContinue
```

Check both user and project configuration. A trusted repository may contain its own
`.codex\config.toml`, which can override the user config. Remove only a project file
that was proven to be accidentally generated; preserve intentional project settings.

Never include `auth.json`, bearer tokens, API keys, or cookies in the diagnostic
bundle.

## Phase 2: choose the failing layer

### A. The app opens, but local commands hang before the shell starts

Typical evidence:

- external PowerShell runs the same command quickly;
- no child `pwsh.exe` or `git.exe` appears;
- `codex-windows-sandbox-setup.exe` remains active with high CPU;
- the sandbox log or helper payload indicates `refresh_only=true` and many roots.

Actions:

1. Do not change global ACLs.
2. Inspect all registered worktrees and their sizes.
3. For each obsolete worktree, confirm its worktree is clean and its branch is
   preserved.
4. Remove only clean obsolete worktrees through the owning repository:

   ```powershell
   git -C D:\Workstation\CodexHub worktree remove -- "D:\absolute\worktree\path"
   git -C D:\Workstation\CodexHub worktree prune
   ```

5. Repeat the check for Paseo-managed worktrees. Use `git worktree remove` from the
   owning repository; do not recursively delete their directories first.
6. Restart Codex and test one small command before testing the repository root.
7. If immediate work must continue and elevated still hangs, use the temporary
   `unelevated` fallback described below.

Do not remove the main checkout, the current task worktree, a dirty worktree, or the
separate clean-verification worktree unless the user explicitly approves it.

### B. The app is stuck at “Finish Windows setup” or Windows Repair fails

First confirm the package exists and its manifest parses. If AppX logs show
`0x80073D02`, all package processes must exit before registration can finish.

1. Ask the user to close ChatGPT/Codex normally.
2. Confirm `ChatGPT.exe`, App-managed `codex.exe`, and the sandbox setup helper have
   exited.
3. If they cannot exit normally, get explicit approval before stopping the remaining
   processes.
4. Re-register the existing package for the current user:

   ```powershell
   $ErrorActionPreference = "Stop"
   $pkg = Get-AppxPackage -Name OpenAI.Codex
   if (-not $pkg) { throw "OpenAI.Codex package is not registered" }
   $manifest = Join-Path $pkg.InstallLocation "AppxManifest.xml"
   [xml](Get-Content -Raw -LiteralPath $manifest) | Out-Null

   Add-AppxPackage `
     -DisableDevelopmentMode `
     -Register $manifest `
     -ForceApplicationShutdown
   ```

5. Launch through the registered AppsFolder identity:

   ```powershell
   Start-Process explorer.exe "shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"
   ```

Do not launch the package-directory executable directly.

### C. Elevated setup fails with a policy or logon-right error

The official troubleshooting guide identifies UAC denial, blocked local user/group
creation, firewall policy, sandbox-user logon rights, and enterprise policy as common
causes. Error `1385` specifically indicates Windows denied the required logon type.

Collect `%USERPROFILE%\.codex\.sandbox\sandbox.log`, the Windows version, and the
exact error. Use `unelevated` temporarily rather than changing security policy without
an evidence-backed plan.

## Temporary fallback to unelevated

OpenAI documents `elevated` as the preferred Windows-native sandbox and `unelevated`
as a supported but weaker fallback. Use the fallback only to restore the ability to
work while the elevated failure is investigated.

1. Back up `config.toml`.
2. Change only this existing value:

   ```toml
   [windows]
   sandbox = "unelevated"
   ```

3. Keep the task permission at **Ask for approval**. Do not compensate by enabling
   unrestricted full access.
4. Restart ChatGPT/Codex and create a new task.
5. Verify a simple PowerShell command and a read-only Git command.

## Restore elevated after the cause is removed

1. Make another timestamped config backup.
2. Confirm there is no accidental project-level `.codex\config.toml` forcing full
   access or another sandbox mode.
3. Change only:

   ```toml
   [windows]
   sandbox = "elevated"
   ```

4. Restart ChatGPT/Codex through its registered app identity.
5. In a new **Ask for approval** task, verify:

   ```powershell
   Write-Output shell-ok
   git status --short --branch
   ```

6. Confirm the shell/Git child process actually starts and
   `codex-windows-sandbox-setup.exe` does not remain at high CPU.
7. Test the active worktree first, then the repository root.
8. If either command hangs, return to `unelevated` using the latest safety backup and
   preserve the elevated failure evidence.

Success means command output is returned, not merely that the UI accepts the prompt.

## CodexHub-specific containment

CodexHub must not close, restart, or force-kill ChatGPT/Codex during route changes.
History synchronization must be online-safe and separate from route switching.

For Beta builds:

- Beta must report that takeover is required until the user explicitly chooses it.
- Startup must be read-only against the real Codex target.
- Before launching a new Beta, hash `%USERPROFILE%\.codex\config.toml`.
- Launch Beta without clicking takeover, wait for startup refresh, then hash the file
  again.
- The bytes must be identical and there must be no new
  `# BEGIN CODEX PROXY SESSION CONFIG` block.
- If the file changes, stop the test, preserve the diff, restore the pre-launch
  backup, and do not retry that build.

Commit `90c2b482` added the read-only startup preflight and routing-owner guard. It is
the minimum acceptable 0.1.4 Beta baseline for this check.

## Final acceptance checklist

- ChatGPT/Codex reaches its normal main window.
- Official account and usage are visible.
- A new Ask-for-approval task can run `Write-Output shell-ok`.
- `git status --short --branch` returns successfully.
- No sandbox setup helper remains stuck at high CPU.
- AppX package status is `Ok`.
- User config contains the intended sandbox mode.
- No accidental project-level full-access override exists.
- CodexHub/Gateway ownership is explicit.
- No CodexHub configuration marker appears unless takeover was explicitly requested.
- Existing history databases pass integrity checks before any migration is attempted.

## Evidence to preserve if escalation is required

- `%USERPROFILE%\.codex\.sandbox\sandbox.log`
- current and pre-incident config hashes, plus a redacted diff
- `Get-AppxPackage -Name OpenAI.Codex` output
- recent AppX deployment events containing the failure code
- process tree showing whether the shell child was ever created
- helper CPU duration and root counts
- `git worktree list --porcelain` for each affected repository
- Windows version and whether the machine is enterprise-managed
- the smallest command that reproduces the failure

The original machine-specific recovery evidence is under
`D:\Users\noirb\Desktop\Codex-App-Recovery-20260710-230001`. Treat it as evidence,
not as a script to replay blindly.

## Official references

- [OpenAI: Windows sandbox](https://developers.openai.com/codex/windows)
- [OpenAI: Codex configuration reference](https://developers.openai.com/codex/config-reference)

The official Windows guide states that `elevated` is preferred, `unelevated` is the
fallback, and persistent failures should be accompanied by the sandbox log and the
machine/error context.

