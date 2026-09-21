# Claude Code Gateway client (experimental)

Not a stable release claim. Verified observation pin: Claude Code **2.1.278**.
Live three-protocol CLI evidence is still blocked on a dedicated credential contract.

## English

- Scope: inbound `/v1/messages`, discovery aliases, Connect into `~/.claude/settings.json`.
- Converted: Chat Completions and Responses. Native Anthropic SSE is passthrough.
- Unsupported: `count_tokens` (explicit 400). Images/caching/compaction/subagents are not live-proven.
- Non-Claude models are not Anthropic-endorsed.
- Stable requires: credential-contract Issue, live CLI multi-turn/text/SSE/tools/cancel/errors on all three upstreams, and an updated support table.

## 中文

- 范围：入站 `/v1/messages`、发现别名、Connect 写入 `~/.claude/settings.json`。
- 转换：Chat Completions 与 Responses。原生 Anthropic SSE 透传。
- 不支持：`count_tokens`（明确 400）。图片/缓存/压缩/子代理未经 live 证明。
- 非 Claude 模型未经 Anthropic 背书。
- 转稳定需要：独立凭证合同 Issue、三条上游上的真实 CLI 多轮/文本/SSE/工具/取消/错误证据，以及更新后的支持表。
