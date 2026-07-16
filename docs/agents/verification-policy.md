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
| Python Gateway, routing, protocol translation, analyzers, Python configuration, or Python test infrastructure | `python -m pytest -q` |
| Frontend source, UI contracts, frontend configuration, or frontend dependencies | `npm run build` and `npm run test:ui-contract` in `frontend/` |
| Tauri/Rust commands, Gateway lifecycle, configuration, packaging code, Rust dependencies, or Rust test infrastructure | `cargo test --locked` and `cargo clippy --locked --all-targets -- -D warnings` in `src-tauri/` |
| Shared frontend/Tauri command or persisted-settings contract | Frontend and Rust suites |
| Shared Python/Rust Gateway, process-lifecycle, catalog, packaging, updater, release, or installer boundary | Every suite touched by the contract; release instructions may add an explicit build matrix |

Documentation-only changes need link/content inspection and diff hygiene, not a
language full suite. A `fast` isolated UI or pure-logic change still runs the
narrow compile/test command that proves its acceptance, but does not duplicate
the repository's complete language suite locally.

After review fixes, run affected targeted checks and rely on CI. Repeat a local
full suite only when the delta crosses a new row of this matrix. Do not rerun a
full suite merely because a reviewer re-read the same candidate.

## Manual and runtime evidence

Manual/Desktop/live-provider evidence is required only when the Issue names an
observable behavior that deterministic tests cannot prove. Record the exact
variable, bound, success/failure cues, and sanitized artifact before running.
One clean run does not create a new acceptance gate. Retry only for a new
hypothesis or materially changed environment.

`python scripts/report_quality_gates.py` is always report-only. Run it once when
changed Python, TypeScript/TSX, or Rust source is in its scan scope; findings do
not block PR, merge, or release under the current policy.

## CI authority

GitHub Actions runs all four repository jobs for every PR to `dev` or `main`
regardless of local verification class. Local risk selection reduces duplicate
work; it does not weaken CI. When CI is unavailable and a merge must proceed,
reproduce the full fallback in `docs/agents/ci.md`.

Existing active work migrates incrementally: retain already completed full
suites and formal reviews, do not restart a Worker, and verify only later
deltas unless they cross a new matrix boundary.
