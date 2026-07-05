# Gateway SSE / Compact / Client Protocol Repair

日期：2026-07-05

## 结论

- ZCode、OpenCode、Pi、OMP 等第三方客户端导出不应把某个 provider 固定成某个端点协议。
- 客户端配置必须读取 CodexHub 中每个 provider 的端点协议设置，也就是 `upstream_format`：
  - `responses` 导出为 OpenAI Responses 路径。
  - `chat_completions` 导出为 Chat Completions 路径。
  - `anthropic_messages` 当前对 OpenAI-compatible 客户端仍走 Chat Completions 兼容路径。
- compact 是 gateway 的通用 request kind，不是 ZCode 专用逻辑。它沿用同一次路由选出的 provider/upstream format，只额外做工具剥离、空响应识别和 compact 专用重试。

## 已确认问题

### ZCode 协议配置 Gap

CodexHub provider 设置中 `ollama-cloud` 已经可以配置为 `upstream_format = "responses"`，catalog 也能导出 Responses endpoint。但 ZCode v2 的 `D:\zcode\.zcode\v2\config.json` 曾只写入 `options.baseURL`，没有写入 `apiFormat` 和 `endpoints.paths`。

结果是 ZCode 重新生成 `bots-model-cache.v2.json` 时回退成 `openai-chat-completions`，造成 ZCode 使用 Chat，而 CodexHub/Gateway 中同一个 provider 已设置为 Responses。

修复：ZCode v2 config 导出现在写入：

- `apiFormat`
- `endpoints.baseURL`
- `endpoints.paths.openai-compatible`
- 保留 `options.baseURL` 作为旧版本兼容字段

### SSE Framing

部分上游或转换链路会在输出后关闭流，但没有完整 `response.completed`。Gateway 不再把半截流合成成功 JSON：

- Chat Completions SSE 的 `[DONE]` 会保留为终止信号参与转换。
- Chat SSE 转 Responses SSE 时，即使没有文本输出也会产生 `response.completed`，避免客户端一直等待。
- Responses SSE passthrough 看到 `response.completed`、`response.failed`、`response.incomplete` 或 `error` 后，会补齐事件分隔并停止转发。
- EOF 但未看到终止事件仍视为 incomplete，不合成成功。

### Compact

compact 请求可能来自 ZCode，也可能来自其他客户端。Gateway 通过以下方式识别：

- Header：`x-query-source: compact` 或 `x-request-kind: compact`
- 或 compact summary prompt 的文本特征

已实现策略：

- compact 请求剥离 `tools` 和 `tool_choice`。
- compact 请求不注入 gateway 工具。
- compact 空响应返回 `compact_empty_response`。
- compact 空响应默认最多重试 3 次。

### Vision Proxy

vision model 子请求是 text-only proxy 请求，不应带工具。

已实现策略：

- `inject_codex_tools=False`
- adapter 转换后再次剥离 `tools` 和 `tool_choice`
- vision 子请求默认只尝试 1 次，不继承普通生成或 compact 的重试预算

## 错误时限与重试预算

- 已开始输出后，上游静默默认 `60` 秒返回 `upstream_stream_idle_timeout`。
- 未开始输出但 SSE 已建立后，上游静默默认 `90` 秒返回 `upstream_stream_idle_timeout`，event log 字段 `stream_idle_phase=pre_output`。
- compact 空响应最多重试 `3` 次，可通过 `gateway_compact_retry_max_attempts` 或 `CODEX_PROXY_COMPACT_RETRY_MAX_ATTEMPTS` 调整。
- main generation 的可见输出前重试最多 `3` 次，可通过 `gateway_main_generation_retry_max_attempts` 或 `CODEX_PROXY_MAIN_GENERATION_RETRY_MAX_ATTEMPTS` 调整。
- image proxy vision 请求保持 `1` 次，不继承全局 retry budget。

## 客户端影响面

- ZCode：受 v2 config/cache 影响最大，必须写入 `apiFormat` 和 endpoint path。
- OpenCode / Pi / OMP：同样应从 provider endpoint selection 读取协议，不应按 provider 名硬编码。
- Codex App：SSE framing 和 upstream incomplete 处理影响 Codex App 的长流式链路。已开始输出后的断流不能自动重试，只能尽快返回可见错误；未开始输出前可以按 request kind 重试。

## 不做的事

- 不在 gateway 中自动续写已开始输出后的 agent turn。原因是工具调用和文件编辑可能已经发生，gateway 无法安全重放上下文并保证副作用幂等。
- 不把某个 provider 永久固定成 Responses 或 Chat。协议必须来自 provider 的配置项。
