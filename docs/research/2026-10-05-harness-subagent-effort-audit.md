# Harness Default subagent effort audit

Execution ticket: [#629](https://github.com/NOirBRight/CodexHub/issues/629).
Base: `3ce63b0a` (merged theme/OpenCode fix #628). Risk: strict, shared native
persistence and desktop command contract. No inference or provider requests.

| Harness | Installed version / verification | Outcome |
| --- | --- | --- |
| Codex | 0.159.3; native read returned 8 models with levels | Independent native child model/effort; no omission found. |
| OpenCode | 2.0.23; native read returned 73 models, 49 with variants | Earlier #628 fix retained v2 declared variants; save/readback regression remains covered. |
| OMP | 18.6.1; native read returned one configured model with `default/max` | Existing CLI `thinking` metadata and `task.agentModelOverrides` suffix path retain native declared levels; no omission found. |
| Grok | 1.0.46 (2765805b9442); native read returned 4 models with levels | Existing native cache `reasoning_efforts` and child role persistence retain advertised levels; no omission found. |
| ZCode | Not installed; isolated native config/cache + builtin state tests | Config identity/name rows hid cache reasoning. Merge cache metadata with configured fields taking precedence, including explicit disabled states. Offer empty effort to remove an override while retaining the model. |
| Claude Code | 2.1.286; native default-subagent writer and official settings docs | Global `CLAUDE_CODE_SUBAGENT_MODEL` controls model only. Individual agent frontmatter `effort` is separate. Explain this entry's limitation instead of blaming the selected model. |
| Pi | 1.0.2; installed README and official core docs | Core still explicitly excludes built-in subagents. No invented independent setting or mandatory extension added. |
| DSH | 0.2.0-rc.2; installed official schema/adapters and public API isolated CLI test | Child `agentOptions.reasoningEffort` now supported; previous adapter discarded levels and effort. Extend the existing Headless composition seam. |

## DSH contract and compatibility

Primary evidence: the installed official `@deepseek-ai/dsh-tool-subagent`,
`dsh-llm-pi-ai`, `dsh-llm-deepseek`, and `dsh-agent` packages at 0.2.0-rc.2.
`tool-subagent/lib/index.js` declares and forwards `agentOptions.reasoningEffort`;
`dsh-agent/lib/types/runtime-types.d.ts` declares the child field.
The Pi adapter uses its own `pi-ai` catalog and `getSupportedThinkingLevels`,
with configured `reasoningEfforts` and `modelOverrides` taking precedence.
DeepSeek's public `resolveAdapterOptions` / `DeepSeekAdapter.resolveModel`
provide exact advertised choices, including the deployment's off-only restriction.

Discovery serializes the native tool Config schema to detect the field; old
model-only schema produces no effort choices. The helper reads detached metadata
only. Credentials, endpoints, headers and other provider configuration are
excluded from helper arguments. It does not start DSH sessions or initialize a
persistent DeepSeek file index. Levels come from declarations/native utilities,
never from model-name guesses. Empty effort retains client/default behavior.

The existing Headless `cordis.patch.yml` marked block changes only provider,
model and reasoningEffort for both spawn/fork. The composed rows preserve persona,
filters, maxTokens and other fields. Save validates catalog membership, and actual
readback checks all three fields for both rows; mismatch rolls back the file.
Reset removes effort, including stale effort when changing models. Main settings,
credentials and Web presets remain intact. Start a new Headless invocation to
apply; the application never automatically restarts the client.

The official CLI evidence runs entirely inside disposable DSH_HOME, exercises
native save/readback, effort/reset, Connect, republish and Disconnect. Cordis 0.2
rejects patch entries for nonexistent identities, so the unrelated preservation
fixture now uses the real `fs-local` row. No live client settings were written.

## Claude and Pi limits

[Claude's official subagent documentation](https://code.claude.com/docs/en/sub-agents)
specifies effort per agent definition. Its global default-subagent model setting
has no parallel independent effort setting. Changing global effort would also
change the parent; shadowing builtins would replace prompts/tools rather than
patch only effort, violating ADR0019. This repair therefore provides an accurate
entry-specific explanation and preserves existing agent definitions.

[Pi's official README](https://github.com/earendil-works/pi#readme) explicitly
places subagents outside core. The locally installed 1.0.2 README says the same.
No claim is made about third-party extensions.

## Verification

- Red ZCode fixture: configured `native/child` had cache `off/high`, but returned
  `[]` rather than `default/off/high`. It now saves high, reads it back, and resets
  only effort; explicit configured disable remains authoritative.
- DSH official 0.2.0-rc.2 public native API: isolated discovery, high save/readback,
  reset and connection lifecycle passed (no inference).
- Deterministic DSH regressions: exact declared grades, model overrides, DeepSeek
  restrictions, legacy model-only schema, both child rows, invalid-grade rejection,
  parent preservation and rollback passed.
- Full Linux verification and Standards/Spec review results are recorded in the PR.
