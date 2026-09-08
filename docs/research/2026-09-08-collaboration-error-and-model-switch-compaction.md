# Grok Collaboration 错误回放与切换模型压缩故障

## 实际记录（2026-09-08，北京时间）

仅提取用户指定故障会话的工具结构、错误及 Gateway 请求元数据；不复制任务正文或凭据。

- 16:39:42：两次 `collaboration.spawn_agent` 均返回有效 `task_name`，创建成功。
- 16:39:47：`collaboration.wait_agent` 参数为 `{"timeout_ms":180000.0}`。
  客户端返回：`failed to parse function arguments: invalid type: floating point
  \`180000.0\`, expected i64 at line 1 column 22`。
- 16:39:48：Gateway 拒绝下一次历史回放，报
  `Tool compatibility failed at history: malformed_collaboration_result.`。
- 16:40:22–16:40:29：Grok 的普通压缩请求也被同一历史错误阻断。
- 16:43:16、16:44:01：会话记录两次远程压缩失败，官方接口不支持
  `xai/grok-4.6`。该时段 Gateway 日志没有对应请求。

## Gateway 修复与验证

冻结的 V2 声明中 `timeout_ms` 是 `number`，因此浮点形式能通过声明验证，
但客户端的整数解析拒绝它。错误输出是合法的客户端工具失败记录，并非成功结果 JSON。
此前只有 `interrupt_agent` 的纯文本失败得到兼容，其余工具的解析错误会堵死历史。

`validate_collaboration_result` 现在接受 V2 客户端以
`failed to parse function arguments: ` 开头且包含错误详情的输出，原样回传给模型，
使它有机会自行纠正参数。没有更改工具声明、参数、路由或成功结果结构。
未知文本、空错误、错误类型的 JSON 字段及重复键继续拒绝。

复现命令：

```bash
./scripts/codexhub-python.sh -m pytest -q tests/test_issue_282_collaboration_v2.py -k wait_argument_parse_error
```

修改前两条路径均报截图中的 `malformed_collaboration_result`；修改后均通过。
相关 Collaboration 测试合计 126 项通过。该测试验证错误能够回放，
不保证模型下一次一定生成正确参数，也不等同于真实 Grok 端到端验证。

## 压缩问题的边界

仓库已有 [CLI E2E 记录](../agents/beta4.1-cli-e2e-plan.md) 明确指出：
Codex 在发送当前模型的回合前，可能用上一轮模型执行 pre-sampling compaction。
因此，选择 Astra 不保证前置压缩也使用 Astra。

本次现象与旧模型压缩路径一致；官方错误中的 Grok 名称及 Gateway 日志缺席
支持客户端直接发送了旧模型压缩请求的判断。但没有捕获本次客户端的原始 HTTP 请求，
也没有独立运行客户端模型切换复现，不能将推断写成已完成的客户端修复。

后续恢复可在安装本修复后，先通过 Gateway 使用原 Grok 路由完成压缩，再切换 Astra。
这是待验证的恢复路径。不要通过删除历史、篡改会话模型元数据或全局强制改写模型名恢复。
本次未部署 Gateway、未修改原会话，也未执行远程压缩。
