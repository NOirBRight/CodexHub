# Claude Code Gateway support table (#78)

Status: **experimental**. Not a live GO. Pinned observed CLI: `2.1.278`
(host `claude --version` matches; version-gate exit 0). Loopback harness
`self-check` and `upstream-self-check --scenario text` passed on this SHA
(buffered fixtures only). Isolated CLI `run --enable-discovery` on 2.1.278
exited 0 with protocols `discovery`+`messages`, egress 0, non-loopback
connects 0, and `gateway-models.json` keeping only `claude*` ids. A
`--scenario tool` print-run admitted two `/v1/messages?beta=true`
requests and exited 0. `--scenario error` admitted two messages, CLI
exit 1 with `is_error` and `api_error_status=400`, egress 0. Live credential contract is #558. Approved identities:
Official Codex `gpt-5.6-luna` (Responses) and CommandCode
`deepseek/deepseek-v4.1-flash` (Chat). Native Anthropic is not approved.

| Capability | Class | Notes |
| --- | --- | --- |
| Main-session text JSON | adapted / preserved | Native Anthropic passthrough; Chat/Responses converted |
| Incremental SSE | adapted | Live Luna `POST /v1/messages?stream=true` HTTP 200, deltas `CODEXHUB_E2E_OK`, `message_stop`. Chat live conversion currently fail-closes |
| Tool call/result identity | adapted | Isolated CLI tool scenario: 2 messages, `stop_reason=end_turn`, no egress |
| Discovery `/v1/models` | adapted | CLI 2.1.278 cached 2 `claude*` ids; non-matching ids dropped before cache |
| Role mappings | adapted | Env keys written on Connect; empty mapping allowed |
| `count_tokens` | unsupported | Explicit Anthropic 400 |
| Images / caching / compaction / subagents / thinking | unknown or fail-closed | Not live-proven |
| Error handling | adapted | Isolated CLI error scenario: `is_error`, HTTP 400 surfaced, no egress |
| Cancellation | unknown | Native cancel exists in prototype; not live-proven |

Live HTTP (campaign Gateway, isolated `CODEX_HOME`, no keys in git):

- Responses/`gpt-5.6-luna`: streaming Messages GO.
- Chat/`commandcode/deepseek/deepseek-v4.1-flash`: HTTP 400 `unsupported_for_chat_conversion`.
- Native Anthropic: not approved, not run.
- Real Claude Code CLI against the same Luna route: model rewrite to `openai/gpt-5.6-luna` observed; CLI then dropped the SSE (`DownstreamKeepaliveFailedError` / broken pipe). Not a CLI GO.

Experimental → stable still needs a real-CLI pass on each approved upstream, plus a Chat conversion that does not refuse the live CommandCode body.
