# Claude Code Gateway client (experimental)

Not a stable release claim. Pin: Claude Code **2.1.278** (Linux and Windows
CLI pin match). Credential contract: #558. Full release-operator GO remains
issue #78; see `docs/evidence/claude-code-support-table.md`.

## English

- Scope: inbound `/v1/messages`, discovery aliases, Connect into `~/.claude/settings.json`.
- To preserve every Claude subscription model, leave global Connect off and run
  `codexhub-claude-gateway --list`, then `codexhub-claude-gateway MODEL` for a
  separate Gateway session. Ordinary `claude` stays on the subscription. The
  launcher is included in the Linux portable candidate and was exercised with
  Claude Code 2.1.280 against isolated Gateway routes to DeepSeek and Luna.
  Resuming a native subscription session in a Gateway process is unsupported:
  its explicit model choice overrides the Gateway default.
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
- 要保留全部 Claude 订阅模型，请勿启用全局 Connect。先运行
  `codexhub-claude-gateway --list`，再运行 `codexhub-claude-gateway MODEL`
  启动单独的 Gateway 会话；普通 `claude` 仍走订阅。Linux 候选包包含该启动脚本，
  已用 Claude Code 2.1.280 对隔离 Gateway 的 DeepSeek 和 Luna 路由验证。
  不支持在 Gateway 会话中恢复原生订阅会话，因为显式选择的模型会覆盖默认模型。
- 转换：Chat Completions 与 Responses。原生 Anthropic SSE 透传。
- `count_tokens`：本地 best-effort 估算（非计费 tokenizer）。
- 图片、prompt cache、子代理、MCP、工具往返与取消的 Linux+Windows live/HTTP
  证据见 support table。Compaction 为客户端行为，未强制 live。
- 非 Claude 模型未经 Anthropic 背书。
- 可按 support table 作为 experimental 交付；升为稳定客户端声明仍需 #78 的
  release-operator GO。
