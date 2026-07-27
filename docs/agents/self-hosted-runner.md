# Repository self-hosted Actions runners

CodexHub final CI uses two repository-scoped runner instances on one trusted
host. GitHub Actions remains the control plane for workflow triggers,
exact-commit logs, artifacts, Check results, and readback.

## Runner inventory

| Runner | Required labels | Purpose |
|---|---|---|
| Native Windows x64 | `self-hosted`, `Windows`, `X64`, `codexhub-ci-windows-x64` | Python, synthetic real-client contract, frontend, Rust normal/debug, clippy, and release-flavor checks |
| Ubuntu WSL2 x64 | `self-hosted`, `Linux`, `X64`, `codexhub-ci-linux-x64` | Linux-only `safe_file` lint, compile, and tests |

The host runner roots and their `_work` directories must live under a
dedicated `GitHubActions/CodexHub` root. They must never point at the stable
developer checkout or a Paseo Worker worktree.

Each runner is registered at repository scope with the normal OS/architecture
labels plus its repository-specific label. Registration tokens are short-lived secrets:
obtain them immediately before configuration, pass them only to the runner
configuration command, and never write or echo them in the repository, issue
comments, logs, or artifacts.

The WSL2 listener keeps its runner package and user-local runtime dependencies
inside its dedicated Linux root. The Actions runner's .NET runtime uses a
user-local Ubuntu ICU package through `LD_LIBRARY_PATH`; no system package
installation is required. Rust uses the official
`x86_64-unknown-linux-musl` standard library and `rust-lld`, so the standalone
Linux gate does not require a host-wide C compiler.

The Windows listener is started with a dedicated portable Node.js 22 directory
at the front of `PATH`. Python 3.13 and Rust 1.97.1 are also provisioned before
the listener starts. Workflows verify these versions instead of using setup
actions that can attempt machine-level registry or installation cleanup on a
non-administrator runner. Test dependencies belong in job-local environments,
not the host toolchain.

## Public-repository boundary

The repository is public, so arbitrary fork code must not run on the trusted
host. Job-level `if` conditions in a `pull_request` workflow are only
defence-in-depth: the pull request can change that workflow. Each runner
therefore has a host-owned pre-job guard configured with
`ACTIONS_RUNNER_HOOK_JOB_STARTED` in the runner application's `.env` file.
The guard executes before checkout or any workflow step and fails closed
unless all of the following are true:

- `GITHUB_REPOSITORY` is exactly `NOirBRight/CodexHub`;
- the event is one of `pull_request`, `push`, `workflow_dispatch`, or
  `schedule`;
- a `pull_request` event has base and head repositories both exactly
  `NOirBRight/CodexHub`;
- a `push` event targets `refs/heads/dev` or `refs/heads/main`;
- the event payload exists and parses when repository identity must be read.

The hook lives under the dedicated host `GitHubActions/CodexHub/hooks`
directory, outside both the runner application and its `_work` directory.
It is not sourced from the checkout. A non-zero hook exit prevents the job
from running and is visible in the Actions `Set up runner` log. Restart the
listener after installing or changing `.env`, then exercise the guard locally
with accepted same-repository and rejected fork payload fixtures before smoke.

Every final job also verifies that a pull request head belongs to this
repository before selecting a self-hosted runner. Pushes to `dev`/`main`,
manual dispatches, and the scheduled full validation remain eligible. The
host hook is the authoritative boundary; workflow conditions are only an
additional readable assertion.

External contributions require a maintainer-controlled same-repository branch
before CI. Do not replace this guard with `pull_request_target`, which would
mix privileged repository context with untrusted changes.

## Provisioning and smoke

1. Install the current GitHub Actions runner package separately for Windows
   and Linux x64, verifying the published package digest.
2. Configure distinct runner names and work directories with the exact labels
   above.
3. Start both listeners under the dedicated host account. Installing them as
   system services is a separate administrator action; interactive/background
   listeners are sufficient for bounded provisioning evidence.
4. Read back the repository runner inventory and require both runners to be
   `online`.
5. Dispatch `CI` with `validation_scope=runner-smoke` against the exact
   candidate ref. The two five-minute smoke jobs verify checkout, runner
   identity, Python, Git, and the required native toolchain. Windows proves
   Node.js 22, Rust 1.97.1, and Clippy. Linux proves Python 3.13, Rust 1.97.1,
   Clippy, the musl target, and the actual `rust-lld` executable.
6. After the workflow candidate has passed local review and is frozen, dispatch
   `validation_scope=full` once and require all existing final Check names to
   succeed on that exact SHA.

The smoke and full run must also prove that an unmanaged Actions checkout does
not masquerade as a Paseo-managed Workspace. Live Paseo probes remain local; if
such a probe is ever collected by CI, it must emit the typed
`not_applicable_unmanaged_checkout` classification rather than a failure.

Do not use the smoke as acceptance evidence for product changes. Worker and
repair iterations continue to run only the affected local checks; the complete
Actions matrix is reserved for the reviewed frozen SHA.

## Operations

- Keep one listener per configured runner directory. Never run two listeners
  from the same runner configuration or `_work` directory.
- Allow the runner's built-in self-update. If an update fails, stop scheduling
  work, update the package in place, and repeat the bounded smoke.
- Runner workspaces are disposable checkouts. Preserve Actions logs and
  artifacts in GitHub, not local `_work` contents.
- Record per-job wall time from each frozen-SHA run. The checked-in timeouts are
  hard safety bounds; adjust them only from measured evidence in a separate
  reviewed candidate.
- Stop both listeners before deleting a runner registration or its directory.
- Removing or rotating a runner must not change Actions budget or the account
  `Stop usage` setting.
- A runner that is offline, busy without an associated job, has label drift, or
  points at a developer/Paseo worktree fails the final gate closed.
