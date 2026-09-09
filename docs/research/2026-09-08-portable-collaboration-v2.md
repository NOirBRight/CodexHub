# 第三方子代理明文交接与默认 V2

2026-09-08；Codex CLI 0.153.4；本次在候选源码启动的隔离 Gateway 上验证，未部署到用户当前 Gateway。

## 根因与处理

官方父代理发起 `spawn_agent` 时，`message` 参数按原生 Collaboration schema 加密。
即使 `fork_turns="none"`，新建 Grok 子代理仍收到含 `encrypted_content` 的
`agent_message`，随后被第三方历史边界拒绝。此问题与第三方 provider 名称无关。

当前客户端通过 `input[].type=additional_tools` 发送声明；只处理顶层 `tools`
不会改变真实请求。直接移除原生 `collaboration` 声明的 `encrypted` 标记也不可行：
官方服务返回 HTTP 400，要求保留工具的 configured schema。

候选实现将已识别的完整 V2 声明映射为普通 namespace
`codexhub_plaintext_collaboration`，在消息生成前请求明文。Gateway 在响应 item、
SSE added/done/completed 以及后续明文调用历史中还原客户端身份，并设置
`encrypted_function_args=[]`。映射仅在本次请求实际声明了该 namespace 时启用。
未知 schema、用户自有同名 namespace 和已有密文不会被误改或伪装为明文。

另一处失败来自第三方模型目录未声明 V2，导致客户端使用旧工具表、Gateway 注入的
`multi_agent_v1__spawn_agent` 被客户端拒绝。按本次用户要求，第三方目录和 provider
解析现默认选择 V2；显式 V1 配置优先保留。官方模型仅对完成独立验证的 Luna 和 5.5 设置 Gateway 默认 V2，
其余官方模型沿用目录声明。V2 选择表示客户端/Gateway 协议，不表示每个上游原生支持 V2，
也不保证未实测模型具备可靠工具调用能力。

## 真实 E2E

[脱敏结构证据](../evidence/portable-collaboration-v2/runtime-results.json) 包含 9 条路径：

| 父模型 | 子模型 | 结果 |
| --- | --- | --- |
| Astra | xAI Grok 4.6 | 通过 |
| Astra | Command Code DeepSeek V4 Flash | 通过 |
| Astra | Command Code GLM 5.3 Flash | 通过 |
| Astra | OpenCode Go Muse Spark 1.2 Contributor | 通过 |
| Astra | 官方 Luna | 通过 |
| xAI Grok 4.6 | Command Code DeepSeek V4 Flash | 通过 |
| Command Code DeepSeek V4 Flash | xAI Grok 4.6 | 通过 |
| Command Code GLM 5.3 Flash | xAI Grok 4.6 | 通过 |
| OpenCode Go Muse Spark 1.2 Contributor | xAI Grok 4.6 | 通过 |

每条路径都执行真实 spawn、wait、followup、再 wait。证据分别检查子代理实际模型、
子代理自身 assistant 输出、父代理收到的结果、明文调用标记和子代理交接内容类型。
每条路径有 2 次明文交接、0 个密文内容项。统一第三方默认 V2 后，另重跑了
Astra → Grok 和 Grok → DeepSeek，两条均通过。

复跑命令（显式复用本机已有 Official/xAI/provider 配置；产生真实上游用量）：

```bash
./scripts/codexhub-python.sh scripts/e2e_third_party_collaboration.py \
  --source-home /home/noirbright/.codex \
  --child-model xai/grok-4.6 \
  --output test-results/third-party-collaboration.json
```

`--parent-model`、`--parent-effort`、`--child-model`、`--child-effort` 可选择其他组合。
脚本建立独立客户端与 Gateway home，复用缓存官方目录，并通过候选 provider loader
更新隔离目录的协议默认值。凭据与原始运行记录仅在私有临时目录中使用，结束后删除；
持久化证据只保留结构和固定测试结果。普通 pytest 不运行 live probe。

## 回归与生效

Python core：2199 passed / 170 skipped，另 263 subtests passed。
Rust：709 passed / 4 ignored；clippy 通过；Linux 真实窗口物理输入 E2E 通过。
完整 Linux 检查首次仅因新 E2E 脚本未登记入口清单而失败；补齐清单后，Python
全套重跑通过。后续变更仅涉及 Python，不重复已通过的 Rust/窗口检查。

源码修复尚未替换运行中的 Gateway。应用时需要更新并重启 Gateway、刷新模型目录，
让 Codex 客户端重新读取目录；已打开的 Codex 客户端应重启。已经含密文的失败子代理
必须重新创建；本修复不解密既有密文，也不删除用户历史。

## 补充：Luna / 5.5 / 5.4 作为 V2 父代理

用户进一步询问这些官方模型能否也默认 V2。
[补充证据](../evidence/portable-collaboration-v2/official-parent-qualification.json)
来自同一真实客户端与候选 Gateway，显式使用
`--parent-collaboration-version v2`，不修改用户目录或官方默认值。

- `gpt-5.6-luna`：通过。作为 V2 父代理完成 Grok 子代理的 spawn、followup 与两次结果返回。
- `gpt-5.5`：通过。初次使用旧缓存目录时报 `missing_catalog_model`；通过
  `--refresh-official` 读取最新官方目录后重跑成功，证明初次失败不是 V2 协议拒绝。
- `gpt-5.4`：未验证。当前本地目录缺失，最新官方目录的 9 个模型中也未返回它。
  不把目录缺失视为 V2 不兼容，也不推断该模型在所有账户中不可用。

最新官方目录仍将 Luna 标为原生 V1、5.5 标为 null。通过的是 Gateway 普通工具
别名/明文适配路径，不是对原生官方 V2 能力的声明。因此 Luna 和 5.5 已具备在
CodexHub 中选择 V2 默认的运行证据；5.4 应在获得真实模型访问后再决定。
本轮仅完成资格验证，未调整这些官方模型的默认配置。


## 模型详情设置（后续实现）

Luna、5.5 与第三方模型的 V1/V2 选择移到模型详情/编辑页，列表只显示当前模式。
官方模型使用现有原子目录及 override sidecar 保存接口；第三方通过 provider 模型配置保存。
选项为跟随默认、显式 V1、显式 V2；选择显式值不会因为恰好等于默认而被 UI 清除。
Luna/5.5 的 Gateway 默认值与官方 native metadata 分离，旧 baseline 仍可读取，
显式 V1 在升级及后续目录刷新中保留。5.4 未完成真实资格验证，未开放选择或改默认值。


本轮设置变更验证：Python core 2200 passed / 170 skipped（另 263 subtests）；
Rust 710 passed / 4 ignored；clippy 与 Linux 物理输入 E2E 通过；前端 build 与完整
ui-contract 通过。新增测试覆盖官方 V2 默认值、旧 baseline 升级、显式 V1 连续刷新、
清除后恢复 V2、官方身份校验及第三方 TOML V1 往返。

浏览器使用真实 `ModelSection` 组件和隔离内存回调，分别打开 Luna、5.5、Grok
详情，确认默认 V2、选择 V1、Apply 后列表显示 V1；官方其他字段保持不可编辑。
这证明组件交互，持久化由上述 Rust/Python 测试覆盖；未通过浏览器修改生产配置。
临时验证页面已清理，运行中的 Gateway 未替换。
