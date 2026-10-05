# Claude Code independent Default subagent — issue #616

Date: 2026-10-02. Verification class: strict.

## Native contract

The [Claude model configuration documentation](https://code.claude.com/docs/en/model-config#environment-variables)
defines `CLAUDE_CODE_SUBAGENT_MODEL` as a default accepting an alias or a full
model ID. The [subagent model selection documentation](https://code.claude.com/docs/en/sub-agents#choose-a-model)
specifies that an explicit invocation or agent definition takes precedence in
current versions. Removing the environment key restores native model selection.
The implementation leaves custom definitions and `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`
unchanged. There is no independent global Default subagent effort or Fast
setting in this contract; no such fields are written.

## Installed-client observation

`claude --version` returned `2.1.287 (Claude Code)` on Windows. The existing
inference-free discovery script, invoked through the repository Python launcher
with `--native-route` and an isolated configuration path, returned these exact IDs:

- `claude-opus-5-5`
- `claude-fable-5-1`
- `claude-sonnet-5-5`
- `claude-haiku-4-5-20251001`

The discovery control message initializes the CLI and requests its model list;
it submits no user inference. Its process has a 15-second timeout, and version
readback has a 3-second timeout. Native discovery does not inject a Gateway
route. Existing Connect discovery retains its previous Gateway lineup behavior.

## Focused verification

- The frontend regression first failed because a native Subagent selection was
  rejected with an empty Gateway catalog; all 10 Claude settings tests now pass.
- TypeScript typechecking passes. The existing browser scenario was extended
  to save a native subagent with unsaved main/family edits, verify only the
  subagent key changed, and verify preservation after Connect and Disconnect.
  The browser scenario was syntax checked; it has not been executed here.
- All 25 Rust tests matching `claude` pass with the actual prepared Tauri
  resources and no resource override. They cover independent save/readback,
  original authentication/route/main/family/custom configuration preservation,
  explicit native default reset, settings restart, native deployment identities,
  rejection of disconnected Gateway pins, Connect/republish and Disconnect.
- All 23 focused Python native discovery and Claude projection tests pass.
  Discovery tests cover both native and Gateway modes, exact IDs, isolated
  configuration, and failure without publishing an empty model list. Pytest
  emitted an unrelated Windows temporary-directory cleanup permission warning
  after the successful run (exit code 0).

This evidence proves native configuration and lifecycle behavior, plus installed
CLI discovery. It does not claim a live provider inference or a spawned-agent
run. The integrated candidate's full suites and review belong to the orchestrator.
