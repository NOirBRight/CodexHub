# CodexHub Prototype Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Build a working CodexHub prototype that lets users configure third-party providers via providers.toml, switch Codex App between official and custom providers, and manage the proxy lifecycle.

**Architecture:** Python proxy (existing codebase, refactored to read providers.toml) runs as an independent background HTTP server. Tauri 2 app (Rust backend + React frontend) manages configuration, proxy process lifecycle, and model discovery. App and proxy have independent lifecycles.

**Tech Stack:** Python 3.12+ (proxy), Rust/Tauri 2 (desktop), React 18 + Vite + TypeScript + TailwindCSS (frontend)

---

## File Structure

### Phase 1: Python proxy refactor

- src-python/providers_config.py - NEW: providers.toml read/write + model discovery
- src-python/codex_proxy.py - MODIFIED: import from providers_config
- src-python/catalog_sync.py - MODIFIED: use providers_config
- config/providers.toml - NEW: user-editable provider config
- tests/test_providers_config.py - NEW

### Phase 2: Rust/Tauri backend

- src-tauri/Cargo.toml
- src-tauri/tauri.conf.json
- src-tauri/build.rs
- src-tauri/src/main.rs - entry, CLI dispatch, Tauri commands
- src-tauri/src/cli.rs - CLI argument handling
- src-tauri/src/config.rs - providers.toml + config.toml + settings I/O
- src-tauri/src/proxy.rs - start/stop/status proxy process
- src-tauri/src/models.rs - model discovery (OpenAI + provider)
- src-tauri/src/history.rs - history sync trigger
- src-tauri/src/catalog.rs - catalog generation trigger
- src-tauri/src/autostart.rs - OS auto-start registration

### Phase 3: React frontend

- src/App.tsx - main app with tab navigation
- src/pages/ProvidersPage.tsx - provider CRUD + model discovery + sorting
- src/pages/ModelsPage.tsx - official models toggle + refresh + model toggles
- src/pages/SettingsPage.tsx - sync toggle, auto-start, proxy port
- src/components/ProxyStatusBar.tsx - indicator + start/stop/restart
- src/components/ProviderCard.tsx
- src/components/SortableList.tsx - drag-and-drop
- src/lib/tauri.ts - invoke wrappers
- src/lib/types.ts - TypeScript types

---

## Phase 1: Python proxy refactor

### Task 1: Create providers_config.py with TOML parsing

Files:
- Create: src-python/providers_config.py
- Create: config/providers.toml
- Test: tests/test_providers_config.py

- [ ] Step 1: Write failing tests for load/save providers.toml (ProviderConfig dataclass, load_providers, save_providers, env var resolution, enabled/disabled models)
- [ ] Step 2: Run tests, verify ModuleNotFoundError
- [ ] Step 3: Implement ProviderConfig, ModelConfig dataclasses, load_providers, save_providers, resolved_api_key in providers_config.py
- [ ] Step 4: Run tests, verify pass
- [ ] Step 5: Create config/providers.toml with default Ollama Cloud, Volcengine, MiniMax.cn providers
- [ ] Step 6: Run full test suite to verify no regressions
- [ ] Step 7: Commit

### Task 2: Add external model resolution from providers.toml

Files:
- Modify: src-python/providers_config.py
- Test: tests/test_providers_config.py

- [ ] Step 1: Write failing tests for build_external_model_index and resolve_external_model_alias
- [ ] Step 2: Run tests, verify ImportError
- [ ] Step 3: Implement build_external_model_index(providers) -> dict[slug -> config dict] and resolve_external_model_alias(model_id) -> dict|None
- [ ] Step 4: Run tests, verify pass
- [ ] Step 5: Commit

### Task 3: Wire providers_config into codex_proxy and catalog_sync

Files:
- Modify: src-python/codex_proxy.py (replace provider_registry import)
- Modify: src-python/catalog_sync.py (replace configured_external_models)
- Modify: tests/test_routing.py (update mocks)
- Modify: tests/test_catalog_sync.py (update mocks)

- [ ] Step 1: Update codex_proxy.py: replace provider_registry import with providers_config.resolve_external_model_alias
- [ ] Step 2: Update catalog_sync.py: replace configured_external_models with providers_config.load_providers + build_external_model_index
- [ ] Step 3: Update test_routing.py setUp: patch codex_proxy.resolve_external_model_alias to return dicts matching providers_config format
- [ ] Step 4: Update test_catalog_sync.py fixtures similarly
- [ ] Step 5: Run full test suite, verify all pass
- [ ] Step 6: Commit

### Task 4: Add provider model discovery

Files:
- Modify: src-python/providers_config.py
- Test: tests/test_providers_config.py

- [ ] Step 1: Write failing test for discover_provider_models(base_url, api_key) that mocks urlopen and parses /models response
- [ ] Step 2: Run test, verify ImportError
- [ ] Step 3: Implement discover_provider_models: call base_url + /models, parse OpenAI-compatible response, return list of {id, context_window, max_output_tokens}
- [ ] Step 4: Run test, verify pass
- [ ] Step 5: Commit

### Task 5: Add official model discovery

Files:
- Modify: src-python/providers_config.py
- Test: tests/test_providers_config.py

- [ ] Step 1: Write failing test for discover_official_models(api_key) that mocks urlopen, filters gpt-* only
- [ ] Step 2: Run test, verify ImportError
- [ ] Step 3: Implement discover_official_models: call https://api.openai.com/v1/models, filter gpt-* prefix, return sorted list
- [ ] Step 4: Run test, verify pass
- [ ] Step 5: Commit

---

## Phase 2: Rust/Tauri backend

### Task 6: Scaffold Tauri 2 project

Files:
- Create: src-tauri/Cargo.toml (tauri 2, serde, toml, reqwest, tokio, dirs, which)
- Create: src-tauri/tauri.conf.json (window 900x600, CSP allowing 127.0.0.1)
- Create: src-tauri/build.rs
- Create: src-tauri/src/main.rs (Tauri entry + CLI dispatch + command registration)
- Create: src-tauri/src/cli.rs (status/switch/start/stop/refresh-models/sync-history/list-providers/list-models)

- [ ] Step 1: Create Cargo.toml with dependencies: tauri 2, tauri-plugin-shell, serde, serde_json, toml, dirs, reqwest (blocking+json), tokio, which, log
- [ ] Step 2: Create tauri.conf.json with window config and CSP
- [ ] Step 3: Create build.rs
- [ ] Step 4: Create main.rs with: mod declarations, CLI check (args[1] != GUI), Tauri builder with invoke_handler for all commands, Provider/Model/AppStatus/Settings structs
- [ ] Step 5: Create cli.rs with run() function dispatching: status, switch official/custom, start, stop, restart, refresh-models, sync-history, list-providers, list-models, app (launch GUI)
- [ ] Step 6: Create stub modules (config.rs, proxy.rs, models.rs, history.rs, catalog.rs, autostart.rs) with TODO functions
- [ ] Step 7: Verify cargo build succeeds
- [ ] Step 8: Commit

### Task 7: Implement config.rs (providers.toml + settings I/O)

Files:
- Create: src-tauri/src/config.rs

- [ ] Step 1: Implement get_providers() -> Vec<Provider>: read ~/.codex/proxy/config/providers.toml (or bundled config/), parse with toml crate, map to Provider/Model structs
- [ ] Step 2: Implement save_providers(providers): serialize to TOML, write to providers.toml
- [ ] Step 3: Implement switch_mode(mode, auto_sync): if auto_sync, call Python history_overlay normalize; then call config_overlay to write/restore config.toml
- [ ] Step 4: Implement get_settings()/save_settings(): read/write ~/.codex/proxy/settings.json (auto_sync_history, auto_start_proxy, include_official_models, proxy_port)
- [ ] Step 5: Write unit tests for TOML serialization roundtrip
- [ ] Step 6: Commit

### Task 8: Implement proxy.rs (process lifecycle)

Files:
- Create: src-tauri/src/proxy.rs

- [ ] Step 1: Implement start(): find Python executable (bundled sidecar or system python), spawn codex_proxy.py as detached background process (Command::new with stdin/stdout piped, DETACHED_PROCESS on Windows), write PID to ~/.codex/proxy/proxy.pid
- [ ] Step 2: Implement stop(): read proxy.pid, send shutdown signal via POST /shutdown or kill process by PID
- [ ] Step 3: Implement get_status(): HTTP GET http://127.0.0.1:{port}/health, return AppStatus with mode from config.toml, proxy_running from health response, proxy_build from response
- [ ] Step 4: Implement restart(): stop() then start()
- [ ] Step 5: Test: start proxy, verify health, stop proxy, verify stopped
- [ ] Step 6: Commit

### Task 9: Implement models.rs (model discovery)

Files:
- Create: src-tauri/src/models.rs

- [ ] Step 1: Implement refresh_official(): call Python providers_config.discover_official_models via subprocess, or call OpenAI API directly via reqwest, return Vec<Model>
- [ ] Step 2: Implement discover_provider(base_url, api_key): call provider /v1/models via reqwest, parse response, return Vec<Model>
- [ ] Step 3: Implement generate_catalog(): call Python catalog_sync.py --sync via subprocess, read generated catalog JSON
- [ ] Step 4: Commit

### Task 10: Implement history.rs and catalog.rs

Files:
- Create: src-tauri/src/history.rs
- Create: src-tauri/src/catalog.rs

- [ ] Step 1: history.rs: implement sync(target_provider) - call Python history_overlay.py normalize-fast with --target-provider, capture stdout/stderr, return result
- [ ] Step 2: catalog.rs: implement sync_catalog() - call Python catalog_sync.py --sync, return generated catalog path
- [ ] Step 3: Commit

### Task 11: Implement autostart.rs

Files:
- Create: src-tauri/src/autostart.rs

- [ ] Step 1: Windows: register Task Scheduler task to run codexhub start on login (schtasks /create)
- [ ] Step 2: macOS: write ~/Library/LaunchAgents/com.codexhub.proxy.plist
- [ ] Step 3: Linux: write ~/.config/systemd/user/codexhub-proxy.service
- [ ] Step 4: Implement remove_autostart() to unregister
- [ ] Step 5: Commit

---

## Phase 3: React frontend prototype

### Task 12: Scaffold React + Vite + TailwindCSS

Files:
- Create: package.json, vite.config.ts, tsconfig.json, tailwind.config.js, postcss.config.js
- Create: src/main.tsx, src/App.tsx, src/index.css
- Create: src/lib/types.ts, src/lib/tauri.ts

- [ ] Step 1: Initialize Vite React TS project in CodexHub root
- [ ] Step 2: Install TailwindCSS, configure tailwind.config.js and postcss.config.js
- [ ] Step 3: Create src/lib/types.ts with Provider, Model, AppStatus, Settings interfaces matching Rust structs
- [ ] Step 4: Create src/lib/tauri.ts with invoke wrappers for all Tauri commands
- [ ] Step 5: Create App.tsx with tab navigation (Providers, Models, Settings) + ProxyStatusBar at bottom
- [ ] Step 6: Verify npm run dev shows blank page with tabs
- [ ] Step 7: Commit

### Task 13: ProxyStatusBar component

Files:
- Create: src/components/ProxyStatusBar.tsx

- [ ] Step 1: Implement component: green/red dot, build version, Start/Stop/Restart buttons, calls cmd_get_status on mount and every 5s
- [ ] Step 2: Wire to tauri.ts invoke wrappers
- [ ] Step 3: Commit

### Task 14: ProvidersPage with CRUD + model discovery

Files:
- Create: src/pages/ProvidersPage.tsx
- Create: src/components/ProviderCard.tsx
- Create: src/components/SortableList.tsx

- [ ] Step 1: Implement SortableList: generic drag-and-drop list component using HTML5 drag events, calls onReorder with new order
- [ ] Step 2: Implement ProviderCard: shows provider name, base_url, model count, Edit/Delete buttons, expandable model list with toggles
- [ ] Step 3: Implement ProvidersPage: loads providers via cmd_get_providers, shows add-provider form (name, base_url, api_key, Test and Discover button), renders SortableList of ProviderCards, saves via cmd_save_providers on change
- [ ] Step 4: Add-provider flow: fill form, click Test and Discover, cmd_discover_provider_models returns models, user selects which to include, provider added to list
- [ ] Step 5: Commit

### Task 15: ModelsPage with official model refresh

Files:
- Create: src/pages/ModelsPage.tsx

- [ ] Step 1: Implement official models section: toggle Include official models, Refresh button calls cmd_refresh_official_models, checkbox list of available models with display names
- [ ] Step 2: Implement third-party models section: for each provider, show model list with individual toggles and per-provider Refresh button
- [ ] Step 3: Changes trigger cmd_save_providers and catalog regeneration
- [ ] Step 4: Commit

### Task 16: SettingsPage

Files:
- Create: src/pages/SettingsPage.tsx

- [ ] Step 1: Implement: auto-sync history toggle, auto-start proxy toggle, include official models toggle, proxy port input, all wired to cmd_get_settings/cmd_save_settings
- [ ] Step 2: Add Sync Now button that calls cmd_sync_history
- [ ] Step 3: Commit

### Task 17: Switch mode button + integration

Files:
- Modify: src/App.tsx

- [ ] Step 1: Add mode switch button (Official/Custom) in header, calls cmd_switch_mode
- [ ] Step 2: Show confirmation dialog before switching (warns about history sync)
- [ ] Step 3: After switch, refresh proxy status
- [ ] Step 4: Commit

---

## Phase 4: Integration testing

### Task 18: End-to-end prototype test

- [ ] Step 1: Start proxy via codexhub start, verify health endpoint
- [ ] Step 2: Switch to custom mode via codexhub switch custom
- [ ] Step 3: Launch Codex App, verify model list shows official + third-party models
- [ ] Step 4: Send a message with a third-party model (GLM-5.2), verify response
- [ ] Step 5: Switch back to official mode via codexhub switch official
- [ ] Step 6: Verify Codex App shows only official models
- [ ] Step 7: Test CLI: codexhub status, codexhub list-providers, codexhub refresh-models
- [ ] Step 8: Commit

---

## Phase 5: Packaging (post-prototype)

### Task 19: Embed Python runtime

- [ ] Step 1: Configure PyOxidizer or PyInstaller to bundle Python 3.12 + proxy dependencies into a sidecar binary
- [ ] Step 2: Update proxy.rs to use bundled Python path
- [ ] Step 3: Test on clean machine without Python installed

### Task 20: Tauri build + release

- [ ] Step 1: Add app icons
- [ ] Step 2: Configure tauri.conf.json bundle targets (msi, dmg, AppImage)
- [ ] Step 3: Run cargo tauri build, verify output
- [ ] Step 4: Test installer on clean Windows machine
- [ ] Step 5: Set up GitHub Actions CI for cross-platform builds

---

## Self-Review

### Spec coverage check

1. Provider management UI (add/edit/delete/sort) - Task 14
2. Model management UI (official toggle + refresh, third-party toggles + refresh) - Task 15
3. Model catalog display order - Task 14 SortableList + Task 9 generate_catalog
4. Sync history toggle - Task 16 SettingsPage + Task 10 history.rs
5. Official models refresh - Task 5 discover_official_models + Task 15 UI + Task 9 models.rs
6. Third-party model auto-discovery - Task 4 discover_provider_models + Task 14 UI
7. Proxy management UI - Task 13 ProxyStatusBar + Task 8 proxy.rs
8. CLI operations - Task 6 cli.rs
9. Auto-start proxy - Task 11 autostart.rs
10. Independent proxy lifecycle - Task 8 proxy.rs (detached spawn)
11. Embedded Python - Task 19
12. Single binary distribution - Task 20

Gaps: None identified.

### Placeholder scan

No TBD, TODO, or vague steps found. All steps specify exact files and actions.

### Type consistency

- Provider struct fields match across Rust (main.rs) and TypeScript (types.ts)
- Model struct fields consistent
- resolve_external_model_alias returns dict with keys: alias, upstream_name, base_url, api_key, upstream_model, display_prefix, context_window, max_output_tokens - matches codex_proxy.py choose_upstream expectations
