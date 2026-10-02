# Codex independent native Default subagent

Scope: [#615](https://github.com/NOirBRight/CodexHub/issues/615), ADR-0019.
Verification class: strict; this worker ran focused checks only. The
Orchestrator owns committed-candidate review and affected full suites.

## Native contract

The installed CLI reports `codex-cli 0.160.0`. On 2026-10-02 the official
[Codex 0.160.0 configuration schema](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/core/config.schema.json)
was fetched through GitHub's content API. `AgentsToml` declares
`default_subagent_model` and `default_subagent_reasoning_effort` as spawn
defaults. `ReasoningEffort` is a nonempty string advertised by the model.
Neither an independent subagent service tier nor a Fast field is declared.

Native selections therefore use the client's model slug and advertised effort
directly. Native reset removes those two overrides. Native selections do not
offer the Gateway's synthetic Fast aliases. Connected Gateway choices retain
their existing Fast behavior. The parent's service tier stays unchanged.

Native membership comes from a user-owned model catalog, when selected, or
the existing native subscription discovery. It is not filtered by Hub export,
Hub provider activation, or disabled Hub models. UI identities distinguish
`native:<slug>` from the existing Gateway catalog slug for the same model.

## Focused evidence

- Two native lifecycle regressions failed before implementation because the
  direct native save operation did not exist, then passed after implementation.
- The final configuration/history overlay suites pass: 103 tests and 13
  subtests. The four native regressions include direct save/readback, native reset, user catalog
  identity/advertised effort, and rejecting the managed Hub catalog as a native
  model source.
- Two Rust tests pass. The Python-backed configuration transaction proves
  native save/readback, preserving the parent and snapshot, clearing the stale
  Gateway preference, and restoring every changed file if preference persistence
  fails. The other test verifies unknown preference fields remain unchanged.
- Frontend typechecking and 19 focused Subagent/command contracts pass. Native
  choices stay available with every Gateway model excluded, and matching native
  and Gateway slugs remain distinct.
- Diff hygiene passes. Only the new Rust module was formatted; repository-wide
  formatting checks report existing unrelated formatting differences.

Early Rust tests used a process-local `TAURI_CONFIG` resource override while
the Windows runtime archive was absent. The Orchestrator then prepared the
reviewed archive and generated pin metadata; the final two Rust tests passed
with the default resource configuration and no override. Tracked packaging
configuration was unchanged. No bundled runtime launch, release, live model
spawn, or running Desktop reload was verified by this worker. The existing
completion Toast tells the user to restart Codex.
