# Claude Code Gateway support table (#78)

Status: **experimental**. Not a live GO. Pinned observed CLI: `2.1.278`
(host `claude --version` matches; version-gate exit 0). Loopback harness
`self-check` and `upstream-self-check --scenario text` passed on this SHA
(buffered fixtures only). Live credential-contract Issue is still missing,
so no live matrix rows.

| Capability | Class | Notes |
| --- | --- | --- |
| Main-session text JSON | adapted / preserved | Native Anthropic passthrough; Chat/Responses converted |
| Incremental SSE | adapted / preserved | Native forwards `event.raw`; Chat converted per event |
| Tool call/result identity | adapted | Prototype + production conversion; fail-closed if identity breaks |
| Discovery `/v1/models` | adapted | `anthropic-version` projects `claude-codexhub-*` aliases |
| Role mappings | adapted | Env keys written on Connect; empty mapping allowed |
| `count_tokens` | unsupported | Explicit Anthropic 400 |
| Images / caching / compaction / subagents / thinking | unknown or fail-closed | Not live-proven |
| Cancellation | unknown | Native cancel exists in prototype; not live-proven |

Experimental → stable requires: approved credential contract, live three-protocol CLI evidence, and this table updated from those runs.
