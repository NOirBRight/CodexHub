# Claude Code Gateway client (experimental)

Not a stable release claim. Pin: Claude Code **2.1.278** (Linux and Windows
CLI pin match). Credential contract: #558. Full release-operator GO remains
issue #78; see `docs/evidence/claude-code-support-table.md`.

## English

- Scope: inbound `/v1/messages`, discovery aliases, Connect into `~/.claude/settings.json`.
- Converted: Chat Completions and Responses. Native Anthropic SSE is passthrough.
- `count_tokens`: adapted best-effort local estimate (not a billed tokenizer).
- Images, prompt cache, subagents, MCP, tool round-trips, and cancellation have
  recorded Linux+Windows live/HTTP evidence in the support table. Compaction is
  client-side and not forced live.
- Non-Claude models are not Anthropic-endorsed.
- Experimental ship is allowed with the support table evidence. Promoting to a
  stable client claim still needs an explicit #78 release-operator GO.

## 中文

- 范围：入站 `/v1/messages`、发现别名、Connect 写入 `~/.claude/settings.json`。
- 转换：Chat Completions 与 Responses。原生 Anthropic SSE 透传。
- `count_tokens`：本地 best-effort 估算（非计费 tokenizer）。
- 图片、prompt cache、子代理、MCP、工具往返与取消的 Linux+Windows live/HTTP
  证据见 support table。Compaction 为客户端行为，未强制 live。
- 非 Claude 模型未经 Anthropic 背书。
- 可按 support table 作为 experimental 交付；升为稳定客户端声明仍需 #78 的
  release-operator GO。
