# CLI subscription generation output budgets

Date: 2026-10-01. Scope: bounded primary-source research for ADR-0018 and the
mandatory Messages `max_tokens` integration constraint. The Orchestrator's
stated source candidate was `2f2e6e84`; no Gateway, backend, adapter or test was
modified by this research.

## Conclusion

Claude Code has a real **per-native-request** output-token limit:
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` reaches the outgoing API `max_tokens` field.
However, the installed CLI also contains automatic continuation after output
truncation. A process environment assignment alone is therefore not sufficient
evidence of a hard budget across one complete Gateway response. Whether the
isolated backend can preserve caller `max_tokens` remains unqualified.

For the installed Cursor AgentService `Run` protocol, no explicit generation
output-budget field or documented token-budget parameter was found. A generic
model-parameter extension exists, so absence of a named field does not prove
that the service has no such capability. It does mean there is no evidenced
mapping that CodexHub can safely implement from these findings.

## Bound and method

The research began at `2026-10-01T02:07:30Z`, with approximately three minutes
allocated to read-only discovery. Model generation requests: **zero**. No official
Provider CLI process, login, refresh, account/configuration mutation or replay was run.
Official documentation was read over the web; installed first-party binaries
were inspected offline with literal searches and bounded embedded-code excerpts.
No credential files, account logs or service configuration were read.

## Claude Code 2.1.286

The [official environment-variable reference](https://code.claude.com/docs/en/env-vars#environment-variables)
documents an output-token maximum for most requests, with model-dependent
defaults/caps and downward clamping above a model cap. For unresolved models,
it documents a 32000 default and 128000 cap. It separately describes context
and thinking controls; these are not interchangeable with output limits.

The [official CLI reference](https://code.claude.com/docs/en/cli-reference#cli-flags)
defines `--max-budget-usd` as money and `--max-turns` as agentic turns. Neither
is a replacement for caller output-token semantics.

Installed primary artifact:
[Claude executable](/home/noirbright/.local/share/mise/installs/claude/2.1.286/claude),
SHA-256 `fe503f65c6289d59c23e5b21ae44f03583f997dd33a2cbfc75ab4f96fb8fc73f`.
It contains readable embedded JavaScript. The following byte offsets refer to
that exact artifact, not stable source line numbers:

| Offset | Observed first-party implementation |
| --- | --- |
| 199433924 | `V6(model)` resolves default and upper output limits from known model/capability information. |
| 203786931 | `R$e` parses an environment value; invalid/nonpositive input uses the default, and values above the upper limit are capped. A future Gateway mapping must validate caller input rather than relying on this fallback. |
| 206271004 | `Pit(model)` reads `process.env.CLAUDE_CODE_MAX_OUTPUT_TOKENS` and returns that resolver's effective limit. |
| 206183800 | The request builder takes the minimum of the effective limit and a request override before constructing thinking parameters. |
| 206189083 | That computed value is assigned to the outgoing request's `max_tokens`. |
| 206242492 | A native output-limit event becomes a bounded CLI `max_output_tokens` error. |
| 212132907, 212201050 | The query loop has a recovery bound `dr=3`, `max_output_tokens_recovery` and `truncated_response_recovery` transitions, and can continue with another native request after a truncated response. |

These observations establish a genuine native parameter path, rather than a
prompt instruction or local character cutoff. They do not establish how many
requests the production CLI will make under the isolated backend, whether all
auxiliary paths use the same limit, or whether the caller-visible total stays
within its requested budget. Fixed thinking minimums and model-specific
behavior also require boundary checks for small caller values.

## Cursor CLI 2026.09.28-64d2043

The [official CLI parameters reference](https://cursor.com/docs/cli/reference/parameters)
was examined; no generation output-token budget option was found in that page.
This is a bounded documentation observation, not an assertion about every
Cursor product or unpublished service feature.

Installed primary artifact:
[Cursor Agent SEA executable](/home/noirbright/.local/share/mise/installs/cursor-agent/2026.09.28-64d2043/dist-package/cursor-agent-sea),
SHA-256 `6cdd14ae43665e497060ea8cb8aa9a82f2410c1a9f19ec515a41d2a253fa2f2c`.
The current `cursor-agent` launcher resolves into this versioned distribution.
Its embedded generated protobuf descriptors and service binding show:

| Offset | Observed first-party implementation |
| --- | --- |
| 130898435 | `agent.v1.AgentService.Run` is bidirectional streaming, using the generated request type. |
| 134190982 | `AgentRunRequest` has fields 1–33 including conversation/action, `model_details`, optional `requested_model`, caller MCP tools and capability flags; no explicit output-token generation budget is declared. |
| 134189410 | The corresponding `ModelDetails` describes model identity, thinking details, max mode and credential variants; no output-token budget field is declared. |
| 134414780 | `RequestedModel` includes repeated `parameters`; `RequestedModel.ModelParameterValue` is a generic string `id`/`value` pair. This offers an extension point, not evidence of a particular budget parameter. |
| 134669648 | `ModelParameterDefinition` describes parameter IDs/types. This bounded inspection did not identify an authoritative output-budget ID or its enforcement semantics. |
| 134178968, 134181357 | `max_tokens` belongs to prompt/conversation context usage snapshots, rather than a generation request budget. |
| 136026283 | Other absolute-token limits belong to `BugConfigResponse.BugBotV1`; they do not establish a `Run` generation control. |

Literal searches found no `maxOutputTokens`, `max_output_tokens`,
`max_completion_tokens`, `outputTokenLimit` or `generationConfig` in this
artifact. The `maxTokens` occurrences inspected were MCP sampling schemas or
context-usage fields. Output-token usage counters are telemetry, not input
budget controls. No generic parameter ID or new protobuf field should be
invented from these names.

A companion worker's read-only check of the same installed distribution also
examined [the installed index.js](/home/noirbright/.local/share/mise/installs/cursor-agent/2026.09.28-64d2043/dist-package/index.js):
7,549,229 bytes, SHA-256
`45d9b1df85d0165cb2e690f96fa5fbe4b59e8a71ddd22176a301ba7e5b18a0b9`.
Zero-based UTF-8 byte offsets 5084517, 5082945 and 5308315 reproduce the Run,
ModelDetails and RequestedModel descriptors above. Offset 3590736 shows the
actual CLI Run construction without an output cap. Offset 5563183 describes
boolean/enum model parameters, not an evidenced integer generation limit.
Crucially, `GetChatRequest` at offset 5590573 does have a
`desired_max_tokens` field 26, and the distinct `StreamChatContextRequest`
has field 22 with that name. These are other legacy request types; their field
numbers cannot be transplanted into AgentService Run. This strengthens the
version-specific distinction without asserting anything about hidden server
support.

## What would qualify a mapping

For Claude, first prove the request path in an isolated local capture using
the exact supported official binary: accepted caller values reach native
`max_tokens`, invalid/conflicting values fail before launch, and truncation
does not silently initiate additional generation beyond the caller's budget.
The CLI's recovery behavior needs an evidenced control or a lifecycle strategy
that preserves terminal/error semantics without local token estimation.
This bounded pass did not verify a flag that disables output recovery, or
whether `--max-turns 1` counts those recovery transitions. The documented
agentic-turn limit alone does not establish that equivalence. The discovered
native mechanism is a candidate for further mapping work, rather than proof
that a mapping is impossible once permission is restored; until whole-response
semantics are established, Messages parameter rejection remains correct.
Then genuine subscription-account inference requires the current account to
have permission: the recorded Claude 403 remains a blocker, and this research
does not remove it. Any future live probe requires its own explicit bound and
success/failure cues.

For Cursor, first obtain an official documented parameter or an authoritative
installed-code construction showing the exact valid parameter ID/field,
type and server-enforced generation semantics. A generic extension, context
window or money/turn limit cannot satisfy that prerequisite. Until such
evidence and a bounded runtime check exist, explicit unsupported-parameter
rejection remains the truthful behavior.

No parameter support or provider qualification is claimed here. Ordinary
Responses success, advanced Code Mode/V2, Messages compatibility and Windows
same-SHA evidence remain distinct acceptance surfaces.
