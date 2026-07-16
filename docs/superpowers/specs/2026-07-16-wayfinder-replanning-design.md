# Wayfinder 可靠性优先重规划设计

日期：2026-07-16

状态：设计已逐段批准，等待书面规格复核

适用地图：[GitHub Issue #147](https://github.com/NOirBRight/CodexHub/issues/147)

## 1. 背景

当前 Wayfinder #147 按 0.1.6–0.3.0 功能主题组织工作，但它与最新问题台账、里程碑优先级和真实执行能力已经不一致：

- 仓库有 93 个 Issues，其中 60 个 open、33 个 closed；
- #147 创建后新增了 #155–#157，地图没有完整收录最新 P0；
- 现有 `Third-party model agentic reliability` 里程碑声明可靠性最高优先，但 #147 先安排 Stable/Developer 发布渠道；
- #147 要求所有 Worker 使用 GLM-5.2/max，而 #156 已证明该模型的 Worker 回调、工具发现和执行绑定无法满足可靠委派；
- #8 的评论称实现已合并到本地 `dev`，但提交 `400d19bb` 不在当前 `dev` 历史，对应模块也不存在；
- 旧 Beta 双安装规格仍留在仓库，但产品决策已改为单安装、共享状态的 Stable/Developer 模型；
- #147 原有的 main→dev 对账前置条件已由 PR #158 完成，当前基线是 `dev@1d073861`。

本设计把 Wayfinder 改造成门禁驱动的可靠性发布列车。它不实现产品代码，也不领取任何 Issue；GitHub 写入将在规格复核和实施计划批准后执行。

### 1.1 实施范围边界

本规格对应的下一份实施计划只负责 GitHub Wayfinder 迁移：重写 #147、建立/调整 milestones、修正 Issue 归属与依赖、拆分已批准的混合所有权 Issue，并验证新的 frontier。它不会把 0.1.6–0.2.x 的产品代码合并成一份实施计划。每个产品 Issue 继续以自己的 GitHub 验收合同为边界；需要多步设计的 Issue 在进入 frontier 后单独执行 spec→plan→implementation 周期。

## 2. 目标与优先级

### 2.1 优先层

1. **Codex 内部可靠性**
   - 执行控制面；
   - Official GPT 可靠性；
   - 第三方模型分级认证。
2. **现有外部客户端可靠性**
   - ZCode、OMP、OpenCode、Pi 等已管理客户端。
3. **新功能**
   - Stable/Developer 渠道；
   - Provider 预设、凭证和 OAuth；
   - Claude Code Messages；
   - Imagegen；
   - AgentProvider。

### 2.2 分层门禁

- 只有当前层持续补充新任务。
- 较低层已认领任务可以完成当前约定边界，但不继续派生。
- 凭证或隐私泄露、数据损坏、不可恢复迁移、非幂等副作用、异常费用和当前 Gate 的基础阻断可以越级。
- 地图、标签、依赖和里程碑更新不等于领取任务。

### 2.3 非目标

- 不以大爆炸方式重写 Python Gateway 或通用协议中间表示；
- 不把 Claude Code/ACP 当成普通 ModelProvider；
- 不在证据不足时启用 Responses-to-Chat 或原生 Responses 快速路径；
- 不因为 endpoint 名为 Responses 就宣称完整 Codex 兼容；
- 不用 Hidden Subagent、`codex exec`、Background 进程或 Inline 执行冒充 Sidebar-visible Worker；
- 不修改 Codex SQLite 或合成 Task/rollout 状态。

## 3. Wayfinder 治理模型

### 3.1 单一地图

#147 继续作为唯一 Wayfinder Map。地图正文保存目的、优先层、版本 Gate、已做决策、当前 frontier、Fog 和非目标。GitHub Issues、原生依赖、assignee、milestone 和 PR 是唯一持久工作状态。

### 3.2 Issue 角色

每个 open Issue 必须归入一个主要角色：

- **Gate owner**：定义版本退出条件；
- **Fix**：修复已证明的缺陷；
- **Evidence**：生成可重放证据或能力矩阵；
- **Decision**：需要 Maintainer 作 GO/PARTIAL/NO-GO 或产品选择；
- **Support**：诊断、发布、恢复或审计能力；
- **Parking**：有效但不属于当前优先层。

不能保留地图外孤儿、被新证据取代的调度假设，或评论和当前分支互相矛盾的完成状态。

### 3.3 Frontier

候选必须同时满足：

```text
open
+ 当前 milestone
+ ready-for-agent
+ unassigned
+ 无 open blocker
+ 无 hotset 冲突
```

选择顺序是 P0 Gate blocker、依赖图中最早的未阻塞节点、地图顺序，以及同级情况下能最早缩小后续风险的独立任务。`ready-for-human` 不进入自动 frontier，但必须显示在 Gate 的 Fog/Decision 区。

## 4. 发布列车

### 4.1 0.1.6 — Codex 执行控制面可靠性

核心依赖：

```text
#139 Gateway 生命周期单一协调器
  → #143 两秒内取消和退役
    → #112 完整退出与更新重启清理
```

并行 Gate：

- #156：Sidebar-visible third-party Worker、双向通信和结果回执；
- #141：Desktop 重启和 Task 消失的首个失败边界；
- #138：Task 活动中的命令、diff 和进度可审计性；
- #150：合并 usage probe，避免重复冷启动 app-server；
- #149：Rust/Python 跨语言锁所有权和崩溃恢复；
- #151：工作区根目录迁移决策，默认不隐式导入；
- #111：Windows 自动启动注册的真实状态和幂等性。

退出条件：

- Gateway 子进程、监听器、PID 和 UI 状态一致；
- Sidebar-visible Worker 支持完整父子与 Task 间通信；
- Worker 正确经历 Active→Done/Failed/Cancelled；
- 停止、退出、更新和重启不留下工作、子进程或错误所有权；
- Desktop 重启原因被分类，真实 Task 可恢复且不重复创建；
- 活动记录足以审计消息、命令、diff 和终态。

### 4.2 0.1.7 — Official GPT 可靠性

范围：#114、#18→#19→#20、#21、#104、#109 的决策状态和 #157。

退出条件：

- Official GPT 主 Task、Visible Worker、Task 通信和工具循环通过；
- 长流、取消、下游断开和 Gateway 退役有一致分类；
- Official 模型目录、上下文预算和自动压缩来源一致；
- Official context cap 不覆盖第三方模型；
- 未证明由 CodexHub 引起的 Desktop、网络或上游问题不被宣称为已修复。

#109 在一手产品政策矛盾解决前保持 `needs-info`；它不阻塞其他 Official GPT 可靠性工作。

### 4.3 0.1.8 — 第三方模型分级认证

可靠性基础：#17、#22，以及 0.1.7 已完成的 #18–#21。

能力 DAG：

```text
#61 RoutePlan
  → #62 Runtime/Gateway 身份捕获
      → #63 tool_search
      → #65 Provider/Model 能力矩阵
      → #66 Responses→Chat 转换矩阵
      → #63 → #64 Task/协作生命周期
  #63 + #64 + #65 + #66
      → #67 人工 GO/PARTIAL/NO-GO
          → #58 能力驱动原生 Responses
              → #59 仅在获批范围退役兼容逻辑
```

#57 保留为父 Gate。每个获认证模型必须单独通过 Task、Visible Worker、Task 间通信、工具发现/执行、协议、长流、取消、并发、重试、恢复和模型专属上下文预算。

### 4.4 0.1.9 — 现有外部客户端可靠性

范围：

- #8：重新实施或恢复 Gateway client adapter 模块化；
- #28：拆分为客户端探测/目录性能和卸载清理；前者属于 0.1.9；
- #83：区分 Provider 持久化失败、保存后刷新失败和 endpoint 测试失败；
- #153：ZCode 三文件事务与崩溃恢复；
- #154：推理级别忠实导出；
- #155：OMP YAML 字符串类型安全。

退出条件是 ZCode、OMP、OpenCode 和 Pi 分别完成 preview/apply/readback/restore，配置身份和真实请求一致，部分写入与并发编辑不产生假成功，并且不发生静默 Official fallback 或凭证泄露。

### 4.5 0.1.10 — 既有产品可靠性收尾

范围：#86、#87、#88、#113、#115、#126，以及 #28 的卸载清理部分。该版本只收敛既有行为，不以修复为由加入新产品功能。

### 4.6 0.2.x 以后 — 新功能

只有前述可靠性 Gate 通过后才补充：

1. Stable/Developer 渠道：#148→#152；
2. Provider 预设与认证：#71、#89、#90、#91、#92、#93、#94；
3. Claude Code 下游客户端：#73、#74、#75、#76、#77、#78；
4. Imagegen：#68，先完成授权和安全边界；
5. AgentProvider：#85，继续与 ModelProvider/Gateway 分离。

## 5. 模型能力认证

### 5.1 认证键

认证针对具体运行组合，而不是 Provider 品牌：

```text
Codex build
+ CodexHub version
+ provider_id
+ model_id
+ upstream format
+ route mode / codec
+ tool exposure profile
```

同名模型在不同 Provider、协议路径或 codec 上分别认证。Codex build、Provider schema、协议、codec、Task API、工具注册、上下文或推理元数据变化后，相关认证进入待复验。

### 5.2 支持等级

对外只使用：

- **Supported**：完整门禁通过；
- **Experimental**：部分证据，明确列出缺失能力；
- **Unsupported**：已证明不兼容，或未知语义要求失败关闭。

内部决策使用 GO/PARTIAL/NO-GO。PARTIAL 不得在 UI、文档或发布说明中显示成完整支持。

### 5.3 认证维度

| 维度 | 最低证据 |
| --- | --- |
| 运行身份 | 请求和实际模型、Provider、推理级别一致 |
| Task 生命周期 | create/fork/read/list/continue/handoff/complete |
| Task 通信 | 父子、同级定向消息、回执、顺序和结果聚合 |
| Visible Worker | 独立 sidebar Task/thread/worktree 和 Active→Done |
| Hidden Subagent | spawn/send/wait/interrupt/result/cleanup，独立记录 |
| 工具发现 | Direct、Deferred、`tool_search`、hosted |
| 工具执行 | declaration、call、result、ID、history |
| 协议 | 请求、响应、SSE、错误终态、未知 item |
| 传输 | 长流、断开、取消、并发、重试、恢复 |
| 上下文 | 原始窗口、有效窗口、自动压缩来源 |
| 可审计性 | Task 活动、命令、diff、进度和终态 |

证据先保存在 Issue、版本化 fixtures 和 `docs/evidence/`。在 #61/#65 证明需要前，不提前引入新的产品数据库。

## 6. Visible Worker 与 Hidden Subagent

### 6.1 产品定义

本路线图中的委派实施默认指 **Sidebar-visible Worker**：原生创建的独立 Codex Task，具有可发现 thread、隔离 worktree、有效绑定读回、双向通信、确认回执和终态。

Hidden Subagent 是独立能力测试面。即使它成功，也不能替代 Visible Worker 验收。Inline 执行也不能被描述成 Subagent-Driven 或 Visible Worker-Driven。

### 6.2 #156 归属

[#156](https://github.com/NOirBRight/CodexHub/issues/156) 已记录第三方 GLM Worker 无法回调、重复空 `tool_search` 和执行绑定不可证明的问题。截图补充证据已脱敏记录在 [comment 4993730159](https://github.com/NOirBRight/CodexHub/issues/156#issuecomment-4993730159)：`spawn_agent` 返回 `unsupported call`，没有创建委派子任务，后续 Inline 修改不能算 Subagent-Driven，Hidden Subagent 也不能替代 Sidebar-visible Worker。

#156 应拆分为：

- 保留 ready-for-human 的 Host/runtime-only 父 Gate，拥有 Sidebar-visible third-party Worker materialization、有效绑定读回、双向通信、结果回执、显式 unsupported 终态和 Active → Done 可见性；
- 创建两个 CodexHub 本地子 Issue：#159 限制重复空 `tool_search`、产生结构化 unavailable 终态并防止请求/token 放大；#161 保留 Worker selector/codec，并用受支持的有效 agent/model/reasoning 读回 fail closed 地验证绑定；
- #64 在 #156 之后验证完整 Hidden Subagent 与 Visible Worker 矩阵，不把两个表面合并。

### 6.3 当前实施 Worker

当前 Wayfinder 使用 GPT-5.6 Terra/max 或 GPT-5.6 Luna/max 的 Sidebar-visible Workers。每次派发必须明确选择模型并读回有效绑定；两者不能静默互换。如果一个模型的创建或通信失败，该 lane 停止，可以重新明确选择另一个模型并重新 preflight。

第三方模型在 #156 和对应能力认证通过后加入合格 Visible Worker 池。这不阻塞当前路线，因为 Terra/Luna 可以实施 #156 和其他 0.1.6 工作。

## 7. 执行与失败处理

### 7.1 Lane

| Lane | 用途 | 是否算委派实施 |
| --- | --- | --- |
| Visible Worker | Sidebar-visible Task/Worker，独立 thread/worktree | 是，默认 |
| Inline | Orchestrator 在当前任务直接执行 | 否 |
| Hidden Subagent | 验证隐藏协作表面 | 否，不替代 Visible Worker |

### 7.2 Preflight

领取和编辑前必须证明：当前 frontier、无 blocker/assignee/hotset 冲突、精确 base SHA、隔离 worktree、真实 Task materialization、有效模型/Provider/reasoning/Worker 读回、父→Worker 测试消息、Worker→父回执，以及权限 profile。

### 7.3 创建与恢复

```text
create request
    ├─ materialized → 读回 Task/worktree/binding → 通信 preflight
    ├─ rejected → 明确失败，不创建第二个 Worker
    └─ uncertain → 最多一次原生发现/对账，不重复 create
```

- 回调缺失时在编辑前停止；编辑后出现则保留 worktree/WIP；
- 模型绑定不一致时立即停止；
- 同一精确空 `tool_search` 最多两次，之后产生 classified unavailable；
- Task 消失或 Desktop 重启后通过原生 list/read 恢复，不能创建同名替代；
- 超时先 interrupt 并确认终态，旧 Worker 仍可能编辑时不能启动继任者；
- 交接前必须证明原 Worker 终止、所有权明确并保留有用 WIP；
- 不自动修改或删除 Codex 内部 Task 数据。

一个 Issue 同一时间只有一个编辑者、一个 lane 和一个 worktree。Worker 完成后由 Orchestrator 验证 commit、测试、PR 和回执，再更新 GitHub。

## 8. 验证与发布

### 8.1 Issue 级验证

严格遵守 `docs/agents/verification-policy.md`：fast、standard、strict 使用最高适用等级。Python、Rust、Frontend 只运行受影响边界的本地完整套件一次；GitHub Actions 是 PR 到 `dev`/`main` 的最终自动门禁；`report_quality_gates.py` 始终非阻塞。

### 8.2 Worker 证据

每个真实 Worker 证据记录请求/实际模型、reasoning、Task/thread materialization、worktree ownership、双向回执、终态、修改文件和验证结果。不得记录凭证、私有路径、Task ID、callback 地址、prompt 或完整响应。

### 8.3 版本门禁

- **0.1.6**：Terra 和 Luna 各一次 Visible Worker 双向通信控制；GLM-5.2 在 #156 后完成只读 preflight 和一次受限真实任务；生命周期与 packaged Windows smoke 通过。
- **0.1.7**：Official 七模型矩阵、Visible Worker、Task 通信、长流/取消/断开和上下文隔离通过。
- **0.1.8**：每个申请 Supported 的第三方组合有独立矩阵；#67 零未分类项；#58/#59 只启用获批组合；至少一个失败关闭控制通过。
- **0.1.9**：所有已管理客户端完成 preview/apply/readback/restore、崩溃恢复和无静默 fallback。
- **0.1.10**：既有产品可靠性 Issues 通过各自验收，不扩大到新功能。

### 8.4 候选流程

1. Issue 在独立 PR 中达到本地候选；
2. CI、Orchestrator Standards/Spec review 和安全人工证据并行；
3. 合并到 `dev` 后只验证集成差异；
4. 构建完整候选并运行版本命名的 packaged/manual gate；
5. 全绿才进入 Stable；
6. 核心 Gate 不完整时记录 HOLD，不制造空版本。

在 #148/#152 完成前继续使用现有发布方式，路线图不依赖未实现的 Developer 渠道。

## 9. GitHub 迁移

### 9.1 #147 新结构

```text
Destination
Priority gates
Execution policy
0.1.6
0.1.7
0.1.8
0.1.9
0.1.10
0.2.x+
Decisions so far
Current frontier
Fog / external blockers
Out of scope
```

### 9.2 Milestones

建立或重新分配：

- `0.1.6 — Codex control-plane reliability`
- `0.1.7 — Official GPT reliability`
- `0.1.8 — Third-party model certification`
- `0.1.9 — Managed client reliability`
- `0.1.10 — Existing product reliability`

现有 `Third-party model agentic reliability` 由 0.1.8 接替，保留历史描述但不再作为跨版本活跃调度池。

### 9.3 台账修正

- #155–#157 和 #156 加入 #147；#159 与 #161 作为 #156 的两个本地子 Issue 保持嵌套；
- #8 保持 open，移除已在当前 `dev` 实现的错误假设；
- #28 拆成探测性能与卸载清理；
- #62 在确认真实执行状态后清理过期 assignee/Worker 描述；
- #10/#12 核对 main 的最终证据，状态错误则重开或补充正式关闭证据；
- #109 保持 `needs-info`，不阻塞整个 0.1.7；
- #74 保持 `needs-info` 并移入新功能停车区；
- #156 增加对 #159 和 #161 的 blocked-by；#64 保持对 #156 的 blocked-by；
- 每个 Issue 保持一个 canonical lifecycle label；
- 硬依赖使用 GitHub native blocked-by，其他关系只写 Related。

### 9.4 写入顺序

1. 重新读取 Issues、依赖、assignee、milestone 和 open PR，生成预期变更清单；
2. 更新 #147；
3. 更新 milestones、子 Issue 和依赖；
4. 每次写入后 readback；
5. 重新计算 frontier；
6. 地图迁移期间不自动 assign 或启动 Worker。

## 10. 设计决策摘要

- 采用门禁驱动的发布列车并重新切分版本；
- Codex 控制面、Official GPT、第三方模型、现有客户端、新功能严格分层；
- 当前实施使用 Terra/Luna Sidebar-visible Workers；
- 第三方模型逐组合认证后进入合格 Worker 池；
- Visible Worker、Hidden Subagent 和 Inline 是不同执行表面，不能互相冒充；
- #156 是 0.1.6 的 P0 Host/runtime-only Gate，并拆出两个可由 Terra/Luna 实施的 CodexHub 本地子问题：#159 防止重复空搜索放大，#161 保留 Worker selector/codec 并验证有效绑定；
- #64 在 0.1.8 负责完整协作矩阵；
- 所有 open Issues 必须进入 Gate、支撑队列、决策队列或停车场；
- GitHub 是唯一持久工作状态，Wayfinder 更新本身不领取任务。
