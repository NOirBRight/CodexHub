# ZCode native Default subagent — #618

The independent action writes the actual builtin `general-purpose` and `Explore`
overrides in the detected ZCode v2 `agents-state.json`. Model menus read native
provider/model metadata from the client's config and model cache; Gateway IDs
remain distinct. Only model-declared reasoning variants are offered; there is no
invented native Fast field. Reset removes builtin overrides.

Official contract checked on 2026-10-02 at upstream commit
`29628c9acdb81b703bbd4080c207a0e7ce5e276e`:

- [Builtin selection writer](https://github.com/zai-org/ZCode/blob/29628c9acdb81b703bbd4080c207a0e7ce5e276e/packages/services/src/subagents/subagentsService.ts)
  changes `builtInModelSelectionOverrides`, separately from custom agents.
- [Storage](https://github.com/zai-org/ZCode/blob/29628c9acdb81b703bbd4080c207a0e7ce5e276e/packages/services/src/subagents/subagentStorage.ts)
  locates the builtin state under `v2/agents-state.json`.
- [Runtime loader](https://github.com/zai-org/ZCode/blob/29628c9acdb81b703bbd4080c207a0e7ce5e276e/apps/zcode-cli/packages/bootstrap/src/subagents.ts)
  consumes structured builtin selections after importing legacy state.
- [Legacy state import](https://github.com/zai-org/ZCode/blob/29628c9acdb81b703bbd4080c207a0e7ce5e276e/packages/shared/src/subagent-state-migration.ts)
  documents the existing `builtInModelOverrides` and
  `builtInThoughtLevelOverrides` contract. A local existing state file also
  corroborated this legacy shape; it was read without modification.
- [Custom identity encoding](https://github.com/zai-org/ZCode/blob/29628c9acdb81b703bbd4080c207a0e7ce5e276e/packages/shared/src/custom-model-value.ts)
  preserves provider/model identity in legacy state.
- [Official user documentation](https://zcode.z.ai/en/docs/subagents)
  requires a new ZCode session for changed subagent settings. The success Toast
  names that action.

Native saves preserve main configuration, credentials, custom agent Markdown,
plugin overrides and disabled-agent state. A provenance-bound native baseline
prevents stale Gateway pins from overwriting native saves. Explicit Gateway
selection still writes its builtin selection and restores the latest native
baseline on detach. Native model-cache providers are preserved on republish.
Earlier marked Gateway Markdown shadows are restored without rewriting unrelated
header lines or prompt bodies; ordinary user Markdown is untouched.

Verification uses the existing public isolated apply/readback seams and focused
native save/readback tests in both current and existing legacy state shapes.
It covers native save, reset, connected save, manual per-builtin edit,
Connect/republish/Disconnect, native → Gateway → detach, and custom-state
preservation. No real ZCode session, model inference, or user configuration write
was performed. Full strict checks and integrated review belong to the final
candidate.
