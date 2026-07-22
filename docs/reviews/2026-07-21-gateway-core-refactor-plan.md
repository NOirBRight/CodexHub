# CodexHub Gateway 核心重构终版规划

日期：2026-07-21  
代码基线：`cc9df197a709fb4c7548021819ecb8fa716ed664`  
Issue/PR 快照：GitHub `NOirBRight/CodexHub`，Open PR #192，开放 Milestone 0.1.7–0.1.10

## 决策摘要

CodexHub 不应照搬 OpenCodex 的单语言实现，也不应把所有请求强制送入统一语义 IR。最终架构采用“编译 RoutePlan + 四种执行模式 + 一个请求生命周期 Module”：

1. `RoutePlanner` 在任何请求改写或 Provider I/O 前生成完整、不可变的 `CompiledRoutePlan`。
2. `GatewayCore` 以一次完整 Gateway 请求交换为外部 Seam，统一处理路由、Provider I/O、SSE、重试、取消、终止、错误和 telemetry。
3. 同格式、无需 body 改写的流量走 `OPAQUE_SAME_FORMAT`，请求和响应 body/SSE 均保持原始字节。
4. 同格式但确需兼容修正的流量走 `NATIVE_PATCHED`，只执行 RoutePlan 列出的具名 Mutation。
5. Responses ↔ Chat Completions 等跨协议流量走 `TRANSLATED`，只有这里使用语义表示和 `GatewayEvent`。
6. Codex 工具注入、namespace、apply_patch、subagent repair/scheduling 走显式 `CODEX_COMPATIBILITY`；它不能再由“非 Official Provider”或普通 `function_call` 类型自动触发。

这不是一次性重写。#18、#19、#20 先形成可复用的 I/O Module，#114 固定真实故障边界，#61 再冻结完整 RoutePlan；之后按路由族逐项切换并删除旧实现。

## 一、代码评审结论

### 1. 当前 RouteDecision 是浅 Module

`src-python/codex_proxy.py` 当前约 16,315 行，`_proxy_post_request()` 和 `_relay_upstream_response()` 合计承担数千行请求生命周期。现有 `RouteDecision` 只有八个字符串字段，但 `_proxy_post_request()` 随后重新计算 provider-scoped transparent、standard external transparent、Official external transparent、same-format、lightweight fallback、Vision Proxy、retry、usage 和 repair 等策略。

因此 `RouteDecision` 没有产生足够 Depth：调用者仍必须了解几乎全部路由复杂度，#61 不能只给它增加字段或套一个新类。

### 2. “transparent” 当前是标签，不是 invariant

`transparent_request_body()` 仍可能解析并重新序列化 JSON，修改 model/store/stream/reasoning，重写 developer role 和 tool schema，并对所有第三方请求调用 `_rewrite_internal_input_items()`。

`INTERNAL_INPUT_ITEM_TYPES` 又包含标准 Responses 的 `function_call` 和 `function_call_output`。Issue #193 已证明，这会把 OpenCode 拥有的标准工具历史改成 developer 文本，破坏 call/result ID pairing，导致 GLM-5.2 重复调用同一批工具。

根因不是缺少 OpenCode 或 Volc 特判，而是 Gateway 用 wire type 猜测语义所有权。最终设计必须区分：

- `CLIENT_STANDARD`：外部客户端的标准 Responses/Chat 工具契约；
- `CODEX_RUNTIME`：经 namespace、工具身份或 runtime evidence 证明的 Codex 专有契约；
- `PROVIDER_HOSTED`：Provider 自己执行的 hosted capability；
- `UNKNOWN`：native 路径保留，跨协议路径 fail closed。

### 3. retry policy 没有控制真实 retry

`RETRY_CONSERVATIVE_PRE_OUTPUT` 当前只抑制 downstream retry notice；`_open_upstream_response()` 和 relay loop 仍根据 failure class 扩大真实尝试次数，部分路径可到 30 次。

这使 #17 成为正确性任务，而非后续优化。最终 `RetryPlan` 必须同时决定 send state、idempotency、exposure fence、attempt budget 和 protocol fallback，Executor 不得另行推导。

### 4. SSE 生命周期有多个事实来源

当前存在多处直接 `wfile.write`、多个 `readline()` 点、三套部分重叠的 official/transparent/generic relay。`PassthroughSseSemanticStats` 已经自行组装多行 `data:` 做统计，但真实转换路径仍以 physical line 解析；同一协议语义因路由不同产生不同错误、终止和取消行为。

#18、#19、#20 应分别形成真正的内部 Module，而不是只在现有 handler 中增加 helper：

- `DownstreamPort`：所有 headers/body/SSE/keepalive/error 写入的唯一 Seam；
- `UpstreamStreamPump`：queue=32、cancellable backpressure、close/wake/join；
- `SseEventAssembler`：semantic path 的 blank-line event assembly；raw path完全绕过。

### 5. protocol_translation 方向正确，但 Interface 过宽

`protocol_translation.py` 已经对不可逆字段、丢失 ID 和非法 terminal 使用 fail-closed error，这是应该保留的行为。问题是 handler 直接调用大量细粒度函数并通过 callback 倒灌 Codex repair。

重构后它作为 `ProtocolCodec` Implementation 留存；普通调用者只看到 `decode request / encode request / decode stream / encode stream / encode error`，而不是几十个 helper。

### 6. 测试大量穿透 Implementation

`tests/test_routing.py` 约 18,159 行，`tests/test_chat_completions_gateway.py` 约 4,463 行；大量测试 patch 私有函数或 module global。它们保护了真实历史行为，但说明 Interface 不是测试面。

迁移必须遵守 replace-don't-layer：每迁移一个路由族，先建立 `GatewayCore` Interface 测试，再删除对应旧 private-helper 测试；不能永久保留两套测试和两套执行器。

### 7. Rust managed-client 侧同样需要深 Module

`src-tauri/src/gateway.rs` 当前约 9,384 行，同时负责 client discovery、preview、apply、readback、backup、restore、版本探测、Provider export 和大量配置格式。Issue #8 只“拆文件”不够，删除新文件后复杂度仍会回到调用方。

最终应建立 `ManagedClientConfig` Module；OpenCode、ZCode、Pi、OMP 是它的 Adapter。E2E runner 必须调用这一生产 Seam，不得在 PowerShell 中复制配置生成逻辑。

## 二、当前 Wave 的阻塞性规划缺陷

### 1. PR #192 没有测试候选实际托管配置

当前 PR #192 的 runner 在 `Run-RealClientE2E.ps1` 中自行生成 OpenCode、Pi、OMP 和 ZCode 配置，并把 Volc 固定为 Chat Completions。生产 Rust 代码则按 Provider 的 `upstream_format` 选择 Responses 或 Chat；Volc 当前配置为 Responses 时，生产 managed client 会走 `/v1/providers/volc/responses`。

结果是：runner 走 Chat conversion，而真实 OpenCode/Volc 走 Responses transparent；这恰好绕开 #193。真实进程被启动并不等于真实产品路由被测试。

必须新增一个窄的 headless client materialization Seam：候选二进制在隔离 root 下调用与 Tauri UI 相同的 `preview/apply/readback` Implementation。runner 只选择 client/model/root，不再拥有 Provider endpoint、JSON/YAML schema 或 selector 生成规则。

### 2. #190 与 #193 形成不可满足的循环

#190 要求 `0.1.6@cc9df197` 的十二项全部通过；#193 又在同一 SHA 上证明 OpenCode 1.18.3 + Volc GLM-5.2 会出现 excessive tool loop。#193 的真实回归又依赖 #190 runner。

修订后的 bootstrap contract：

- 0.1.6 必须执行全部十二项；
- OpenCode/Volc 在 0.1.6 上必须稳定失败并分类为 `excessive_tool_loop`，其余项必须排除基础设施错误；
- #193 修复 SHA 是首个必须十二项全绿的 control baseline；
- expected product failure 不得记作环境通过。

### 3. VM、版本和 roadmap 已漂移

用户批准的是专用 Windows VM snapshot。当前 #190 V2 contract/PR 改成 machine-bound host isolation；这可以作为额外校验，不能在没有新决策的情况下替代专用 VM。

同时：

- PR #192 的 Codex Desktop pin 已变为 `26.715.7063.0`，#147 仍写 `26.715.4045.0`；
- OpenCode `1.18.4` 已于 2026-07-20 发布，release commit 包含 #191 等待的 `67caf894…` header-timeout fix；#191 不应继续 blocked；
- #190/PR #192 仍固定 OpenCode 1.18.3。

最终 VM pin 应更新为 Desktop 26.715.7063.0、OpenCode 1.18.4，并在同一 VM snapshot 内验证；#147、#190、runner 和 evidence schema 使用同一个版本事实来源。

### 4. #155 是漏关，不是待开发

OMP YAML string fix 已由 PR #167 合入当前基线，commit `e52160c57` 把所有动态 YAML string scalar 改为双引号并增加 typed round-trip tests。#155 应核对 PR/CI/manual evidence后关闭，不应再次派发实现。

## 三、最终目标 Interface

### 1. GatewayCore 外部 Interface

```python
class GatewayCore:
    def preview(self, request: GatewayRequest) -> RoutePlanView: ...

    def execute(
        self,
        request: GatewayRequest,
        downstream: DownstreamPort,
    ) -> ExecutionResult: ...

    def shutdown(self, deadline_seconds: float = 2.0) -> ShutdownResult: ...
```

- `preview`：使用同一纯规划 Implementation，返回脱敏计划，服务 #61 tests、probe、telemetry 和 E2E。
- `execute`：编译并立即消费计划，调用者不能修改 plan，避免 preview/execute TOCTOU。
- `shutdown`：取消 active request、关闭 upstream、唤醒并 join reader。

`CodexProxyHandler` 最终只处理 HTTP ingress/control route，并将请求与 `HttpDownstreamPort` 交给 `GatewayCore`。它不捕获 Provider/translation/SSE 具体错误，也不决定 retry、Vision 或兼容策略。

### 2. CompiledRoutePlan

```python
@dataclass(frozen=True)
class CompiledRoutePlan:
    version: str
    catalog_generation: str
    target: ProviderTarget
    relay: RelayProgram
    capability: CapabilityDecision
    credential: CredentialPolicy
    transport: TransportPolicy
    retry: RetryPolicy
    usage: UsagePolicy
    vision: VisionPolicy
    tool: ToolPolicy
    request_mutations: tuple[MutationSpec, ...]
    response_mutations: tuple[MutationSpec, ...]
    evidence: tuple[EvidenceRef, ...]
```

Invariants：

- plan 完成前不得 mutation、credential acquisition 或 Provider I/O；
- Executor 只能执行 plan 中声明的 Mutation；
- RoutePlan 只保存 opaque `credential_ref`，不保存 raw secret；
- Executor 不得以 Provider 名、User-Agent、model string 或 wire format重新推导策略；
- telemetry 记录 route fingerprint、mutation IDs、stage/count/timing，不记录 body、prompt、credential、绝对路径、call ID 或 runtime ID。

### 3. 四种 Relay Mode

| Mode | 请求 | 响应 | 未知语义 |
|---|---|---|---|
| `OPAQUE_SAME_FORMAT` | 原始 body；仅 URL/auth/header 变化 | 原始 body/SSE bytes | 原样保留 |
| `NATIVE_PATCHED` | 只执行具名、版本化 Mutation | 默认 raw；只允许计划声明的兼容修正 | 不得由 broad allowlist 删除 |
| `TRANSLATED` | ProtocolCodec decode/encode | SSE assembler → GatewayEvent → encoder | 上游 I/O 前 fail closed |
| `CODEX_COMPATIBILITY` | 显式 Codex tool/schema/history codec | 显式 repair/compatibility codec | unknown/candidate 保持兼容或拒绝 |

外部客户端同格式路由默认只允许前两种。Codex App 未经 #67 证据批准的第三方组合继续走 `CODEX_COMPATIBILITY`。

### 4. Mutation contract

每个 Mutation 必须具有：

- 稳定 ID 和版本；
- 精确适用条件；
- `byte_preserving / lossless_semantic / lossy_compatibility` 分类；
- 独立 fixture；
- before/after HMAC 与 applied count；
- 不能包含正文或 secret 的 telemetry label。

首批 Mutation：

```text
model_alias.v1
official_store_false.v1
official_force_stream.v1
official_compaction_sanitize.v1
message_shorthand_normalize.v1
developer_to_system.v1
json_schema_boolean.v1
reasoning_alias.v1
vision_proxy.v1
strict_apply_patch.v1
codex_internal_history_compat.v1
```

`codex_internal_history_compat.v1` 必须依据 tool ownership/evidence 选择，不能再依赖包含标准 function items 的全局 type set。

### 5. DownstreamPort 与 StreamPump

```python
class DownstreamPort(Protocol):
    def start(self, status: int, headers: HeaderBlock) -> None: ...
    def send(self, data: bytes, *, flush: bool = False) -> None: ...
```

生产 Adapter 将 `BrokenPipeError`、`ConnectionResetError` 和相应 `OSError` 统一转换为 `DownstreamClosed`；该异常原子地取消 request、关闭 upstream、阻止后续写入，并最终记录一次 499 completion。

`UpstreamStreamPump` 内部要求：

- queue capacity 固定 32；
- producer 等待 full queue 时观察 cancellation；
- EOF/error/timeout/downstream close/shutdown 均 close+wake；
- close idempotent；reader join ≤1 秒；Gateway shutdown ≤2 秒；
- production transport Interface 使用 chunk read，而不是把 `readline()` 暴露给 GatewayCore。

### 6. SSE 与 GatewayEvent

`OPAQUE_SAME_FORMAT` 不经过 JSON parser、SSE assembler 或 GatewayEvent。observer 只能 side-tap，observer 失败不得让成功 raw stream 失败。

`TRANSLATED`/需要语义处理的 `CODEX_COMPATIBILITY` 才使用：

```text
raw chunks
  → SseEventAssembler(raw bytes + metadata + joined data)
  → ProtocolCodec
  → GatewayEvent(text/reasoning/tool/usage/terminal/error)
  → inbound ProtocolCodec emitter
```

多行 `data:` 用字面 `\n` 连接；metadata/order/raw bytes 保留在 event envelope。非法 JSON、非法顺序、缺失 ID 或不可逆语义返回 sanitized protocol-valid 502，绝不静默 `continue`。

### 7. RetryGate

权威状态：

```text
UpstreamSendState = NOT_SENT | MAYBE_SENT | REJECTED_CAPACITY | ACCEPTED
DownstreamExposure = NONE | HEADERS | CONTENT | TERMINAL
```

规则：

- `DownstreamExposure != NONE`：不得 replay/fallback；
- 明确 DNS/TCP/TLS pre-write failure：可在 plan budget 内重试；
- main-generation POST 为 `MAYBE_SENT`/`ACCEPTED` 且无 idempotency guarantee：不重试；
- pre-output 429/503：最多一次，两个 attempt 都保留；
- stream-body failure：不重新生成；若 downstream 仍连接，输出一次协议有效失败 terminal；
- Responses→Chat auto fallback 也属于第二次 POST，只在明确 404/405/415/422、无 exposure 且无模糊 acceptance 时允许；
- #22 的 jitter/circuit breaker 将来作为 Retry Adapter，不再修改 handler。

### 8. 真实 Adapter Seams

| Seam | 生产 Adapter | 测试 Adapter |
|---|---|---|
| Downstream | `HttpDownstreamPort` | recording/failure injection |
| Transport | Official pooled、generic third-party HTTP | scripted、loopback fault |
| Protocol | Responses、Chat；未来 Messages | fixture codecs/conformance |
| Credential | current config/env；未来 vault/OAuth | static/failing/singleflight |
| Provider catalog | TOML/catalog snapshot | in-memory snapshot |
| Telemetry/clock | JSONL/SQLite/system clock | recording/fake clock |

不要为每个普通 Provider 建空壳 Adapter。Volc、Ollama Cloud 等首先复用 generic Responses/Chat Adapter；只有经证据证明的 wire dialect 才增加 Provider-specific Mutation/Adapter。

## 四、功能级迁移计划

### Wave 0：修正 frontier 与 E2E bootstrap

#### W0.1 新建 P1 strict：候选托管配置 headless materialization

功能：

- CLI 增加隔离 root 下的 managed-client preview/apply/readback 命令；
- 直接复用 Rust production render/apply Implementation；
- 输出 client/provider/model/endpoint/schema generation 的脱敏 manifest；
- runner 不再生成 OpenCode/Pi/OMP/ZCode Provider 配置；
- case root、backup root、APPDATA、CODEX_HOME 等全部可显式注入并禁止 host fallback；
- fake 和 Rust tests 证明四客户端与 UI 使用同一配置字节/结构。

这项是 #190 的 `dispatch_after`，同时为后续 #8 提供真实 Interface，不提前完成完整 client refactor。

#### W0.2 返工并完成 #190 / PR #192

具体改动：

- 恢复专用 VM snapshot 作为授权环境；machine binding 仅作为附加证据；
- Desktop pin 统一为 26.715.7063.0；OpenCode pin 升为 1.18.4；
- 通过 W0.1 materialize candidate-managed configs；
- Gateway telemetry 与 client native events按 case/run binding关联；
- excessive tool loop 在很小的 bounded call count 后 fail fast；
- 0.1.6 calibration：十二项均实际启动，OpenCode/Volc 必须得到已知产品失败分类；
- 所有 failure path 仍生成一个 sanitized summary；
- strict reviewer、CI、VM human evidence 绑定同一最终 SHA。

#### W0.3 frontier 清理

- #191：改为 ready；确认 1.18.4 包含 fix，升级 host/VM/runner并跑 direct OpenAI + managed Gateway regression后关闭；不单独派发与 #190 冲突的 Worker。
- #155：以 PR #167、当前代码、CI 和必要 OMP smoke 关闭。
- #147：同步 VM、Desktop/OpenCode pins、bootstrap expected-failure语义和新依赖。
- #193：移入 0.1.7，P1/heavy/strict，从 `needs-triage` 变为 ready；依赖 #190。

### Wave 1：0.1.7 可靠性地基

依赖：

```text
W0.1 headless managed config
  → #190 revised E2E foundation
  → #193 preserve standard tool history
  → #18 DownstreamPort / 499
  → #19 UpstreamStreamPump / queue=32
  → #20 SseEventAssembler / strict protocol errors
  → #114 fault injection and App/CLI A-B
  → #141 Desktop restart/Task disappearance
```

#### W1.1 #193：标准工具历史所有权修复

- External native Responses 默认 preserve标准 `function_call`/`function_call_output`；
- call ID、name、arguments、output、status、顺序保持；
- 仅经证据识别的 Codex internal、compaction、tool_search、apply_patch 继续具名处理；
- 禁止 OpenCode User-Agent 特判；
- OpenCode 1.18.4/Volc真实回合仅一次 read-only tool call 后 final；
- #190 加 bounded duplicate-tool-loop assertion；
- 该 SHA 首次要求十二项全绿。

#### W1.2 #157：独立并行的 CodexConfigProjection

- 只在 `config_overlay`/catalog seam 修正 Official context cap scope；
- Official persisted default + third-party task override必须使用模型自己的 limit；
- 不把这项逻辑塞入 GatewayCore；
- path claim限制在 config overlay/tests，避免与 SSE hotset冲突。

#### W1.3 #18：DownstreamPort

- 所有 production SSE/header/body/error/keepalive write 通过唯一 Adapter；
- downstream close → cancel upstream → no further write/retry/fallback → exactly one 499；
- static test 禁止流式路径直接 `wfile.write`；
- Desktop/Luna Stop、OMP/Volc Ctrl+C 后下一回合成功。

#### W1.4 #19：UpstreamStreamPump

- queue=32、cancellable put、close/wake/join；
- 所有 EOF/error/timeout/close/shutdown 使用同一 lifecycle；
- join≤1s、shutdown≤2s；
- Luna/Volc 四客户端并发，无 reader/thread leak。

#### W1.5 #20：SseEventAssembler

- raw mode 保留原始 byte identity；
- semantic mode按 event boundary组装多行 data/metadata；
- Responses↔Chat fixtures覆盖 `[DONE]`、CRLF/LF、fragment、invalid UTF-8 raw、invalid JSON；
- invalid semantic SSE → exactly one sanitized protocol-valid 502。

#### W1.6 #114 / #141 / #138

- #114 在新 I/O Module 上做 Direct Official / Gateway Luna / Gateway Volc 同窗口 A/B；
- 确认 initiating failure boundary 后才调整 transport/retry；
- #141 继续依赖 #114；
- #138 保持独立 P2 human evidence；
- #21/#22 继续 `needs-info`，直到 #114 给出资源放大和 failure clustering 数据。

### Wave 2：0.1.8 GatewayCore 架构化

#### W2.1 重契约 #61：Compiled RoutePlan

- 引入 typed ProviderTarget、RelayProgram、ToolPolicy、RetryPolicy、TransportPolicy、MutationSpec；
- pure planner 不读 secret、不发 I/O；
- table fixtures覆盖 Official Codex App、Official external、OpenCode/Volc native、provider-scoped Chat/Responses conversion、Codex external compatibility、unknown capability；
- handler 删除所有 plan 后的重复 boolean 推导；
- candidate/unknown native states仍 fail closed/compatibility，不在架构 refactor 中选择新行为。

#### W2.2 #17：RetryGate

- 让 `retry_policy` 控制真实 attempt，而不是 notice；
- 建立 send-state/exposure fence；
- pre-write、ambiguous acceptance、post-output、capacity、protocol fallback分别测试；
- telemetry记录 attempt/phase/send-state/exposure，不记录正文；
- 使用 #114 evidence设置初始 policy。

#### W2.3 新建 P1 strict：GatewayCore execution Seam

- 实现 `preview/execute/shutdown`；
- 接入 #18/#19/#20 Module；
- handler只创建 GatewayRequest/DownstreamPort；
- exactly-once completion latch和统一 GatewayError；
- 先以 differential replay保持现有行为，不在此 Issue 删除 compatibility。

#### W2.4 新建 P1 strict：External native relay + Mutation pipeline

- 实现 `OPAQUE_SAME_FORMAT` 与 `NATIVE_PATCHED`；
- 先迁移 provider-scoped external Responses/Chat；
- property tests覆盖任意 bytes、CRLF/LF、fragment、unknown tagged item；
- async usage observer不得阻塞或修改 raw stream；
- 每个 request patch进入 Mutation ledger；
- 删除迁移路由在旧 transparent/generic relay中的分支。

#### W2.5 新建 P1 strict：ProtocolCodec registry

- 将现有 Responses↔Chat转换包装为两个真实 Protocol Adapter；
- 只让 semantic paths进入 GatewayEvent；
- Adapter conformance覆盖 request/body/stream/error/terminal/ID；
- 移除 handler callbacks和重复 wrappers；
- 为 ADR-0001 的 future AnthropicMessage保留第三 Adapter位置，但不注册生产 `/v1/messages`。

#### W2.6 新建 P2 strict：删除旧执行分支和测试迁移

- 按路由族删除旧 `_proxy_post_request`/`_relay_upstream_response` 分支；
- 新 Interface tests 建立后删除对应 private-helper tests；
- 禁止 GatewayCore外直接 Provider name policy、direct stream writes/reads；
- `codex_proxy.py` 只保留 composition root、HTTP ingress和control routes。

### Wave 3：0.1.8 capability certification

```text
#61 + GatewayCore
  → #62 runtime plan / route fingerprint / mutation trace
  → #63 official tool_search
  → #64 collaboration V1/V2
  → #65 provider/model capability rows
  → #66 conversion support matrix
  → #67 maintainer GO/PARTIAL/NO-GO
  → #58 capability-driven native Codex Responses
  → #59 evidence-ledger cleanup of Gateway-owned scheduling
```

- #62 的既有只读 evidence保留；最终 trace schema用 RoutePlan/mutation ledger重采，不再创造第三种格式。
- #66 决定 `TRANSLATED` 可逆子集；unknown/lossy语义不能静默掉落。
- #58 只把 #67批准的 provider/model/protocol/tool-class从 Compatibility切到 Native。
- #59 每删除一个 schema/scheduler/repair都要有 #57/#58 evidence ledger和 rollback mode。

### Wave 4：Provider 数据、认证和 onboarding

顺序：

```text
#89 stable Provider preset IDs
  → #91 reviewed Models.dev snapshot
  → #179 ProviderDefinition / ModelMetadata convergence
  → #92 credential_ref + OS vault
  → #90 API-key onboarding using the real credential seam
  → #93 OAuth drivers
  → #94 only after explicit provider approval
```

功能边界：

- `ProviderDefinition`：endpoint/auth/formats/preset provenance；
- `ModelMetadata`：price/reasoning/limits；
- `CapabilityEvidence`：#57/#62–#67 runtime evidence；
- Models.dev不能提供 endpoint、auth或verified capability；
- RoutePlan只含 opaque credential_ref；
- 先用 current config/env Credential Adapter，再由 #92替换 vault、#93增加 OAuth；GatewayCore不变；
- 不提前实现账户池/affinity/cooldown，仅保留真实 CredentialBroker Seam。

### Wave 5：0.1.9 managed-client 深 Module

重契约 #8，不以“拆文件”为验收，而以 Interface 为验收：

```rust
trait ManagedClientAdapter {
    fn inspect(&self, context: &ClientContext) -> ClientState;
    fn preview(&self, target: &ClientTarget) -> ConfigGeneration;
    fn apply(&self, generation: &ConfigGeneration) -> ApplyResult;
    fn restore(&self, owner: RoutingOwner) -> RestoreResult;
}
```

具体功能：

- OpenCode/ZCode/Pi/OMP Adapter各自拥有发现、render、parse/readback和restore；
- shared Module拥有ownership、backup、transaction、generation ID和status；
- W0.1 CLI和Tauri UI调用同一 Interface；
- #153 在 ZCode Adapter实现三文件 generation transaction/journal和restart recovery；
- #28 为版本探测/Provider discovery提供singleflight、timeout、cache/invalidation；
- #83 保持 UI navigation/status task；
- #155 关闭，不进入实现 Wave。

### Wave 6：未来 Messages/Claude

- 继续遵守 ADR-0001；
- #74补齐 thinking/cache/beta/count-token/cancellation真实证据；
- #75只通过新的 ProtocolCodec注册 `/v1/messages`，不修改 GatewayCore Interface；
- #76/#77/#78随后处理managed config/alias/release gate；
- Claude Code作为下游 Gateway client与 #85 ACP AgentProvider保持不同领域。

## 五、Orchestrator 排程与 conflict claims

当前 frontier scan：Ready Reserve 3/target 6、reserve gap 3、parallel width 0，#18/#19/#20均被 dispatch dependency阻塞。应先修复 contracts，而不是为了填满 slot派发重叠核心工作。

建议资源 claim：

- `gateway-core`: #193、#18、#19、#20、#61、#17、GatewayCore/native/codec迁移；始终只有一个编辑 Worker。
- `real-client-e2e`: W0.1、#190、#191 pin update；与 candidate config CLI共享 Rust claim时串行。
- `codex-config-projection`: #157，可与 Python SSE work并行，禁止扩大到 `codex_proxy.py`。
- `managed-client-config`: #8、#153、#28；0.1.9 内串行持久化变更。
- `provider-schema`: #89/#91/#179/#92；涉及 providers.toml/Python/Rust mirror时 repository-level schema claim。

0.1.7 推荐最大宽度：

1. 一个 `gateway-core` Worker；
2. 一个 disjoint `codex-config-projection` Worker；
3. 一个 E2E/review/human gate slot，不再追加新产品功能。

## 六、每个重构 candidate 的验收

所有 Gateway routing/transport/protocol改动均为 strict：

1. targeted Interface tests；
2. `python -m pytest -q`；
3. `git diff --check`；
4. `python scripts/report_quality_gates.py`，仅报告；
5. exact-SHA GitHub Actions；
6. strict Reviewer；
7. pinned VM十二项真实客户端矩阵；
8. Issue-specific取消/并发/协议/identity证据；
9. 新 SHA使旧 E2E和review evidence失效。

新增架构级 gates：

- RoutePlan table：client × provider × inbound/upstream wire × capability state；
- native identity：无 Mutation时 request/response raw SHA一致；
- Mutation ledger：applied mutations必须是planned mutations的有序子集；
- Adapter conformance：production/scripted/loopback在cancel、timeout、EOF、terminal、close上产生相同 Outcome；
- differential replay：old/new executor对迁移路由的合法行为一致；
- static guard：`HttpDownstreamPort` 外无 streaming `wfile.write`，`UpstreamStreamPump` 外无 production stream read；
- fail-closed unknown semantics、no silent Official/model/Provider fallback。

## 七、最终完成标准

重构完成不以文件数量或行数为准，而以以下 observable结果为准：

1. 增加普通 OpenAI-compatible Provider不编辑 HTTP handler或 GatewayEngine；
2. 增加协议只实现 ProtocolCodec和capability evidence；
3. 增加 vault/OAuth不编辑路由或协议代码；
4. 外部同格式请求可以用 raw SHA证明 body/SSE透明；
5. 所有语义变化都能由 RoutePlan和Mutation ledger解释；
6. 普通客户端 function tool永不被Gateway当成Codex internal接管；
7. 未知协议项在native path保留、在translation path明确拒绝，绝不静默消失；
8. downstream close、upstream failure、shutdown和success均只有一个 terminal/completion；
9. E2E使用候选实际托管配置和固定真实客户端，而不是runner镜像；
10. 删除旧执行分支后，调用者和测试只依赖新的深 Module Interface。

