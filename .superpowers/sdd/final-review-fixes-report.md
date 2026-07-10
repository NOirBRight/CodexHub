# Final Review Fixes Report

Base: `96d39250`
Branch: `codex/v0.1.4-codex-compat`

## Completed slices

1. Gateway custom provider authentication
   - RED: overlay and CLI contract showed `requires_openai_auth = true` beside the local bearer.
   - GREEN: generated Gateway provider now uses `requires_openai_auth = false` with `experimental_bearer_token`.
   - Official unified-history provider remains OAuth-backed (`requires_openai_auth = true`).

2. Exact cross-channel takeover restoration
   - RED: Stable/unowned configuration backups were rewritten by unified-history injection on Beta disconnect.
   - GREEN: an existing backup is atomically restored byte-for-byte, then removed; history reconciliation is not applied inside that restore transaction.
   - Rust-to-Python chain covers unowned, Official, and Stable inputs with default `unified_codex_history = true`, repeated apply, and disconnect.

3. Windows custom Codex home lock discovery
   - RED: the close script accepted no Codex target and used `$USERPROFILE\\.codex`.
   - GREEN: `paths.codex_dir()` is passed through the controller boundary and safely single-quoted for PowerShell.
   - An executable injected-lock contract uses no real processes or files and returns the locked-target outcome. No force-kill path was added.

4. Portable flavor/version gate
   - RED: portable builds defaulted to Stable and had no executable dry-run validation.
   - GREEN: `Flavor` is mandatory; generated version and flavor-specific Tauri fields are validated before Python/npm/cargo build work.
   - Dry-run contracts verify `CodexHub.exe`, `CodexHubBeta.exe`, flavor configuration, output naming, and both channel mismatch rejections.

5. Shared release-channel SemVer validation
   - RED: plan/manifest scripts duplicated a `0.1.4-beta.N` regex and rejected the next-version prerelease.
   - GREEN: both scripts and the portable builder use `ReleaseChannel.ps1` with generic SemVer parsing and Stable/Beta prerelease rules.
   - Candidate manifests remain `0.1.4-beta.1`.

## Commits

- `13c3ea04` fix: use local bearer for gateway provider
- `053fd5ba` fix: restore takeover backup before history reconciliation
- `d02f0527` fix: inspect locks under configured Codex home
- `57a83ed3` fix: validate portable build flavor before build
- `8a850e20` fix: share release channel semver validation

## Verification

- `python -m pytest tests/test_config_overlay.py -q` — 26 passed
- `python -m pytest tests/test_release_channel_scripts.py -q` — 18 passed
- `cargo test config::tests:: -- --nocapture` — 30 passed
- `cargo test history::tests:: -- --nocapture` — 32 passed
- `cargo clippy --all-targets -- -D warnings` — passed
- `git diff 96d39250..HEAD --check` — passed
- UI contracts/build — not run; no UI files touched
- `cargo fmt` — intentionally not run

## Self-review

- Leakage: the generated local bearer is sourced only from the configured Gateway client key; tests use synthetic keys. No credential logging or real user data access was added.
- Restore/data loss: backup deletion occurs only after the atomic config write succeeds. Write failure keeps the backup. Exact bytes, line endings, and prior owner marker are preserved on backup restoration.
- Process safety: Windows handling still uses graceful `CloseMainWindow`; no `Stop-Process`, `taskkill`, or force-kill fallback exists.
- Release safety: dry-run returns before runtime preparation and builds; no publication, tag, merge, signing, or release mutation was performed.

## Final re-review follow-up

### Takeover-scoped exact restore

- RED: an ordinary Stable custom-to-official restore with default unified history returned the raw backup instead of producing the unified Official provider configuration.
- GREEN: apply writes a versioned takeover sidecar only for explicit takeover. Exact restoration requires that sidecar plus matching active takeover owner and original backup owner state.
- Repeated takeover apply preserves the sidecar. Successful exact restore removes both the backup and sidecar.
- Ordinary same-channel backups continue through normal unified-history reconciliation.
- Commit: `e63a436c`.

### Strict SemVer boundaries

- RED: the shared regex rejected `1.2.3-0alpha` and accepted a version with a trailing line feed.
- GREEN: validation now uses `\A...\z`; numeric prerelease identifiers are `0|[1-9][0-9]*`, while non-numeric identifiers must contain at least one letter or hyphen.
- Executable PowerShell contracts accept `1.2.3-0alpha`, reject `1.2.3-01`, reject trailing LF, and retain next-version and Stable/Beta channel checks.
- Commit: `f87b3354`.

### Follow-up verification

- `python -m pytest tests/test_config_overlay.py -q` — 26 passed
- `python -m pytest tests/test_release_channel_scripts.py -q` — 21 passed
- `cargo test config::tests:: -- --nocapture` — 31 passed
- `cargo clippy --all-targets -- -D warnings` — passed
- `cargo fmt` — intentionally not run
