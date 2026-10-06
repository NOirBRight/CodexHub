# Issue #632: current main integration

Local integration for [#632](https://github.com/NOirBRight/CodexHub/issues/632)
and draft [PR #614](https://github.com/NOirBRight/CodexHub/pull/614).
Risk class: **strict**. This report records integration checks, not Provider
acceptance, permission to push, or a release gate.

## Source identity

- Campaign starting SHA / first merge parent:
  `d5e5a858c7b9b96b418708e6f4a1f1cd03352858`.
- Fetched `origin/main` / second merge parent:
  `2532b361fe08236a66ebd6f053a292448e4ccdee`.
  `git fetch origin main`, `FETCH_HEAD`, `origin/main`, and a subsequent
  `git ls-remote origin refs/heads/main` agree on this SHA. The PR API returned
  the older merge-base in `baseRefOid`; it was not used as the current main.
- Merge-base: `61e63514fb21c2f3e4ed4478d0261e78a3806c1c`.
- Tested integration tree before adding this report and its inventory:
  `e472a1c44af1485b0759785897aacc7b2d1d004b`.
  The final merge adds only these two documentation artifacts to that tree.
  Its candidate commit identity is available from this report's containing
  integration commit; no earlier runtime result is rebound to that commit.
- Branch remains `campaign/cli-subscription-providers`. Initial tracked status
  was clean. Existing untracked `.verification-tmp/` was retained; new local
  logs are under `.verification-tmp/issue-632-integration-20261005/`.

## Conflict and path inventory

The merge imported main's changes without rewriting campaign history.
[Machine-readable inventory](issue-632-integration-2026-10-05-paths.json)
records all **113 integration paths relative to the campaign**, **18 paths
changed on both sides**, and **36 preserved campaign historical documents**.
These counts exclude this report and the inventory itself.

### Pi cleanup: one textual conflict

Path: `src-tauri/src/gateway/clients/pi.rs`.

Primary sources:

- Shared ancestor implementation `f01a6c69` preserves Pi's user-owned
  `settings.json`, removes only managed entries from `models.json`, and reports
  whether removal occurred, under ADR-0004.
- Campaign `1da7a38b` replaces a nested conditional with
  `removed_any |= detach_pi_managed_models(path)?` to keep cumulative cleanup
  state while avoiding the nested-match lint.
- Main `f81030ee` expresses the same condition as a match guard:
  `"models.json" if detach_pi_managed_models(path)? => { removed_any = true; }`.

Resolution uses main's match guard. Cleanup still runs only for the models
target, still propagates errors, and still leaves `removed_any` true once a
removal happens. A no-op does not clear an earlier true value. There is no
contract trade-off. The complete Pi source now matches main exactly: main
subsumes the campaign's equivalent lint repair, so no duplicate repair was added.

### Theme contract: one integration repair after automatic merge

Path: `frontend/src/components/providers/CliSubscriptionCard.tsx`.

Main `8fe52b8c` migrates production controls to semantic theme tokens and adds
`frontend/scripts/theme-colors.test.mjs`. The campaign adds a new subscription
card that still used `text-slate-600`. The focused theme test failed on that
exact card. Replace only that class with `text-muted`, matching main's control
contract. The same test passes after the repair. Consent, enablement, status,
and Toast behavior are unchanged.

### Automatically merged overlap

- `ProviderEditor.tsx` and `ProviderWorkspaceView.tsx` retain CLI subscription
  consent/enable guards and main's theme/native Default subagent behavior.
- Both locale files retain the subscription copy alongside main's independent
  native subagent and effort copy.
- Frontend `commands.ts`, `tauri.ts`, and `types.ts`, Rust `cli.rs`, `config.rs`,
  `config/tests.rs`, `desktop_commands/{handlers,mod,web_adapter}.rs`, and
  `main.rs` retain the CLI status command and Provider consent field alongside
  main's native subagent commands/settings. Rust compilation and the actual
  registry-to-frontend manifest contract pass.
- `gateway/clients/claude.rs` retains main's independent native subagent
  lifecycle plus the campaign's Provider consent field in test constructors.
- `gateway_compat/official_passthrough.py` retains main's schema-reference
  normalization and the campaign's request-scoped portable handler names.
  `route_primitives.py` retains main's `0.2.38` user agent and the campaign's
  `OFFICIAL_CLI_SESSION` strategy. Selected schema, routing/history and
  protocol tests pass together.

Main's image-history preservation, native subagent lifecycle, OpenCode 2 E2E
fixture handling, release metadata, and other imported changes remain their
main implementations. No new Provider feature or acceptance exception was
introduced. Both subscription presets remain disabled by default; exact Claude
prompt-priority consent remains required before enablement. Immutable routing,
caller-owned tools, task preservation and visible unsupported controls remain
the campaign contract.

## Focused checks

All successful commands use the existing development interpreter through
`./scripts/codexhub-python.sh`; no account or user configuration was changed.
For this host, bind
`CODEXHUB_PYTHON=/home/noirbright/Workstation/CodexHub/.venv-ci/bin/python`
(Python 3.14.7, pytest 9.1.1). Rust commands put the existing
`/home/noirbright/.cargo/bin` first on process `PATH` (Rust/Cargo 1.98.1).
Frontend checks use Node 26.10.0.

Initial launcher invocation failed closed because ambient compatible Python had
no pytest; the ambient mise Cargo shim had no selected version. These are
invocation failures, not passing tests. Their correction uses existing tools
only. Full suites were deliberately not run during this implementation round.

| Check | Result | Local artifact |
| --- | --- | --- |
| Subscription exchange/backends/discovery/config; completed native/history; Code Mode/V2; encrypted-task prevention; Messages/Chat/schema/model-switch/image contracts; module discipline | **652 passed, 28 subtests passed**, 51.49 s | `python-focused-valid.log`, `python-focused-valid.xml` |
| Qualification harnesses; runtime contract; configuration overlay; native Claude model enumeration; Linux CLI/CI partition planners | **271 passed, 121 skipped, 13 subtests passed**, 6.88 s | `python-contracts.log`, `python-contracts.xml` |
| `cargo test --locked pi_ -- --test-threads=1` | **57 passed** | `rust-pi.log` |
| `cargo test --locked cli_subscription::tests -- --test-threads=1` | **2 passed** | `rust-subscription.log` |
| `cargo test --locked claude -- --test-threads=1` | **26 passed** | `rust-claude.log` |
| `cargo test --locked config::tests -- --test-threads=1` | **60 passed** | `rust-config.log` |
| `cargo test --locked desktop_commands:: -- --test-threads=1` | **5 passed** | `rust-commands.log` |
| Frontend `./node_modules/.bin/tsc --noEmit` | **passed** | Implementation tool transcript |
| Provider catalog/workspace/editor/discovery and theme contract tests | **63 passed** after the theme repair | Implementation tool transcript |
| Default subagent, Claude settings and command-name tests with `node --experimental-strip-types --test` | **33 passed** | Implementation tool transcript |
| `node --test scripts/desktop-commands.contract.test.mjs` with the actual Rust manifest | **3 passed** | `frontend-command-manifest.log` |
| `scripts/report_quality_gates.py` | report-only: **0 parse errors**; 328 unused imports, 458 dead functions, 340 duplicate-name findings | `report-quality.log` |
| `git diff --check`, `git diff --cached --check`, unresolved index inventory | **passed; no unresolved entries** | Implementation tool transcript |

Python test files and the complete changed-path inventory are recoverable from
the JUnit files and the JSON inventory. Existing campaign evidence files were
compared by Git blob identity and remain byte-for-byte unchanged. No vendor
generation, native account discovery, model retry, account change, or new
runtime acceptance probe was performed.

## Remaining gates owned by the Orchestrator

1. Review the exact integration candidate and delta against both immutable
   parents. Confirm both intents and the one-line semantic theme repair.
2. Run the relevant complete local matrix once on the reviewed candidate:
   `./scripts/verify-linux.sh` (Python core, partition completeness, serial Rust
   tests, clippy, frontend/native build, physical pointer E2E), plus
   `npm run test:ui-contract` in `frontend/`.
3. The imported main delta touches the synthetic real-client contract surface
   (`Run-RealClientE2E.ps1`, fixtures, synthetic tests, operator docs). Select
   the full Windows-watchdog synthetic partition, not a Linux substitute:
   `scripts/codexhub-python.cmd tests/fixtures/real_client_e2e/run-with-windows-watchdog.py
   --timeout-seconds 3600 -- scripts/codexhub-python.cmd -m pytest -q
   tests/test_real_client_e2e.py`, with JUnit/duration retention. Windows core
   tests skipped on Linux remain a platform gate; Linux results do not clear
   Windows qualification.
4. Keep issue-required Provider runtime qualification and all known gaps open.
   Retained older evidence is bound to its original SHA. Messages whole-turn
   budget mapping, authoritative old-assignment provenance, Claude entitlement,
   after-first-text cancellation, reverse mixed-provider restart exact-output
   failure, and platform gates are not repaired or accepted by this merge.
5. Parent owns push, acceptance, issue closure, draft status, merge and release
   decisions. This implementation round performs none of those operations.

GitHub Actions remains disabled and does not approve this candidate.
