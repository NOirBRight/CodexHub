# ADR-0003: Linux platform support coexisting with Windows in one codebase

Date: 2026-08-14
Status: Accepted. Phase 0 (spike) completed 2026-08-14 and the gate passed;
Phases 1–4 are authorized. Recorded at 0.1.8-beta.3.3.

## Context

CodexHub ships today as a Windows-only desktop application: a Tauri 2 / Rust
backend, a bundled Python sidecar Gateway, and a Vite/TypeScript frontend.
A platform-coupling inventory (2026-08-14) established where Windows
assumptions actually live:

- **Rust layer — no blockers.** Every `windows-sys` use sits behind
  `cfg(windows)` (`Cargo.toml` target-specific dependency; `proxy.rs`,
  `safe_file.rs`, `gateway.rs`, `models.rs`, `openai_usage.rs`), and each
  Windows block is paired with a working Unix arm. Process supervision
  (`proxy.rs`), atomic file locking (`safe_file.rs`), autostart
  (`autostart.rs`: Task Scheduler / systemd `--user` / launchd), and bundled
  Python discovery (`runtime_paths.rs:134-236`: Unix arm resolves
  `python/bin/python`) are already tri-platform. `safe_file.rs` already
  compiles standalone for musl on the ubuntu-24.04 CI job.
- **Python sidecar — near-zero coupling.** Pure-stdlib
  `ThreadingHTTPServer`; `atomic_io.py` already falls back to `fcntl` on
  non-NT; all state paths derive from `CODEX_HOME` or `Path.home()`; the
  only vendored wheel (`urllib3`) is pure Python.
- **Frontend — no platform code.** Only i18n copy references
  PowerShell/Windows Terminal (`en-US.ts:332`, `zh-CN.ts:332`).
- **Packaging/release — the real coupling.** `Prepare-PythonRuntime.ps1`
  provisions a Windows embeddable Python; `build-windows-release.ps1`
  produces NSIS-only artifacts and an updater manifest with only
  `platforms."windows-x86_64"`; `ReleaseChannel.ps1` /
  `Test-ReleaseManifest.ps1` hardcode `.exe` names and the Windows platform
  key; `ci.yml` pins windows-2025 for every job except the safe_file Linux
  compile.
- **Residual bounded Rust work.** Unix child processes lack the Windows Job
  Object kill-on-close guarantee (`openai_usage.rs:573-575`,
  `proxy.rs:2953`); the Unix listener→PID path requires `lsof`
  (`proxy.rs:3371`); codex/npm/ZCode discovery assumes Windows layouts and
  contains literal `%APPDATA%` fallback paths (`gateway.rs:3565,3647`);
  `build_info.rs:45-48` hardcodes `*_x64-setup.exe` artifact names.

Prior art anticipating this port already exists in-repo:
`build-flavors.json` carries a `linuxServiceFile` field, and
`autostart.rs` implements and tests the systemd `--user` backend.

## Decision

### One codebase, no fork

Windows and Linux builds ship from the same repository, the same tag, and
the same version number. Platform differences are expressed only through the
two conventions already established: paired `cfg(windows)` /
`cfg(not(windows))` blocks at compile time, and `OperatingSystem` runtime
dispatch (the `autostart.rs:89-106` pattern) where behavior is selected at
runtime. No third convention is introduced. User data layout is unchanged:
`dirs` resolves XDG paths on Linux automatically, and the `CODEX_HOME`
(`~/.codex`) contract is platform-identical.

### Distribution: AppImage primary, deb secondary

Linux artifacts are produced in two formats per release:

- **AppImage** is the primary, updater-enabled channel. It is the only
  Linux format `tauri-plugin-updater` can self-update, preserving the
  Windows quiet-update experience.
- **deb** is the secondary, system-integrated channel (no FUSE requirement,
  proper package-manager install). deb installs do not self-update; the
  updater UI must report this honestly on deb installs.

The updater manifest (`latest.json`) gains a `linux-x86_64` platform entry
alongside `windows-x86_64` in the same GitHub Release; each platform's
client selects its own entry. Artifact naming in `build_info.rs` and the
release scripts becomes per-platform.

### Bundled Python runtime, mirroring the Windows contract

Linux ships a bundled interpreter under `resources/python/` using
python-build-standalone plus a manylinux `zstandard` wheel, provisioned by a
new script that replicates the existing `Prepare-PythonRuntime.ps1`
contract: SHA256-pinned downloads plus a checked-in manifest
(`codexhub-python-runtime.json`). `runtime_paths.rs` already resolves
`python/bin/python` on Unix, so the resource contract is unchanged and the
sidecar remains hermetic against distro Python drift.

### Bounded code changes, then packaging

1. Unix orphan-kill parity for the sidecar and `codex app-server` children:
   `PR_SET_PDEATHSIG` (preferred; closest to Job Object kill-on-close
   semantics) or `setsid` + process-group kill on exit.
2. Listener→PID resolution without `lsof`: `/proc/net/tcp` first, `ss`
   fallback, `lsof` last.
3. Linux-aware executable discovery: npm global bin layouts
   (`~/.npm-global/bin`, `/usr/local/bin`, …); ZCode detection and the
   Codex Desktop (AppX) launch are Windows-only by design and degrade to a
   no-op on Linux; literal `%APPDATA%` fallback paths are removed.
4. Per-platform updater artifact naming and manifest platform keys.
5. Uninstall autostart cleanup parity: the NSIS pre-uninstall hook's job is
   performed by a deb `postrm` maintainer script calling the existing
   `cleanup-autostart-on-uninstall` CLI path; `autostart.rs` already
   implements systemd unit removal.
6. Platform-appropriate i18n copy for shell references.

### Phased execution with a gating spike

- **Phase 0 — spike (gates everything).** Install the two missing Tauri
  system dependencies (`libwebkit2gtk-4.1-dev`, `libsoup-3.0-dev` — the only
  ones absent on the reference machine), run `cargo check` and the pytest
  core partition natively on Linux. Confirms the zero-change compile claim
  and measures the real test portability baseline (estimated ~95% of the
  core partition; Windows-only evidence mocked via `sys.platform`
  patching). Phases 1–4 estimates are revised if Phase 0 surprises.
- **Phase 1 — Rust PORT items 1–4 and i18n copy.**
- **Phase 2 — Linux Python runtime provisioning script; `tauri.conf.json`
  targets `["nsis", "appimage", "deb"]`; resource layout parity.**
- **Phase 3 — local Linux release script mirroring
  `build-windows-release.ps1` outputs (installer + `.sig` + manifest);
  minisign signing is platform-agnostic and unchanged; multi-platform
  `latest.json`; `Test-ReleaseManifest` parameterized by platform; deb
  `postrm`.**
- **Phase 4 — CI ubuntu-24.04 legs; skip guards for the 3–4 Windows-bound
  test modules (`test_issue_62_runtime_trace.py` currently fails rather
  than skips off-Windows); README, `docs/agents/ci.md`, and
  `verification-policy.md` updates; only then evaluate moving release
  builds into CI.**

Releases remain locally built and published as immutable GitHub Release
assets, matching the current Windows workflow; CI release automation is a
separate, later decision.

## Consequences

Windows behavior is untouched: every change is either additive (new Unix
arms, new scripts, new platform manifest entries) or behind existing cfg
gates, and the Windows release chain keeps its current scripts and artifact
names. Linux users get the same gateway, catalog, and updater UX, minus
Windows-only capabilities (ZCode integration, Codex Desktop AppX launch)
that degrade to no-ops by design.

The cost is a permanently dual-platform release checklist: every release now
produces, signs, and validates two platform artifact sets, and CI runtime
grows by the ubuntu legs. Test authors must keep the core partition
platform-portable (mocked platform evidence, skip guards for genuinely
OS-bound tests) so both CI legs stay green.

## Alternatives considered and superseded

### Fork or per-OS repositories

Rejected. The coupling inventory shows the shared surface is ~95% of the
code; a fork would double maintenance for two packaging deltas. The
existing cfg-pair and runtime-dispatch conventions already absorb every
known difference.

### Rely on the system Python on Linux

Rejected. It breaks the bundled-runtime contract the sidecar assumes
(hermetic interpreter, pinned `zstandard`), exposes the gateway to distro
Python version drift, and saves only ~45 MB of package size. The
python-build-standalone approach mirrors the Windows embeddable model with
no code contract change.

### deb-only distribution

Rejected as the sole format. `tauri-plugin-updater` cannot self-update
deb/rpm installs, so deb-only would silently drop the quiet-update
experience Windows users have. deb ships as the secondary channel with
honest updater UX.

### AppImage-only distribution

Rejected as the sole format. AppImage requires host FUSE and provides weak
system integration; a meaningful share of Linux users expects a native
package. AppImage ships as the primary, updater-enabled channel.

### Move release builds into the CI matrix immediately

Deferred, not rejected. The Windows release chain is local today; building
the Linux chain locally first keeps the change reversible and mirrors the
established workflow. Phase 4 revisits CI release automation with real
build data in hand.

## Scope and follow-up boundaries

- This ADR records the platform decision and plan only; implementation is
  executed and verified in the phases above and their own issues, with
  Phase 0 evidence gating Phases 1–4.
- **Target scope is Linux x86_64 (glibc) only.** aarch64, musl targets,
  Flatpak, Snap, and an apt repository are out of scope.
- The synthetic E2E pytest partition (PowerShell runner, `.cmd` fixtures,
  Win32 Job Object watchdog) remains Windows-by-design; Linux CI runs the
  core partition.
- Windows-on-ARM and any macOS product decision are unaffected and remain
  as they are today (`autostart.rs` and `build-flavors.json` already carry
  partial macOS structure, but no macOS release is authorized here).
- CONTEXT.md terminology (Gateway, Vision Proxy, Route Plan) is unchanged;
  no glossary additions are needed for this port.

## Phase 0 findings (appended 2026-08-14, spike complete)

Phase 0 ran natively on the Linux reference machine (Ubuntu, cargo 1.94.1,
python 3.14.4, node 22.22.2). The gate passed; Phase 1–4 estimates hold at
4–6 working days. Evidence and adjustments:

- **System dependencies.** Only `libwebkit2gtk-4.1-dev` and
  `libsoup-3.0-dev` were missing, as predicted. No other system-level
  surprise.
- **The "zero-change compile" assumption was falsified cheaply.** CI had
  only ever compiled `safe_file.rs` on Linux, so the crate's Unix arms had
  never been type-checked. `cargo check` failed on two errors, both in
  `proxy.rs` Unix arms: E0597 (temporary drop order in
  `drain_reader_to_buffer`, ~:2199) and E0515 (`process_start_ticks`
  returning a `&str` borrowed from a dropped local, ~:3475). Fixed with two
  minimal edits plus `cfg(windows)` gates on three Windows-only
  imports/helpers (`main.rs` `Command` import, `proxy.rs`
  `split_command_line`, `proxy.rs` `configure_no_window`). The main build is
  now error- and warning-free on Linux.
- **First-ever `cargo test` on Linux: 463/526 pass.** Genuine failures are
  ~5; the remaining ~56 failures are `TEST_ENV_LOCK` poison cascades from
  the first panicking assertion (confirmed by single-test and serial runs).
  Genuine failures: (a) two `autostart::tests::windows_*` tests run
  ungated on Linux — one spawns a PowerShell fixture, one asserts Windows
  SID/account-name semantics; the production code they test never executes
  off-Windows, so the tests get `cfg(windows)` gates; (b) two
  write-failure-injection tests (`opencode/pi_cleanup_write_failure_*`)
  rely on the read-only file attribute, which a rename-based atomic write
  ignores on Unix — Phase 1 must rework injection (read-only directory)
  and confirm no product invariant depends on attribute-based write
  rejection; (c) one proxy startup-timeout fixture test. The real-Python
  proxy lifecycle test fails until Phase 2 ships the Linux runtime — expected.
- **pytest core partition: 2213/2217 pass (99.8%)**, better than the 95%
  estimate. All 4 failures are dev tooling, not shipping gateway code: two
  bounded-phase-runner terminate assertions (Windows taskkill semantics),
  one SO_LINGER test using the Windows-sized linger struct, one release
  script test reading `$env:TEMP` under Linux PowerShell.
- **Frontend builds clean** (1655 modules); ui-contract 145/146 with one
  pre-existing, platform-independent source-regex failure
  (`USAGE_REFRESH_COORDINATOR` line wrap).
- **Unix test builds carry 7 warnings** (unused imports/helpers exercised
  only by `cfg(windows)` tests): `gateway.rs:7260`, `history.rs:1000,1010`,
  `proxy.rs:3540,3561,3573,1904`. Phase 1 gates them.
- **Environment note.** The working copy at
  `/home/noirbright/Workstation/CodexHub` is not a git repository; Phase 0
  fixes were applied directly to the snapshot.

Phase 1 scope is amended to add: cfg gates for the autostart tests,
write-failure injection rework plus the rename/read-only semantic review,
the proxy startup-timeout fixture fix, and the 7 test-build warnings.
