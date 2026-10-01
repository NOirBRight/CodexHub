# Current Code Mode compatibility implementation evidence

Tracker: [#605](https://github.com/NOirBRight/CodexHub/issues/605).
Risk class: strict. Candidate full suite and live subscription qualification
remain owned by the campaign Orchestrator.

A loopback-only capture on 2026-10-01 observed installed Codex CLI **0.159.2**.
It made no vendor inference request and used an empty temporary CLI home with
no account credentials. The sanitized declaration fixture is
`tests/fixtures/collaboration/codex-cli-0.159.2-linux-code-mode.json`.
The prototype's `[features.multi_agent_v2] enabled=true` table no longer enables
this stable feature; the actual current setting is `features.multi_agent_v2=true`.
The captured catalogue includes namespaced custom exec and a native six-method
V2 namespace. This does not claim that Code Mode always advertises every method:
the implementation also recognizes complete typed nested handler declarations
and retains the exact supported caller subset.

Deterministic public-interface checks cover current flat/namespaced custom exec,
additional_tools, eager/deferred preparation, subscription and generic external
Providers, namespace/custom collisions and malformed envelopes, true Call and
Item identity, completed history with a fresh plan, same-child followup targets,
and SSE reconstruction. Official portability uses the existing ordinary
namespace inverse. External preparation uses the existing V2/custom codecs.

Opaque agent_message content now fails before an external generation request,
with bounded instructions for caller-owned restatement/reissue from original
requirements and existing child state/results. It is never silently discarded
or sent as a success-shaped replacement. Completed tool effects must not be
replayed. Reasoning ciphertext remains subject to the separate existing policy.

`recover_observed_assignments` requires explicit executed-assignment provenance
(author, recipient, exact text, source Call ID and typed Item ID). Different
addresses/text or ambiguous calls remain opaque. It has no process-global cache,
JavaScript execution, ciphertext inference or automatic source observation.
Production does not invent provenance from arbitrary source text; a boundary
without trusted observations continues to reject opaque tasks. Real mixed-model
V2 lifecycle and restart qualification remains required on the integrated
Provider candidate; the historical 0.158 prototype is not substituted for it.

Targeted Python checks use the repository launcher with the selected development
interpreter and a work-disk TMPDIR (host /tmp quota is exhausted). During
implementation, the final targeted selection passed **637 tests**, including
the sanitized 0.159.2 Gateway fixture, namespace/flat custom owner collisions,
complete SSE terminal output, and repository entry/seam/module boundaries.
The report-only quality command reported zero parse errors and no new-module
unused imports. No full-suite or hosted Actions gate is claimed here.
