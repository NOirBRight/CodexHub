# Claude Code Gateway support table (#78)

Status: **experimental**. Not a live GO. Pinned observed CLI: `2.1.278`
(Linux host `claude --version` matches; version-gate exit 0). Windows host
`yoga` upgraded `C:\Users\noirb\.local\bin\claude.exe` **2.1.232 → 2.1.278**.
Windows live: isolated checkout `D:\Workstation\CodexHub-claude-live` SHA
`e554bb8`. Claude Code 2.1.278 `-p` on Luna, CommandCode, and DeepSeek
Anthropic all `is_error=false`. Operator worktree untouched.
Loopback harness
`self-check` and `upstream-self-check --scenario text` passed on this SHA
(buffered fixtures only). Isolated CLI `run --enable-discovery` on 2.1.278
exited 0 with protocols `discovery`+`messages`, egress 0, non-loopback
connects 0, and `gateway-models.json` keeping only `claude*` ids. A
`--scenario tool` print-run admitted two `/v1/messages?beta=true`
requests and exited 0. `--scenario error` admitted two messages, CLI
exit 1 with `is_error` and `api_error_status=400`, egress 0. Live credential contract is #558. Approved identities:
Official Codex `gpt-5.6-luna` (Responses), CommandCode
`deepseek/deepseek-v4.1-flash` (Chat), and DeepSeek Anthropic
`deepseek-flash` at `https://api.deepseek.com/anthropic`.

| Capability | Class | Notes |
| --- | --- | --- |
| Main-session text JSON | adapted / preserved | Native Anthropic passthrough; Chat/Responses converted |
| Incremental SSE | adapted | Live Luna HTTP and Claude Code CLI 2.1.278 print-run `result=ok` |
| Tool call/result identity | adapted | Linux+Windows HTTP `tool_use` then `tool_result` (`sunny, 18C`) `end_turn` on all three |
| Discovery `/v1/models` | adapted | CLI 2.1.278 cached 2 `claude*` ids; non-matching ids dropped before cache |
| Role mappings | adapted | Env keys written on Connect; empty mapping allowed |
| `count_tokens` | unsupported | Explicit Anthropic 400 |
| Images | adapted | Valid 1×1 PNG live HTTP `end_turn`/`ok` on Luna, CommandCode, DeepSeek |
| Prompt cache | adapted | `cache_control.ephemeral` live HTTP 200 on all three (CommandCode empty `max_tokens`) |
| Compaction | unknown | Claude Code client-side; no Messages compact API. Not forced live. |
| Thinking | unknown or fail-closed | DeepSeek thinking+tool_choice 400; otherwise not separately proven |
| Subagents | adapted | Connect env maps haiku/sonnet/opus/fable. Isolated live with `ANTHROPIC_DEFAULT_HAIKU_MODEL`→Luna: Explore `spawned=1` `completed=1` `result=> ok`. Gateway still rejects unknown child ids. |
| MCP | adapted | Chrome DevTools MCP (local binary, headless slim) connected; CLI 3-turn `result=ok` |
| Error handling | adapted | Isolated CLI error scenario: `is_error`, HTTP 400 surfaced, no egress |
| Cancellation | adapted | Linux DeepSeek and Windows Luna HTTP abort: `downstream_stream_closed` 499, `request_complete` 499 |

Live (campaign Gateway, isolated `CODEX_HOME`, no keys in git):

- Responses/`gpt-5.6-luna`: Linux + Windows CLI 2.1.278 `-p` `result=ok`.
- Chat/`commandcode/deepseek/deepseek-v4.1-flash`: Linux + Windows CLI `-p` `result=ok`.
- Native Anthropic via DeepSeek `deepseek-flash`: Linux + Windows CLI `-p` `is_error=false` (Windows result `好的`).

Live tool round-trip HTTP (Linux+Windows): all three identities `tool_use` then `tool_result` then `end_turn`. Subagent mapping is CLI env, not Gateway substituting `haiku`. Chrome DevTools MCP live `result=ok`. Compaction not forced. No keys in git.
