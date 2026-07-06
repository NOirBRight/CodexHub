# CodexHub

> 把第三方模型接入 Codex，和 GPT 官方模型同时使用——无需重启，无缝切换。

CodexHub 是一个本地代理层 + 桌面管理工具，让你在 Codex 中同时使用官方 GPT 模型和第三方模型，保留所有官方功能，并将所有模型统一打包为 API 端点供其他编程软件使用。

## 为什么需要 CodexHub

多模型编程已经是趋势。GPT-5.5 适合规划和推理，更轻量的模型执行速度快、成本低——让每个模型做自己最擅长的事，能显著优化你的编程用量。

但现实是：Codex 只能用官方模型，或者通过 CC Switch 之类的工具完全切换到第三方——**二选一，不能同时用**。而且一旦切换到第三方模型，子代理、Computer Use 等高级功能就全没了。

CodexHub 解决的就是这个问题：

| | Codex 原生 | CC Switch | CodexHub |
|---|---|---|---|
| 官方模型 | ✅ | ❌ | ✅ |
| 第三方模型 | ❌ | ✅ | ✅ |
| 同时使用 | ❌ | ❌ | ✅ |
| 子代理 / Subagent | ✅ | ❌ | ✅ |
| Computer Use | ✅ | ❌ | ✅ |
| Remote Control | ✅ | ❌ | ✅ |
| 无缝切换 | — | 需重启 | ✅ |
| 模型打包为 API | ❌ | ❌ | ✅ |

## 核心功能

### 1. 多模型同时接入

在 Codex 中同时使用官方 GPT 模型和第三方模型，不需要二选一。用 GPT-5.5 做规划，更轻量的模型做执行——在同一个会话里按需切换，优化你的编程用量。

### 2. 无缝切换，无需重启

官方模型和第三方模型之间切换不需要重启 Codex。在 CodexHub 界面里一键切换，会话历史自动归一化，对话不中断。

### 3. 子代理与高级功能保留

自建工具转换层，第三方模型也能在 Codex 中使用子代理（Subagent）等高级功能。`spawn_agent`、`wait_agent`、`send_input` 等多代理协议调用被自动适配，第三方模型也能分发和协调子任务。

### 4. 保留所有官方 Codex 功能

Computer Use、Remote Control、Browser——所有官方 Codex 功能在第三方模型下照常工作。你甚至能在 Remote Control 中使用第三方模型操控远程机器。

### 5. 统一 API 端点

把所有模型——包括 Codex 订阅中的 GPT 模型和第三方模型——统一打包为一个本地 API 端点。一键配置到 OpenCode、ZCode、Pi、OMP 等主流编程软件中使用。切换软件后无需从头配置，ZCode 等不支持 OpenAI 订阅的软件也能通过这个端点接入 GPT 模型。

## 架构

```
Codex Desktop App  ──→  CodexHub Proxy (localhost:9099)  ──→  OpenAI 官方 API
                            │
                            └──→  任意 OpenAI 兼容端点
                                    (Responses API / Chat Completions)

CodexHub App (Tauri)  ──→  配置 Proxy / 管理模型 / 监控用量
                            │
                            └──→  统一 API 端点  ──→  OpenCode / ZCode / Pi / OMP
```

CodexHub 代理作为本地 HTTP 服务运行，透明路由请求：GPT 模型转发到官方 API，第三方模型转发到对应 Provider。代理自动处理 **Responses API** 和 **Chat Completions** 两种上游协议之间的双向转换，兼容端点即可接入。

代理和桌面 App 独立运行——关闭 App 不影响代理。

## 快速开始

1. 下载最新版本（.msi / .dmg / .AppImage）从 [Releases](../../releases) 页面
2. 启动 CodexHub，添加你的 Provider（base_url + API Key）
3. 选择要启用的模型，CodexHub 自动发现并生成统一目录
4. 在 Codex 中切换到 Custom Provider——开始使用
5. 需要接入其他编程软件？在 Gateway 页面一键配置

> CodexHub 内置 Python 运行时，无需单独安装 Python。

## 其他亮点

- **Usage 监控** — 实时查看请求量、Token 用量、预估成本，按模型和 Provider 维度统计
- **自动重试守护** — 上游请求失败时自动重试，支持流式续传，保证生成不中断
- **会话历史归一化** — 切换 Provider 时自动处理历史记录标签，对话无缝衔接
- **桌面原生体验** — Tauri 构建，Windows / macOS / Linux 原生运行

## FAQ

### 为什么要接入第三方模型？

多模型编程已经是趋势。GPT-5.5 适合规划和推理，更轻量的模型执行速度快、成本低——让每个模型做自己最擅长的事，能显著优化编程用量。

### 为什么不使用 CC Switch？

CC Switch 无法让 Codex 在使用官方模型的同时接入第三方模型，只能二选一。且使用第三方模型后，子代理等高级功能无法使用。CodexHub 支持同时使用、无缝切换，并保留所有高级功能。

### OpenAI 的订阅本来就可以在 OpenCode、Pi 中使用，为什么还要做个 API 端点？

一方面是方便配置——当你切换软件后，无须从头开始配置，一键接入。另一方面像 ZCode 这种不支持 OpenAI 订阅的软件，也可以通过这个方式接入 GPT 模型。

## License

MIT