# InfoDigest 中断根因与 V1/V2 现状：重新审计

日期：2026-09-10。原任务：`01a06ecb-ba14-7e83-bd30-ef9c18738620`，标题「提升资讯抓取与推送稳定性」。正文时间均为北京时间，JSON 证据保留 UTC。

## 结论与纠正

1. **原任务明确是 V2，不是 V1。** V1 的强制 final 缺陷不能被直接用来解释这项任务的中断。
2. 原任务包含不同性质的停止：**已有 final 的回合结束、与本机异常重启对应的两个未闭合回合、三次显式 interrupted，以及损坏历史造成的长期索引停滞**。不能归并成一种“V2 挂死”。
3. **当前生产 Gateway 与工作区修复候选不是同一份代码。** 生产仍运行 AppImage 内的旧核心模块；候选也仍有可复现的工具归属缺陷。
4. **此前真实 E2E 的验收结论需要撤回。** Harness 自身会缺失初始 fixture、破坏第二回合输入、误认成功证据。旧 Grok/Muse 通过率、提示 A/B 胜负以及“模型虚报成功”等归因，均不能继续作为删除安全的证明。
5. 当前状态应标为：**已有部分确定性修复，但存在开放 P1；真实父任务连续执行尚未有效验收，不具备交付结论。** 这不等于 V2 完全不可用，也不等于必须恢复所有旧补丁。

本轮执行的是诊断与现状评估：新增失败回归、只读取证和报告；**没有修复原始历史、修改生产 Gateway、调整全局模型配置或继续运行有缺陷的付费 Provider E2E**。保留此前未提交实现，不 reset、stash、提交或推送。

## 一、诊断方法与证据边界

按 `diagnosing-bugs` 先建立 RED 反馈：

- 只读 SQLite 游标，再在原 rollout 的对应 byte offset 解析一行：确定性失败。
- E2E 的五个最小回归：全部失败，直接驱动实际 fixture/采证函数，不依赖模型文字。
- 公共 request → body/SSE 入口：普通同名函数被改成 V1 调用，形成新增失败回归。

核对的五个可证伪假设及结果：

| 假设 | 预测 | 结果 |
|---|---|---|
| 回合提前结束 | “继续”之前有 final / `task_complete`，但仍列出工作 | 部分实例成立；不能把所有正常问答结束算作故障 |
| 主机中断 | 原回合缺失终态，附近出现 boot 切换和异常恢复 | 两个历史未闭合回合均吻合；重启发起者未知 |
| 历史索引故障 | projection 恰好停在不可解析行，读回缺失后续历史 | 精确成立，并且现在仍存在 |
| Gateway/V2 错误终止任务 | 相应请求失败后，该回合没有继续活动或终态异常 | 已记录的六次 502 后均恢复同回合工具调用；未找到与硬停止对应的 V2 协议失败证据 |
| 验收误报 | 不运行模型也能让错误 fixture 或用户提示影响判定 | 五个稳定 RED，旧 E2E 不能支持模型/补丁因果结论 |

不在用户机器上人为复现断电或异常重启。系统层采用只读历史关联，未复现具体 NUL 写入机制；因此只确认现存损坏和恢复时间线，不指定写坏进程。

脱敏结构证据：

[analysis.json](/home/noirbright/Workstation/CodexHub/docs/evidence/infodigest-thread-stops-2026-09-10/analysis.json)

其中记录源文件 SHA-256、字节数、逐回合终态、损坏位置、请求错误及其后续活动、生产/候选核心模块哈希。不保留会话正文、工具输出、请求 body 或凭据。

## 二、原任务实际发生了什么

### 2.1 协议与模型不能混为一谈

重新解析时，只计顶层 `response_item`，不递归计入 `compacted.replacement_history` 或 `item_completed` 中重复出现的条目。

- 72 条 `turn_context` 的 `multi_agent_version` **全部为 `v2`**。
- 模型上下文：`gpt-6-astra` 56 条、`gpt-5.6-terra` 6 条、`xai/grok-4.6` 10 条。这里是上下文记录数，不是不同任务数。
- 真实顶层 Collaboration 调用共 **123 次**：8 spawn、75 send_message、31 followup_task、7 list_agents、2 interrupt_agent。
- **这 123 次新生成的 Collaboration 调用都发生在 `gpt-6-astra` 上下文中**，没有 `multi_agent_v1` 调用。后续 Grok 使用 V2 上下文/历史，不等于这些子代理由 Grok 创建。
- 123 次调用均有配对结果，顶层结果中未发现明确错误形状。这不证明业务结果正确，也不排除工具内部或未记录的错误。

因此原任务不能被概括成“Grok V1 子代理结束导致父代理停止”，也不能拿 Muse V1 实验的现象反推这里的因果。

### 2.2 有 final 的停止：客户端结束回合，业务却尚未结束

当前原始文件有 **54 个启动回合：49 个 `task_complete`、3 个 `turn_aborted`、2 个历史未闭合回合**。

用明确短消息规则筛出 18 条“继续 / OK，继续 / 继续修复 / 继续解决”后核对前一回合：15 次已有 `task_complete`，2 次是历史未闭合，1 次是 interrupted。这个样本不能代表所有用户消息，也不能把咨询类答复正常结束算成缺陷。

有明确实施授权但仍以阶段汇报收尾的实例包括：

- 09-06 15:21:33，第 4471 行：候选包已整理，但切换脚本、回滚演练、canary 被列为下一步，随后结束回合。
- 09-07 23:25:37，第 18948 行：转换器和读取兼容仍待修复，回合已完成。
- 09-08 09:44:41，第 20962 行，Grok 上下文：runner/timer/canary 尚未完成，回合已结束。
- 09-09 23:48:46，第 27418 行，Grok 上下文：对外发布仍是旧版本，继续发布仍被留作下一步。

**这类现象不是进程卡死，而是已产生 final 的提前收尾。** `task_complete` 只证明客户端回合结束，不证明业务验收通过。现有材料不足以逐一判断是模型任务理解、提示/上下文诱导还是未捕获的上游行为；不能仅凭该事件宣称“模型主动放弃”或排除所有 Gateway 影响。

### 2.3 两个真正未闭合的回合：最强证据在主机层

| 回合 | 最后实际活动 | 本机日志 |
|---|---|---|
| `01a07205-bb38-7dc2-ab61-d74721a479d3` | 09-05 22:51:47.711，第 4163 行；最后工具已有返回；该回合没有 Collaboration 调用 | 旧 boot 最后日志 22:51:50，新 boot 22:52:33；22:53 文件系统 journal recovery / unclean shutdown |
| `01a0772b-6786-73a3-8cd6-8cd798f9b696` | 09-07 09:20:27.920，第 15434 行 | 09:21:17/26/30 连续 memory pressure；新 boot 09:24:32；09:25 journal recovery / unclean shutdown |

两个回合都为 V2、`gpt-6-astra`，均无 `task_complete` 或 `turn_aborted`。主机异常中断是当前最有力的解释，尤其第一回合根本没有新子代理调用。

仍未知：谁触发重启、为何系统失去响应、哪个进程占用了多少内存。**内存压力不等于已经证明 OOM kill，更不能证明 CodexHub 内存泄漏。** 09-07 上传的现存崩溃报告实际发生于 09-06 01:26，不能当成 09-07 的新崩溃证据。

### 2.4 历史损坏与索引停滞：现在仍未修复

原始文件：

`/home/noirbright/.codex/sessions/2026/09/05/rollout-2026-09-05T07-40-40-01a06ecb-ba14-7e83-bd30-ef9c18738620.jsonl`

- 第 4164 行：2927 字节，其中 2926 个 NUL，剩余一个换行。
- byte offset：`41305234`；零基 ordinal：`4163`。
- 只读 projection 正好停在同一 offset / ordinal。
- 本轮对 bytes 直接 `json.loads` 得到 `UnicodeDecodeError`；因 bytes 自动探测编码，错误类别与显式文本解码后的 JSON 错误可能不同，但同样证明该行不是有效 JSON。
- 原文件 54 回合，索引仍只有 **8 回合**。
- SQLite 最新仍是 09-05 的 `01a07205… / inProgress`；App `read_thread` 当前叠加显示 `interrupted`，但同样只读回这个旧回合。不能再把 App 最新状态写成旧报告里的 `inProgress`。

这是“重开后读到旧状态 / 历史看似一直卡住”的直接原因。它**不等于**后面每次采样终止都由坏行造成，因为原始文件后续确实继续记录了多天工作。

异常重启与坏行位置相邻，但无损坏前副本，不能证明具体写入机制或写入者。本轮没有删除该行、伪造终态或重建原历史数据库。

### 2.5 Gateway 失败有发生，但观察到的失败都不能解释那两个硬停止

按 `window_id.split(':', 1)[0] == 原任务 ID` 关联，而不是按模型名或不存在的 `thread_id` 字段查日志。再用 request ID 收集流错误，按原始回合时间对照后续活动。

- 日志覆盖：09-07 21:41:12 至 09-10 00:22:57，**晚于两个历史硬停止**。
- 1449 个请求；1442 个 `request_complete`（1430 个 200、11 个 499、1 个 502），另有 5 个 `request_error / 502`；2 个请求无已记录的请求终态。
- **六次 502 全部在同一回合内恢复工具调用**：

| 时间 | request ID | 错误 | 同回合后续工具 |
|---|---|---|---|
| 09-08 07:06:49 | `9dbb4a039317` | compact / IncompleteRead | 07:09:44.878 |
| 09-09 12:24:38 | `4ed219ce67df` | Grok / IncompleteRead | 12:24:49.588 |
| 09-09 20:12:31 | `9cc85cc1e40a` | Grok / RemoteDisconnected | 20:13:30.085 |
| 09-09 23:06:00 | `e65abbd1e18f` | Grok / RemoteDisconnected | 23:06:10.733 |
| 09-09 23:08:34 | `88772035a0db` | Grok / IncompleteRead | 23:09:05.218 |
| 09-09 23:55:58 | `2bbbf09af3fb` | Grok / IncompleteRead | 23:56:08.377 |

- 11 个 499 的关联流事件均记录 `client_disconnected=true / failure_side=downstream_write`。其中两次与 interrupted 同秒，其余所在回合后来继续或正常结束。**不能据此声称 Gateway 主动结束整个任务，也不能直接指定是用户点击停止。**
- 3 个 `turn_aborted` 的 reason 均为 `interrupted`，仅两个落在现有 Gateway 日志覆盖中。
- 两个无请求终态的 ID：`104b9c497498`、`ec64e6c0e71d`。前者回合后来有 final，后者后来有工具执行和 final。请求终态缺失原因未确定，不能当成功请求补数，也不能将其认作永久卡死。
- 431 个 200 记录了 `response.completed`；另外 999 个 200 没有同样的 SSE 终态字段，不能用 HTTP 200 单独证明其整个事件链正确。
- 26 个成功 `compacted` 后都在同回合继续工具调用，间隔 5.799–54.816 秒。这只验证成功压缩的续跑，不表示从未压缩失败。

“同窗口后续请求成功”不是已证明的完全相同请求重试；上表另以原始同回合工具事件佐证恢复。

## 三、V1/V2 当前实现现状

### 3.1 生产与候选必须分开

- HEAD：`d069caf5156c62194fa4861a6cd538973eac3a1a`，**工作区有未提交改动**，没有新候选 commit SHA。
- 运行中的 Gateway entry：`/tmp/.mount_CodexHpHGelB/usr/lib/CodexHub/src-python/codex_proxy.py`。
- 抽查生产的 `gateway_compat/multi_agent.py`、`request.py`、`sse.py`、`tool_compatibility/collab_v2.py`、`collaboration_runtime_contract.py`、`subagent_state.py`，六个模块均与 HEAD 相同，均不同于工作区候选。

因此不能把工作区删除了调度器说成生产已经运行了删除版。抽样哈希也不代表已经验证整个安装包的身份。

| 层次 | V1 | V2 |
|---|---|---|
| 生产核心 | 仍含旧强制调度/修补实现 | 仍含旧参数投影及历史校验实现 |
| 工作区候选 | 已撤销主要强制 final、限制父工具、补造下一步等调度行为 | 已撤销有损字段删除/猜 task_name；增加失败参数历史回放、严格 JSON、SSE 错误终态与绑定证据修复 |
| 确定性证据 | 旧父任务继续等回归通过，但本轮发现新的工具归属 RED | 已有严格校验与错误恢复回归通过；不代表真实模型验收 |
| 真实 E2E | 旧结果被 harness 缺陷污染，提示 A/B 尚无有效结论 | 旧 Grok/Muse 3/3 不能再视为完整父任务连续执行验收 |

### 3.2 新发现：候选还不是严格的“仅协议适配”

三组行为已从公共入口复现并保留为失败测试：

| 优先级 | 开放缺陷 | 证据 |
|---|---|---|
| P1 | 普通 `spawn_agent / wait_agent / close_agent / resume_agent / send_input` 响应被改成 `namespace=multi_agent_v1`；spawn 还会从 `{}` 被补上 `fork_context=false` | request 中普通声明保持不变，关闭注入后 body 与 SSE 仍被改写，10 个参数化用例失败 |
| P1 | 未注册的 `multi_agent_v1__spawn_agent` 普通函数被当成 V1；开启注入还覆盖其原 schema | 两个请求用例失败，不能只靠字符串前缀认领工具 |
| P1 | 客户端只声明本地父工具、未声明 Collaboration 时，Gateway 仍增补 V1 子代理工具 | `strict` 与 `repair_policy=none` 下仍稳定失败 |

主要位置：

- `/home/noirbright/Workstation/CodexHub/src-python/tool_surface_adapter.py:1471`：`_should_preserve_owned_wire_value` 对所有同名工具绕过 plan 所有权保护。
- `/home/noirbright/Workstation/CodexHub/src-python/tool_surface_adapter.py:1497`：旧全局名称归一化继续恢复 namespace、修改参数。
- `/home/noirbright/Workstation/CodexHub/src-python/codex_semantic_adapter.py:65`：别名前缀分类没有本次请求的注册来源。
- `/home/noirbright/Workstation/CodexHub/src-python/tool_surface_adapter.py:907`：直接覆盖同名 V1 别名 schema。
- `/home/noirbright/Workstation/CodexHub/src-python/gateway_compat/request.py:468`：非 V2 路径仍无条件开启 V1 工具注入。

这些是候选仍不满足原修复契约的证据，**不是已证明原 InfoDigest V2 停止由它们触发**。已有“普通同名工具”测试只检查了请求，漏掉响应及 SSE，因而之前的绿灯覆盖不足。

### 3.3 Harness 缺陷使此前模型结论失效

文件：`/home/noirbright/Workstation/CodexHub/scripts/e2e_third_party_collaboration.py`。

1. `_write_parent_fixture`（294）：没有创建 `parent_task.py`、`test_parent_task.py`，首轮不是原计划中的标准修复任务。
2. `_write_resume_fixture`（301）：第二用户回合之前把已修好的源码重写为 `BROKEN`，破坏“原修复必须保留”的输入。
3. `_verify_fixture`（347）：以 `return "FIXED"` 字符串判断修改；合法单引号被误判，模型可修改/清空的测试也不能作为独立判定标准。
4. `_sentinels_in_records`（97）：未要求 `role=assistant`，用户提示里的成功标记能进入 parent 结果。
5. `_test_tool_call_count`（142）：任意工具参数包含测试文件名就计数，spawn 提示也会被算作父代理运行测试。

另外，经代码复核仍缺少：真实 call/result 与退出码关联、同一 child 的生命周期顺序、第二回合禁止全部 Collaboration 调用、修改归属、失败时可归因的有界轨迹。`_read_session_records` 只取最后一次模型，遇坏行会跳过整个 session。现有 V1 提示还将 `resume_agent` 描述成发送 follow-up 的工具，而冻结契约中它只有 `id`，跟进消息应通过 `send_input` 发送；修 harness 时需再对照 0.153.4 实际声明。

所以：

- 旧通过不能证明原计划的父任务真实继续并完成。
- 旧失败/超时也不能直接归为模型不遵循或“删除提示导致退化”。
- 缺少 close 仅是未满足用例，尚未证明它造成 CLI 超时；有 close 的旧实验也出现过超时。
- 不将旧统计改成 0% 或给出反向模型排名；正确状态是 **该验收结论无效，需要同一个正确 harness 重测**。

### 3.4 哪些补丁可能必须保留

应保留/完善的是**有完整逆映射的结构适配**：真实 namespace、请求局部注册的别名、工具调用/结果关联、Chat/Responses 转换、必要的明文 handoff。这里确实有必要兼容，并非“所有旧补丁都不需要”。

但不应恢复强制 final、吞父工具调用、按文本推断当前角色、替模型补造下一步调用。正确修复是把别名认领绑定到客户端声明及其本次请求来源，而不是重新启用旧调度器。

非强制的协议说明是否能提高某些模型的遵循率，需要修好 harness 后做有效 A/B。当前证据既不能证明这些说明全都多余，也不能证明必须恢复。

## 四、是否所有第三方模型都能使用 V2

不能无条件保证。至少要区分：

1. **配置启用**：当前 provider 配置解析出的 8 个外部模型（commandcode 3、opencode-go 4、xai 1）均声明 V2；resolver 缺省也是 V2。这只是客户端选择，不是能力或稳定性证明。
2. **协议可表示**：端点需支持原生 namespace 生命周期，或者可逆的 function/namespace 适配、调用结果与 SSE/history 保真。纯 text compatibility 不能自动等同完整 V2；没有可用明文的 encrypted handoff 必须明确失败，不能猜解密、换 Provider 或降级 V1。
3. **模型遵循**：即使协议可表示，模型也可能不正确完成 spawn/followup/wait，或者提前发 final。
4. **运行与配置**：网络、限流、上下文、reasoning 参数和客户端版本都有各自边界。两款模型通过不能推出所有 Provider 永不停止。

还发现配置漂移：当前 live client catalog 的 Muse 默认 effort 仍为 `max`，而维护目录及本地 E2E 默认使用用户已确认的 `xhigh`。本轮不修改全局设置；实际请求应以该任务显式 effort 为准。这不是原任务中断的解释，因为原任务未使用 Muse。

OpenAI Docs 的当前 [Troubleshooting](https://learn.chatgpt.com/docs/reference/troubleshooting) 提醒 CLI 与 Desktop 版本可能不同，并给出审批/终端/日志检查；没有为本项目的自定义 V1/V2 或第三方模型提供通用支持保证。本报告的具体归因来自本地证据，而不是由文档推测。

## 五、验证结果与重新安排优先级

本轮命令均通过仓库 Python 3.13 launcher。CLI：`0.153.4`。

```bash
./scripts/codexhub-python.sh -m pytest -q --ignore=tests/test_real_client_e2e.py
# 加入额外 13 个 Gateway 回归前：
# 5 failed, 2191 passed, 170 skipped, 269 subtests passed in 59.30s

./scripts/codexhub-python.sh -m pytest -q \
  tests/test_subagent_execution_regressions.py \
  tests/test_e2e_third_party_collaboration.py --tb=short
# 最新增量：18 failed, 29 passed in 0.38s

./scripts/codexhub-python.sh scripts/report_quality_gates.py
# report-only 执行完成，parse_errors=0；仍有未使用 import / 同名等报告项

git diff --check
# 通过
```

18 个失败用例 = 13 个 Gateway 工具归属参数化用例 + 5 个 harness 用例，**不是 18 个互相独立的根因**。原有该组 23 个 Gateway 回归仍通过；不能用它们掩盖新增 RED。整套测试保持 RED，不修改快照或弱化断言来制造完成状态。

未验收：修正后的真实 Grok/Muse V1/V2 A/B、连续三次矩阵、Windows synthetic real-client、生产发布和原历史恢复。Linux 不执行 Windows 专用契约不能算 Windows 通过。

建议按两条独立修复线推进：

**原任务连续性**

1. 单独处理旧历史：正常停止写入者，备份 rollout 与相关数据库，在副本验证损坏行恢复/隔离和目标索引重建；禁止直接删全局数据库或伪造完成事件。
2. 核实主机重启来源及资源压力；目前不指定哪个应用背锅。
3. 对已有实施授权，阶段进展使用中间消息，只有完成、实际阻断或需用户输入才结束回合。不通过 Gateway 自动续跑或强制 final 掩盖问题。

**V1/V2 候选交付**

1. 先修 harness 的输入、行为断言及调用/退出码/父子关系证据，使错误能真实判失败。
2. 修请求局部工具所有权：普通名称/别名不能被认领，未声明子代理不能被重新授权；保留必要可逆适配，继续禁止强制调度。
3. 在正确且相同的 harness 上做隔离旧实现/候选 A/B：Grok high、Muse xhigh，V1/V2，父子同模型；保留失败终态和可归因结构证据，不自动换模型或续跑。
4. 矩阵与严格回归完成后才确定删除范围、生成精确候选 SHA 并验收打包产物；工作区测试通过不等于生产已部署。

**最终判断：原任务的已证实问题不由 V1 缺陷解释；CodexHub 仍有明确需修的执行边界与验收缺陷。当前既不能宣布 V2 全面可靠，也不能拿失真的 E2E 支持恢复或删除整套旧 V1 补丁。**
