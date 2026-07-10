# CC Switch 的 Codex 路由切换与统一历史实现研究

日期：2026-07-11
对照版本：CC Switch `99e11e0851972e0ef7307be9c328a85b8371531e`；CodexHub `983b3529`

## 结论

CodexHub 与 CC Switch 只在“统一历史使用稳定的 `model_provider = "custom"`”这一数据模型上相同，当前运行编排并不相同。

CC Switch 将三件事分开处理：

1. 普通 Provider 切换：原子写入 live 配置，不控制 Codex 进程，并提示用户重启客户端。
2. 本地代理接管后的上游切换：Codex 始终连接同一个 localhost；只替换代理的 active target，因此是真正的热切换。接管期间禁止切换到官方 Provider。
3. 存量历史统一：用户显式选择后做一次后台迁移；失败不写完成标记，下次启动重试。迁移不关闭或启动 Codex。

CodexHub 0.1.4 将模型目录重载、路由切换和历史修复绑成了一个流程。即使历史检查结果为 `clean`，每次连接或断开仍会关闭并重新启动 Codex。这不是稳定 `custom` 桶方案的要求，而是提交 `58e32789`（`fix: reload Codex models after route switch`）后来引入的回归。

## CC Switch 的实现

### 稳定的历史桶

官方 ChatGPT OAuth 配置在统一历史开启时被投影为：

```toml
model_provider = "custom"

[model_providers.custom]
name = "OpenAI"
requires_openai_auth = true
supports_websockets = true
wire_api = "responses"
```

它不写 `base_url`，认证仍来自官方 `auth.json`。第三方 Provider 同样使用 `custom`，所以新会话的历史桶 ID 不随上游变化。

来源：[codex_config.rs](https://github.com/farion1231/cc-switch/blob/99e11e0851972e0ef7307be9c328a85b8371531e/src-tauri/src/codex_config.rs#L1334-L1499)

### 普通切换与热切换不是一回事

普通切换会原子写 live 配置，但 CC Switch 不关闭或启动 Codex；前端明确显示“请重启客户端”。

来源：[ProviderService::switch](https://github.com/farion1231/cc-switch/blob/99e11e0851972e0ef7307be9c328a85b8371531e/src-tauri/src/services/provider/mod.rs#L2283-L2468)、[useProviderActions.ts](https://github.com/farion1231/cc-switch/blob/99e11e0851972e0ef7307be9c328a85b8371531e/src/hooks/useProviderActions.ts#L251-L289)、[对应测试](https://github.com/farion1231/cc-switch/blob/99e11e0851972e0ef7307be9c328a85b8371531e/tests/hooks/useProviderActions.test.tsx#L196)

本地代理接管后，Codex 的 localhost 地址保持不变。热切换只更新数据库中的当前 Provider、恢复备份以及代理的 active target。官方 Provider 在接管期间被明确拒绝，因此“第三方之间无重启热切换”不能外推为“本地代理与官方直连之间也能无重启切换”。

来源：[hot_switch_provider_inner](https://github.com/farion1231/cc-switch/blob/99e11e0851972e0ef7307be9c328a85b8371531e/src-tauri/src/services/proxy.rs#L2118-L2213)、[热切换测试](https://github.com/farion1231/cc-switch/blob/99e11e0851972e0ef7307be9c328a85b8371531e/src-tauri/src/services/proxy.rs#L5151-L5319)

### 一次性在线历史迁移

官方旧历史只有在统一开关开启、用户选择迁入存量历史且当前 live 配置确实路由到 `custom` 时才迁移。成功后写目录绑定的完成标记；失败不写标记，后续启动重试。

JSONL 迁移：

- 读取前记录修改时间和长度；
- 生成新内容；
- 备份前、原子替换前各检查一次文件未变化；
- 文件发生变化时中止，不覆盖活动文件。

SQLite 迁移：

- 设置 5 秒 `busy_timeout`；
- 使用 SQLite Backup API 在线备份；
- 在短事务中更新 `threads.model_provider`；
- busy 或事务失败时返回失败。

来源：[codex_history_migration.rs](https://github.com/farion1231/cc-switch/blob/99e11e0851972e0ef7307be9c328a85b8371531e/src-tauri/src/codex_history_migration.rs#L190-L272)、[JSONL/SQLite 实现](https://github.com/farion1231/cc-switch/blob/99e11e0851972e0ef7307be9c328a85b8371531e/src-tauri/src/codex_history_migration.rs#L961-L1209)

限制：CC Switch 没有“真实运行中的 Codex App”迁移 E2E；内部互斥锁也只约束 CC Switch 自己。因此这个模式值得采用，但 CodexHub 仍需补充真实 Windows E2E，不能把源码级测试当作绝对安全证明。

## CodexHub 当前实现对照

### 已经相同的部分

- `src-python/config_overlay.py` 使用固定 `PROXY_PROVIDER_ID = "custom"`。
- Gateway 模式写 `[model_providers.custom]` 与 localhost `base_url`。
- 统一历史开启时，官方模式写无 `base_url` 的 OpenAI `custom` Provider。
- `config.toml` 使用临时文件和 `os.replace` 原子替换。

### 不相同且需要修复的部分

1. `frontend/src/pages/ProvidersPage.tsx` 每次连接或断开都会调用 `reconcileAfterRouteSwitch()`。
2. `src-tauri/src/history.rs::reconcile_after_route_switch_with_budget` 即使检查结果为 `clean`，只要 Codex 正在运行就执行 `close_gracefully()` 和 `launch()`。
3. 单元测试 `route_switch_restarts_running_codex_even_when_history_is_clean` 明确把错误行为固化为契约。
4. 提交 `58e32789` 为了“重新加载模型目录”引入了这条自动进程控制；此前历史 clean 不会触发关闭和启动。
5. 前端把路由目标与历史策略混成一个 `targetProvider`：统一历史关闭时，连接 Gateway 仍传 `custom`，会把官方旧历史迁入 `custom`，违背“关闭统一历史后不自动归桶”的设置语义。
6. CodexHub 的历史迁移不是 CC Switch 的同一实现：SQLite 备份前执行 `wal_checkpoint(FULL)`，没有显式的应用级延期结果；JSONL 只比较第一行并原位覆写等长字节。它有备份和重试，但没有完整文件的修改时间/长度乐观并发契约。

修复前的可重复诊断命令：

```powershell
cd D:\Workstation\CodexHub\.worktrees\v0.1.4-codex-compat\src-tauri
cargo test --locked route_switch_restarts_running_codex_even_when_history_is_clean -- --nocapture
```

该旧测试曾以 `1 passed` 固化“历史 clean 仍关闭并重新启动 Codex”的错误行为。`0.1.4-beta.2` 已将契约反转为：路由切换不调用任何 Codex 进程控制接口。

### 0.1.4-beta.2 的安全恢复边界

- 连接、断开和托盘切换只修改路由配置，不再检查或迁移历史，也不关闭、启动 Codex。
- 启动时检查历史漂移；确认存在漂移后才运行一次可延期的在线迁移，不控制 Codex 进程。
- JSONL 在计划和替换前后核对长度、修改时间、文件标识和完整 SHA-256；先从已验证快照备份，再执行同目录原子替换。文件变化或占用时返回 `deferred`，不写完成标记。
- SQLite 使用 5 秒 `busy_timeout`、在线备份和 `BEGIN IMMEDIATE` 短事务；数据库忙碌时返回 `deferred`，下次启动继续。
- `scripts/e2e_history_online_sync.py` 使用 App 管理版 CLI 和隔离 `CODEX_HOME` 验证了运行中的 app-server 不被关闭：第一次在持锁时延期，释放锁后完成，JSONL 追加尾部保持不变。

## 对 0.1.4 任务的修订

优先级最高的阻断修复应改为：

1. 永久移除连接、断开、启动预检和历史迁移中的自动关闭/自动启动 Codex；不再由 CodexHub 控制官方 App 生命周期。
2. 将 `RouteTarget`、`HistoryPolicy`、`CatalogReloadState` 拆成独立状态：路由变化不能隐式触发历史搬迁，目录变化只能产生手动重启提示。
3. 统一历史开启：官方与第三方新会话稳定写入 `custom`；日常路由切换只做只读漂移检查，不迁移旧数据。
4. 统一历史关闭：路由切换不改任何旧历史；仅用户显式关闭开关时，按 ledger 精确还原原官方会话。
5. 存量迁移改成一次性、后台、可延期：JSONL 使用修改时间/长度复核和原子替换；SQLite 使用 busy timeout、在线备份和短事务。busy 或文件变化返回 `deferred`，不写完成标记、不重试循环。
6. Gateway 内第三方 Provider 切换保持 localhost 不变，只更新内存上游，实现热切换。官方直连切换仍只原子写配置，并提示用户手动重新打开 Codex 才能保证 catalog/config 生效。
7. 先增加真实运行中 Codex 的 Windows E2E，再允许在线迁移成为正式功能。

这组修复完成后，才继续模型显示、排序、额度名称、Beta UI、子代理和发布工作。
