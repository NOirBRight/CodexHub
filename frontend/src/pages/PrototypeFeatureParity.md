# 0.2.0 界面重构：源码功能对照

设计已锁定：revision 5 的紧凑桌面框架、顶部导航、浅深双主题和 SVG 标识。当前 revision 6 补齐功能界面与演示状态，保存于 `codex/prototype-ui-020`。**这是可交互原型，不是已接通后端的生产重构。**

对照范围是现有源码向用户暴露的功能，不为内部 API 或兼容字段凭空增加开关。生产组件、API、持久化格式和安装资源未替换。所有新增操作使用内存状态；Provider 目录来自 `config/providers.toml` 的脱敏快照（8 个维护预设、123 个模型）。

## 常用工作区

| 源码能力 | 新位置 | 界面与状态 |
|---|---|---|
| GatewayPage：启动、停止、重启、地址复制 | 常驻服务条 | 独立运行状态、忙碌、失败重试、保留接入配置 |
| ProvidersPage：Codex 接入 / 官方配置 | 概览、客户端的 Codex ↔ CodexHub 连接条 | 连接 / 断开，Gateway 离线区分，连接时启动服务 |
| ProvidersPage：Codex 重启、通道归属 | 连接确认弹窗 | 正在运行、接管 Beta、取消、自动重启不支持、已切换但重启失败 |
| GatewayClientCard / GatewayPage：五类客户端 | 客户端卡片与详情 | 真实图标、路径、连接 / 断开、忙碌、检测不可用、配置偏离、修复、通道接管确认 |
| GatewayPage：刷新客户端与版本信息 | 客户端顶部、详情 | 当前 / 最新版本、尚未检查、检查中、可更新 |
| GatewayPage：通用接入与配置说明 | 其他客户端、客户端详情 | 已保存端口与本地密钥，配置预览、复制，说明受管范围 |
| 原生窗口控制 | 标题栏 | 最小化、窗口尺寸切换、关闭到托盘的模拟，以及打开窗口恢复入口 |

默认展示运行状态、Codex 连接、Provider 余量；概览没有提醒栏。客户端异常只在客户端界面展开。

## Provider 与模型

| 源码能力 | 新位置 | 界面与状态 |
|---|---|---|
| ProviderCatalogPicker / config/providers.toml | 添加 Provider | 全部维护目录、品牌 Logo、搜索、已添加标识、重复账户、自定义服务 |
| ProviderEditor | Provider → 连接配置 | 名称、API Key 显隐 / 复制、Base URL、上游协议、探测、字段校验、保存失败保留草稿 |
| ProvidersPage：启用、排序、删除 | Provider 列表 / 详情 | 启停、上下移动、删除确认，官方源不能删除；新添加项目使用同一套完整管理流程 |
| ProviderModelSection | Provider → 模型 | **直接复用原 ModelSection**：添加、发现、刷新、取消刷新、删除、排序、启用、ID 复制、端点测试 |
| ProviderModelSection：模型详情 | 模型编辑弹层 | **原组件保留**：ID、名称、上下文、视觉能力、思考模式、推理等级与默认等级等原有字段 |
| ProvidersPage：官方模型配置 | OpenAI → 模型 | 官方模型启停 / 顺序、V1 / V2 协作版本、恢复基线、刷新 / 取消；未登录禁用交互 |
| ProvidersPage：官方认证 | OpenAI → 账户与用量 | 打开 Codex、复制登录命令、刷新认证、登录 / 未登录场景 |
| OfficialOpenAIUsageLimitBars | OpenAI / xAI 账户 | **原组件复用**：多窗口额度、剩余比例、重置时间、未知额度占位 |
| OfficialOpenAIUsagePanel | OpenAI → 账户与用量 | **原组件复用**：日 / 周 / 月用量和热图，加载、无用量、查询失败、未授权状态 |
| XaiLoginCard / xAI auth adapter | xAI → 订阅账户 | 设备码、复制、授权页入口、等待 / 取消 / 完成、过期、403 无资格、服务失败、API Key 替代说明、退出与用量刷新 |
| ProviderWorkspace 保存反馈 | Provider 保存 / 关闭 | 草稿保护、继续编辑 / 放弃、失败不丢草稿、部分客户端同步失败与重试、明确 Codex 重启要求 |

API 余额与请求数字是示例数据，不代表真实账户。自定义或新 Provider 的未知额度显示为未知，不伪装成已查询成功。

## 设置字段逐项保留

| 原有字段 / 操作 | 原源码 | 新位置 |
|---|---|---|
| locale | SettingsDrawer | 通用 → 语言，中英界面随保存切换 |
| auto_start_software | SettingsDrawer | 通用 → 开机自启动软件 |
| auto_start_gateway | SettingsDrawer | 通用 → 打开软件后启动 Gateway |
| include_official_models | SettingsDrawer | Codex 与客户端 → 包含官方模型，影响模型统计和概览可见性 |
| auto_sync_clients | SettingsDrawer | Codex 与客户端 → 自动同步已绑定客户端 |
| openai_context_guard_enabled | ProvidersPage | Codex 与客户端 → 上下文保护；全局覆盖冲突 / 状态未知预览 |
| unified_codex_history、手动历史同步 | SettingsDrawer | Codex 与客户端 → 历史对话；忙碌、目录占用、延期、归属冲突、迁移确认、写入失败与重启反馈 |
| gateway_bind_address | Settings / runtime contract | Gateway → 只读 127.0.0.1 |
| proxy_port | GatewayPage | Gateway → 端口，整数 1024–65535，保存后同步到服务条和接入端点 |
| gateway_request_timeout_seconds | GatewayPage | Gateway → 超时，整数 5–600 秒 |
| gateway_client_key | GatewayPage | Gateway → 本地密钥，编辑、显隐、复制、重新生成；与上游密钥分开 |
| Models / Responses / Chat Completions | GatewayPage | Gateway → 三类端点，使用已保存配置并支持复制 |
| gateway_auto_retry_enabled | SettingsDrawer / recovery panel | 请求策略；诊断保留快捷入口 |
| gateway_auto_retry_max_attempts | SettingsDrawer | 请求策略 → 最大重试次数，整数 1–30，关闭重试时禁用 |
| gateway_image_proxy_enabled | SettingsDrawer | 请求策略 → 图片代理 |
| gateway_image_proxy_model | SettingsDrawer | 请求策略 → 从启用 Provider 的视觉模型生成候选，开启时必须选择 |
| 保存、放弃、未保存保护 | SettingsDrawer | 常驻设置页脚；切换主页面 / 分类保留草稿 |
| 应用 / 重启反馈 | SettingsDrawer / App | 运行时设置确认后模拟重启 Gateway；历史变更明确要求重启 Codex；失败保持原配置 |

`normalizeSettings` 保留其他历史兼容、catalog、端点能力、模型排序等内部持久字段。没有现有可见控件的内部字段不被删除，也不新增成误导性的用户开关。

## 统计

`StackedUsageChartShell` 原组件完整复用，源文件没有修改：

- Token / 请求 metrics。
- Provider / 模型 / 客户端拆分。
- 日 / 周聚合。
- 周 / 月 / 自定义双日历范围、月份导航。
- 图例逐系列筛选与摘要重算，悬浮明细。
- 总 Token、请求数、估算成本、缓存输入比例与空数据状态。

原型提供最近 62 天的确定性演示请求。概览数字与 API 余额仍是视觉示例，不作为后端统计准确性证据。

## 诊断与更新

| 原源码 | 新位置 | 界面状态 |
|---|---|---|
| DebugDiagnosticsPanel | 设置 → 诊断 | 滚动窗口、记录大小、快照数、暂停 / 恢复、标记、删除、刷新；暂停禁用标记；离线、加载、延迟、失败、正式构建隐藏预览 |
| RecoveryActivityPanel | 设置 → 诊断 | 自动重试快捷入口、最近恢复记录、展开请求 / 尝试 / 耗时详情 |
| SettingsDrawer / VersionUpdateBlock | 设置 → 关于 | 当前 / 最新版本、检查中、已最新、检查失败、发布说明、安装重启确认、下载 / 安装进度、失败重试、完成反馈 |

## 验收方式与边界

标题栏「交互原型 · 演示场景」控制下一次保存 / 连接的成功、失败、部分同步和重启失败；各详情里的折叠预览控制授权、客户端、历史、诊断和更新场景。默认成功视图保持已批准的设计。

已执行 TypeScript 编译、diff 检查、report-only 质量报告；浏览器代表性检查覆盖目录到 xAI 授权、双主题与语言切换、Gateway 设置确认、失败草稿保留后重试、版本与配置偏离、诊断暂停。原图表和模型编辑的其余能力由原组件复用承接，未声称逐组合端到端测试完成。

真实认证、文件注入、配置持久化、Gateway / Codex 进程操作、诊断数据采集、自动更新安装和原生托盘需要后续生产实现及对应验证等级；当前没有执行这些外部操作。设计锁定及本对照表是实现约束，不能把未接通后端等同于允许删减功能。
