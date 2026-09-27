# ADR-0015: Managed ChatGPT Web Runtime behind the existing Gateway

Date: 2026-09-27
Status: Accepted — scope, responsibilities, eight slices, and Codex CLI/Desktop acceptance approved

Tracking: [specification and implementation slices #567](https://github.com/NOirBRight/CodexHub/issues/567).

Users want ChatGPT webpage models available through CodexHub to Codex, Claude Code,
and OpenCode, including a complete plaintext V2 lifecycle in Codex. The Spike at
`2924d113` proved real text, ordinary tool transport, and one plaintext V2 spawn/result
transport, but did not prove a production Gateway route or real child execution.

CodexHub will automatically install and manage a version-pinned ChatGPT Web Runtime
with one private browser account and a separate CodexHub-opened login/diagnostic
window. Gateway keeps route selection, compatibility, and Client Projection;
clients keep tool execution and agent scheduling. The runtime owns the browser,
tunnel, and per-turn MCP delivery. Its installer must never take over client config
or reuse the Spike's DEV configuration as a production listener.

This chooses a managed independent browser implementation over moving Electron's
automation into Tauri's webview. It accepts a second runtime's packaging and update
cost to retain the implementation already exercised by the Spike. A narrow local
versioned contract separates readiness, account-visible Web model identity,
request submission, cancellation, and drain from Gateway's public client protocols.

The implementation must preserve ADR-0002's fixed selected route and client-owned
execution, ADR-0004's client configuration ownership, and ADR-0006/0010's owning
modules and command registry. Browser state is not a refreshable OAuth token under
ADR-0005. Provider Preset metadata under ADR-0008 does not own runtime installation.

First release requires real Codex V2 spawn, message, follow-up, wait, list, interrupt,
and terminal delivery, including same-Web-runtime parent/child progress at bounded
capacity. Opaque encrypted input remains explicitly unsupported; no global V1
downgrade, alternative-model fallback, invented agent, or fabricated tool result.
