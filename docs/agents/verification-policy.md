# Risk-tiered verification policy

GitHub Issues own observable product acceptance. This repository owns the
engineering verification matrix below. Skills and agents select and execute
these rules; they must not add hidden acceptance gates.

## Classes

| Class | Use for | Local candidate gate | Review |
|---|---|---|---|
| `fast` | Documentation, copy, metadata, isolated UI, or small deterministic logic with no shared lifecycle/public contract | Targeted checks and `git diff --check`; no local full suite | One direct Orchestrator scope/acceptance check |
| `standard` | Reversible feature or bug contained to one clear boundary | Targeted checks during development, then one relevant full suite from the matrix below | One Orchestrator-owned Standards/Spec review |
| `strict` | Protocol, routing, transport, auth, permissions, persistence/migration, release/update/install, security/privacy, concurrency/cancellation, or nondeterministic runtime evidence | Targeted checks, one relevant full suite, and only Issue-required harness/manual evidence | One Orchestrator-owned Standards/Spec review; later review is delta-only |

Choose the highest applicable class. Before expanding a `fast` or `standard`
task across a public/persisted contract, shared lifecycle, security boundary,
or another subsystem, record an architecture decision in the Issue and upgrade
the class. File count alone does not determine risk.

## Relevant full-suite matrix

Run targeted tests freely while implementing. At the candidate commit,
`standard` and `strict` work runs each relevant suite once:

| Changed boundary | Relevant local full suite |
|---|---|
| Python Gateway, routing, protocol translation, analyzers, Python configuration, or Python test infrastructure | `python -m pytest -q --ignore=tests/test_real_client_e2e.py` plus `python tests/fixtures/real_client_e2e/run-with-windows-watchdog.py --timeout-seconds 3600 -- python -m pytest -q tests/test_real_client_e2e.py` when the changed paths touch the real-client E2E contract surface |
| Frontend source, UI contracts, frontend configuration, or frontend dependencies | `npm run build` and `npm run test:ui-contract` in `frontend/` |
| Tauri/Rust commands, Gateway lifecycle, configuration, packaging code, Rust dependencies, or Rust test infrastructure | `cargo test --locked` and `cargo clippy --locked --all-targets -- -D warnings` in `src-tauri/` |
| Shared frontend/Tauri command or persisted-settings contract | Frontend and Rust suites |
| Shared Python/Rust Gateway, process-lifecycle, catalog, packaging, updater, release, or installer boundary | Every suite touched by the contract; release instructions may add an explicit build matrix |

Documentation-only changes need link/content inspection and diff hygiene, not a
language full suite. A `fast` isolated UI or pure-logic change still runs the
narrow compile/test command that proves its acceptance, but does not duplicate
the repository's complete language suite locally.

After review fixes, rerun the affected targeted checks on the reviewed exact
SHA. For `standard` and `strict` work, run the relevant local full suite once
on that candidate. Do not rerun a full suite merely because a reviewer re-read
the same candidate.

## Manual and runtime evidence

Manual/Desktop/live-provider evidence is required only when the Issue names an
observable behavior that deterministic tests cannot prove. Record the exact
variable, bound, success/failure cues, and sanitized artifact before running.
One clean run does not create a new acceptance gate. Retry only for a new
hypothesis or materially changed environment.

`python scripts/report_quality_gates.py` is always report-only. Run it once when
changed Python, TypeScript/TSX, or Rust source is in its scan scope; findings do
not block PR, merge, or release under the current policy.

## Local verification authority

GitHub Actions CI is disabled for CodexHub. There is no required status check
and no Hosted runner result can approve a merge or release. The former
classifier and Hosted matrix are retained as historical reference only.
The authoritative sequence for every candidate is:

1. implement on an isolated worker branch;
2. review the exact candidate SHA (Spec/Quality as required by the Issue);
3. run the local focused and affected full suites selected below;
4. run any Issue-required CLI, Desktop, or provider manual check;
5. record the local result before merge, tag, or release.

Unknown paths, planner/workflow changes, and release candidates should fail
closed to the complete local matrix. True real-client Desktop/CLI evidence
remains a local release-operator procedure.

### Python checks

The Python validation is split into two stable checks so the real-client E2E
suite is only invoked when its contract surface actually changed:

- **`Python core`** is selected for Python source, configuration, and ordinary
  Python tests. It executes every Python test except
  `tests/test_real_client_e2e.py`.
- **`Synthetic real-client contract`** is selected for the real-client E2E
  dependency surface and runs `tests/test_real_client_e2e.py` in full.

The unified planner lives at
`scripts/ci/ci_change_plan.py`; the existing Python partition planner remains
the source of the core/synthetic partition definitions used by local fallback
and is tested by `tests/test_ci_python_plan.py`. Use the same stable pytest
arguments locally when the corresponding boundary is selected. Collection
completeness is verified by
`python scripts/ci/check_python_test_partitions.py`.

Existing active work migrates incrementally: retain already completed full
suites and formal reviews, do not restart a Worker, and verify only later
deltas unless they cross a new matrix boundary.
