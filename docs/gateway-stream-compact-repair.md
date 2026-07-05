# Gateway Stream / Compact / Client Protocol Repair

日期：2026-07-05

## 背景

本轮排查覆盖三类现象：

- Codex App 会话在长 Responses SSE 请求中频繁重连，典型错误为 `stream closed before response.completed`。
- ZCode 使用 `codexhub-openai/gpt-5.5` 做 compact 时出现 Bad Gateway、Headers Timeout、Invalid JSON response。
- ZCode 切到 `codexhub-ollama-cloud/glm-5.2` 后 compact 仍失败，上游 HTTP 200 但返回空文本。

相关证据来自：

- `C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl`
- `C:\Users\noirb\.zcode\cli\log\zcode-2026-07-05.jsonl`
- `C:\Users\noirb\.zcode\cli\rollout\model-io-sess_ac47daa7-82b9-425e-bc24-25f98013a07c.jsonl`

## 结论

- SSE 完整性是 gateway 通用问题，不应按模型名特殊处理。GPT-5.5 最明显，是因为长流式请求更大更久；日志中其他 OpenAI、Ollama Cloud、Volcengine 模型也出现过 timeout、reset、remote disconnect、502。
- compact 是 gateway 的通用 request kind，不是 ZCode 专用逻辑。它沿用同一次路由选出的 provider 和 `upstream_format`，只额外做工具剥离、空响应识别和 compact 专用重试。
- ZCode、OpenCode、Pi、OMP 等第三方客户端导出不应把某个 provider 固定成某个端点协议。客户端配置必须读取每个 provider 的 `upstream_format`：
  - `responses` 导出为 OpenAI Responses 路径。
  - `chat_completions` 导出为 Chat Completions 路径。
  - `anthropic_messages` 对 OpenAI-compatible 客户端仍走 Chat Completions 兼容路径。

## 关键证据

### 长 SSE / Stream

2026-07-05 UTC 当天：

- `Codex App + openai/gpt-5.5`: 576 个 stream starts，535 成功，40 个明确失败，约 6.9%。
- 主要失败类型：`TimeoutError`、`ConnectionResetError`、`ConnectionAbortedError`、`request_complete status=502`。
- `Codex App + openai/gpt-5.4-mini` 同日也出现 `upstream_stream_interrupted TimeoutError` 和 `ConnectionAbortedError`。

历史日志还显示：

- `opencode + openai/gpt-5.5`: 有 `ConnectionResetError`、`URLError`、`upstream_stream_interrupted`。
- `ZCode + openai/gpt-5.5`: 有 `ConnectionResetError`、`ConnectionAbortedError`、stream 502。
- `ollama_cloud/kimi-k2.7-code`、`ollama_cloud/glm-5.2`、`volc/glm-5.2`: 有 provider 侧 502、reset、remote disconnect、URLError。

### Compact GLM-5.2

ZCode compact 请求 `d01229de-0a11-491b-9236-3d35c7afc317`：

- request body 约 878 KB，gateway 记录 `content_length: 923977`。
- `messages: 676`
- `tools: 57`
- `tool_choice: "auto"`
- final user prompt 包含 `CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.`
- gateway 事件 `explicit_codex_tools_injected` 又注入 5 个 multi-agent tools。
- 上游结果：HTTP 200，`finishReason: stop`，`textLength: 0`，`toolCallCount: 0`。
- ZCode 之后记录 `compact.failed`，用户可见错误为 `Failed to generate compact summary`。

### ZCode 协议配置 Gap

CodexHub provider 设置中 `ollama-cloud` 可以配置为 `upstream_format = "responses"`，catalog 也能导出 Responses endpoint。但 ZCode v2 的 `D:\zcode\.zcode\v2\config.json` 曾只写入 `options.baseURL`，没有写入 `apiFormat` 和 `endpoints.paths`。

结果是 ZCode 重新生成 `bots-model-cache.v2.json` 时回退成 `openai-chat-completions`，造成 ZCode 使用 Chat，而 CodexHub/Gateway 中同一个 provider 已设置为 Responses。

## 已实现修复

### SSE Framing

- Buffered Responses SSE 必须看到 terminal event 才能合成 non-stream JSON。
- Responses SSE 转 Chat Completions SSE 必须看到 terminal event 才能写 finish chunk 和 `[DONE]`。
- Chat Completions SSE 必须看到 `[DONE]` 或 finish chunk 才能写 `[DONE]`。
- Responses SSE passthrough 会跟踪 `response.completed`、`response.failed`、`response.incomplete`、`error`；EOF 前没 terminal 时写 downstream SSE error。
- terminal event 之后会补齐事件分隔，避免客户端继续等待。

### Compact

- 支持显式请求类型 header：`x-query-source: compact`、`x-request-kind: compact`。
- 保留 prompt heuristic：检测 `Respond with TEXT ONLY`、`Do NOT call any tools`、`summary/compact`、`<summary>`。
- compact 请求进入 upstream 前删除 `tools`、`tool_choice`，并禁止 gateway 注入 Codex/multi-agent tools。
- compact 响应为空时返回 `compact_empty_response`。
- compact 空响应默认最多重试 3 次。

### Client Protocol Export

ZCode v2 config 导出现在写入：

- `apiFormat`
- `endpoints.baseURL`
- `endpoints.paths.openai-compatible`
- 保留 `options.baseURL` 作为旧版本兼容字段

### Vision Proxy

- vision 子请求保持 text-only。
- `inject_codex_tools=False`
- adapter 转换后再次剥离 `tools` 和 `tool_choice`。
- vision 子请求默认只尝试 1 次，不继承普通生成或 compact 的重试预算。

## 错误时限与重试预算

- 已开始输出后，上游静默默认 `60` 秒返回 `upstream_stream_idle_timeout`。
- 未开始输出但 SSE 已建立后，上游静默默认 `90` 秒返回 `upstream_stream_idle_timeout`，event log 字段 `stream_idle_phase=pre_output`。
- compact 空响应最多重试 `3` 次，可通过 `gateway_compact_retry_max_attempts` 或 `CODEX_PROXY_COMPACT_RETRY_MAX_ATTEMPTS` 调整。
- main generation 的可见输出前重试最多 `3` 次，可通过 `gateway_main_generation_retry_max_attempts` 或 `CODEX_PROXY_MAIN_GENERATION_RETRY_MAX_ATTEMPTS` 调整。
- image proxy vision 请求保持 `1` 次，不继承全局 retry budget。

## Client 配合建议

### ZCode

- compact 请求不要带 tools。
- compact 请求发送 `x-query-source: compact` 或 `x-request-kind: compact`。
- 空 summary 响应记录为 `empty_compact_response`，保留 request id 和 provider id。
- 超大 compact 使用分块摘要，避免一次提交 600+ messages。

### 其他客户端

- summary、memory compaction、session compression、handoff summary 等 text-only 请求也应发送显式 request kind。
- 不应在 text-only summary 请求中携带工具 schema。
- 遇到 `compact_empty_response`、`upstream_stream_incomplete` 时可安全重试同一个 compact 请求。

## 不做的事

- 不在 gateway 中自动续写已开始输出后的 agent turn。原因是工具调用和文件编辑可能已经发生，gateway 无法安全重放上下文并保证副作用幂等。
- 不把某个 provider 永久固定成 Responses 或 Chat。协议必须来自 provider 配置项。
