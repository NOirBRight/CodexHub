# PR #589 review and portable test candidate

The user fixed the settings-feature review at `cfb6f893`, then requested a
rebase onto the latest published code. That release was `v0.2.30`, commit
`22ac0d021eb3519fe71deeeaf39bde7ffecfc65c`; `main` was still an older release
line. The rebased equivalent of the feature baseline is `00cee4e9`.

The fixed review command remained `git diff cfb6f893...7608bb17`, with
`git log cfb6f893..7608bb17 --oneline` as its commit inventory. Because the
original SHA predates the rewrite, that three-dot diff uses merge-base
`aad053cf` and includes the integration ancestry. The rebased equivalent
`00cee4e9` identifies the original feature boundary without silently changing
the user-confirmed baseline. The subsequent repair review was delta-only.

Rebase candidate `09c05264` has exactly the tree produced by combining the
old reviewed head `cc191965` with v0.2.30. Historical merge resolutions for
the runtime ADR number and test fixtures were preserved. The final portable
candidate is `7608bb175c03bba3df73014fd295e562696d93ef`, which includes the
review repairs below. No application release or production installation is
part of this delivery.

Post-build qualification commit `8707366f` changes only the xAI test fixture
reader to explicit UTF-8. Application, frontend, Rust, config, and script trees
are identical to `7608bb17`, so the two archives remain that exact application
candidate. Later evidence commits likewise do not require rebuilding.
The UTF-8 test-only delta was reviewed separately on both Standards and Spec;
neither axis requested another change. Its initial environment-related gate
concern was closed by the 52 passing Windows cases recorded below.

## Standards

The actionable new-card design issue is resolved. Model enable controls now
reuse the existing `SwitchControl`; status badges use `ws-status-chip` and the
existing density/type treatment. No documented-standard violation remains in
the repair delta.

The review's heuristic observations were considered separately from hard
violations, as required by the code-review skill:

- The literal ChatGPT brand and “original settings” / “component” wording are
  intentional: the specification and user use those terms. They are not an
  undocumented translation or domain-name violation.
- **Possible Divergent Change:** the managed-runtime module owns installation,
  lifecycle, and status. Its size is a future design consideration; this review
  identified no concrete additional defect requiring a module split.
- **Possible Feature Envy:** checks call owning-runtime private helpers. Public
  wrappers were not required to repair this change; coupling can be revisited
  if the interface grows.
- Readiness is projected at several policy boundaries. The concrete missing
  capability check is fixed and covered by a rendered-component regression;
  text and tool admission intentionally have different requirements. A broader
  readiness API refactor was not required to correct this defect.
- Existing runtime tests exercise private helpers; this was recorded as a mild
  test-surface observation, not a newly introduced documented-standard breach.
  The new regression exercises public component output with the repository's
  established frontend contract harness.

Standards: zero documented-standard violations; five heuristic observations
were adjudicated above, with zero unresolved actionable findings. They were
not represented as five additional code fixes.

## Spec

The native connection card could report text available after a capability
mismatch even though Gateway rejected the turn. It now requires the same
explicit `capabilities_match === true` evidence. A rendered-component test
covers true, false, null, absent capability, and absent checks, while proving
that pending saved settings do not disable the still-active configuration.

ADR-0017 now links completed, candidate-specific acceptance instead of saying
implementation remains outstanding. The retained browser layout, click/save,
reload, secret redaction, native entry, and document-unload evidence is
published in [settings browser acceptance](../issue-588/settings-browser-acceptance.md).
The empty-account screenshots contain no real credentials or account identity.
The report distinguishes historical browser checks from the new package runs.

Spec: zero unresolved code/documentation findings. Final same-SHA package
checks are recorded below.
The separate #567–#575 client/V2 acceptance remains open, without a waiver.

## Verification

- Rebase-focused xAI/ChatGPT route/recovery checks: 26 passed.
- Final application UI code: 174 UI contract tests passed. Early runner attempts
  selected an unconfigured Cargo shim, then lacked the isolated worker's bundled
  runtime resource; canonical toolchain/resource preparation resolved both.
- Linux Python core at 7608: 3,575 passed, 196 skipped, 283 subtests passed.
  Partitions: 3,935 total = 3,771 core + 164 synthetic, disjoint and complete.
- Linux Rust: 798 passed, one ignored, one interpreter-selection fixture failed
  because the outer verification command explicitly set CODEXHUB_PYTHON. The
  exact failing test passed after clearing that override. Clippy, frontend
  build, and physical pointer-input E2E passed.
- Report-only quality scan: zero parse errors; other report-only diagnostics
  are not represented as an empty whole-repository debt report.
- Windows Rust/Clippy evidence is retained from the previous candidate: 782
  Rust tests passed and three were ignored, with five initial invocation
  failures passing focused rerun after interpreter overrides were cleared;
  Clippy passed. From `c0d4840e` to `7608bb17`, all Rust and script sources are
  unchanged; the three Tauri manifest changes only set version 0.2.30.

Windows full pytest initially finished with 3,853 passed, 68 skipped, 283
subtests passed, and 14 failures. Four failed while creating Tunnel test
fixtures because the isolated development interpreter lacked `pip`; ten
failed because the newly rebased xAI JSONL test used Windows' default GBK
encoding for a UTF-8 file. `ensurepip` populated only the isolated test venv,
and commit `8707366f` fixes the fixture reader. Linux's 10 xAI cases passed.
Both affected Windows modules then passed: **52 passed in 79.12 seconds**.
JUnit identities confirm every one of the 14 initial failures appears in that
passing rerun. [Qualification summary](windows-test-qualification.json) keeps
the initial failure counts and corrections; the original full invocation is
not represented as an exit-zero run. Zero test failures remain unresolved.

The earlier c0 Windows watchdog synthetic run (164 passed) remains applicable:
its runner, real-client fixtures, and contract source are unchanged by this
review/rebase. The current Windows invocation accidentally omitted the core
suite's `--ignore=tests/test_real_client_e2e.py`, so it also reruns that synthetic
file. Its JUnit partition is 3,689 passed / 68 skipped / 14 failed core tests,
plus all 164 synthetic tests passed.

## Linux portable

`CodexHub_0.2.30_linux_portable_7608bb17.tar.gz`, 68,763,102 bytes.

SHA-256: `d15cfae6d9bae9539451bc9e0fc02545780503746dd4281bab2d2102252ac2d3`.

[Resource inspection](linux-resources-7608bb17.json) matched all 99 Python/HTML
sources to the candidate, verified the pinned runtime archive, and found no
private state. First-launch and upgrade dock smokes passed. The Linux portable
requires a host Python 3.13 or newer.

[Packaged isolated upgrade](linux-packaged-upgrade-7608bb17.json) preserved the
existing test account and settings, started successfully, and reported nine
models, current text/tools readiness, and no pending restart. This run checked
upgrade/start/readiness; it does not claim new live text/tool turns. Historical
real Codex CLI streamed text → client-owned tool → text evidence remains in
[settings acceptance](../issue-588/settings-acceptance.md), with its original
candidate identities retained.

## Windows portable

`CodexHub_0.2.30_portable_7608bb17.zip`, 83,803,333 bytes.

SHA-256: `b15e128ef160ba20e0ede90d4b0d441375ee2882a9996339d2f1a8180145d7fb`.

[Resource and HTTP inspection](windows-package-inspect-7608bb17.json) matched
all 99 Python/HTML sources, verified the runtime archive hash, and found no
private state. Authorization/origin checks, legacy settings, save/reopen,
pending restart, and synthetic-secret redaction passed.

[Packaged lifecycle rerun](windows-packaged-runtime-lifecycle-7608bb17-r2.json)
passed actual pinned-runtime startup/health, save without restart, explicit
restart activation, upgrade preserving settings and synthetic account markers,
and final stop. An independent process lookup confirmed all recorded owned
PIDs had exited. The probe asserts that its runtime/settings modules come from
the extracted new package and that the executable embeds the exact candidate
revision. It was rerun in a fresh isolated directory because the first probe
had retained a hardcoded old candidate label; the old-labelled report is not
used as final candidate evidence.

This Windows probe used synthetic state, no real account or provider calls.
It correctly reported text/tools not ready. Linux live-account and Windows
synthetic evidence are deliberately identified separately.

Both archives were copied to the delivery folder and their SHA-256 sums
verified. The Windows ZIP and test instructions are also in Yoga's user
Downloads folder. Portable defaults to the current user's configuration; it
does not automatically isolate that configuration. Neither archive contains
account or Tunnel credentials.
