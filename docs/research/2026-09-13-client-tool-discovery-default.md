# xAI 工具名称修复与客户端逐步披露默认值

日期：2026-09-13。按用户要求先修复 xAI 工具调用停止，再验证所有 Provider 默认启用逐步披露的可行性。实现与测试在隔离分支 `codex/xai-tools-deferred` 完成，继承已有工作区改动用于集成验证，未修改运行中的 Gateway 或用户模型目录。

## 实现

### xAI 名称冲突

当选定 Provider 为 xAI、协议为 Responses，且声明了原生 `web_search` / `web_search_preview` 时，为客户端 `view_image` 分配请求级、不冲突的函数别名。沿用已有工具注册表、流式生命周期和历史映射，Codex 收到的函数名称仍为 `view_image`，调用 ID、参数、结果和顺序保持不变。其他 Provider 或没有原生搜索的请求不触发此适配。搜索功能保留。

原始问题和最小化证据见 [原诊断](2026-09-13-ayaspace-thread-early-completion.md)。修复后的真实 Gateway 链路分别验证：

| 场景 | 输入 | 结果 |
| --- | --- | --- |
| 最小复现 | 两工具、996 字节 | 上游调用内部别名，下游恢复 `view_image` |
| 重建原线程历史 | 284 工具、424468 字节、94699 input token | 下游同时返回 `view_image` 和 `exec_command` |

这两次调用仅捕获、不执行；未续跑原线程或操作真机。HTTP 流式回归另验证 added/delta/done/completed、别名碰撞和下一轮历史/结果回放。

### 所有第三方 Provider 默认启用客户端发现

`catalog_sync` 为 Ollama 与通用外部模型生成 `supports_search_tool=true`，覆盖旧 fallback 模板中的 false。通用外部模型仅在路由解析为 `responses_structured` 或 `chat_tools` 时默认开启；显式 `none`、`text_compat` 和无法解析为结构化协议的端点维持关闭；Official 仍沿用官方目录能力，不伪造上游能力。

该字段表示 Codex 客户端的工具发现能力，Gateway 使用函数适配承载搜索调用/结果；并不宣称各厂商原生实现了 OpenAI `tool_search`。

不全局改写 `tool_surface_strategy`。真实客户端捕获表明，启用发现后首轮不再包含 40 个合成 MCP 工具，而提供客户端 `tool_search`。Gateway 的 `eager` 只展开当前已经披露的声明。改为 `deferred_core` 会主动删除旧客户端提供的部分 namespace，两者不是同一个开关。保留现有显式配置，并验证两种策略均能保留搜索命中的 namespace。

### E2E 发现并修复的两个缺口

1. Codex 将命中声明放在 `tool_search_output.tools`，下一轮不会重新加入顶层 `tools`。Gateway 现在将客户端已完成的搜索结果合并到当前工具列表，再走原有声明适配。相同 namespace 合并命中子工具，不展开未命中的工具，不修改客户端结果历史。
2. 已发现工具仍带 `defer_loading=true`，Chat 翻译拒绝这个字段。仅对已提升的声明副本移除此标记，表示此轮已完成披露。

同时保留工具搜索的描述，让模型知道它能搜索哪些工具来源。

## 可重复的 E2E

新脚本 `scripts/e2e_client_tool_discovery.py` 使用真实 Codex 二进制、隔离 CODEX_HOME、候选代码生成的目录、真实 Gateway HTTP 路径和包含 40 个工具的合成 MCP 服务。合成工具只返回固定包裹状态，不访问外部应用。

成功条件同时要求：客户端退出 0、工具执行标记存在、返回值进入后续上游历史、最终输出包含该返回值，以及请求次数在 3–5 次边界内。只输出正确文字或只返回 HTTP 200 不算通过。输出证据只保留计数、布尔值和脱敏分类。

```bash
./scripts/codexhub-python.sh scripts/e2e_client_tool_discovery.py \
  --codex /usr/lib/chatgpt/resources/codex \
  --output /tmp/discovery-responses-chat.json

./scripts/codexhub-python.sh scripts/e2e_client_tool_discovery.py \
  --codex /usr/lib/chatgpt/resources/codex --strategy deferred_core \
  --output /tmp/discovery-deferred-core.json

./scripts/codexhub-python.sh scripts/e2e_client_tool_discovery.py \
  --codex /usr/lib/chatgpt/resources/codex --live-xai \
  --xai-auth-file /home/noirbright/.codex/proxy/xai_auth.json \
  --output /tmp/discovery-xai-live.json
```

| 验证 | 结果 |
| --- | --- |
| 真实 Codex + 受控 Responses upstream | 3 轮；18 → 19 → 19 工具；MCP 实际执行并回传 |
| 真实 Codex + 受控 Chat upstream | 3 轮；18 → 19 → 19 工具；MCP 实际执行并回传 |
| 上述两协议，显式 deferred_core | 两者均通过同一执行/回传验收 |
| 真实 Codex + 真实 xAI Grok | 3 轮；18 → 26 → 26 工具；Grok 自主搜索、调用命中工具、读回结果 |
| 第三方目录默认值 | 覆盖 xAI、Volc、MiniMax、Kimi 双端、CommandCode、OpenCode Go、自定义 Provider、Ollama，以及旧模板 false |

此次真实厂商推理覆盖 xAI；Chat 厂商的网络、认证和模型行为未逐一实测。受控 Chat E2E 验证的是真实客户端/Gateway/Chat 协议/工具执行闭环，不能等同于所有厂商线上验收。

证据：[名称映射](../evidence/client-tool-discovery/xai-name-mapping-e2e.json)、[两协议 E2E](../evidence/client-tool-discovery/responses-chat-e2e.json)、[deferred_core](../evidence/client-tool-discovery/deferred-core-e2e.json)、[真实 Grok](../evidence/client-tool-discovery/xai-live-e2e.json)。

## 初始修复验证记录（重构整合前）

定向协议、目录、发现和名称冲突检查：326 passed，51 subtests passed。新增脚本运行时登记及模块行数检查修正后：37 passed，101 skipped（主机不适用的运行时/平台检查）。最终完整 Python 检查：2623 passed，16 failed，177 skipped，267 subtests passed；耗时 63.96 秒。16 项失败与原工作区基线一致。

完整检查首次有 18 项失败：本次引入的脚本登记和模块行数限制已修正；其余 16 项均在未加入本次修改的原工作区复现，分别涉及现有 strict 字段移除断言、prompt_cache_key 断言、跨模块私有导入和 Issue 66 矩阵漂移。没有更新快照或放宽测试来掩盖这些问题。report-only 质量报告执行成功，parse_errors=0；保留仓库既有报告项。

本次实现未发布、未重启生产 Gateway，也未声称原线程已在生产恢复。运行中的进程和现有客户端仍使用旧版本及旧目录；候选代码生效需要正常发布并重新生成模型目录，客户端加载新目录后建立新的模型会话。

## 审核收尾

整合时保留已声明工具的当前 schema，并仅移除搜索已命中声明副本上的 `defer_loading`（包括已存在的 namespace child、function/custom）。畸形 namespace 搜索结果不参与提升，原始结果历史保持不变。通用外部模型与 Ollama 共用目录能力判断；`auto` 端点按真实路由的 Responses/Chat 尝试保留开启，显式 `none`、`text_compat` 或禁止函数/搜索适配的 capability facts 维持关闭。Ollama metadata 同步携带协议及 capability facts。协议解析复用 `route_plan.external_tool_protocol`，生命周期能力复用 `ProtocolCapabilities.for_protocol`。

上述初始验证的 16 项基线失败已在架构收尾中处理；旧 [candidate-validation.json](../evidence/client-tool-discovery/candidate-validation.json) 明确作为历史记录保留，其源码哈希不代表当前候选。真实厂商证据仍绑定原实验，不借本轮离线复查宣称重新完成所有厂商线上验收。
