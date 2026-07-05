# Gateway Stream and Compact Repair Note

## 背景

2026-07-05 的故障排查覆盖了三类现象：

- Codex App 内部会话频繁重连，集中出现在 large Responses SSE 请求。
- ZCode 使用 `codexhub-openai/gpt-5.5` 做 compact 时反复出现 Bad Gateway、Headers Timeout、Invalid JSON response。
- ZCode 切到 `codexhub-ollama-cloud/glm-5.2` 后 compact 仍失败，但这次上游 HTTP 200，返回空文本。

相关证据来自：

- `C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl`
- `C:\Users\noirb\.zcode\cli\log\zcode-2026-07-05.jsonl`
- `C:\Users\noirb\.zcode\cli\rollout\model-io-sess_ac47daa7-82b9-425e-bc24-25f98013a07c.jsonl`

## 结论

这里有两个独立根因，gateway 都可以改善。

第一类是模型无关的长 SSE/stream 完整性问题。GPT-5.5 最明显，是因为流量最大、上下文最大；但日志显示 `gpt-5.4-mini`、历史 `gpt-5.4`、`ollama_cloud`、`volcengine` 也出现过 timeout、reset、remote disconnect、502。修复应落在 gateway 的通用 SSE 完整性层，而不是只对 `openai/gpt-5.5` 做特殊处理。

第二类是 compact 语义没有被 gateway 识别。ZCode 的 GLM-5.2 compact 请求明确要求 text-only 且禁止工具，但请求仍带 `57` 个 tools 和 `tool_choice:auto`，gateway 又额外注入了 5 个 multi-agent tools。上游返回 HTTP 200、`finishReason: stop`、`text: ""`，ZCode 最终报 `Failed to generate compact summary`。这次直接命中的是 ZCode，但机制风险不是 ZCode 专属：任何客户端发 summary/compact/text-only 请求时仍带工具，都可能触发同类空输出或工具误选。

## 关键证据

### 长 SSE/stream

2026-07-05 UTC 当天：

- `Codex App + openai/gpt-5.5`: 576 个 stream starts，535 成功，40 个明确失败，约 6.9%。
- 主要失败类型：`TimeoutError`、`ConnectionResetError`、`ConnectionAbortedError`、`request_complete status=502`。
- `Codex App + openai/gpt-5.4-mini`: 同日也出现 `upstream_stream_interrupted TimeoutError` 和 `ConnectionAbortedError`。

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

## 当前代码风险点

`src-python/codex_proxy.py` 的几个路径会把不完整流伪装成成功：

- `_events_to_responses_body(events)` 会无条件合成 `"status":"completed"`。
- `_response_events_to_chat_stream_chunks(events)` 在没有 finish 时默认 `finish_reason = "stop"`。
- Chat Completions SSE 收集后会无条件写出 `data: [DONE]`。
- Responses SSE passthrough 遇到 EOF 时目前没有统一校验是否看到了 `response.completed`、`response.failed` 或等价 terminal event。

这些行为会让客户端看到半截流、坏 JSON、或看起来成功但内容为空的响应。

## 修复原则

1. Stream 完整性按 wire format 修，不按模型名修。
2. Compact 是 request kind，不是 ZCode 私有逻辑。
3. 对已经开始下发的 SSE，不能改 HTTP status；必须发送合法 downstream SSE error，并关闭连接。
4. 对还没下发的 buffered/non-stream 响应，可以返回结构化 502。
5. Compact 可做 gateway 内部幂等重试，因为它不应包含工具调用副作用。
6. 非 compact 的空文本先做 telemetry，不直接全局改成 502，避免误伤正常工具调用或特殊模型协议。

## Gateway 修复范围

- 支持显式请求类型 header：`x-query-source: compact`、`x-request-kind: compact`。
- 保留 prompt heuristic：检测 `Respond with TEXT ONLY`、`Do NOT call any tools`、`summary/compact`、`<summary>`。
- compact 请求进入 upstream 前删除 `tools`、`tool_choice`，并禁止 gateway 注入 Codex/multi-agent tools。
- compact 响应为空时返回 `compact_empty_response`，并记录 request id、query id、session id、trace id。
- buffered Responses SSE 必须看到 `response.completed` 才能合成 non-stream JSON。
- Responses SSE 转 Chat Completions SSE 必须看到 `response.completed` 才能写 finish chunk 和 `[DONE]`。
- Chat Completions SSE 必须看到 `[DONE]` 或 finish chunk 才能写 `[DONE]`。
- Responses SSE passthrough 必须跟踪 terminal event；EOF 前没 terminal 时写 downstream SSE error。
- GPT-5.5 catalog/context 从 `272000` 统一到 `258400`，避免客户端贴着错误上限发请求。

## Client 配合建议

### ZCode

- compact 请求不要带 tools。
- compact 请求发送 `x-query-source: compact` 或 `x-request-kind: compact`。
- 空 summary 响应记录为 `empty_compact_response`，保留 request id 和 provider id。
- 超大 compact 使用分块摘要，避免一次提交 600+ messages。

### 其他客户端

- 如果有 summary、memory compaction、session compression、handoff summary 等 text-only 请求，也应发送显式 request kind。
- 不应在 text-only summary 请求中携带工具 schema。
- 遇到 gateway 的 `compact_empty_response`、`upstream_stream_incomplete` 时可安全重试同一个 compact 请求。

## 验收标准

- ZCode GLM-5.2 compact 请求不再带工具进入上游，也不会把 HTTP 200 空文本当作成功。
- GPT-5.5 compact 的 Bad Gateway/Invalid JSON 类故障减少；仍发生上游断流时，客户端收到结构化 SSE error 或 JSON error。
- Codex App 重连不再由 gateway 合成假成功或半截 `[DONE]` 放大。
- gateway event log 可按 `request_kind=compact`、`upstream_stream_incomplete`、`compact_empty_response` 检索。
- `python -m unittest discover -s tests -q` 和 `cargo test -q` 通过。
