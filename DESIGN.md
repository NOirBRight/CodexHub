# CodexHub Design Document

## Overview

CodexHub is a local proxy + configuration manager for OpenAI Codex desktop app.
It lets users use official OpenAI models and third-party providers side by side,
with a native desktop UI for configuration and a CLI for scripting.

## Architecture

### Process model

```
CodexHub App (Tauri)        ←─── opens, configures, closes
  │
  ├── reads/writes config files (config.toml, providers.toml)
  ├── starts/stops Python proxy (independent background process)
  └── closes. Proxy keeps running.

Python Proxy (codex_proxy.py)  ←─── standalone HTTP server on localhost:9099
  │
  ├── routes requests to official OpenAI or third-party endpoints
  ├── runs independently of the Tauri app
  ├── health check: GET /health
  └── survives app closure

Codex Desktop App            ←─── talks to proxy via http://127.0.0.1:9099
```

Key principle: **proxy and app have independent lifecycles**.
- App is a configuration UI + control panel (open, configure, close).
- Proxy is a persistent background service (start, run, stop).
- Closing the app does NOT kill the proxy.
- App re-open checks proxy health and shows status.

### Startup / shutdown flow

1. User opens CodexHub app → app reads current mode (official/custom) from config.toml
2. If custom mode → app checks proxy health via GET /health
   - If healthy → show "Proxy: running"
   - If not → show "Proxy: stopped" with "Start" button
3. User configures providers, models, toggles → app writes config files
4. User clicks "Switch to Custom" → app writes config.toml, ensures proxy is started
5. User closes app → proxy keeps running
6. Auto-start proxy on system login if last mode was custom (enabled by default)

### Proxy lifecycle commands

- `codexhub start` / app button → spawn Python proxy as detached background process
- `codexhub stop` / app button → send shutdown signal (or kill by PID)
- `codexhub status` → check /health, print running/stopped
- `codexhub restart` → stop + start

## Tech stack

### Desktop app: Tauri 2 + React + Vite + TypeScript + TailwindCSS

Same stack as CC Switch. Native desktop binary, no web server.
- React frontend bundled into Tauri binary
- Rust backend handles file I/O, process management, model API calls
- Packaged as .msi (Windows), .dmg (macOS), .AppImage (Linux)

### Proxy: Python 3.12+ (existing codebase)

- HTTP proxy server (codex_proxy.py)
- Catalog sync (catalog_sync.py)
- Provider registry (provider_registry.py)
- Distributed as pip-installable package + bundled in Tauri sidecar

### CLI: codexhub command

```
codexhub app                    # Open desktop UI
codexhub switch official        # Switch to official provider
codexhub switch custom          # Switch to custom (proxy) provider
codexhub start                  # Start proxy background process
codexhub stop                   # Stop proxy
codexhub status                 # Show proxy + mode status
codexhub refresh-models         # Refresh model catalog from all providers
codexhub sync-history           # Normalize history labels for current provider
codexhub list-providers         # List configured providers
codexhub list-models            # List available models
```

## Configuration

### providers.toml (user-editable, UI-managed)

```toml
[[providers]]
id = "ollama-cloud"
name = "Ollama Cloud"
base_url = "https://ollama.com/v1"
api_key = "{env:OLLAMA_API_KEY}"
display_prefix = "Ollama"
sort_order = 1

  [[providers.models]]
  id = "minimax-m3"
  display_name = "Ollama MiniMax-M3"
  context_window = 512000
  max_output_tokens = 524288
  sort_order = 1

  [[providers.models]]
  id = "glm-5.2"
  display_name = "Ollama GLM-5.2"
  sort_order = 2

[[providers]]
id = "volcengine"
name = "Volcano Engine"
base_url = "https://ark.cn-beijing.volces.com/api/coding/v3"
api_key = "{env:VOLCENGINE_API_KEY}"
display_prefix = "Volc"
sort_order = 2

  [[providers.models]]
  id = "glm-5.2"
  display_name = "Volc GLM-5.2"
  sort_order = 1
```

### catalog_policy.toml (generated from UI settings)

Routing rules, model visibility, display names. Generated, not hand-edited.

### config.toml (Codex App config, managed by CodexHub)

CodexHub writes/overlays provider section when switching modes.

## Features

### 1. Provider management (UI page)

- Add provider: enter name, base_url, api_key → click "Test & Discover"
  - Calls provider's /v1/models endpoint to auto-discover available models
  - Shows discovered models, user selects which to include
- Edit provider: change name, base_url, api_key, re-discover models
- Delete provider: removes provider and its models from catalog
- Sort providers: drag-and-drop or up/down arrows, persisted as sort_order
- Sort models within provider: drag-and-drop or up/down arrows

### 2. Model management (UI page)

- Official models section:
  - Toggle: "Include official OpenAI models" (on/off)
  - "Refresh" button: calls OpenAI API to get current model list
  - Checkbox list of available official models (gpt-5.5, gpt-5.4, etc.)
  - New models (e.g. GPT-5.6) appear after refresh, user enables them
- Third-party models section (per provider):
  - Each provider shows its model list
  - Toggle individual models on/off
  - "Refresh" button per provider: re-discovers models from provider API
  - Sort models within provider

### 3. Model catalog display order

The catalog sent to Codex App respects user-defined sort order:
1. Official models (if enabled) in user-specified order
2. Third-party models grouped by provider, in user-specified order
3. Within each provider group, models in user-specified order

### 4. Sync history toggle (UI settings page)

- Toggle: "Auto-sync conversation history on provider switch"
  - On: switching provider runs history_overlay normalize before writing config
  - Off: switching only writes config.toml, does not touch history
- Manual sync: "Sync Now" button runs history_overlay for current provider

### 5. Official models refresh

- Calls OpenAI /v1/models endpoint (through existing auth)
- Parses response for gpt-* models
- Updates available model list
- User selects which to include in catalog
- Handles new model releases (GPT-5.6 etc.) gracefully

### 6. Third-party model auto-discovery

When adding or refreshing a provider:
- Calls provider's base_url + /models (or /v1/models)
- Parses OpenAI-compatible model list
- Shows discovered models with auto-detected context/output limits where available
- User confirms which to add

### 7. Proxy management (UI status bar)

- Status indicator: green (running) / red (stopped)
- Start / Stop / Restart buttons
- Port number (default 9099, configurable)
- Build version and features
- Last 5 request summaries from event log

## Installation

### Single binary download

Download .msi (Windows) / .dmg (macOS) / .AppImage (Linux) from GitHub Releases.
The Tauri binary is both GUI and CLI. No Node.js, Python, or pip required.

`
codexhub                    # No args = open GUI
codexhub status             # CLI: check proxy status
codexhub switch custom      # CLI: switch to custom provider
codexhub start              # CLI: start proxy
codexhub refresh-models     # CLI: refresh model catalog
`

### Embedded Python runtime

Python 3.12+ runtime is bundled inside the Tauri binary as a sidecar (PyOxidizer or PyInstaller).
Users do not need to install Python separately.

### Auto-start proxy on system login

Enabled by default. Proxy auto-starts on system login if last mode was custom. Can be disabled in settings.
- Windows: Task Scheduler (default on)
- macOS: launchd (default on)
- Linux: systemd user unit (default on)

## File layout

```
CodexHub/
  src-tauri/              Rust backend (Tauri 2)
    src/
      main.rs             Tauri entry, command registration
      commands/           Tauri commands (invoked from frontend)
        config.rs         Read/write providers.toml, config.toml
        proxy.rs          Start/stop/status proxy process
        models.rs         Model discovery, refresh, catalog generation
        history.rs        History sync toggle and execution
      Cargo.toml
      tauri.conf.json
    src/                  React frontend
      App.tsx
      pages/
        ProvidersPage.tsx
        ModelsPage.tsx
        SettingsPage.tsx
      components/
        ProviderCard.tsx
        ModelList.tsx
        SortableList.tsx
        ProxyStatus.tsx
      lib/
        tauri.ts          Tauri invoke wrappers
        types.ts          TypeScript types
    src-python/           Python proxy (existing codebase)
      codex_proxy.py
      catalog_sync.py
      catalog.py
      provider_registry.py
      config_overlay.py
      global_state_repair.py
      history_overlay.py
      history_consolidate.py
      probe_provider_endpoints.py
      bucket_sync.py
    config/
      catalog_policy.toml
    scripts/
      codex-mode.cmd
      codex-mode.ps1
      run-codex-proxy.ps1
      launch-codex-proxy-app.ps1
    tests/
      test_routing.py
      ...
    frontend/             (empty, using src/ instead for Tauri)
    .gitignore
    README.md
    DESIGN.md
```

## Decisions

1. **Python runtime**: Embedded in Tauri binary via PyOxidizer/PyInstaller sidecar. No separate Python install required.
2. **Distribution**: Single Tauri binary from GitHub Releases (.msi/.dmg/.AppImage). No npm/pip for v1.
3. **Auto-start proxy**: Enabled by default on system login if last mode was custom. Can be disabled in settings.
4. **Proxy lifecycle**: Independent of app. App closes, proxy keeps running. App re-open checks health.
5. **Model refresh**: Both official (OpenAI /v1/models) and third-party (provider /v1/models). Auto-discover on provider add.
6. **Sorting**: Drag-and-drop for providers and models-within-provider. Persisted as sort_order in providers.toml.

## Open questions

1. Python distribution: bundle Python runtime in Tauri sidecar (PyOxidizer/PyInstaller)
   vs require user to install Python separately?
   - Recommendation: bundle for v1, simpler UX
2. Auto-start proxy on system login: implement via Windows Task Scheduler / launchd / systemd?
   - Recommendation: yes, optional toggle in settings
3. i18n: CC Switch uses i18next. Should CodexHub support Chinese + English from day 1?
   - Recommendation: yes, given target audience
4. Update mechanism: CC Switch uses Tauri updater plugin. Should CodexHub auto-update?
   - Recommendation: yes, same approach
