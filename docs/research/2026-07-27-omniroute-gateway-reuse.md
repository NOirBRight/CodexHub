# OmniRoute Gateway 复用评估

- 日期：2026-07-27
- CodexHub 基线：[`1758b830130ac3bf4a0d55f08b69fce342f4a969`](https://github.com/NOirBRight/CodexHub/tree/1758b830130ac3bf4a0d55f08b69fce342f4a969)
- OmniRoute 审阅基线：[`ed7db3ee5f89a144b2d931d8605534522f83de30`](https://github.com/diegosouzapw/OmniRoute/tree/ed7db3ee5f89a144b2d931d8605534522f83de30)

## 结论

可以参考，而且值得参考；但不适合把 OmniRoute 整体作为 CodexHub Gateway 的运行时依赖，也不建议用它替换现有 Codex 路径。

最合适的定位是：

> 将 OmniRoute 作为 Claude Code / Codex 兼容行为的参考实现和回归用例来源，选择性移植协议边界、测试场景与客户端配置体验；生产实现继续落在 CodexHub 现有 Python Gateway、Rust 生命周期和严格协议转换框架内。

这并不是“从头再造一个 Gateway”。CodexHub 已经具备 Responses、Chat Completions、认证、路由、SSE、取消、诊断和 Codex 官方认证路径，也已有经过测试的 Anthropic Messages 中间表示原型。真正缺少的是把该原型按既定语义约束接入生产 `/v1/messages`，再补齐 Claude Code 的 launcher、模型发现和兼容矩阵。

## OmniRoute 确实提供了什么

OmniRoute 当前同时暴露：

- [`/v1/messages`](https://github.com/diegosouzapw/OmniRoute/blob/ed7db3ee5f89a144b2d931d8605534522f83de30/src/app/api/v1/messages/route.ts)，用于 Claude/Anthropic Messages 请求；
- [`/v1/messages/count_tokens`](https://github.com/diegosouzapw/OmniRoute/blob/ed7db3ee5f89a144b2d931d8605534522f83de30/src/app/api/v1/messages/count_tokens/route.ts)；
- [`/v1/responses`](https://github.com/diegosouzapw/OmniRoute/blob/ed7db3ee5f89a144b2d931d8605534522f83de30/src/app/api/v1/responses/route.ts)，用于 Codex/OpenAI Responses 请求；
- Claude Code 的 [`ANTHROPIC_BASE_URL` 配置、隔离 profile 与 launcher`](https://github.com/diegosouzapw/OmniRoute/blob/ed7db3ee5f89a144b2d931d8605534522f83de30/docs/guides/CLAUDE-CODE-CONFIGURATION.md)；
- Codex 的 [`wire_api = "responses"` 配置与 launcher`](https://github.com/diegosouzapw/OmniRoute/blob/ed7db3ee5f89a144b2d931d8605534522f83de30/docs/guides/CODEX-CLI-CONFIGURATION.md)。

它的协议转换以 OpenAI Chat 形态作为枢纽：格式注册在
[`formats.ts`](https://github.com/diegosouzapw/OmniRoute/blob/ed7db3ee5f89a144b2d931d8605534522f83de30/open-sse/translator/formats.ts)
和
[`registry.ts`](https://github.com/diegosouzapw/OmniRoute/blob/ed7db3ee5f89a144b2d931d8605534522f83de30/open-sse/translator/registry.ts)，
无法直接转换时由
[`translator/index.ts`](https://github.com/diegosouzapw/OmniRoute/blob/ed7db3ee5f89a144b2d931d8605534522f83de30/open-sse/translator/index.ts)
执行“源格式 → OpenAI → 目标格式”。

这套实现有较大的真实客户端覆盖面和丰富测试资产，特别适合作为以下工作的输入：

- Claude Code 请求/响应、工具调用和流式事件的边界案例清单；
- `event: ping` 形式的早期保活；
- 上游 4xx 错误透传；
- Claude discovery alias 与 `/v1/models` 行为；
- Claude/Codex profile、配置片段和 launcher 体验；
- Responses、Messages、Chat 三种形态之间的差异测试。

## 官方兼容边界

Anthropic 当前的
[`Gateway protocol reference`](https://code.claude.com/docs/en/llm-gateway-protocol)
进一步确认了 CodexHub 应守住的协议边界：

- `ANTHROPIC_BASE_URL` 对应 `/v1/messages`；`/v1/messages/count_tokens` 是可选端点；
- 推理响应必须实时流式传递；
- `anthropic-beta`、`anthropic-version` 以及未来新增的相关字段应按开放集合处理；
- 上游错误正文应保持原样，否则会破坏 Claude Code 自身的重试与能力降级；
- `/v1/models` 发现可选，并有认证、3 秒超时、ID 前缀和缓存规则。

官方文档同时明确表示：支持的 API 形状可以连接第三方 Gateway，但 Anthropic
[`不支持通过 Gateway 把 Claude Code 路由到非 Claude 模型`](https://code.claude.com/docs/en/llm-gateway)。
因此，OmniRoute 所称“支持 Claude Code”可以证明客户端连通与工程兼容，不能被理解为 Anthropic 对任意异构模型语义等价性的背书。CodexHub 仍需针对每类上游做自己的能力声明和 live probe。

## 为什么不能直接拿来用

### 1. 它不是一个可独立引入的协议库

`@omniroute/open-sse` 是仓库内的 TypeScript path alias，而不是一个独立发布、具有稳定 API 的包。转换入口会导入 OmniRoute 自身的 provider、缓存、reasoning、配置等 `@/` 模块。要直接使用，实际上需要拆分并长期维护一个 fork。

CodexHub 的生产 Gateway 是 Python，生命周期、配置和客户端适配由 Tauri/Rust 管理。引入 Next.js/Node 服务会形成第二套进程、认证、遥测、故障和打包边界，集成成本高于选择性移植。

### 2. 两个项目的语义策略不同

OmniRoute 的策略偏向“尽量让异构模型继续运行”。例如其
[`claude-to-openai.ts`](https://github.com/diegosouzapw/OmniRoute/blob/ed7db3ee5f89a144b2d931d8605534522f83de30/open-sse/translator/request/claude-to-openai.ts)
会：

- 重排工具结果，使其靠近工具调用；
- 对缺失的工具响应插入占位内容；
- 只构造已识别字段组成的新请求；
- 将 Anthropic thinking/effort 近似映射到 OpenAI reasoning effort。

其
[`openai-to-claude.ts`](https://github.com/diegosouzapw/OmniRoute/blob/ed7db3ee5f89a144b2d931d8605534522f83de30/open-sse/translator/response/openai-to-claude.ts)
还包含生成 ID、默认 usage 和兜底 finish reason 等兼容行为。

这些做法对通用路由产品有现实价值，但与 CodexHub 已接受的
[`Claude Messages 中间表示 ADR`](../adr/0001-claude-messages-intermediate-representation.md)
冲突。CodexHub 要求：

- 不静默丢弃开放字段；
- 不偷偷修复、重排工具历史；
- 无等价映射时返回明确的 non-forwardable/unsupported 结果；
- 不伪造 thinking 签名、usage 或 token 数；
- 保留内容块顺序和工具 ID。

所以相关代码可以用来发现边界，不能逐行照搬其默认策略。

### 3. `count_tokens` 和 beta header 不满足当前严格边界

OmniRoute 的
[`count_tokens` 路由](https://github.com/diegosouzapw/OmniRoute/blob/ed7db3ee5f89a144b2d931d8605534522f83de30/src/app/api/v1/messages/count_tokens/route.ts)
在上游不支持时会本地估算。CodexHub 当前 ADR 明确不应把估算值伪装成 Anthropic token count；由于官方协议允许省略该端点，第一阶段更安全的选择是不提供它，让客户端自行估算，或把估算能力做成显式、可识别的产品功能。

OmniRoute 的
[`anthropicHeaders.ts`](https://github.com/diegosouzapw/OmniRoute/blob/ed7db3ee5f89a144b2d931d8605534522f83de30/open-sse/config/anthropicHeaders.ts)
会通过 allowlist 筛选客户端 beta。Claude Code 的 beta 与版本头属于开放集合，CodexHub 应先完整分类、保留或明确拒绝，不能因本地列表未更新而静默删除。

### 4. 不应替换现有 Codex 路径

OmniRoute 的 Responses 兼容价值是真实的，但其主路径会把 Responses 转入 Chat Core，再转换回 Responses。CodexHub 对官方 Codex/Responses 的既有原则是：能透明透传时不做有损中转。

CodexHub 已有完整的 `/v1/responses`、`/v1/chat/completions`、官方 OAuth、provider 路由、SSE、取消和诊断能力；用 OmniRoute 替换这一层会扩大回归面，并削弱 Responses 透明性。OmniRoute 的 Codex 支持应当作为兼容测试参照，而不是迁移理由。

## 能力对照

| 领域 | CodexHub 现状 | OmniRoute 参考价值 | 建议 |
|---|---|---|---|
| Responses / Codex | 已生产化，含官方认证和透明路径 | 有真实下游配置及大量兼容案例 | 保留现状；只吸收测试案例 |
| Chat Completions | 已生产化且严格转换 | 作为多 provider 行为样本 | 不替换 |
| Anthropic Messages | 有独立 IR 原型和 43 个聚焦测试，尚未开放生产路由 | 有完整路由、stream/tool/error 实战案例 | 在本地架构内实现，移植测试而非运行时 |
| SSE 保活 | 已有通用 SSE comment keepalive | Claude 使用命名 `event: ping` | 为 Messages 增加协议形状正确的 ping |
| 工具调用 | 本地策略保持顺序并拒绝不安全历史 | 覆盖延迟 tool name、参数分片等案例 | 移植边界用例；不移植自动重排/补结果 |
| Token 统计 | 明确未授权生产端点 | 有 provider + 本地估算 fallback | 第一阶段显式 unsupported |
| Headers | 本地原型按开放集合分类 | 有 Claude OAuth/指纹兼容经验 | 借鉴测试；不采用静默 allowlist |
| 模型发现 | 尚待 Claude 外部客户端阶段 | alias、缓存、超时经验成熟 | 在 Messages 稳定后实现 |
| Profile / launcher | Tauri 客户端适配体系已存在 | Claude/Codex 配置 UX 很有参考价值 | 用 Rust/Tauri 原生方式复刻体验 |

## 推荐实施边界

### 可以直接借鉴或移植的内容

1. 将 OmniRoute 固定到本报告 SHA，提取协议 fixtures 和测试场景，并在测试注释中保留来源链接。
2. 为 `/v1/messages` 使用 Anthropic 形状的 `event: ping`，复用 CodexHub 现有取消、背压和事件边界提交机制。
3. 移植流式工具调用的边界测试：tool name 晚到、arguments 多分片、多个 tool call、stop reason、上游 4xx。
4. 参考其 discovery alias、短超时与缓存策略，但保持 alias 解析为纯函数、默认不开启。
5. 参考其 Claude profile/launcher 体验：隔离配置目录、运行时注入 token、不把密钥写入配置文件。

### 应明确拒绝或置于显式兼容开关之后的内容

1. 自动重排工具消息；
2. 插入“缺少的工具结果”；
3. 丢弃未知 top-level、content block、beta 或版本字段；
4. 伪造 Messages ID、thinking/signature、usage 或 token count；
5. 把未知 finish reason 默认为 `end_turn`；
6. 为了展示“活跃”而发出用户可见的虚构 thinking 内容。

## 建议落地顺序

1. **完成现有验证门槛**：以当前 Claude Code 版本重新执行一个官方 Responses provider 和一个 Chat provider 的 live probe，更新 [#74](https://github.com/NOirBRight/CodexHub/issues/74) 的结论。
2. **生产化本地 IR**：把 `anthropic_messages_spike.py` 的中间表示与 fail-closed 决策迁入专用生产模块，接入既有 transport、认证、路由、SSE、取消和诊断。
3. **增加 `/v1/messages`**：按 [#75](https://github.com/NOirBRight/CodexHub/issues/75) 补齐非流式、流式、工具、错误、usage、limits 和 headers；第一阶段不伪装支持 `count_tokens`。
4. **吸收 OmniRoute 测试语料**：按固定 SHA 建立 source-derived fixture 清单，逐项声明“等价适配 / 明确拒绝 / 原样透传”。
5. **再做客户端体验**：依次推进 launcher/profile [#76](https://github.com/NOirBRight/CodexHub/issues/76)、aliases/discovery [#77](https://github.com/NOirBRight/CodexHub/issues/77) 和兼容矩阵 [#78](https://github.com/NOirBRight/CodexHub/issues/78)。

若希望快速验证产品体验，可以临时把 OmniRoute 当作外部 sidecar，让它连接 CodexHub 的既有上游能力，验证 Claude Code 的 profile、模型选择和流式交互。但这只适合作为短期实验，不应成为正式打包架构。

## 许可与维护风险

OmniRoute 根仓库采用
[`MIT License`](https://github.com/diegosouzapw/OmniRoute/blob/ed7db3ee5f89a144b2d931d8605534522f83de30/LICENSE)。
复制代码或实质性片段时应保留许可证与版权声明；部分文件还标注了其他项目的移植来源，需要继续追踪原始 provenance。只移植行为测试与独立小算法，通常比 vendoring 整个 translator 更容易审计和维护。

该项目活跃且覆盖面大，但默认分支和 Claude/Codex 兼容代码变化较快。任何借鉴都应固定 commit、记录来源、由 CodexHub 自己的严格测试锁定行为，而不是追随其默认分支。

## 最终决策

**采用“选择性参考与测试移植”，不采用“运行时依赖或整体替换”。**

这条路线能复用 OmniRoute 已经踩过的 Claude Code/Codex 兼容坑，同时保住 CodexHub 的核心优势：单一 Gateway 生命周期、官方 Codex 透明路径、统一认证与诊断，以及对协议语义的显式、可审计处理。
