# InfoDigest 任务中途停止：二次核查

对象：`01a06ecb-ba14-7e83-bd30-ef9c18738620`，任务标题「提升资讯抓取与推送稳定性」。核查时间：2026-09-09 20:04 左右，以下时间均为北京时间。仅做本机只读取证和文档记录，未修改原始任务、历史数据库或 Gateway 配置。

本次证据支持三个不同现象：**模型结束回合但业务仍未完成；两次历史中断与本机异常重启吻合；损坏的历史行使客户端索引一直停在旧回合。** 不能继续把「超长会话频繁压缩」作为已经证实的主因，也不能据此在 Gateway 收到 499 时强制结束客户端回合。

**本机重启与两次未闭合回合对上了时间。**

| 回合 | 原始记录的最后活动 | 系统日志 | 随后的恢复 |
|---|---|---|---|
| `01a07205-bb38-7dc2-ab61-d74721a479d3` | 09-05 22:51:47.711，rollout 第 4163 行；此前工具已返回 | 旧 boot 最后日志 22:51:50，新 boot 首条 22:52:33；22:53:35 文件系统记录 `recovering journal` | 第 4164 行损坏；23:16:33 开始新回合 |
| `01a0772b-6786-73a3-8cd6-8cd798f9b696` | 09-07 09:20:27.920，第 15434 行 | 09:21:17、09:21:26、09:21:30 连续 `Under memory pressure, flushing caches.`，多个应用无响应；新 boot 首条 09:24:32 | 09:25 文件系统恢复，journald 报 `corrupted or uncleanly shut down`；09:30:33 开始新回合 |

两次均无 `task_complete` 或 `turn_aborted`，与客户端所在机器中断、来不及记录终态一致。第二次停止采样比旧 boot 最后一条日志早约一分钟，不能把日志时间精确当作进程死亡时间。现有记录未确定重启由人工、系统或硬件触发；内存压力不等于已证实 OOM kill，更不能归因于某个应用的内存泄漏。

09-07 09:20:28 有 ChatGPT 崩溃报告上传日志，但现存报告的 `Date` 实际为 **09-06 01:26:00**，`Signal: 4`。这是旧报告，不能用来证明 09-07 09:20 发生了新的客户端崩溃。

**历史损坏与索引停滞之间存在直接证据。**

原始文件：

`/home/noirbright/.codex/sessions/2026/09/05/rollout-2026-09-05T07-40-40-01a06ecb-ba14-7e83-bd30-ef9c18738620.jsonl`

- 第 4164 行共 2927 字节：2926 个 NUL 加一个换行，无法解析 JSON。
- 字节范围 `[41305234, 41308161)`，紧接第一次重启前最后一条有效事件。异常重启时写入未持久化是合理解释，但缺少损坏前副本，不能证明具体写坏机制或写入者。
- `/home/noirbright/.codex/thread_history_1.sqlite` 的 `thread_history_projection_state` 正好停在 `next_rollout_byte_offset=41305234`、`next_rollout_ordinal=4163`。ordinal 从 0 开始，对应文件第 4164 行。
- 客户端日志反复记录 `deferring rejected rollout line until a later ordinal resolves it` 及 `expected ordinal 4163, got 4162`。20:00 前的定向样本已有 600 条相关警告。
- 原始文件已记录 **43 个回合**，索引只记录 **8 个**，最新索引仍为 09-05 的 `01a07205… / inProgress`。通过 `read_thread` 读取也返回这一旧状态。
- 原始记录有 **37 次 task_complete、3 次 turn_aborted、2 个历史未闭合回合和 1 个当前活动回合**。当前活动回合 `01a085fa…` 在核查中继续写入工具结果，不能算作第三次历史挂死。

因此，原始运行仍能推进，而持久化的任务读取结果已经落后数天。这能直接解释「显示旧的运行中状态／重开任务后状态不对」。它不等于每一次模型停止采样都是索引异常造成的。把旧回合的状态直接改成完成，也不能让解析器越过损坏行。

**不少“继续”是在模型正常结束回合后发生的。**

原始 `task_complete.last_agent_message` 中多次列出明确未完成事项，然后结束回合。例如：

- 09-06 15:21:33：报告候选包，切换脚本、服务回滚演练、canary 仍是下一步，随后 `task_complete`（第 4471 行）。
- 09-07 23:27:13 前一回合：转换器和读取兼容仍待修复，但已于第 18948 行结束。
- 09-08 09:44:41：正式 runner、timer 和生产 canary 尚未完成，仍记录 `task_complete`（第 20962 行）。
- 09-09 16:57:32：日报尚未发布，单篇错误诊断仍是下一步，随后 `task_complete`（第 24104 行）。

对能直接定位的 18 条短“继续”类用户消息核对前一回合：15 条之前已有 `task_complete`，2 条之前是上述历史未闭合回合，1 条之前是明确中断。这只是可直接读取样本，不能代表全部用户消息；问题咨询之后正常结束也不应计为异常。结合已有「全部推进、一次性落地」的授权，可以确认其中部分任务存在阶段汇报代替继续实施的执行行为问题。

**压缩和 Gateway 断流确实存在，但证据不支持此前的主因判断。**

- 24 条成功写入的 `compacted` 事件之后，均在 **5.799–54.816 秒**内继续出现工具调用。这只验证成功压缩后的续跑，不能据此宣称从未发生压缩失败。
- Gateway 可关联记录从 09-07 21:41 开始，晚于两次历史中断，不能拿后几天的请求状态解释前两次中断。
- 09-08 07:05:29 的 compact 请求 `a91157e35259` 为 499，与原始 `turn_aborted` 同秒；随后新回合再次尝试。
- 09-08 07:06:49 的 compact 请求 `9dbb4a039317` 遇到 `IncompleteRead`，记录 502；07:06:49 已发起下一次 compact，07:09:32 得到 200，07:09:45 已继续普通生成。
- 09-09 12:24:38 的 Grok 请求 `4ed219ce67df` 遇到 `IncompleteRead`，记录 502；同秒后续请求 `e61adaacb33a` 开始，12:24:50 得到 200，当前回合继续推进。未将请求 body 是否相同作为结论依据。
- 11 条 499 是下游连接关闭的记录；抽查对应 `official_passthrough_stream_closed` 为 `client_disconnected=true`、`failure_side=downstream_write`。它不说明 Gateway 主动终止了整项任务。
- 累计 rollout 约 122 MB 包含多天事件、工具输出和压缩替换历史，并非单次模型上下文或请求大小。工具适配事件的次数也不能证明适配增加了多少 token。

Gateway 代码边界见 `src-python/gateway_relay_passthrough.py` 与 `src-python/gateway_error_dispatch.py`：这里记录的是 HTTP/SSE 请求终态，不拥有 Desktop 的整个任务回合。现有历史迁移 ledger 未提及目标任务，也没有该任务的迁移备份，未找到 CodexHub 改坏第 4164 行的直接证据；这不构成对所有历史版本或外部文件写入者的排除。

**修复应分层进行。**

1. **恢复工作连续性**：新任务只接收短交接与剩余事项；避免依赖原任务已经卡住的索引和完整历史复制。这是临时绕行，不修复原历史。
2. **离线恢复原任务历史**：先正常停止该任务并关闭持有写入的客户端；备份 rollout 与相关数据库。在副本中优先从备份恢复损坏记录；无法恢复时保留损坏证据、明确缺失范围，并验证隔离该行及重建目标任务索引的方案。不能直接删全局历史库、伪造 `task_complete` 或在活动文件上截断。验收应包含：解析越过该位置、后续历史回合可见、末尾状态与原始记录一致、小范围恢复测试通过。本次未执行恢复，尚未验证具体客户端版本的重建入口。
3. **客户端恢复能力**：历史解析器需能对损坏记录明确报错并提供隔离恢复；重启后应识别未闭合的旧回合，不能无限保留 `inProgress`。是否已有版本修复，需另行验证。
4. **修正执行结束条件**：已授权的剩余实施继续推进；阶段进展发为中间消息，只有完成、确实需要外部输入或明确阻断才结束。需要长期等待时采用可恢复的检查点和调度，不只留下“下一步”。
5. **核实主机稳定性**：若重启不是用户操作，继续排查内存压力、系统无响应和重启来源。当前证据不足以指定是 CodexHub、Desktop 或其他进程耗尽资源。
6. **Gateway 的合理补强**：补齐 thread/turn/window 与客户端取消、上游失败、恢复请求的关联诊断。不要把 499 自动转成整个任务失败，也不要以加大 idle timeout 或关闭压缩替代上述修复。

**可复核的只读检查。**

以下命令已运行，分别确认 boot 切换、未正常卸载、损坏行及数据库游标卡点。JSON 元数据摘要见 `docs/evidence/infodigest-thread-stops/analysis.json`，不包含会话正文、工具输出或凭据。

```bash
journalctl --list-boots --no-pager -n 12
journalctl --since '2026-09-07 09:19:30' --until '2026-09-07 09:27:00' --no-pager
./scripts/codexhub-python.sh - <<'PY'
import json
import sqlite3
from pathlib import Path

thread = '01a06ecb-ba14-7e83-bd30-ef9c18738620'
base = Path.home() / '.codex'
source = base / 'sessions/2026/09/05/rollout-2026-09-05T07-40-40-01a06ecb-ba14-7e83-bd30-ef9c18738620.jsonl'
db = sqlite3.connect(f'file:{base}/thread_history_1.sqlite?mode=ro', uri=True)
offset, ordinal = db.execute(
    'SELECT next_rollout_byte_offset, next_rollout_ordinal '
    'FROM thread_history_projection_state WHERE thread_id=?', (thread,)
).fetchone()
with source.open('rb') as handle:
    handle.seek(offset)
    line = handle.readline()
print({'offset': offset, 'ordinal': ordinal, 'bytes': len(line), 'nul': line.count(b'\0')})
json.loads(line)  # 本次在这个位置确定性失败；只读，不修改历史。
PY
```

核查输出：`offset=41305234, ordinal=4163, bytes=2927, nul=2926`，随后 JSON 解码失败。该检查验证现存损坏和索引卡点，没有重演主机异常重启，也没有验证修复后行为。

OpenAI Docs 的 [Troubleshooting](https://developers.openai.com/codex/app/troubleshooting/) 建议检查审批等待、查看日志，并以更小、更聚焦的新任务恢复。该文档未解释本例的 ordinal 错误；本报告的具体归因来自本机证据。
