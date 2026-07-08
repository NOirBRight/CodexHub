# CodexHub

> [中文](README.zh-CN.md) | English

> Bring third-party models into Codex, side by side with official GPT models — no restart, seamless switching.

CodexHub is a local proxy layer + desktop management tool that lets you use official GPT models and third-party models together in Codex, preserves all native features, and packages every model as a unified API endpoint for other coding tools.

## Why CodexHub

Multi-model programming is the trend. GPT-5.5 excels at planning and reasoning; lighter models execute faster and cost less — letting each model do what it does best optimizes your coding spend.

But the reality is: Codex only supports official models, or you use a tool like CC Switch to switch entirely to third-party — **one or the other, never both**. And once you switch to a third-party model, subagents, Computer Use, and other advanced features are gone.

CodexHub solves exactly this:

| | Codex Native | CC Switch | CodexHub |
|---|---|---|---|
| Official models | ✅ | ❌ | ✅ |
| Third-party models | ❌ | ✅ | ✅ |
| Use both at once | ❌ | ❌ | ✅ |
| Subagents | ✅ | ❌ | ✅ |
| Computer Use | ✅ | ❌ | ✅ |
| Remote Control | ✅ | ❌ | ✅ |
| Seamless switching | — | Restart needed | ✅ |
| Models as API endpoint | ❌ | ❌ | ✅ |

## Core Features

### 1. Multi-Model Side-by-Side

Use official GPT models and third-party models together in Codex — no need to pick one. Plan with GPT-5.5, execute with a lighter model — switch on the fly within the same session to optimize your coding spend.

### 2. Seamless Switching, No Restart

Switching between official and third-party models doesn't require restarting Codex. One click in the CodexHub UI — conversation history is automatically normalized, no dialogue interruption.

### 3. Subagents & Advanced Features Preserved

A custom tool translation layer lets third-party models use subagents and other advanced features in Codex. `spawn_agent`, `wait_agent`, `send_input` and other multi-agent protocol calls are automatically adapted — third-party models can dispatch and coordinate subtasks too.

### 4. All Native Codex Features Retained

Computer Use, Remote Control, Browser — all native Codex features work normally under third-party models. You can even use third-party models in Remote Control to operate remote machines.

### 5. Unified API Endpoint

Package every model — including GPT models from your Codex subscription and third-party models — into a single local API endpoint. One-click configure for OpenCode, ZCode, Pi, OMP and other major coding tools. No need to reconfigure when switching tools, and tools like ZCode that don't support OpenAI subscriptions can access GPT models through this endpoint.

## Architecture

```
Codex Desktop App  ──→  CodexHub Proxy (localhost:9099)  ──→  OpenAI Official API
                            │
                            └──→  Any OpenAI-compatible endpoint
                                    (Responses API / Chat Completions)

CodexHub App (Tauri)  ──→  Configure Proxy / Manage Models / Monitor Usage
                            │
                            └──→  Unified API Endpoint  ──→  OpenCode / ZCode / Pi / OMP
```

The CodexHub proxy runs as a local HTTP service, transparently routing requests: GPT models forward to the official API, third-party models forward to their respective providers. The proxy automatically handles bidirectional conversion between **Responses API** and **Chat Completions** upstream protocols — any compatible endpoint works.

The proxy and desktop app run independently — closing the app does not affect the proxy.

## Quick Start

1. Download the latest version from the [Releases](../../releases) page
2. Launch CodexHub, add your provider (base_url + API Key)
3. Select models to enable — CodexHub auto-discovers and generates a unified catalog
4. Switch to Custom Provider in Codex — start using
5. Want to connect other coding tools? One-click configure in the Gateway page

> v0.1.0 beta bundles the proxy scripts and default configuration, but requires Python 3.11+ on `PATH` or configured through `CODEXHUB_PYTHON` / `CODEXHUB_PROXY_PYTHON`.

## More Highlights

- **Usage Monitoring** — Real-time view of request volume, token usage, and estimated cost, broken down by model and provider
- **Auto-Retry Guardian** — Automatically retries failed upstream requests with stream continuation, ensuring generation is not interrupted
- **Conversation History Normalization** — Automatically handles history record labels when switching providers, seamless dialogue continuity
- **Native Desktop Experience** — Built with Tauri, native Windows support

## FAQ

### Why bring in third-party models?

Multi-model programming is the trend. GPT-5.5 excels at planning and reasoning; lighter models execute faster and cost less — letting each model do what it does best significantly optimizes your coding spend.

### Why not use CC Switch?

CC Switch cannot let Codex use official models and third-party models at the same time — it's one or the other. And after switching to third-party models, advanced features like subagents are unavailable. CodexHub supports using both simultaneously, seamless switching, and preserves all advanced features.

### OpenAI subscriptions already work in OpenCode, Pi — why make an API endpoint?

For one, it's convenient configuration — when you switch tools, no need to set up from scratch, one-click connect. For another, tools like ZCode that don't support OpenAI subscriptions can access GPT models through this endpoint.

## License

MIT
