# Codex rust-v0.145.0 Sub-agent V2 对 CodexHub 的影响评估

日期：2026-07-22
范围：OpenAI Codex rust-v0.145.0，重点为 Multi-agent / Sub-agent V2，以及与 CodexHub Gateway、配置 overlay 和历史合并的交叉影响。

## 结论

这次 release 不是首次引入 V2 协议。V2 的任务路径、邮箱和六个 collaboration 工具在 rust-v0.144.0 已经存在；rust-v0.145.0 的核心是把这套体验稳定化，包括统一配置、子代理模型和推理级别、角色冷恢复、父线程所有权、导航和分页历史。

对 CodexHub 的影响分为三档：

1. Official/OpenAI 透明路由：低风险。该路径不应重写 Codex 原生工具，保持透明即可。
2. 第三方模型继续使用 V1：短期行为不变。CodexHub 当前的 schema、修复器和调度状态机全部是 V1。
3. 第三方模型进入 V2：高风险，当前不兼容。V2 不是 V1 的字段改名，而是另一套寻址、通信、等待和所有权模型。当前 Gateway 如果把 V1 repair 应用于 V2，会生成无效调用或破坏原生工具计划。

另一个 P0 风险是统一历史。rust-v0.145.0 更依赖 thread_spawn_edges、agent_path 和分页历史投影，而 CodexHub 当前跨 home 合并只复制 threads 表，并会在 JSONL 分叉时拼接记录。前者会丢失 V2 子代理拓扑；后者可能破坏分页历史 ordinal 和 SQLite projection offset。

因此，不建议现在对第三方路由自动开启 V2。先做版本识别和 fail-closed，再补 V2 原生透传/独立状态机与历史兼容。

## rust-v0.145.0 实际改变了什么

### 1. V2 从实验能力进入稳定阶段，但仍不是全局默认

PR #34383 将 multi_agent_v2 标为 Stable，同时保留 default_enabled = false。这里的“默认关闭”只针对全局 feature flag；模型元数据仍可指定使用 V2，所以不能据此认为用户不会进入 V2。

CodexHub 已经固定了相同的模型元数据：

- gpt-5.6-sol：V2
- gpt-5.6-terra：V2
- gpt-5.6-luna：V1

这说明模型目录本身基本对齐；真正缺口在 Gateway 的 V2 工具与生命周期适配。

### 2. 配置统一到 [agents]

PR #33550 将常用设置收敛到 [agents]：

- enabled
- max_concurrent_threads_per_session
- default_subagent_model
- default_subagent_reasoning_effort
- role 配置

max_threads 保留为 max_concurrent_threads_per_session 的兼容别名。V2 开启时具有 backend 选择优先级。max_depth 只约束 V1；旧的 CSV agent jobs 已在 #34413 删除，job_max_runtime_seconds 等旧字段仅以 no-op 形式继续被解析。

PR #33631 补齐 spawn 的模型和 reasoning 默认值。优先级是显式 spawn 参数、[agents] 默认、父线程当前配置；应用 role 后还会重新校验 reasoning effort。PR #32751 又限制子代理只能切换到支持当前 V1/V2 backend 的模型。

### 3. 冷恢复成为正式生命周期的一部分

PR #32837 在根线程冷恢复时重建后代的 path、nickname 和 role 身份，并按需加载原线程。PR #33657 进一步恢复角色定义的 instructions、model、provider、reasoning effort，同时保留运行时 cwd、审批和权限配置。

这使 thread_spawn_edges、threads.agent_path 和角色元数据从“展示信息”变成恢复正确性的必要状态。

### 4. V2 子线程由父线程拥有

PR #33841 增加 Thread.canAcceptDirectInput。V2 spawned thread 由父代理控制，直接 turn/start 或 turn/steer 会被拒绝；TUI 对这类线程只读。V1 子线程仍可直接输入。

未来如果 CodexHub 提供线程选择或输入 UI：

- false 必须作为强制只读；
- true 可直接输入；
- null 表示旧 app-server 未提供能力，不能武断当作 true。

### 5. 分页历史覆盖子代理

release 同时引入实验性分页线程历史。PR #33432 让子代理继承父线程的分页模式与模型上下文，并记录 inherited prefix 边界；SQLite 投影保存 thread_turns、thread_items 和 projection checkpoint。

JSONL 仍是耐久来源，但投影依赖 byte offset 和 ordinal 连续性。任何 JSONL 重写都必须考虑投影失效和重建。

## V1 与 V2 的协议差异

| 语义 | V1 | V2 |
| --- | --- | --- |
| 命名空间 | multi_agent_v1 | collaboration |
| 寻址 | agent_id / UUID | 相对或 canonical task path |
| 创建 | spawn_agent + fork_context | spawn_agent，必需 task_name、message；使用 fork_turns |
| 普通消息 | send_input | send_message，只入队，不启动新 turn |
| 启动后续任务 | resume_agent / send_input 组合 | followup_task |
| 等待 | wait_agent(targets) 返回目标状态/内容 | wait_agent() 等 mailbox 活动、用户 steering 或 timeout |
| 中断 | send_input(interrupt=true) 等 | interrupt_agent |
| 列表 | 无对应核心工具 | list_agents |
| 关闭 | close_agent | 无 close；agent 身份可持久化并按需重载 |

因此，V2 不能通过以下方式兼容：

- 把 task_name 政名为 agent_id；
- 把 fork_turns 改成 fork_context；
- 给 wait_agent 强加 targets；
- 在完成后自动 close_agent；
- 从 wait_agent 输出里提取 V1 状态 map；
- 直接向 V2 child thread 发 app-server 输入。

## 对 CodexHub 的具体影响

### Gateway 的第三方 sub-agent repair：高风险

当前以下模块只认识 V1：

- src-python/codex_proxy.py：硬编码 multi_agent_v1 工具发现、schema、别名和 repair；
- src-python/subagent_protocol.py：只解析 spawn/wait/close/resume/send_input 和 agent_id；
- src-python/subagent_state.py：强制 V1 的 spawn → wait → close 生命周期；
- src-python/subagent_scheduler.py：注入 fork_context=false、targets 和 close_agent；
- src-python/codex_semantic_adapter.py：只识别 V1 五个函数。

而第三方 Codex App 路由会应用 REPAIR_CODEX_SUBAGENT。这意味着，只要模型能力或用户配置令该请求走 V2，当前修复器就可能主动把正确的 V2 tool plan 改坏。

建议：

1. 在任何 schema 注入或 repair 前解析实际 multi_agent_version。
2. V1 继续走现有状态机。
3. V2 未实现前 fail closed，并给出明确诊断；不要静默降级或注入 V1 参数。
4. 最终优先做 capability-driven 的原生 Responses/tool passthrough；只有供应商确实不能原生转发时，才使用版本化适配器。

### Official 路由：低风险

Official 路由当前保持透明、不做 sub-agent semantic repair。这正是理想行为。rust-v0.145.0 不要求重写 official 请求，也不应为了“统一”而把第三方 V1 修复器加到 official 路径。

### 模型目录：基本已对齐

config/official_model_catalog_metadata.json、src-python/catalog_sync.py 和 src-tauri/src/models.rs 已把 Sol/Terra 固定为 V2、Luna 固定为 V1。建议下一次 catalog refresh 更新 source revision，并保留校验。

第三方生成模型通常没有 multi_agent_version 元数据，当前会落回 V1；这暂时降低了即时事故概率，但不是长期兼容保证。不得把“现在多数第三方仍为 V1”当成 V2 支持。

### 配置 overlay：设计上可透传，但缺少回归证明

src-python/config_overlay.py 只剥离 Gateway 管理的 provider/top-level 字段，并只修改少量 [features] flags；未知表通常会保留。因此 [agents] 和 role 表按代码检查应能往返。

仍应增加测试，覆盖：

- [agents] 的 enabled、并发、默认模型和 reasoning；
- 嵌套 role / config_file；
- [features.multi_agent_v2]；
- connect、takeover、restore 后字节级或语义等价；
- 不自动开启 V2。

### 统一历史：P0 数据完整性风险

src-python/history_consolidate.py 的 merge_state_db 只枚举并复制 threads 表的共同列，不复制 thread_spawn_edges。跨 home 导入后，子线程记录可能还在，但父子拓扑不存在，根线程冷恢复就无法还原完整 agent 树和角色路径。

另外，当前 merge_session_lines 在 divergent branch 上返回 source + active_extra。对分页历史而言，这可能产生重复或乱序 ordinal；重写 JSONL 也会使原有 projection byte offset 失效。当前代码只识别 state_5.sqlite，不处理独立的 thread_history_1.sqlite。

建议：

1. 合并 state DB 时事务性迁移 threads 与 thread_spawn_edges，并先检查父子 FK 完整性。
2. 对分页 JSONL 只允许严格 prefix/append；发现分叉时 fail closed，备份并要求人工选择，不再自动拼接。
3. Codex 停止写入后再迁移；重写过的线程应使对应 projection 失效并从 JSONL 重建。
4. 不要跨 home 盲拷贝 projection checkpoint；如果无法证明 offset/ordinal 一致，以重建为准。
5. 补 V2 root + child + grandchild、冷恢复、角色恢复和分页历史的端到端夹具。

## 建议实施顺序

### P0：升级前保护

1. 为第三方路由加入 backend/version gate；V2 未支持时明确 fail closed。
2. 禁止 V2 请求进入 V1 REPAIR_CODEX_SUBAGENT、subagent_state 和 scheduler。
3. 修复统一历史对 thread_spawn_edges 的遗漏。
4. 分页 JSONL 检测到分叉时停止自动合并。
5. 不在 CodexHub UI 或默认配置中自动开启第三方 V2。

### P1：完整支持

1. 先完成 capability-driven native Responses passthrough。
2. 如仍需要 Gateway-owned compatibility，建立独立的 V2 schema/state machine，覆盖 spawn_agent、send_message、followup_task、wait_agent、interrupt_agent、list_agents。
3. 以 task path 和 mailbox 为核心建模，不复用 V1 agent_id/close 状态。
4. 加入 [agents] 与 role 配置 overlay 回归测试。
5. 建立 rust-v0.145.0 实机矩阵：Official V1、Official V2、第三方 V1、第三方 V2、cold resume、分页历史。
6. app-server 消费方尊重 canAcceptDirectInput。

### P2：产品化

1. 在 UI 展示 sub-agent 默认模型、reasoning、并发和角色配置。
2. 为 agent tree、canonical path、liveness 和只读 child thread 增加导航。
3. 补 release 同批 audio input/tool output 的透明转发或明确 fail-closed 测试。
4. 复核 Windows sandbox 恢复手册，但不要仅凭 release note 删除现有 fallback。

## 与现有 issue 的对应

- #57：继续完成第三方 provider 的模型能力与工具暴露 spike；需要把 multi_agent_version 纳入能力事实。
- #58：capability-driven native Codex Responses passthrough，是 V2 最低维护成本的主路径。
- #59：只有 #57/#58 证明原生行为后，才退休 Gateway-owned schema 与调度。
- #64：直接更新为 rust-v0.145.0 的 V1/V2 runtime evidence 目标，并加入 task path、mailbox、cold resume、角色、canAcceptDirectInput 和分页历史。

现有 issue 已覆盖协议工作，不建议创建重复 issue。历史完整性风险若 #64 不接实现范围，则应单独拆一个 ready-for-agent 任务。

## 验收标准

升级/适配完成至少应证明：

- Official 请求工具计划在 Gateway 前后不变；
- 第三方 V1 现有工作流无回归；
- 第三方 V2 不会进入 V1 repair，未支持时错误明确；
- V2 spawn 的 task_name、fork_turns、model、reasoning 和 role 不丢失；
- send_message 不误启动 turn，followup_task 能启动后续 turn；
- wait_agent 不被强加 targets，且能处理 mailbox/user steering/timeout；
- V2 child thread 不接受直接输入；
- restart 后 root 能按 canonical path 找回原 child，角色和权限不丢；
- 统一历史迁移后 thread_spawn_edges 完整；
- 分页 JSONL 无 ordinal 分叉，projection 可安全重建。

## 证据与限制

本评估对比了 rust-v0.144.0 与 rust-v0.145.0 的官方源码和 PR，并检查了当前 CodexHub 工作树。当前机器 PATH 中的 codex CLI 是 0.144.5，因此还没有在本机完成 0.145 的端到端运行证据；这正是 #64 应补的内容。结论中的协议和持久化判断来自 tag 源码，运行兼容性判断来自 CodexHub 当前实现。

## 主要来源

- [rust-v0.145.0 release](https://github.com/openai/codex/releases/tag/rust-v0.145.0)
- [V2 在 rust-v0.144.0 已存在的工具注册](https://github.com/openai/codex/blob/rust-v0.144.0/codex-rs/core/src/tools/spec_plan.rs#L786-L838)
- [#33550：统一 multi-agent 配置](https://github.com/openai/codex/pull/33550)
- [#33631：spawn 默认模型与 reasoning](https://github.com/openai/codex/pull/33631)
- [#33657：冷恢复角色](https://github.com/openai/codex/pull/33657)
- [#33841：父线程所有、子线程只读](https://github.com/openai/codex/pull/33841)
- [#34383：V2 标为 Stable、仍默认关闭](https://github.com/openai/codex/pull/34383)
- [#33432：子代理分页历史](https://github.com/openai/codex/pull/33432)
- [V2 配置结构](https://github.com/openai/codex/blob/rust-v0.145.0/codex-rs/config/src/config_toml.rs#L678-L728)
- [V2 spawn model/reasoning 解析](https://github.com/openai/codex/blob/rust-v0.145.0/codex-rs/core/src/tools/handlers/multi_agents_common.rs#L244-L299)
- [thread_spawn_edges schema](https://github.com/openai/codex/blob/rust-v0.145.0/codex-rs/state/migrations/0021_thread_spawn_edges.sql)
- [分页历史 SQLite schema](https://github.com/openai/codex/blob/rust-v0.145.0/codex-rs/state/thread_history_migrations/0001_thread_history.sql)
- [CodexHub #57](https://github.com/NOirBRight/CodexHub/issues/57)
- [CodexHub #58](https://github.com/NOirBRight/CodexHub/issues/58)
- [CodexHub #59](https://github.com/NOirBRight/CodexHub/issues/59)
- [CodexHub #64](https://github.com/NOirBRight/CodexHub/issues/64)
