# Codex Code Mode 与 Collaboration V2 范围研究

日期：2026-08-07

## 结论

官方 Codex 源码中没有一个独立的 `code_mode_version = v2` 协议。当前语境里的“Code Mode V2”通常是以下概念之一：

1. App Server Protocol V2（JSON-RPC 协议版本）；
2. Multi-agent/Collaboration V2（agent 工具面版本）；
3. Code Mode host/runtime（`exec`、`wait` 和独立的代码执行 host）。

这三者不能合并为一个模型能力开关。

## 官方行为

- OpenAI 的 Responses multi-agent 文档把 `spawn_agent`、`send_message`、`followup_task`、`wait_agent`、`interrupt_agent`、`list_agents` 定义为 hosted orchestration actions；模型产生动作，Codex/API 的 agent control 负责生命周期，不是普通的第三方函数结果。
- 官方 Codex `multi_agents_spec.rs` 中，V1 是 `multi_agent_v1` namespace 下的旧工具面；V2 是直接函数工具面，并引入 task path/name、`fork_turns`、V2 消息与中断语义。
- 官方 Codex `code_mode` 模块的公开工具是 `exec` 和 `wait`，通过独立或共享 Code Mode host 执行；该模块没有独立的 Code Mode V2 wire protocol。`app-server/tests/.../v2/code_mode_host.rs` 中的 `v2` 是 App Server 协议路径。
- 官方当前模型目录不是“所有官方模型都是 V2”：`gpt-5.6-sol`、`gpt-5.6-terra` 为 `multi_agent_version = v2`，`gpt-5.6-luna` 为 `v1`，更旧模型为 `null`。API 层宣称 GPT-5.6 可用 multi-agent beta，与 CLI 选择的具体 wire version 是两个层次。

来源：

- <https://developers.openai.com/api/docs/guides/responses-multi-agent>
- <https://developers.openai.com/api/docs/guides/tools>
- <https://github.com/openai/codex/blob/95c7265e849e6e360a7fa53ffeac70b25d6051a3/codex-rs/models-manager/models.json>
- <https://github.com/openai/codex/blob/95c7265e849e6e360a7fa53ffeac70b25d6051a3/codex-rs/core/src/tools/handlers/multi_agents_spec.rs>
- <https://github.com/openai/codex/blob/95c7265e849e6e360a7fa53ffeac70b25d6051a3/codex-rs/core/src/tools/code_mode/mod.rs>

## CodexHub 当前覆盖

发布分支 beta2.2 的 Gateway 仍是 V1 导向：`src-python/codex_proxy.py` 只声明 `multi_agent_v1` namespace 和 V1 工具名；`src-python/catalog_sync.py` 与 `config/official_model_catalog_metadata.json` 保存官方模型的 V1/V2 目录值。官方请求走原生路径，第三方请求使用当前兼容层，不能把“官方原生 V2 可用”当成“第三方 V2 已经完成”。

- Code Mode：只有 `apply_patch` 等特殊/严格 codec 的部分适配；完整通用 `exec`/`wait`、custom/freeform 编解码和真实 CLI 工作流尚未覆盖。
- Collaboration V2：官方 Sol/Terra 可原生使用；beta2.2 的第三方桥接没有完整 V2 生命周期，不能保证从 V2 历史到第三方端点的可逆转换。
- `origin/dev` 中已有更通用的 runtime tool compatibility 基础，但它不等于 beta2.2 已发布能力。

## 第三方模型的正确目标

目标不是要求每个第三方模型“原生实现 Collaboration V2”，也不是按模型名建立准入白名单。应保持用户选择的精确 provider/model/endpoint，并根据运行时声明和端点协议生成不可变的 Tool Bridge 计划：

`native` → 原样发送；`adapt` → 可逆地转换 namespace/function/custom/freeform、调用 ID、顺序、流和历史；`omit` → 可选能力无法表达时不暴露；`required-but-unavailable` → 用户明确要求时在采样前失败。

Codex 客户端仍是 agent 的创建者、执行者和调度者；Gateway 只做 wire/schema 适配。不得静默 V2→V1 降级、fallback 到 Terra/Official、伪造 agent 结果，或让 Gateway 代理 OpenAI hosted `web_search`/compaction。Hosted 工具只有选定的上游真正支持时才可暴露。

## 版本边界建议

- Beta2：完成运行时派生兼容性契约和 V1/V2 隔离，止住错误 fallback/协议污染；不宣称完整 Code Mode 或 Collaboration V2 工作流。
- Beta3：完成 generic `tool_search` 生命周期和通用、可逆 Code Mode（对应 #63、#251、#279、#280）。建议在 ticket 中称“generic Code Mode”，不要称“Code Mode V2”。
- Beta4：完成 generic Collaboration V2 生命周期、真实 CLI 验证和 gate（对应 #252、#282、#283、#284；#199 负责用户 agent 配置持久化边界）。
- Beta5：对维护中的 GLM/K2.7 等 provider 做回归与资格化，不把维护模型验证误写成通用协议能力（#351）。

本研究未修改实现或 GitHub issues；后续规划应在这个概念边界上进行。
