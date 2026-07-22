# OpenCodex 与 CodexHub 实现对比

日期：2026-07-21  
OpenCodex 固定版本：[`b9b73f711663dac5019312d03fda2d1d81c9a10c`](https://github.com/lidge-jun/opencodex/tree/b9b73f711663dac5019312d03fda2d1d81c9a10c)（`package.json` 版本 2.7.28）  
CodexHub 固定版本：`cc9df197a709fb4c7548021819ecb8fa716ed664`

## 结论

OpenCodex 相对 CodexHub 最值得重视的优势，不是 Provider 数量，而是协议内核的形状：它用一个显式 `ProviderAdapter` 接口隔离各家上游差异，把请求、消息、工具调用和流式增量归一成类型化的内部表示，再由一个公共 Bridge 生成 Responses 事件。这个结构能显著减少 Provider 特例进入主请求处理器，也使工具调用历史、取消、错误和流终止更容易做一致性测试。

CodexHub 的优势则在产品边界和下游覆盖：原生 Windows 桌面生命周期、独立 Gateway、`/v1/responses` 与 `/v1/chat/completions` 双入口、Provider-scoped 路由，以及 OpenCode、ZCode、Pi、OMP 的托管配置。这些能力不应为了照搬 OpenCodex 而退化。

建议借鉴 OpenCodex 的 Adapter、内部事件代数和 Adapter 合同测试，但采用渐进迁移；不要照搬其大型硬编码 Provider Registry、宽职责 Responses 控制器，也不要把“解析后重新序列化”称为字节透明透传。

## 实现对比

| 维度 | OpenCodex | CodexHub | 判断 |
|---|---|---|---|
| 核心运行时 | Bun/TypeScript 单语言服务，Web GUI 由同一服务提供 | Rust/Tauri 管理层 + Python Gateway + React UI | OpenCodex 的协议迭代和类型同步成本更低；CodexHub 的原生桌面和独立 Gateway 生命周期更完整 |
| 下游协议 | 以 Responses 为主，同时提供 Anthropic Messages、图片和搜索端点；没有通用入站 `/v1/chat/completions` | 同时提供 Responses、Chat Completions 和 Provider-scoped 路由 | CodexHub 更适合多种外部客户端 |
| Provider 差异 | 显式 Adapter 接口和集中解析器 | 已有协议转换与 Provider 配置，但大量协调逻辑集中在 Python Gateway | OpenCodex 的模块边界更清楚 |
| 中间表示 | 类型化 `OcxParsedRequest`、消息类型和 `AdapterEvent` 流事件 | Responses/Chat 转换能力强，但边界更多依赖大型协调模块 | OpenCodex 更容易形成通用合同测试 |
| 原生上游 | `openai-responses` Adapter 标记 passthrough，绕过通用事件 Bridge，但仍会做窄范围清洗和重新序列化 | 目标是 Official/外部客户端尽可能透明，并正加强透明字节路径 | 应借鉴“路径显式化”，保留 CodexHub 更严格的透明契约 |
| 配置管理 | 细粒度 Management API；禁用整份配置 PUT；避免回写被遮蔽的 secret | 原生 UI、Rust 命令和 Python 配置共同协作 | OpenCodex 的写边界和 secret 处理更容易审计 |
| 账户与故障转移 | OAuth、账户池、会话亲和、quota/cooldown/failover 是一等能力 | Provider、重试、恢复和遥测成熟，但账户池不是当前核心抽象 | 可借鉴接口，不宜在协议边界稳定前扩大范围 |
| 分发与测试 | npm + 内置 Bun，三平台 CI、全局安装 smoke、服务生命周期 workflow | Windows 原生安装器、Tauri updater、签名替换 smoke，以及更广的真实客户端 E2E 计划 | 两边优势不同；CodexHub 的真实客户端矩阵更贴合当前风险 |

## OpenCodex 的具体优势

### 1. Provider Adapter 是一个真正的深模块边界

[`ProviderAdapter`](https://github.com/lidge-jun/opencodex/blob/b9b73f711663dac5019312d03fda2d1d81c9a10c/src/adapters/base.ts) 明确定义了 `buildRequest`、可选的 `fetchResponse`、`parseStream`、`parseResponse`、`runTurn` 和错误脱敏入口；[`resolveAdapter`](https://github.com/lidge-jun/opencodex/blob/b9b73f711663dac5019312d03fda2d1d81c9a10c/src/server/adapter-resolve.ts) 再把 Provider 类型集中映射到实现。这样新增 Provider 通常是在一个 Adapter 内完成，而不是把条件分支散入路由、请求构建、SSE 解析和错误处理各层。

相比之下，固定 CodexHub 基线中的 `src-python/codex_proxy.py` 约 1.63 万行，`src-tauri/src/gateway.rs` 约 0.94 万行。它们承担的产品职责更多，但也说明路由、协议、生命周期和兼容逻辑的变化更容易跨模块、跨语言联动。这里的差距是“边界深度”，不是单纯文件行数。

### 2. 工具调用和流式输出先进入统一事件代数

OpenCodex 在 [`src/types.ts`](https://github.com/lidge-jun/opencodex/blob/b9b73f711663dac5019312d03fda2d1d81c9a10c/src/types.ts) 定义了统一消息、工具结果、usage 和 `AdapterEvent`：文本、思考、工具调用 start/delta/end、搜索、done/error 都是显式事件。随后 [`bridgeToResponsesSSE`](https://github.com/lidge-jun/opencodex/blob/b9b73f711663dac5019312d03fda2d1d81c9a10c/src/bridge.ts) 统一负责生成 Responses SSE。

这对工具调用尤其有价值：Provider Adapter 只需正确产生内部工具事件，不必每个 Provider 都自行拼装 Codex Responses 的完整生命周期。它减少了 N 个入站协议 × M 个上游协议的组合爆炸，也更容易验证“tool call ID、参数增量、tool result 和终止事件”是否闭合。

### 3. 原生协议路径与语义转换路径是显式选择

[`openai-responses` Adapter](https://github.com/lidge-jun/opencodex/blob/b9b73f711663dac5019312d03fda2d1d81c9a10c/src/adapters/openai-responses.ts) 使用 `passthrough: true`，其测试明确验证原生 Responses 路径绕过普通解析 Bridge，并只执行列明的兼容清洗（见 [`openai-responses-passthrough.test.ts`](https://github.com/lidge-jun/opencodex/blob/b9b73f711663dac5019312d03fda2d1d81c9a10c/tests/openai-responses-passthrough.test.ts)）。

这个“路由计划先决定是否进入语义层”的设计优于在处理途中由零散条件接管工具调用。不过它仍会解析和重新序列化请求，因此是协议语义上的原生路径，不是严格的 byte-for-byte 透明代理。

### 4. Provider 能力、路由和账户模式有类型化的统一入口

[`router.ts`](https://github.com/lidge-jun/opencodex/blob/b9b73f711663dac5019312d03fda2d1d81c9a10c/src/router.ts) 返回包含 Provider、模型、配置和账户模式的路由结果；[`providers/registry.ts`](https://github.com/lidge-jun/opencodex/blob/b9b73f711663dac5019312d03fda2d1d81c9a10c/src/providers/registry.ts) 统一描述认证类型、Adapter、URL、模型能力和 effort 映射。OAuth、账户池、会话亲和与 cooldown 因而能围绕同一 Provider 身份工作，而不是成为协议处理器内的临时特例。

优势是 schema 和优先级明确；代价是 Registry 已经很大且硬编码，继续扩展会形成新的中心化瓶颈。

### 5. 管理面写操作和安全边界更细

[`management-api.ts`](https://github.com/lidge-jun/opencodex/blob/b9b73f711663dac5019312d03fda2d1d81c9a10c/src/server/management-api.ts) 以 Provider、模型、账户、OAuth、usage 等细粒度端点修改状态，并明确禁用整份配置 PUT；这避免 UI 把被遮蔽的 secret 当普通字段回写。Provider URL 写入时还会经过 [`destination-policy.ts`](https://github.com/lidge-jun/opencodex/blob/b9b73f711663dac5019312d03fda2d1d81c9a10c/src/lib/destination-policy.ts) 的字面地址和 DNS 解析检查，以阻止 metadata、loopback、private/link-local 目标，除非显式允许私网。

这比在 Rust、Python、前端分别维护一份可写配置镜像更容易审计和演进。

### 6. 单语言核心降低了协议开发的摩擦

OpenCodex 的服务、Adapter、路由、CLI 和管理 API 都在 TypeScript/Bun 中，GUI 也是 TypeScript。其 [`package.json`](https://github.com/lidge-jun/opencodex/blob/b9b73f711663dac5019312d03fda2d1d81c9a10c/package.json) 依赖较少，并把 Bun 作为 npm 包依赖携带；[`ci.yml`](https://github.com/lidge-jun/opencodex/blob/b9b73f711663dac5019312d03fda2d1d81c9a10c/.github/workflows/ci.yml) 在 Windows、macOS、Linux 执行 typecheck、测试、隐私扫描、GUI build 和全局安装 smoke。

这使协议类型可以贯穿服务端与 UI，并降低本地安装的运行时前置条件。它不代表系统天然简单：[`server/responses.ts`](https://github.com/lidge-jun/opencodex/blob/b9b73f711663dac5019312d03fda2d1d81c9a10c/src/server/responses.ts) 仍是宽职责控制器，Provider Registry 和 Management API 也已经很大。

## CodexHub 应该借鉴什么

### P0：先建立三条可执行的协议边界

1. **引入窄的 `ProviderAdapter` 合同。** 最少包含：构建上游请求、打开/执行请求、解析流、解析非流响应、分类并脱敏错误；统一接收 cancellation、deadline 和 route plan。先迁移第三方语义转换路径，不做大爆炸重写。
2. **引入最小 `GatewayEvent` 事件代数。** 覆盖 text、reasoning、tool start/delta/end、usage、done/error；由公共 Responses/Chat emitter 输出。GLM-5.2 工具调用问题应落在 Provider Adapter → `GatewayEvent` 的合同测试，而不是为 OpenCode 在主处理器追加专用分支。
3. **把透明性变成路径类型和测试不变量。** 路由计划必须在进入处理器前选择 `raw/native` 或 `semantic`。`raw/native` 默认不得进入统一 IR；任何兼容修改都必须具名、可观测并有字节保持/差异测试。这样才能兑现“外部 client 尽可能透明透传”。

### P1：围绕这些边界拆分现有大模块

4. **按深模块拆 `codex_proxy.py`。** 建议边界为 inbound parser、route decision、provider adapters、transport/relay、stream emitters、telemetry；不要只做机械分文件。
5. **建立类型化、数据驱动的 Provider capability registry。** 借鉴 OpenCodex 的 schema 与“内置默认值 + 用户覆盖”优先级，但不要复制单个巨型硬编码表。可与 CodexHub 既有 metadata 数据化工作合并。
6. **记录统一 `RoutePlan`。** 至少包含 client、inbound protocol、provider、upstream protocol、adapter、是否透明、启用的 semantic transforms。E2E 与 telemetry 使用同一记录，避免从日志猜测请求实际走了什么端点。
7. **为所有 Adapter 建一套 conformance tests。** 通用断言应覆盖取消、超时、输出截断、错误脱敏、tool call ID 闭合、SSE 终止、非流结果和非法上游响应；每个 Provider 只补 fixture 和差异断言。
8. **管理面改用细粒度命令。** UI 不应读取 masked secret 后再整份回写；Provider URL 写入增加 DNS-resolved destination policy，并保留显式 `allow_private_network`。

### P2：有需求时再吸收

9. **统一 OAuth/账户池接口。** 可以预留 credential source、account selection、quota/cooldown 接口，但不要在 SSE 与工具调用合同未稳定前扩大实现。
10. **减少跨语言模型漂移。** 不必把 CodexHub 改写为 TypeScript；更合适的是让路由与 Provider schema 只有一个事实来源，再生成或校验 Rust/Python/TypeScript 表示。

## 不建议照搬的部分

- **不复制大型硬编码 Provider Registry。** 它短期新增预设很快，长期会成为高冲突、高认知负担的中心文件。
- **不以单语言重写替代模块设计。** OpenCodex 自身的 Responses 控制器已经很宽，说明语言统一不能自动解决职责膨胀。
- **不把重新序列化称为透明透传。** CodexHub 应继续追求更严格的外部客户端透明路径，并将必要兼容变换显式披露。
- **不放弃原生桌面与独立 Gateway。** OpenCodex 的 Web Dashboard 和 npm 分发适合 CLI-first 产品；CodexHub 的 tray、updater、安装替换、Gateway owner 和外部客户端配置是差异化能力。
- **不为了 Provider 数量提前引入账户池、sidecar 和额外协议。** 当前优先级仍应是 SSE 边界、真实客户端工具调用和路由可证明性。

## 证据边界

以上 OpenCodex 事实来自固定提交的 README、源码、测试和 GitHub Actions，没有使用第三方文章。对“维护成本更低”“更容易测试”等表述属于基于代码结构的工程推断，而非上游作者声明。CodexHub 的对比依据为本仓库 `README.md`、`DESIGN.md`、`CONTEXT.md` 和当前源码布局；本次没有运行两项目的性能基准，因此不对吞吐、延迟或资源占用作结论。
