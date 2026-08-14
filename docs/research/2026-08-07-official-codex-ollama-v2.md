# 官方 Codex 模型声明与 Ollama Cloud Responses/流式边界

日期：2026-08-07  
用途：补充 [Issue #324](https://github.com/NOirBRight/CodexHub/issues/324) 的官方一手证据。本文只记录研究结果，不改变产品代码或 GitHub Issue。

## 结论摘要

- 在 `openai/codex` 当前 `main`（提交 [`85e0661c`](https://github.com/openai/codex/commit/85e0661c3baacc62db7b006d2b8085b006d0795e)）的模型目录中，**明确写 `multi_agent_version = "v2"` 的只有 `gpt-5.6-sol` 和 `gpt-5.6-terra`**；`gpt-5.6-luna` 明确为 `v1`，其余模型为 `null`。
- `null` 不是显式 `Disabled`。官方配置解析顺序是“配置/feature override > 模型目录值 > feature fallback”；启用 `multi_agent_v2` 时，目录为 `null` 的模型仍可能得到 V2，是否可用必须按实际 runtime/route 验证。
- Codex 源码有明确的 V2 声明：`MultiAgentVersion::{Disabled,V1,V2}`，且 `multi_agent_v2` feature 标为 Stable、默认关闭。公开 Responses API 文档则只称 Multi-agent 是 GPT-5.6 全系 beta，并使用 `responses_multi_agent=v1` beta 标头；这不是 Codex 内部 `multi_agent_version = v2` 的同一层协议。
- Ollama 官方 OpenAI 兼容文档确认 `/v1/responses` 支持**非状态化** Responses、流式、function tools 和 reasoning summaries，但不支持 `previous_response_id`/`conversation`。文档没有声明 Codex Collaboration V2 的 hosted actions 或 `agent_message`。
- Ollama 官方源码的 Responses writer 会输出 SSE 并在每个事件后 flush；Cloud proxy 当前使用 `http.DefaultClient.Do`，并明确留下 TODO：连接/TLS/TTFB 应有阶段性超时，但开始流式后不应施加短的总超时。因此“连接慢/首 token 慢/流间隔过长”不能直接判为“不支持”。

## 1. `openai/codex` 当前模型目录

固定来源：[codex-rs/models-manager/models.json @ `85e0661c`](https://github.com/openai/codex/blob/85e0661c3baacc62db7b006d2b8085b006d0795e/codex-rs/models-manager/models.json)。该快照共有 8 个模型：

| 模型 | `multi_agent_version` | `visibility` | 解释 |
| --- | --- | --- | --- |
| `gpt-5.6-sol` | `v2` | `list` | 明确声明 V2、可列出（L4/L21/L63） |
| `gpt-5.6-terra` | `v2` | `list` | 明确声明 V2、可列出（L119/L136/L178） |
| `gpt-5.6-luna` | `v1` | `list` | 明确声明 V1、可列出（L232/L249/L287） |
| `gpt-5.5` | `null` | `list` | 无模型级版本声明（L341/L358/L392） |
| `gpt-5.4` | `null` | `hide` | 无模型级版本声明、隐藏（L448/L465/L499） |
| `gpt-5.4-mini` | `null` | `hide` | 无模型级版本声明、隐藏（L553/L570/L604） |
| `gpt-5.2` | `null` | `list` | 无模型级版本声明、可列出（L653/L670/L704） |
| `codex-auto-review` | `null` | `hide` | 无模型级版本声明、隐藏（L750/L767/L801） |

`visibility` 是 picker/API 可见性，不是能力准入。官方 `ModelPreset` 注释把 `multi_agent_version` 定义为“该模型开始新 thread 时选择的 Multi-agent backend”；`ModelVisibility` 枚举为 `list`、`hide`、`none`：[openai_models.rs](https://github.com/openai/codex/blob/85e0661c3baacc62db7b006d2b8085b006d0795e/codex-rs/protocol/src/openai_models.rs#L237-L263)。

## 2. V2 声明与运行时语义

一手源码证据：

- `MultiAgentVersion` 枚举明确包含 `Disabled`、`V1`、`V2`：[protocol.rs](https://github.com/openai/codex/blob/85e0661c3baacc62db7b006d2b8085b006d0795e/codex-rs/protocol/src/protocol.rs#L3047-L3054)。
- V1 使用 `multi_agent_v1` namespace，V2 使用直接 function tools，并有不同的 task/path、fork 和 wait 参数：[multi_agents_spec.rs](https://github.com/openai/codex/blob/85e0661c3baacc62db7b006d2b8085b006d0795e/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L14-L15)（V1）及 [#L67-L113](https://github.com/openai/codex/blob/85e0661c3baacc62db7b006d2b8085b006d0795e/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L67-L113)（spawn V1/V2）。
- `Feature::MultiAgentV2` 的注释是“Enable task-path-based multi-agent routing”，feature key 为 `multi_agent_v2`，stage 为 Stable，但 `default_enabled = false`：[features/lib.rs](https://github.com/openai/codex/blob/85e0661c3baacc62db7b006d2b8085b006d0795e/codex-rs/features/src/lib.rs#L160-L164) 与 [#L1115-L1125](https://github.com/openai/codex/blob/85e0661c3baacc62db7b006d2b8085b006d0795e/codex-rs/features/src/lib.rs#L1115-L1125)。
- 运行时选择顺序由 `multi_agent_version_for_model` 固定为 override → model manifest → feature fallback：[core/config/mod.rs](https://github.com/openai/codex/blob/85e0661c3baacc62db7b006d2b8085b006d0795e/codex-rs/core/src/config/mod.rs#L1569-L1596)。所以目录 `null` 不能被解释成永久“不支持 V2”；它只表示没有模型级声明。

无 override 时，feature fallback 会在 `multi_agent` 开启时选择 V1，在 agents 被禁用时选择 Disabled；`multi_agent_v2` override 才会把该层选择为 V2。因而“目录 null”既不是 V1/V2 的硬拒绝，也不是完整生命周期资格。

公开 API 层的边界不同：官方 [Responses Multi-agent 文档](https://developers.openai.com/api/docs/guides/responses-multi-agent.md) 声明“all GPT-5.6 models”可用 beta（L9），HTTP/WS 需 `responses_multi_agent=v1` 标头（L39-L45），并说明 hosted collaboration actions、`multi_agent_call`、`multi_agent_call_output` 与 `agent_message`（L146-L156、L758-L772）。文档没有公开按模型的 `multi_agent_version = v2` 矩阵；因此 API beta v1 标头、Codex CLI V1/V2 wire tool surface、以及完整生命周期资格不能混为一谈。

## 3. Ollama Cloud Responses API 与流式行为

### 官方文档契约

- [OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility.md#v1responses)（Ollama v0.13.3 起）明确支持 `/v1/responses` 的非状态化 flavor；`previous_response_id` 和 `conversation` 不支持；支持 streaming、function tools、reasoning summaries，支持字段包括 `model`、`input`、`instructions`、`tools`、`stream`、`temperature`、`top_p`、`max_output_tokens`。
- [Cloud API access](https://docs.ollama.com/cloud.md#cloud-api-access) 说明云端直连以 `https://ollama.com` 为 host、使用 API key；[Codex integration](https://docs.ollama.com/integrations/codex.md#profile-based-setup) 的 Codex profile 使用 `wire_api = "responses"`。本地 CodexHub 的 `https://ollama.com/v1` provider 配置与该 API 家族一致；Cloud 页面没有对每个 OpenAI-compatible 字段（特别是 `agent_message`）作额外保证。
- [Errors](https://docs.ollama.com/api/errors.md) 将 `502 Bad Gateway` 定义为“cloud model cannot be reached”；如果错误发生在流中，则以流内错误对象返回，HTTP status 不再改变（L14-L16、L28-L34）。这属于可用性/传输错误，不是协议不支持。

官方实现证据（`ollama/ollama` 当前 main 提交 [`144893850f`](https://github.com/ollama/ollama/commit/144893850fa778c8c81ff931f26614d62e6689c1)）：

- Responses middleware 在 `stream=true` 时设置 `text/event-stream`、`Cache-Control: no-cache`、`Connection: keep-alive`，每个 SSE event 写入后调用 `Flush()`：[middleware/openai.go](https://github.com/ollama/ollama/blob/144893850fa778c8c81ff931f26614d62e6689c1/middleware/openai.go#L495-L506) 与 [#L544-L605](https://github.com/ollama/ollama/blob/144893850fa778c8c81ff931f26614d62e6689c1/middleware/openai.go#L544-L605)。
- Cloud proxy 当前调用 `http.DefaultClient.Do`；源码紧邻处的 TODO 明确写出：connect/TLS/TTFB 应设置有界超时，但开始 streaming 后不应施加短的 total timeout：[server/cloud_proxy.go](https://github.com/ollama/ollama/blob/144893850fa778c8c81ff931f26614d62e6689c1/server/cloud_proxy.go#L216-L223)。响应复制循环按读取到的块写出并 flush，没有产品级总时限：[同文件](https://github.com/ollama/ollama/blob/144893850fa778c8c81ff931f26614d62e6689c1/server/cloud_proxy.go#L468-L491)。这描述的是当前实现，不是对所有外部 CDN/负载均衡器的 SLA 承诺。
- Ollama 的 Responses 解析器只识别 `message`、`function_call`、`function_call_output`、`reasoning` 输入项；未知类型返回 `unknown input item type`：[openai/responses.go](https://github.com/ollama/ollama/blob/144893850fa778c8c81ff931f26614d62e6689c1/openai/responses.go#L241-L288)。因此 `agent_message` 等 Collaboration V2 item 不在 Ollama OpenAI 兼容层的已声明输入契约内；云端模型是否接受它必须按“provider + model + endpoint”实测，不能由普通文本 Responses 成功推断。

客户端补充：官方 [`ollama-python` BaseClient](https://github.com/ollama/ollama-python/blob/25b93290d8cd07b0d00732641f812ee34fd4c989/ollama/_client.py#L79-L116) 将 `timeout` 默认设为 `None` 并传给 `httpx`；流式迭代逐行读取并在流内错误对象出现时抛出错误（[#L174-L199](https://github.com/ollama/ollama-python/blob/25b93290d8cd07b0d00732641f812ee34fd4c989/ollama/_client.py#L174-L199)）。这意味着调用方应自行设置阶段性预算，不能把某个 SDK 默认值当成 Ollama Cloud 的“不支持”判定。

## 4. 与 Issue #324 的对应关系

Issue #324 已记录：Luna 手动切到 V2 后 spawn 成功；Ollama Cloud GLM/DeepSeek 路由拒绝 V2 `agent_message`；Kimi 路由在 Responses 字段转换失败。上述官方契约支持以下解释：

1. Luna 的 spawn 成功只能证明该次 Codex runtime 选择了 V2 并完成了初始动作，不等于 provider route 完成 `agent_message`、follow-up、wait/result、stream/history 等完整生命周期。
2. Ollama 普通 `/v1/responses` 成功只能证明非状态化文本/工具/流式子集可用；`agent_message` 不在已声明输入字段和当前解析器类型中，应标为“未资格化/语义不兼容”，而不是由慢连接推断。
3. Kimi 的字段转换失败同样是 schema/协议适配问题；它与 Cloud 502、connect timeout、TTFB 慢、流中断是不同故障类。

## 5. 可验证测试与判定边界

对每个精确的 provider + model + endpoint 保存原始请求/响应摘要（脱敏）、客户端/代理版本和单调时钟时间戳，至少分四层执行：

1. **基线可达性**：列模型或最小 `stream=false` Responses，记录 DNS、TCP connect、TLS、TTFB、HTTP status、响应 schema。`401/403` 是凭据/权限问题，`404/405` 是路径/方法问题，`502` 是云端不可达；这些都不能直接标为“V2 不支持”。
2. **流式契约**：同一模型 `stream=true`，记录首个 SSE event、每个 event 的 inter-event gap、最终 `response.completed`（或 Ollama 对应的流内错误）。测试客户端应分开设置 connect、TLS、TTFB、read-idle 和总预算；不要使用短的固定总超时覆盖长生成。
3. **V2 item/lifecycle**：按 Codex 实际顺序测试 spawn → task identity/path → message/`agent_message` → follow-up → wait/result → list/interrupt（适用时）→ replay/restart。每步保存 exact status、error type、SSE 生命周期和历史可逆性；初始 spawn 通过不应提升整条 route 为 supported。
4. **重复与对照**：同一 route 用短文本/长推理、`stream=false/true`、空闲间隔可控的 fixture 重复多次；把 Ollama Cloud 与本地 Ollama/另一 provider 分开，避免把上游拥塞或代理问题归因于模型 schema。

如果独立 raw HTTP 客户端在足够长的阶段预算下收到了合法 2xx/SSE，而 Gateway/UI 报 timeout，优先判为 Gateway/client deadline、读取 idle budget 或代理缓冲问题：对比网卡收到首字节/每个 SSE event 的时间与 Gateway 记录的 deadline、取消和最后一条上游事件。只有在上游明确返回不支持字段的 4xx，或完整流中确定缺失 required V2 item 时，才升级为协议/生命周期不支持。

建议的 fail-closed 分类：

| 观察 | 分类 | 是否支持结论 |
| --- | --- | --- |
| connect/TLS/TTFB 超时、尚未收到合法 SSE | transport/availability unknown | 暂不判“不支持”，重试或换网络后复测 |
| 首 event 很慢，但随后合法事件并完成 | slow provider/model | 不是“不支持” |
| 流中间歇超过 read-idle 预算后断开 | idle/transport failure | 不是语义不支持；调整 idle budget/调查代理 |
| 明确 4xx `unknown input item type: agent_message` 或字段不支持 | semantic/schema unsupported | 该精确 route 对该能力 fail-closed |
| 200/流式完成但缺少 required V2 item、调用 ID、顺序或可 replay 历史 | lifecycle unqualified | 不能宣称完整 V2 |
| 502 “cloud model cannot be reached” 或流内错误对象 | upstream availability | 不是“不支持” |

最后，CodexHub 应继续区分 `manifest declaration`、`wire compatibility` 和 `complete lifecycle qualification`；不要把官方 Sol/Terra 的 V2 目录值、Luna 的一次 spawn 成功、或 Ollama 的普通 Responses 流式成功互相替代。
