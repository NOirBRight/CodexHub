# Authenticated Ollama Provider qualification

Source candidate: `73e4ed3d444414dedcc1a5902fa9f0a9c848d469`
Client: `codex-cli 0.147.0`
Provider: `ollama-cloud` (`https://ollama.com/v1`)

This evidence qualifies the maintained GLM-5.2, Kimi K2.7 Code, and DeepSeek
V4 Flash 0731 routes. The real Codex CLI sends Responses requests to the
Gateway; the Chat cells execute through the Provider's real
`/v1/chat/completions` endpoint and return converted Responses streams.

## Credential boundary

The runner resolves the existing installed `ollama-cloud` key but never copies
it into an isolated configuration or evidence. The temporary Provider file
contains only `{env:CODEXHUB_AUTH_QUAL_KEY}`. That variable is scoped to
catalog-sync and Gateway child processes and is absent from the CLI child.
Every isolated Home is removed in `finally`; `.evidence-homes/` is ignored to
prevent interrupted runs from becoming repository inputs.

## Chat results

See `chat/summary.json` (`codexhub.authenticated-provider-cli.v1`). All three
cells passed:

| Model | Non-stream Chat probe | Streaming CLI text | exec → apply_patch → verify | Collaboration V2 | Same-/Cross-Home |
| --- | :---: | :---: | :---: | :---: | :---: |
| `glm-5.2` | pass | pass | pass | six tools + child result | pass / rejected before request |
| `kimi-k2.7-code` | pass | pass | pass | six tools + child result | pass / rejected before request |
| `deepseek-v4-flash:0731` | pass | pass | pass | six tools + child result | pass / rejected before request |

Each cell remained bound to Provider `ollama-cloud`, its exact upstream model,
and protocol `chat_completions`. Fallback count is zero and no V1 namespace or
tool was observed. Qualification now requires at least one genuinely progressive
text stream (three or more Chat chunks and at least two text delta sources), the
real CLI's explicit default reasoning policy, and a second text turn resumed in
the same session. The file workflow records both `command_execution` and
`file_change`, plus exact standard/custom call-to-result identity. The exact
target-scoped patch is supplied to remove model prompt variance while still
exercising the reversible custom-tool call/result/history adapter.

Collaboration runs across bounded same-session phases: spawn/wait must first
deliver a non-timeout child result; follow-up, message, list, and interrupt are
then each required in their own turn against that canonical task identity. This
removes model instruction-order variance without weakening the lifecycle oracle:
every phase must complete, emit its sentinel, and increase the target tool count.
Evidence also requires exact call/result history identity, frozen
wait/list-status/interrupt result shapes, parent completion, and another turn
after Gateway restart without a new spawn.

The lifecycle deliberately interrupts a live child. Corresponding bounded 499
`downstream_client_closed` observations are retained rather than rewritten as
Provider success. Any observed 400/transport attempt remains visible by status
in the summary; final identity and lifecycle assertions must still pass without
fallback or cross-Provider execution.

The separate Beta5 Responses regressions #275/#276/#393 remain open. Direct
non-streaming Responses probes reached all three exact models, but those probes
are not committed as Beta6 Chat qualification evidence and no shim is used to
mask their independent CLI schema gate.

## Generic implementation evidence

The Provider run is combined with deterministic coverage in:

- `tests/test_issue_394_chat_collaboration_v2.py` for alias/call/item/history
  inverse and the protocol-scoped native Responses codec;
- `tests/test_issue_395_chat_v2_lifecycle_fixture.py` and
  `tests/test_issue_395_evidence.py` for progressive stream and terminal rules;
- `tests/test_authenticated_provider_cli_runner.py` for credential scoping,
  bounded summaries, matrix completeness, and session evidence extraction;
- the #66 matrix and C4 negative cases for unavailable/mixed semantics.

No GLM-, Kimi-, or DeepSeek-specific production conversion/routing branch was
added. The only production correction is protocol-based: a configured native
Responses codec cannot filter declarations on a Chat attempt.

## Reproduce

```powershell
.\scripts\codexhub-python.cmd scripts/qualify_authenticated_provider_cli.py `
  --cell chat_completions:glm-5.2 `
  --cell chat_completions:kimi-k2.7-code `
  --cell chat_completions:deepseek-v4-flash:0731 `
  --output-dir docs/evidence/authenticated-provider/chat `
  --candidate-sha 73e4ed3d444414dedcc1a5902fa9f0a9c848d469
```
