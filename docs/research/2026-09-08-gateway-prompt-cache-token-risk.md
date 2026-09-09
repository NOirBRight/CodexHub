# Gateway 提示缓存与 token 消耗风险调查

调查日期：2026-09-08（Asia/Shanghai）。调查代码基线：`166029f` 加当时工作区已有未提交改动。以下调查保留当时证据；两批修复的实现状态见文末。

## 结论

**存在可复现的局部风险，主要集中于第三方协议和工具适配；没有证据表明 Gateway 普遍破坏官方 Codex 缓存。** 本机最近七天 Codex App 官方普通生成请求中，有缓存用量记录的 2,435 次请求，输入 token 的 97.05% 被上游报告为缓存命中。

优先处理顺序：稳定跨轮工具别名 → 修正上游缓存 key 遥测 → 按供应商能力保留缓存控制 → 限制动态工具/兼容提示的前缀扰动与大小。实际多付费用、订阅额度影响及相对直连的性能差额尚未经过对照实验。

## 证据边界与缓存规则

一手来源包括当前仓库代码和测试、只读本机 Gateway SQLite 遥测，以及实际抓取的 [OpenAI Prompt caching 官方文档](https://developers.openai.com/api/docs/guides/prompt-caching)（2026-09-08）。当前会话没有官方文档搜索工具，因此直接抓取官方页面。

官方文档说明：

- 缓存复用依赖相同的模型输入前缀及可匹配的缓存断点。工具名称、描述、schema、排序以及某些推理/输出设置都可能改变前缀；改变请求并不等于清空已有缓存。
- `prompt_cache_key` 是帮助相同前缀请求抵达缓存的路由提示，不保证命中；没有 key 也不等于关闭自动缓存。
- 缓存 token 仍然是输入 token。缓存命中下降通常增加未缓存计算、费用和延迟，不能直接称为“输入 token 数变多”。真正新增提示、工具定义或生成请求才会增加 token 工作量。
- 缓存写入、读取价格和断点策略依模型而异。当前官方文档包含 `prompt_cache_options`、`prompt_cache_breakpoint` 和 `cache_write_tokens`；不能把旧缓存定价公式普遍用于当前模型。

以上 API 规则用于解释代码风险；不直接证明 ChatGPT 订阅内部的缓存路由或额度计费规则，也不替代第三方供应商的实现证据。JSON 空格、键顺序、Unicode 转义等 wire 字节变化本身不能证明模型 token 前缀变化。

## 本机实际用量

只读来源：`~/.codex/proxy/codex-proxy-telemetry.sqlite` 的 `gateway_requests`。固定 UTC 窗口为 `2026-08-31T23:14:12Z` 至 `2026-09-07T23:14:12Z`，即北京时间 09-01 07:14 至 09-08 07:14。筛选 `client_id = 'codex-app'`，普通生成与压缩分开；未读取或输出提示正文、会话标识或凭据。

| 路径 / 请求类型 | 请求数 | 有缓存用量的请求 | 这些请求的输入 token | 缓存 token | 缓存 token 占比 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Official Responses / main_generation | 2,488 | 2,435 | 337,098,386 | 327,154,944 | **97.05%** |
| CommandCode Chat / main_generation | 159 | 159 | 143,907 | 130,176 | **90.46%** |
| OpenCode Go Responses / main_generation | 182 | 182 | 194,684 | 98,774 | **50.74%** |
| Official Responses / compact | 28 | 26 | 5,880,627 | 266,112 | 4.53% |
| xAI Responses / main_generation | 13 | 0 | — | — | 无法判断 |

计算方法为 `sum(cached_tokens) / sum(input_tokens)`，分子分母均仅包含有缓存用量的请求。这是按 token 加权的缓存占比，不是“多少请求至少命中一次”的比例。缺失用量没有按零命中处理。

这些记录跨越历史部署版本及不同任务，可能包括本机验收流量；没有同一提示的直连对照，也没有逐条绑定当前 checkout 的运行版本。不能据此比较不同供应商的实现优劣，或把 OpenCode Go 的较低比例归因于 Gateway。官方压缩请求与普通生成的内容不同，低比例本身不是缺陷证据。xAI 的其他客户端另有一条用量记录，样本不足且未混入上表。

## 1. 高优先级：新增工具导致所有适配工具改名

**代码和无网络复现均确认。** [official_passthrough.py](../../src-python/gateway_compat/official_passthrough.py) 的 `_runtime_tool_alias_token`（359–395 行）将完整 `declarations`、协议和能力集合一起 hash，再在 462 行传入别名 registry；[registry.py](../../src-python/tool_compatibility/registry.py) 69、132–136 行用该 token 构造所有 namespace/custom/search 别名。

调用公开入口 `gateway_compat.compatible_request_body`，采用第三方 `chat_tools` + `eager`，关闭额外 Codex 工具注入：

```text
原 tools: [namespace vendor.run]
上游名称: [__codexhub_ns_7bbdd54399_1]

仅在末尾追加普通 function new_tool:
上游名称: [__codexhub_ns_9dbc8cd2cd_1, new_tool]
```

旧工具声明不变，旧工具的上游名称却改变。原本仅追加尾部工具的差异，被扩大到最早的适配工具名称；如果这个位置早于可复用断点，就可能丢失大段前缀的缓存收益。修改某个工具描述/schema 也会产生这种扩大效应。这项风险不依赖 V1 子代理提示注入是否启用。

[现有测试](../../tests/test_runtime_tool_compatibility_integration.py) `test_runtime_plan_aliases_are_deterministic_for_identical_external_request` 只证明完全相同请求得到相同别名，未证明工具增量变化时旧名称保持稳定。

建议按单个工具的稳定身份生成别名，碰撞处理也应稳定；为“末尾增添无关工具、旧工具修改、工具重排、历史调用逆映射”补充边界测试。修复仍需保留现有命名冲突保护与响应反向映射。

## 2. 中优先级：Responses → Chat 丢失缓存 key

**代码、现有测试及无网络复现均确认。** [protocol_translation.py](../../src-python/protocol_translation.py) 228–234 行验证后移除 `prompt_cache_key`，658–659 行的 transport-field 丢弃分支也会移除它。[test_prepare_exchange.py](../../tests/test_prepare_exchange.py) 的 `test_prepare_exchange_consumes_real_codex_transport_defaults` 明确断言 key 不出现在 Chat 输出。

```text
客户端: {model, instructions, input, prompt_cache_key: "stable-test-session", store: false}
Chat 上游: {model, messages}
```

影响的是跨协议路由的显式缓存亲和性，不是所有 Responses 请求，也不是证明所有 Chat 路由缓存失效。上表 CommandCode Chat 的 90.46% 缓存 token 占比正说明应避免“删 key = 关闭缓存”的结论。仓库当前内置 xAI 为 Responses 路径，不能把 Chat 转换缺陷直接归到它。

建议对已验证支持该字段的供应商保留 key；不支持的端点继续做明确降级，并记录 `cache_control_dropped`。不要向所有 OpenAI-compatible 端点无条件添加字段。

对当前官方文档中的 `prompt_cache_options`，无网络调用 `prepare_exchange` 的结果是 `NonForwardable: Cannot translate Responses request fields without losing them: prompt_cache_options.`，属于明确拒绝，不能描述成静默关闭显式缓存。正常官方透传会保留这些未知字段。

## 3. 中优先级：遥测可能掩盖真实缓存 key 丢失

**无网络复现确认。** [gateway_exchange.py](../../src-python/gateway_exchange.py) 435–448 行为上游 body 构造遥测时传入 `request.prompt_cache_key`（来自客户端）；[proxy_telemetry.py](../../src-python/proxy_telemetry.py) 184–188 行优先使用该值，只有值为 `None` 时才从实际 body 提取。

```text
upstream_body_has_key = False
telemetry_has_key_hash = True
```

因此 `upstream_prompt_cache_key_hash` 与 caller 相同，不足以证明 key 真正上送。建议上游字段只依据最终上游 body，并分别记录 caller/upstream key 的存在性和 hash。

同模块 `REQUEST_PREFIX_BYTES = 65536` 与 body SHA/HMAC 是 wire 观测，不是模型 token 前缀证据。重序列化会改变它们，却不一定改变 token；反之，截取 wire 前 64 KiB 也不能覆盖所有模型输入构造。

[gateway_events.py](../../src-python/gateway_events.py) 155–205 行归一化缓存读取 token，但不保留 `cache_write_tokens`。无网络构造 `input_tokens_details={cached_tokens:6000, cache_write_tokens:4000}` 后只得到缓存读取数，说明现有汇总不足以计算需要区分缓存写入价格的模型成本。应增加写入用量和缺失状态，不能用“缺失=0”估费。

## 4. 条件性风险：动态工具表和额外兼容提示

[gateway_compat/request.py](../../src-python/gateway_compat/request.py) 519–538 行按子代理当前状态选择 spawn/wait/close 工具；715–719 行在生命周期完成时隐藏工具；644–685 行追加状态/收尾 developer 提示。工具表变化可能影响较早前缀；追加提示会增加输入内容，但追加本身不等于破坏前面所有缓存。

[subagent_policy.py](../../src-python/subagent_policy.py) 14–37 行说明提示和语义修复受 repair policy、assist mode 控制，Collaboration V2 明确禁用此 V1 guidance。不能把这项描述成所有第三方请求每轮必然发生。

同一七天窗口的事件汇总确实记录到 44 次 `multi_agent_current_state_guidance_injected`（CommandCode/OpenCode Go 各 22），47 次 `worker_subagent_finalization_guidance_injected`（xAI）。这些是事件数，可能含本机验收流量，不是额外生成请求数或 token 数。

建议先稳定工具表和别名；将易变的状态提示限制在尾部，设置大小上限，避免为改善缓存而移除必要的兼容能力。应测量实际提示增量和 token 数，而不把 JSON bytes 当 token。

## 5. 已有保护与剩余风险

- **官方正常路径没有发现每轮随机改 prompt 的行为。** [official_passthrough.py](../../src-python/gateway_compat/official_passthrough.py) 1589–1627 行仅按需改模型/tier/store、清理不兼容 compaction/reasoning。无网络构造正常请求（含缓存 key、显式缓存控制、`store=false`），返回 body 与输入字节完全一致。混合供应商历史清理会改变上下文，不能承诺这种请求完全等价。
- **官方透传不覆盖原生 session 身份。** [subscription_credential.py](../../src-python/subscription_credential.py) 137–149 行在 strict passthrough 直接返回。其他官方适配入口在没有 `Session-id` 时生成 ID（[gateway_transport.py](../../src-python/gateway_transport.py) 1205–1210 行），但没有证据证明该 header 的变化必然破坏服务端缓存。
- **第三方重复计费有防护。** [gateway_transport.py](../../src-python/gateway_transport.py) 2429–2464 行对非官方主生成 POST 的 write/header/body 后失败或未知阶段抑制重试，除非路径有幂等保证；[gateway_exchange.py](../../src-python/gateway_exchange.py) 831–855 行也将该规则用于协议 fallback。官方请求不受同一保守规则限制，故网络故障后重复生成仍有条件性风险，未取得重复扣费证据。
- **真实重试记录没有证明普遍重复生成。** 七天窗口有 80 条 upstream retry，均为 official：51 次 response_headers/provider_throttle、25 次 TLS、3 次 TCP、1 次 response_headers/quick_transient。连接阶段失败不能直接记作新增模型 token；没有每次尝试的上游 usage，无法量化费用。
- **Vision Proxy 会调用额外视觉模型，但有持久化缓存。** [vision_proxy.py](../../src-python/vision_proxy.py) 300–312、649–674 行用图片内容/URL、vision model、prompt version 确定 key，SQLite 命中后复用描述。模型/图片 URL/提示版本改变或缓存写入失败可能导致重算；当前事件窗口没有相关事件，未证实这是本机主要消耗来源。
- **第三方状态续接存在语义限制。** [gateway_request.py](../../src-python/gateway_request.py) 294–377 行移除 `previous_response_id`、不可移植的 encrypted reasoning/item reference。它不直接增加输入 token，但只发送增量输入的客户端可能失去续接语义，跨供应商历史也可能引发重新推理。不能把会话状态缓存与 prompt cache 混为一谈。

## 验证与后续验收建议

本次为只读调查及文档变更，按 `fast` 文档工作验证。无外部付费请求；无网络复现使用仓库 Python 3.13+ launcher 和公开转换入口，遥测 helper 使用临时目录。

```bash
./scripts/codexhub-python.sh -m pytest -q tests/test_gateway_characterization.py tests/test_gateway_transport.py tests/test_gateway_exchange.py tests/test_chat_completions_gateway.py
# 153 passed, 13 subtests passed in 25.43s

./scripts/codexhub-python.sh -m pytest -q tests/test_prepare_exchange.py tests/test_runtime_tool_compatibility_integration.py tests/test_proxy_event_logging.py
# 78 passed in 0.53s
```

合计 231 tests + 13 subtests 通过。现有测试通过不等于缓存风险消失，其中部分测试正将字段移除固定为兼容行为。本次未改业务代码，因此没有运行全量 Python/Rust/UI 或真实供应商 E2E。

修复验收应包含：相同模型/账号/设置/工具顺序/长前缀的多轮直连与 Gateway 对照，分别测试固定工具集合、尾部发现新工具、子代理状态变化。记录每次实际发送的缓存控制、tools 和 instructions 的稳定摘要、上游输入/缓存读取/缓存写入/输出 token、TTFT 与 retry 次数；将冷缓存首轮和后续轮次分开。只存脱敏摘要，不保存用户 prompt 或凭据。

## 已批准的修改方案（2026-09-08）

第一批聚焦已确认缺陷：工具别名稳定性与最终上游请求遥测。第二批增加有证据支持的端点缓存控制，以及缓存写入用量。用户已批准一次性完成两批，实际实现见下节。

### 第一批 A：稳定工具身份，保留每请求映射

主要涉及 `tool_compatibility/registry.py`、`tool_compatibility/dispositions.py`、`gateway_compat/official_passthrough.py`。

- 将别名生成依据改为版本化的工具身份，例如 `(alias_format_version, adapter_kind, namespace, original_name, declared_version)`。Namespace child 使用所属 namespace 和 child name；custom/search 使用各自的适配类型与名称。工具的协议身份变化才需要改变其别名。
- 整份 declarations、数组下标、请求 ID、session ID、其他工具能力、描述与 schema 不参与该工具的名称生成。描述或 schema 改变时，其实际声明自然会改变，不应再带动其他工具名称变化。
- 移除对全局递增 ordinal 的身份依赖，使用每工具摘要及局部冲突后缀。保留名称长度限制、原生名称预留、重复/歧义声明拒绝和有界碰撞处理；真实名称碰撞允许受影响的工具改名，但不能重命名无关工具。
- 保留请求内 registry、call ledger 与响应反向映射。不引入跨会话数据库或全局可变映射，也不能因为别名可预测就接受当前请求没有声明的工具。
- 保持客户端工具顺序，不通过全局排序“稳定”前缀。工具集合本身改变造成的自然缓存差异仍然存在，修复只消除 Gateway 将差异扩散到早期工具的行为。
- 升级会使旧算法生成的上游名称发生一次变化，可能短暂冷缓存。常规客户端接收的是反向映射后的原工具身份，应验证跨升级历史继续可编码；旧 opaque alias 若已泄漏且无法证明归属，仍应明确拒绝，不能猜测映射或重写用户历史。

验收使用公开 `build_tool_compatibility_plan` / `compatible_request_body` / plan encode/decode：新增尾部普通工具、新增 namespace child、前插和重排、修改无关工具描述/schema时，未改变的工具名称保持稳定；旧历史调用可继续编码；返回工具调用和 SSE 可逆解码；原生名称碰撞仅影响冲突项；未知别名、重复身份及超过长度/碰撞限制仍拒绝。现有 bulk alias 和 native collision 测试必须保留。

### 第一批 B：遥测以实际发送的请求为准

主要涉及 `gateway_exchange.py`、`proxy_telemetry.py` 与其边界测试。

- 调整 `request_observability_for_attempt`：第三方及发生转换的请求，从最终 `attempt_body` 获取缓存 key，禁止传入 caller key 作为上游值的兜底。
- caller 与 upstream 各自记录 key 的存在性和 HMAC，不输出原值。可明确区分 absent、empty、present；解析不可用不能伪装成 absent。
- 官方字节透传可在证明 body 未变时复用已解析的 caller 值，避免重复解析长上下文；发生改写则用最终 payload/body。保持官方正文与 HTTP 转发行为不变。
- 未加前缀的通用字段继续表示实际上游，caller 值仅出现在 caller 字段，防止旧消费方继续得到误报。

验收包含：Responses → Chat 丢弃 key 后 caller 有值而 upstream 无值；同协议保留时两端一致；key 被改写时上游摘要反映新值；多次协议尝试各自反映实际 body；日志不出现明文 key。

### 第二批：端点缓存控制和用量

- 新增端点/协议级的已验证缓存控制能力，经 `RoutePlan` 固化后传给转换器。现有 `reports_cached_input_tokens` 只说明用量报告能力，不能复用为接收 `prompt_cache_key` 的开关。
- 对确认支持的转换目标保留有效 `prompt_cache_key`；未知/不支持的 Chat 目标保持现有兼容行为，并记录明确丢弃原因。不能靠一次失败后重试来探测支持，也不能新增随机 key。
- 同协议请求继续保留原缓存控制。跨协议的显式 breakpoint/TTL 只有存在等价表示时才转换，否则明确拒绝；不将显式缓存请求偷偷降级成隐式缓存。还需覆盖 Chat → Responses，目前它的字段白名单不接受 `prompt_cache_key`。
- `gateway_events.normalize_usage_for_event` 增加已知格式的缓存写入 token；`proxy_telemetry` 的列声明和迁移增加可空字段。原始用量缺失保持 NULL，不回填为 0；缓存写入字段与输入 token 的关系按供应商定义解释，避免重复计数。

### 暂不合并的行为调整与验证范围

动态隐藏工具承担当前 V1 的行为约束，不能直接删除或仅用 `tool_choice` 替换——现有代码因部分第三方不支持 named/required choice 而将其改为 `auto`。这部分应另行验证工具约束的等价性，再考虑稳定工具表。官方重试、Vision Proxy 和 reasoning/history 清理也没有足够证据支持在首批一并修改。

首批属于协议边界，按仓库 `strict` 级执行：隔离分支、现有公开 seam 上的针对性回归、候选变更审查、一次相关完整 Python core suite（`./scripts/codexhub-python.sh -m pytest -q --ignore=tests/test_real_client_e2e.py`）、report-only quality gates、diff 检查。是否增加真实供应商对照由验收目标决定；本地稳定性测试只能证明前缀不被额外扰动，不能承诺具体缓存收益百分比。

## 实现状态（2026-09-08）

开发分支为 `codex/gateway-prompt-cache`，保留开工前全部未提交改动。本次完成两批代码；未向实际供应商发起生成请求，未部署或重启正在运行的 Gateway。

| 范围 | 实现 |
| --- | --- |
| 稳定别名 | `registry.py` 使用版本化的 family/namespace/original name/declared version 身份生成摘要，保留原有 10 位 hex 长度以兼容 32 字符工具名端点；后缀仅用于该身份的有界碰撞处理。移除全工具表 seed 与共享递增序号，请求/重试的映射和独立 call ledger 继续保留。 |
| 实际上游遥测 | `enrich_request_observability` 从传入的最终 body 读取 key，移除 caller override。caller/upstream 分别记录 `prompt_cache_key_state` 与 HMAC；区分 present/empty/null/absent/invalid/unavailable，落库时清除失效的旧 hash。原始 `prompt_cache_key` 加入通用递归脱敏名单，保留 hash/state。实现选择解析实际 body，暂未采用官方解析值复用优化。 |
| 端点能力 | 新增 [prompt_cache_policy.py](../../src-python/prompt_cache_policy.py)，按 HTTPS origin、默认端口、准确 endpoint path 和目标协议核定能力；由不可变 `RouteAttemptPlan` 固化。不是根据供应商名称、模型名称或 cached usage 推断，也未增加需跨 Rust/UI 同步的配置字段。 |
| 跨协议 key | 已验证端点的有效 string/null/empty key 在 Responses ↔ Chat 两向转换中保留。未知目标保持兼容性丢弃，由 `PreparedExchange.dropped_cache_controls` 驱动一次脱敏 `cache_control_dropped` 事件（含 attempt index 与原因）。未增加探测重试。 |
| 显式缓存 | 同协议保持原始缓存控制；跨协议的 options/retention/content breakpoint 在未实现等价映射时明确拒绝，测试覆盖 breakpoint 不被悄悄剥离。 |
| 写入用量 | 归一化两种 OpenAI wire 的 `*_tokens_details.cache_write_tokens`，新增可空 SQLite 列 `usage_cache_write_input_tokens`。缺失不变成 0，也不再次加到输入/总 token。旧库在现有迁移入口自动增加列。 |
| 协议证据 | [生成器](../../scripts/build_issue_66_chat_conversion_matrix.py) 与 [矩阵](../evidence/issue-66/chat-conversion-matrix.json) 同步支持/丢弃条件、显式缓存拒绝和写入用量说明，绑定缓存策略源码摘要。 |

当前已验证能力表包含 `https://api.openai.com/v1/responses`、`https://api.openai.com/v1/chat/completions` 和已保留原生 key 的 `https://chatgpt.com/backend-api/codex/responses`。公开 API 依据是实际抓取的 [OpenAI Chat create reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) 与前述 Prompt caching 文档；订阅端点依据是已有原生透传契约。未知第三方端点的同协议请求保持原样，其跨协议 key 仍按未知能力记录丢弃。进一步开放端点需要该端点的字段支持证据。

验证结果：新增 [缓存边界测试](../../tests/test_gateway_prompt_cache.py) 48 项，以及 exchange 丢弃事件回归；已有测试同步新的接口参数与有界别名分配行为。最终完整 Python core 为 **2,168 passed、169 skipped、263 subtests passed（55.01s）**。运行使用临时 `CODEX_HOME`；回环服务器和临时 Git 签名测试获得沙箱所需权限后通过，没有关闭 hook 或签名。旧环境失败中的矩阵变化通过更新真实能力说明解决，并非仅替换源码 hash。

需求审查无阻塞项。规范审查发现新别名摘要过长会导致 32 字符工具名端点失败，已恢复原有 10 位 hex 长度，并补上 namespace/custom/search 三类工具的短名称端点回归；同时补齐原始缓存 key 的递归脱敏及其回归。两项修正经增量复审确认关闭，无新增阻塞项。上述完整测试覆盖最终修正；测试结束后逐项核对 18 个源码、测试和协议证据文件的 SHA-256，与已审查候选一致。

Report-only quality gates 已执行，0 个 parse errors；该启发式仍报告既有噪音，并将实际由 `route_plan` 调用的 `cache_key_policy_for_endpoint` 列为可能死函数，人工核对为误报，未添加忽略规则。`git diff --check` 通过。未执行 Windows synthetic 分区、Rust/UI 或真实供应商 A/B；本次没有修改这些实现边界，节省金额仍未实测。

源码变更需重启使用该 checkout 的 Gateway 进程后加载；本次没有重启运行实例。旧别名算法切换到新算法时可能发生一次缓存前缀变化，随后增量工具变化不再使所有已有工具改名。无需迁移用户会话或清理服务端缓存。
