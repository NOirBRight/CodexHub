# OpenCodex provider / model 隔离实现研究

> 研究对象：AITabby/opencodex v1.2.0，快照 commit [efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175](https://github.com/AITabby/opencodex/tree/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175)。仅检查该仓库源代码、测试、README 和 Git 历史。

## 结论

OpenCodex 的“隔离”主要是 **双 app-server 进程 + 回环 HTTP 分流 + 模型目录驱动的逻辑路由 + 会话投影**。它对“网关故障不拖垮官方 GPT”和“不把第三方模型误发给 OpenAI”很有参考价值；但它**不是安全沙箱**：两个子进程与网关仍以同一 OS 用户运行，共享该用户的文件和凭据可读边界。

## 四个声明的实际实现

### 1. 官方 OpenAI 路径

- 全局 model_provider 保持 openai；网关只额外注册 opencodex -> 127.0.0.1:8765。见 [buildManagedCodexConfig()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/gateway.ts#L176-L189)。
- Provider bridge 按 provider 启动独立 Codex app-server 子进程；官方 turn 发给 openai runtime，第三方 turn 发给 opencodex runtime。见 [spawnRuntime()/ensureRuntime()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L1172-L1243) 和 [handleTurnStart()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L1828-L1870)。
- **“直连”不是字面上的直接 TCP 连接。** v1.2.0 给 native runtime 覆盖了一个随机本地 openai_base_url；普通请求先进入 bridge 的短命回环 HTTP server，再透传至 chatgpt.com/backend-api/codex。它绕过了 **8765 第三方网关/适配器**，但没有绕过本地 bridge。见 [nativeRuntimeArgs()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L640-L657)、[nativeUpstreamTarget()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L397-L415) 和 [handleNativeEgressRequest()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L539-L588)。

### 2. 第三方网关隔离

1. classifyProviderModel() 将 openai/...、gpt-*、o*、codex-* 等官方命名优先判为 openai；非 OpenAI 命名空间判为 opencodex；未知裸模型不猜 provider。见 [分类函数](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L147-L190)。
2. thread/start 始终先创建/保留一个原生 canonical thread；如果 UI 选择第三方模型，物理 thread 先用 native default model，对 Desktop 装饰成所选模型。见 [handleThreadStart()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L1602-L1628)。
3. 每个第三方 turn 都读取 canonical thread 的用户/助手历史，启动 ephemeral gateway thread，注入历史后执行；用户输入和第三方回答再通过 thread/inject_items 镜像回 canonical native thread。见 [historyToResponseItems()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L981-L1024)、[beginGatewayTurn()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L1496-L1599) 和 [finishGatewayTurn()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L1245-L1265)。
4. 8765 网关从导入目录获取唯一 provider 和 backend model；官方 ownerless model 优先 native，不存在“providers[0] 兜底”。见 [findCatalogProvider()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/gateway.ts#L2244-L2265) 和 [/v1/responses 路由](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/gateway.ts#L3267-L3327)。

所以它同时具备：

- **进程隔离**：openai/opencodex 两个 app-server child。
- **网络隔离**：官方 upstream 与 127.0.0.1:8765 第三方网关分开。
- **配置/逻辑隔离**：provider 目录、模型所有权和 thread route map。
- **不具备**：OS 权限、文件系统、用户身份或容器级隔离。

### 3. Provider Split Bridge 自动分流

Bridge 是 Desktop 与 app-server 之间的 stdio JSON-RPC 多路复用器。它保存 externalId -> nativeId + selectedModel 路由表（原子写入、0600），从 thread/start、resume、settings/update、turn/start 的模型选择定位 runtime，并把内部物理 thread id 改写成 Desktop 所见外部 id。见 [route 持久化](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L759-L810) 和 [RPC dispatch](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L1906-L1951)。

历史上，provider split 主体在 [3c0d396](https://github.com/AITabby/opencodex/commit/3c0d396aa38e90adc71a08c05184d817585582d6) 加入，1.1.5 checkpoint 是 [18277fe](https://github.com/AITabby/opencodex/commit/18277fed00600e82dcb5bf1d1d36c67161af4ed1)；spawn_agent 网关化在 [90a1a7c](https://github.com/AITabby/opencodex/commit/90a1a7cb74479ee58120f152e7034b5f25848ee1) 引入，最终 native egress/subagent bridge 在 [efc8b3e](https://github.com/AITabby/opencodex/commit/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175) 完成。官方说明见 [README v1.1.5/v1.2.0](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/README.md#L65-L71)。

### 4. Native Subagent Bridge

- Native app-server 的 spawn_agent 生命周期没有被替换；只有 child HTTP egress 带 x-openai-subagent、thread_source=subagent、parent-thread 等元数据时，本地 egress bridge 才把请求送到 127.0.0.1:8765/v1/responses。见 [isNativeSubagentRequest()/nativeEgressRoute()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L359-L386)。
- Gateway 忽略 native child prewarm，优先以 child thread id 绑定路由，从显式 model/Profile 或用户保存的 capability/Profile 调用 TaskRouter.resolve()；路由绑定有 TTL，避免同一 parent 的并发 child 相互覆盖。见 [chooseSubagentRoute()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/gateway.ts#L1668-L1829) 和 [TaskRouter.resolve()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/services/task_router.ts#L391-L448)。
- 选中 native model 后仍透传官方 backend；选中第三方则进入 provider router。Gateway 用响应头回报实际 model/effort/task-id，bridge 只修正 Desktop 子 thread 的显示/effort，不更换主会话 provider。见 [subagent 路由应用](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/gateway.ts#L3193-L3287) 和 [display settings 回传](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L321-L357)。
- 另一条路径是“第三方主模型调用 spawn_agent”：网关向 provider 注入 synthetic tool，消费 tool call 后回调本机 /v1/responses。这类 child 的 view_file/list_dir/exec_command 是网关自己的受限实现，**不是** Desktop 私有工具执行器。见 [dispatchThirdPartySubagent()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/gateway.ts#L1260-L1389) 和 [executeGatewaySubagentWorkerTool()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/gateway.ts#L1191-L1257)。

## 故障行为

- **Bridge 不完整**：macOS 启动器不会在 managed third-party catalog 存在时无 bridge 启动 Desktop；可回退场景只保证 native GPT。见 [launchDesktopClient()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/gateway.ts#L430-L480)。
- **Gateway/provider 不可用**：第三方 turn 返回错误，不会偷偷改走 OpenAI 或其他 provider；native turn 仍可在同一 thread 执行，gateway 恢复后第三方 turn 无需重启 bridge。见回归测试 [离线后切回 native](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/test/provider_split_protocol.test.mjs#L384-L447) 和 [网关恢复](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/test/provider_split_protocol.test.mjs#L535-L602)。
- **不支持 Responses**：只允许在**同一 provider** 内从 Responses 降级为 Chat 协议转换，不换 provider。见 [proxyThirdPartyResponses()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/router.ts#L353-L416) 和 [handleResponses()](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/router.ts#L687-L760)。
- **Subagent 无可用路由**：/v1/responses 返回 400；第三方主模型的子任务全部失败时中止父模型续答。Native egress 上游不可达返回 502，WebSocket 明确 426 回退 HTTP。见 [无路由分支](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/gateway.ts#L3203-L3211)、[subagent continuation](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/router.ts#L422-L450) 和 [native egress error](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/codex-provider-bridge.ts#L494-L536)。

## 安全边界与局限

1. **不是沙箱。** spawn() 只创建普通同用户子进程，没有 container、namespace、chroot、macOS sandbox profile、独立 UID 或文件 ACL。被攻破的组件仍可能读取同用户可读的 ~/.codex 和 ~/.opencodex。
2. **8765 的工作面未做 bearer 验证。** Server 只绑定 127.0.0.1，/api/* 要求 admin token，但 /v1/responses 位于 requireAdmin 范围外。因此远程攻击面较小，但任意同机本地进程可尝试发起模型请求、消耗 provider 配额。见 [API auth 边界](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/gateway.ts#L3072-L3085)、[Responses route](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/gateway.ts#L3193-L3200) 和 [loopback listen](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/gateway.ts#L6651-L6658)。
3. Native egress 的随机 URL path 只能减少误调用，没有独立认证；x-openai-subagent 等元数据是**分流标记**，不是可验证的身份。
4. **第三方数据暴露是功能所需。** Bridge 会把 canonical thread 的用户/助手文本投影给所选 provider；这是 provider 隔离，不是隐私隔离。不能宣称第三方看不到会话上下文。
5. 新 API key 存入 macOS Keychain，providers.json 只保留 credential_ref 并用 0600；但仍兼容旧 api_key 和环境变量，运行时密钥必然进入网关进程内存。见 [CredentialStore](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/services/credential_store.ts#L41-L77) 和 [Keychain/兼容解析](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/services/credential_store.ts#L104-L172)。
6. 设计文档要求不记录 request body；检查的主路由只打印 body keys/model，但 provider 错误响应原文会写入 stderr，上游若回显敏感内容仍可能进入日志。见 [安全不变量](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/docs/PROVIDER_WORKSPACE_REDESIGN.md#L52-L59) 和 [upstream error logging](https://github.com/AITabby/opencodex/blob/efc8b3e2cb84f3a0f67e1281c6b2ab94a012b175/src_v2/server/router.ts#L1214-L1226)。

## 是否值得我们参考

**值得，但要准确定位。**

- 可直接借鉴：官方 provider 始终为全局默认；在单一窄边界做 request-scoped 分流；provider 所有权由显式 catalog 决定；未知模型/无可用 route 失败关闭；第三方 turn 临时化、canonical native history 不变；subagent 只在 child request 边界改路由。
- 如果目标是**可用性和故障域隔离**，这套结构参考价值高。
- 如果要宣称**安全隔离**，还需要独立低权限用户/沙箱或容器、受认证 IPC（Unix socket/named pipe + peer credential）、provider egress allowlist、独立凭据 broker、按 provider 限制历史投影、结构化日志脱敏，以及本地 catalog/route state 完整性保护。

## 与 CodexHub 当前架构的差距

当前稳定分支仍是单 Gateway 数据面：Codex 配置被改成
`model_provider = "custom"`，`base_url` 指向本地 `/v1`；官方和第三方模型都先进入
同一个 Gateway。README 也明确说明 Gateway 停止后，CodexHub 模式下的官方请求同样无法继续。
见 [`config_overlay.py`](../../src-python/config_overlay.py) 的
`build_overlay()` / `build_provider_section()`、[`codex_proxy.py`](../../src-python/codex_proxy.py)
的 `choose_upstream()`，以及 [`README.zh-CN.md`](../../README.zh-CN.md#连接-codex)。

最新本地 `origin/dev` 已经把“选定 route 不可被工具兼容层改变”和“Gateway 不执行工具、
不创建 agent、不成为第二调度器”写入 ADR-0002。OpenCodex 的 Provider Split 主体不违反
前一条，但“第三方主模型 synthetic spawn_agent + Gateway 自己执行受限 worker tools”违反后一条。
这部分不能直接移植到现有 Gateway；若采用，必须作为新的 Agent Runtime / bridge 产品边界
单独决策，而不是悄悄塞进工具协议适配层。

另一个实质差异是历史：CodexHub 当前工具兼容契约要求调用 ID、顺序、流式生命周期和 replay
可逆；OpenCodex 的第三方 turn 通过用户/助手文本投影到 ephemeral thread，再把结果注入
canonical thread。它适合保持聊天连续性，但不能自动视为完整保留 reasoning、tool lifecycle、
usage 和 provider-specific item 的等价实现。

## 建议落地边界

建议先做设计 Spike，不直接移植：

1. **可以采用**：双 runtime 的故障域、显式 catalog ownership、request-scoped immutable route、
   unknown model fail-closed、第三方 Gateway 懒启动/恢复，以及 child request 独立路由键。
2. **不要采用**：无认证的 loopback inference 端点、按裸模型前缀猜 provider、Gateway 执行
   agent tools、静默丢失非文本历史、把随机 URL path 当身份认证。
3. **实现位置**：真正的 OpenCodex 式分流需要位于 Codex Desktop 与 app-server 之间的
   JSON-RPC/process supervisor 控制面；只修改 `codex_proxy.py` 无法让已经进入本地 9099 的
   官方请求获得独立故障域。CodexHub 若做，应优先放在 Rust/Tauri launcher/runtime 层，
   Python Gateway 继续只负责第三方协议数据面。
4. **最小验收**：第三方 Gateway 进程被杀后，已有 canonical thread 的下一次 GPT turn 仍成功；
   未知/冲突模型在发网前失败；不会联系第二 provider；恢复 Gateway 后第三方 turn 可继续；
   native subagent 与第三方 subagent 并发时按 child thread ID 隔离；所有本地 inference IPC 均鉴权。

因此，OpenCodex 方案值得作为 **CodexHub 下一代 split control plane 的参考实现**，但不是当前
Gateway 的一个小改动。若近期目标只是不让第三方适配器崩溃拖垮 GPT，可以先把稳定 split
bridge 与第三方 adapter 拆成两个进程；若目标是其完整的同会话双 runtime 体验，则需要单独
设计 app-server bridge、会话投影语义和 Agent Runtime 权限边界。
