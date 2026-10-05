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

### Independent native Fast follow-up (2026-10-05)

The requested behavior is a native Luna subagent using Fast while disconnected
from CodexHub, with the main session's service tier unchanged. Luna's Fast
capability is separate from whether the native client can configure that tier
independently for a child. A shared root-tier toggle does not satisfy this request.

The initial schema-only explanation above is insufficient: native agent role
files can contain `service_tier`. The runtime ordering is the actual blocker:

- Codex 0.159.0 and 0.160.0
  [`prepare_agent_spawn_config`](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/core/src/agent/child_config.rs#L51)
  applies role configuration, then calls `apply_spawn_agent_service_tier`.
- That
  [tier operation](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/core/src/agent/child_config.rs#L255)
  clears a child's configured tier when the root has no tier, otherwise copies
  the root tier if the child model supports it. A role-only `priority` override
  is overwritten even when Luna supports Fast.
- Both native V1 and V2 spawn handlers use this preparation path. Upstream
  `main`, checked at
  [`823ea830c0fd418b09ff02d36cad9a1fff66465b`](https://github.com/openai/codex/blob/823ea830c0fd418b09ff02d36cad9a1fff66465b/codex-rs/core/src/agent/child_config.rs#L255),
  retains the same behavior.

A temporary Rust harness compiled the unmodified 0.160.0 tier function with a
model metadata stub that advertises Fast. With root tier unset and child tier
`priority`, the assertion that the child's tier remains `priority` fails: the
result is `None`. This isolates the upstream overwrite; it is not live-provider
or Desktop evidence.

No shared-tier UI/configuration change was applied. Supporting the requested
independent behavior requires a native Codex runtime change and an exposed
child-tier configuration contract before CodexHub can persist it effectively.

Related upstream reports, version-boundary evidence, and workaround options are
recorded in the [2026-10-05 upstream research](../../research/2026-10-05-native-subagent-fast-upstream.md).

The follow-up UI change is classified `fast`. Fast-capable native models now
show a disabled Fast row labeled “Follows main session”, with a visible
explanation that the tier cannot be configured independently. The row does
not claim the inherited tier is On or Off, and cannot save a setting. Native
model and effort remain selectable without a Gateway connection; Gateway
Fast aliases remain editable. No persistence or runtime behavior changed.

The focused frontend suite passes 23 tests, including the disconnected native
Luna explanation, the disabled action producing no save, and both Gateway
Luna Fast toggle directions retaining effort. TypeScript compilation and diff
hygiene pass. The report-only quality scan completed with no parse errors;
its existing dead-code/duplicate reports remain non-blocking. This follow-up
does not include a running Desktop or live-provider test.

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
