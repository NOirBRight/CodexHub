# Retired self-hosted Actions runners

Routine CodexHub CI no longer uses repository self-hosted runners. The
workflow runs on GitHub-hosted `windows-2025` and `ubuntu-24.04` virtual
machines, which are disposable and do not depend on the developer host,
desktop session, local credentials, or a Paseo workspace.

## Historical inventory

The former registrations were:

| Runner | Former purpose | Retirement state |
|---|---|---|
| `codexhub-windows-x64` (id 21) | Python, synthetic, frontend, Rust, Clippy, release checks | Deregistered; GitHub currently reports zero self-hosted runners |
| `codexhub-linux-x64` (id 22) | Linux `safe_file` compile, lint, and tests | Deregistered; GitHub currently reports zero self-hosted runners |

The registrations were separate from local runner directories, WSL files, and
historical logs. The GitHub registrations have been removed after the Hosted
parity workflow, path-aware `CI / gate`, branch protection, and Beta1 PR checks
passed. Do not restart either listener to unblock a PR. The retirement pass
removed the GitHub registrations but did not delete
`D:\GitHubActions\CodexHub\windows`, WSL data, or logs.

## Hosted CI invariants

- Routine jobs use only GitHub-hosted labels and install/select their exact
  toolchains in the job.
- Cargo caches may contain `~/.cargo/registry` and `~/.cargo/git`; never cache
  `src-tauri/target`.
- No routine job may require a persistent Windows service, desktop GUI
  session, local account credentials, or a developer/Paseo workspace.
- Real-client Desktop/CLI E2E remains local release evidence documented in
  `docs/agents/real-client-e2e.md`.

## If a self-hosted runner is ever reconsidered

This requires a separate architecture decision and security review. It must
not be reintroduced by adding a `self-hosted` label to the current workflow.
The review must cover public-repository fork isolation, host-owned pre-job
guards, registration-token handling, service lifecycle, workspace isolation,
toolchain provisioning, and an explicit rollback to the Hosted workflow.
