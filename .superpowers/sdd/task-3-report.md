## Task 3 Report: Codex Config Owner Detection

### Status

Done.

### What Changed

1. `src-python/config_overlay.py`
   - Added owner marker emission to the managed Codex overlay as `# owner = release|beta`.
   - Extended `build_overlay(...)` to include the owner line.
   - Extended `apply_overlay(...)` to accept `owner`, default it to `release`, and reject unsupported values.
   - Added CLI support for `--owner release|beta`.
   - Passed the CLI owner argument through to `apply_overlay(...)`.

2. `tests/test_config_overlay.py`
   - Added a regression test that verifies applying an overlay writes `# owner = beta` and preserves the expected `/v1` base URL.
   - Added a regression test that verifies restore removes the owner marker together with the managed overlay block.

3. `src-tauri/src/config.rs`
   - Changed managed config backup naming to be routing-owner specific:
     - `config.toml.release.backup`
     - `config.toml.beta.backup`
   - Passed the current flavor routing owner into the Python overlay helper as `--owner release|beta`.
   - Extended focused Rust tests to assert the release owner argument and release-scoped backup filename in the current test flavor.

### TDD Evidence

#### RED

1. Python focused test before implementation:

```text
pytest tests/test_config_overlay.py -q
```

Result:
- Failed as expected.
- Exact failure:
  - `TypeError: apply_overlay() got an unexpected keyword argument 'owner'`

2. Rust focused test before implementation:

```text
cargo test config::tests
```

Result:
- Failed as expected.
- Exact failures:
  - missing `--owner` argument in `switch_mode_custom_applies_config_overlay_without_history_sync`
  - backup filename still `config.toml.backup` instead of `config.toml.release.backup`

#### GREEN

1. Python focused test after implementation:

```text
pytest tests/test_config_overlay.py -q
```

Result:
- `14 passed in 0.51s`

2. Rust focused test after implementation:

```text
cd src-tauri
cargo test config::tests
```

Result:
- `19 passed; 0 failed`

### Tests / Results

1. `pytest tests/test_config_overlay.py -q`
   - PASS
   - `14 passed in 0.51s`

2. `cargo test config::tests`
   - PASS
   - `19 passed; 0 failed`

### Files Changed

- `D:\Workstation\CodexHub\.worktrees\beta-release-channel-design\src-python\config_overlay.py`
- `D:\Workstation\CodexHub\.worktrees\beta-release-channel-design\tests\test_config_overlay.py`
- `D:\Workstation\CodexHub\.worktrees\beta-release-channel-design\src-tauri\src\config.rs`

### Self-Review

1. Scope stayed within the three files required by the task.
2. Stable/release owner is emitted as `release`, not `stable`.
3. Beta owner is emitted as `beta`.
4. Backup files are now channel-specific, which avoids stable/beta restore collisions in shared proxy state.
5. The Rust change only passes owner metadata and adjusts backup naming; it does not implement Task 4 routing-owner detection logic.
6. No gateway protocol, transport, or process-management behavior was changed.

### Concerns

1. Rust focused verification runs in the current build flavor, so the Rust test coverage exercised the release-owner path directly. The beta owner path is covered at the Python layer here, and flavor-to-owner mapping already exists in `app_flavor` from Task 2.

### Review Blocker Fix

1. `tests/test_config_overlay.py`
   - Added a regression that applies an owner-marked overlay, deletes the backup, restores with `unified_history=False`, and asserts the managed marker block is removed.

2. `src-python/config_overlay.py`
   - Updated the no-backup `restore_overlay(...)` fallback to call `strip_marked_overlay(...)` on the live config before unified-history handling.
   - Kept the backup-present path unchanged: restore from backup, then delete the backup file.

### Blocker Verification

1. `pytest tests/test_config_overlay.py -q`
   - PASS
   - `15 passed in 0.37s`
