# 2026-09-09 审查后续修复与验收

范围：用户授权推进的 1–4 项——第三方工具 schema、Linux Gateway 生命周期、Windows 真实客户端 E2E 接线，以及历史文档/证据/原型目录整理。本记录承接本地审查，不替代发布或全模型兼容性认证。

## 修改与候选

- `28d466b`：修正根 schema 约束与 nullable union；Linux 关闭到托盘时保留 Gateway，真正退出时清理，CLI 启动通过 `setsid` 独立运行；xAI 离线预检隔离 HOME。
- `f658c64` / `d504a71`：CLI 合同与三份模拟夹具统一 Muse Spark 1.3；OpenCode Go 对该模型使用实际支持的 `xhigh` 上限。保留用户显式保存的设置，测试只更新隔离配置。
- `ef591f5`：Windows 断连明确区分 `request_write` 与 `response_headers`，两者均禁止发送后重试。它是最终 runtime 候选。
- `dcc3c36`：调整响应头超时的测试断言，与上述明确阶段一致。
- `e550a6b`：Windows 测试显式以 UTF-8 读取操作文档，修正两处旧模型展示名称。
- `158c74d` / `0ee1a5e`：重新生成脱敏原型目录（8 providers / 136 models），归档历史调查记录并去除会话标识。原型 API key 均为演示占位符。

## 验证

[Linux 结构化结果](linux-local.json)记录运行命令、二进制 SHA256 和 8 组真实客户端结果；[Windows 结构化结果](windows-local.json)记录完整套件、增量复验与真实 CLI 阻塞。

| 检查 | 结果 |
|---|---|
| Linux Python core | 2223 passed，170 skipped，265 subtests passed |
| Linux Rust / clippy | 715 passed，4 ignored；clippy 通过 |
| Linux 原生窗口输入 | 13 次真实点击与设置页打开/关闭通过，窗口 820×620 |
| 前端构建 | `npm run build` 通过 |
| Linux 真实 CLI | Codex CLI / OpenCode / Pi / OMP × Luna / Muse Spark 1.3，8/8 通过 |
| xAI 真实 schema 请求 | HTTP 200，schema accepted；没有执行模型生成的工具调用 |
| Windows Python core | 2357 passed，36 skipped，265 subtests passed |
| Windows 文档编码增量 | 7 passed，145 deselected |
| Windows 模拟客户端 | 全量运行 145 passed / 7 failed（编码问题）；修复后 7/7 增量复验通过，合计覆盖全部 152 项 |
| Windows 真实 CLI | 暂被运行中的 Codex Desktop 重启保护阻止，尚未完成 |

以上为各相关检查的最新结果，不是同一次 `verify-linux.sh` 全绿：该脚本上一轮只有旧超时断言失败；修复后完整 Python core 已重跑通过，其余 Rust、clippy、窗口检查仍沿用未受影响的通过结果。

Windows 使用 Yoga 上的独立 checkout、portable 和专用 E2E 输入目录。portable 来自 `ef591f5`；归档 SHA256 为 `840be98cb5fc4e63fddba29491c9dd8dea3281083acaa540c305d25a0b2277b3`。核心测试仅额外应用 `dcc3c36` 的测试断言修正。构建结束后 MSVC `vctip.exe` 遥测辅助进程未自行退出；确认产物完成后只清理本次构建产生的 helper，才完成 watchdog 收尾，因此不声明本次构建完全无需人工干预。

## 失败记录与范围限制

- Windows 第一次真实 E2E 在 sidecar 检查前停止；核对 portable 来源并写入精确候选 SHA 后再次运行。
- 第二次在 `refresh-models` 阶段被 `codex_desktop_restart_required` 拦截：Yoga 有运行中的 Codex Desktop。未绕过保护，也未擅自关闭它。
- Windows 模拟套件的 UTF-8 文档读取在默认 GBK 环境失败；修复后七项相关测试已通过。完整套件耗时 1338.57 秒，145 项通过；7 项编码失败均已增量复验通过，不将两次运行合并声称为单次全绿。
- Linux 原始 CLI E2E 入口实际通过；没有把替代启动器的诊断结果当作正式通过。
- 真实 CLI 只覆盖两种模型、四个客户端，不代表所有第三方模型或 Desktop 子代理都已通过。先前 xAI 会话问题的调查结论仍保留独立历史语境。

历史材料位于 `gateway-cache-linux`、`gateway-cache-windows` 与 `conversation-xai-timeouts`，仅适用于各自候选。本目录不保存凭据、原始客户端输出或用户会话标识；Linux 临时 provider 副本已清理。
