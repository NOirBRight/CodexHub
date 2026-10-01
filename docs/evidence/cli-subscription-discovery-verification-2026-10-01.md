# CLI subscription discovery verification

Date: 2026-10-01. Implementation: #604; specification: #603; PR: #613.
Reviewed production/test code SHA: `2f100be5`. Tested PR head:
`1cff7092765786a4cd59be23cef7e6d8887b5423` (the later commit only adds the
sanitized historical Claude denial evidence). This record adds documentation
only; no production/test code changed after the reviewed/tested candidate.

## Scope and review

Strict Python discovery scope: current-account status and complete model lists,
private disposable CLI configuration, immutable sanitized results and bounded
errors. The Orchestrator checked Standards and Spec against #604: no blocking
finding. Official login/refresh ownership, source-account recheck, exact vendor
IDs and generation-unqualified status match the confirmed contract. Existing
Claude native-picker shim resolution keeps its default behavior; Cursor can
resolve the installed version-manager shim before HOME isolation.

No Gateway generation backend, route admission, Provider enablement, Client
Projection, downstream settings or shared Python/Rust process contract changed.
The relevant local full suite is Python core under the verification policy;
frontend/Rust/packaged-client/release gates belong to subsequent implementation
and qualification tickets. Windows live discovery remains unexercised.

## Results

- Focused discovery/native-picker/Python-runtime checks: **37 passed, 117 skipped**.
- Full Python core: **3612 passed, 197 skipped, 283 subtests passed**, 229.19 seconds.
- Partition completeness: full 3973, core 3809, synthetic 164; overlap 0,
  missing 0, extra 0. The synthetic real-client contract was not changed/run.
- Report-only quality scan: zero parse errors, no new unused imports. Repository
  duplicate-name/dead-code reports remain non-blocking; the discovery entry is
  called by its checked-in diagnostic command despite the name scanner report.
- Historical evidence index: all 17 retained file hashes match summary.json,
  including the explicitly added sanitized Claude JSONL denial artifact.
- Diff hygiene and clean working-tree checks pass.

Commands used the repository launcher bound to the host development interpreter
(Python 3.14.7, pytest 9.1.1), never ambient Python 3.11. Relevant commands:

```sh
./scripts/codexhub-python.sh -m pytest -q tests/test_cli_subscription_discovery.py tests/test_claude_native_models.py tests/test_python_runtime.py
TMPDIR=<task-owned-work-disk-directory> ./scripts/codexhub-python.sh -m pytest -q --ignore=tests/test_real_client_e2e.py --basetemp=<task-owned-work-disk-directory>/core --junitxml=<task-owned-work-disk-directory>/junit-core.xml
./scripts/codexhub-python.sh scripts/ci/check_python_test_partitions.py
./scripts/codexhub-python.sh scripts/report_quality_gates.py --json
git diff --check
```

## Environment incident and limits

The first full run was invalidated by `/tmp`'s current-user disk quota. The
mount uses `usrquota`; an independent 1 MiB temporary write failed with errno
122 while a work-disk write succeeded. The minimized existing
`test_atomic_write_text_replaces_existing_file` failed with the same error,
then passed unchanged when only TMPDIR/basetemp moved to a task-owned work-disk
directory. The full run above passed there. No unrelated session files were
deleted and no machine/global configuration changed. The invalid run is not
counted as product evidence.

Real isolated Cursor discovery retained 246 IDs. Claude first-party login and
CLI 2.1.285 were detected, but vendor catalog discovery stayed not-eligible.
Neither is production text/tool/V2/restart generation qualification; Claude's
historical organization denial was not revalidated by new inference. See the
[discovery observation](cli-subscription-discovery-2026-10-01.json) and separate
Provider qualification tickets #609/#610, with account prerequisite #611.
