# Claude Code Gateway client (experimental)

Not a stable release claim. Pin: Claude Code **2.1.278** (Linux verified).
Credential contract: #558. Windows `yoga` CLI is now **2.1.278**; live rows wait on login.

## English

- Scope: inbound `/v1/messages`, discovery aliases, Connect into `~/.claude/settings.json`.
- Converted: Chat Completions and Responses. Native Anthropic SSE is passthrough.
- `count_tokens`: adapted best-effort local estimate (not a billed tokenizer). Images/caching/compaction/subagents are not live-proven.
- Non-Claude models are not Anthropic-endorsed.
- Stable requires: Windows CLI at the same pin as Linux, plus release-operator evidence on yoga. Linux live text/tool/cancel is recorded, not a Windows GO.

## 中文

- 范围：入站 `/v1/messages`、发现别名、Connect 写入 `~/.claude/settings.json`。
- 转换：Chat Completions 与 Responses。原生 Anthropic SSE 透传。
- `count_tokens`：本地 best-effort 估算（非计费 tokenizer）。图片/缓存/压缩/子代理未经 live 证明。
- 非 Claude 模型未经 Anthropic 背书。
- 转稳定需要：Windows CLI 与 Linux 同一 pin，以及 yoga 上的 release 操作证据。Linux live 文本/工具/取消已记录，不能代替 Windows。
