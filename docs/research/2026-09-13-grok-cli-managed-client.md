# Grok CLI as a CodexHub managed client: evidence and recommended operation

Date: 2026-09-13
Scope: Research only. This note records whether official xAI **Grok CLI**
(Grok Build / binary `grok`, config at `~/.grok/config.toml` or
`$GROK_HOME/config.toml`) can join CodexHub's official managed-client roster
under Provider Injection (ADR-0004). It does not add, imply, or authorize a
production adapter, a rewrite of live `~/.grok/config.toml`, a live
`grok -p` inference, or a change to the existing xAI Maintained Provider.

## Evidence boundary and source pins

All protocol and product claims below are from xAI's Grok Build documentation,
the grok-build source that owns the config schema, this host's official CLI
install (user-guide extracted under `~/.grok/docs/`), or CodexHub's own ADRs
and managed-client seam. Community CLIs named "grok-cli" are cited only as a
rejected identity.

| Source | Pinned / accessed evidence | What it establishes |
| --- | --- | --- |
| [Grok Build overview](https://docs.x.ai/build/overview) | Accessed 2026-09-13 | Product name **Grok Build**; install `curl -fsSL https://x.ai/cli/install.sh \| bash`; binary `grok`; first launch browser auth or `XAI_API_KEY`; custom models in `~/.grok/config.toml` with `base_url` + `env_key`; select with `grok -p … -m` / TUI `/model`. |
| [Settings](https://docs.x.ai/build/settings) | Accessed 2026-09-13 | User config `$GROK_HOME/config.toml` (default `~/.grok/config.toml`); project `.grok/config.toml` is MCP/plugins/permissions only; managed/requirements layers; `grok inspect` lists loaded sources; example `[model."grok-4.6"]` with `api_backend` and `supports_backend_search`. |
| [Settings reference](https://docs.x.ai/build/settings/reference) | Accessed 2026-09-13 | `GROK_HOME` default `~/.grok`; `XAI_API_KEY`; `GROK_DEFAULT_MODEL`; **`GROK_MODELS_BASE_URL`** = custom inference base, list from `{base}/models`; `[models] default`; `[model.]` fields `base_url` / `env_key` / `api_key` / `api_backend`. |
| [CLI reference](https://docs.x.ai/build/cli/reference) | Accessed 2026-09-13 | `grok login` / `--device-auth`, `logout`, `inspect [--json]`, `models`, `version`, `-m` / `--model`. |
| [Enterprise](https://docs.x.ai/build/enterprise) | Accessed 2026-09-13 | Install also `npm install -g @xai-official/grok`; credential order `model.api_key` > `model.env_key` > session token > `XAI_API_KEY`; BYOK endpoints whose `base_url` is not on `x.ai` keep working under SSO locks; local history in `~/.grok/`. |
| [Sessions](https://docs.x.ai/build/features/sessions) | Accessed 2026-09-13 | History under `~/.grok/sessions/`, keyed by working directory + session id, not by provider id. |
| [Headless](https://docs.x.ai/build/cli/headless-scripting) | Accessed 2026-09-13 | `grok -p`, `-m`, `--output-format`; ACP `grok agent stdio` is a host surface, not a Gateway client. |
| grok-build [README](https://github.com/xai-org/grok-build) | Accessed 2026-09-13 | Official repo: SpaceXAI coding agent; binary artifact `xai-grok-pager`, shipped as `grok`; docs at `crates/codegen/xai-grok-pager/docs/user-guide/`. |
| grok-build [11-custom-models.md](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/11-custom-models.md) | Fetched 2026-09-13 (main) and local `~/.grok/docs/user-guide/11-custom-models.md` (CLI 1.0.30) | Three wire APIs; multiple `[model.*]` coexist; local OpenAI-compatible example `http://localhost:8080/v1`; `[endpoints] models_base_url` replaces catalog + forces API-key auth. |
| grok-build [05-configuration.md](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/05-configuration.md) | Fetched 2026-09-13 + local 1.0.30 extract | `GROK_CONFIG` overlay cannot add `[model.*]` (allowlist is soft settings only); `~/.grok/auth.json`; config for toolset is read at session start. |
| grok-build [`config.rs` `resolve_model_list`](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-shell/src/agent/config.rs) | Fetched 2026-09-13 | `has_custom_endpoint()` **skips built-in defaults** ("custom models endpoint active, skipping built-in defaults"). Takeover of the official catalog. |
| grok-build [`model_providers.rs`](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-shell/src/agent/model_providers.rs) | Fetched 2026-09-13 | `[model_providers.<id>]` holds `base_url`, `api_key`, `env_key`, `api_backend`; models with `model_provider` inherit. Custom `base_url` without a credential is BYOK; tests assert the session JWT does not leak to that URL. |
| grok-build [`config_model_override_parse.rs`](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-shell/src/agent/config_model_override_parse.rs) | Fetched 2026-09-13 | Every `[model.<key>]` is an independent catalog row; invalid fields warn and skip, other entries stay. |
| grok-build sampling tests | `crates/codegen/xai-grok-shell/tests/test_sampling_client.rs` | `api_backend = chat_completions` hits `{base}/chat/completions`; `responses` hits `{base}/responses`. |
| [npm `@xai-official/grok`](https://www.npmjs.com/package/@xai-official/grok) | Accessed 2026-09-13 | Official npm wrapper for the same `grok` binary (`npm i -g @xai-official/grok`). |
| [stevederico/grok-cli](https://github.com/stevederico/grok-cli) | Accessed 2026-09-13 | Unrelated community product (`npm i -g @stevederico/grok-cli`, `GROKCLI_PROVIDER`). Not this binary. |
| ADR-0004 | Repo `docs/adr/0004-coexistence-provider-injection.md` | One owned Injected Block, never activate, surgical detach, block-fingerprint. |
| `src-tauri/src/gateway/managed_clients.rs` | Current coordinator | Adapters: `dsh`, `codex`, `opencode`, `pi`, `omp`, `zcode`. |
| Cursor managed-client note | `docs/research/2026-09-09-cursor-managed-client.md` | Same admission bar; Cursor was rejected. |

Opposite-direction research (xAI as a Gateway **upstream**) is out of scope:
`docs/research/2026-09-07-xai-grok-third-party-incompatibilities.md`,
`docs/research/2026-09-13-xai-chat-endpoint-check.md`.

### CLI and platform pin

Verified locally without sending an inference request (`grok version`,
`grok --help`, listing `$HOME/.grok`, reading `config.toml` keys only):

| Item | Observation |
| --- | --- |
| Product | `grok --help` → "Grok Build TUI" |
| Version | `grok 1.0.30 (04b7ffed98c6)`; `~/.grok/version.json` `"version": "1.0.30"` |
| Binary | `/home/noirbright/.local/bin/grok` → `~/.grok/bin/grok` → `~/.grok/downloads/grok-1.0.30-linux-x86_64` |
| Home | `~/.grok` (`GROK_HOME` unset) |
| User config | `~/.grok/config.toml` — `[models] default = "grok-4.6"`; `[cli] installer = "internal"`; **no** `[model.*]`, `[model_providers.*]`, or `[endpoints]` today |
| Credentials file | `~/.grok/auth.json` present (not dumped; session login store) |
| Bundled user-guide | `~/.grok/docs/user-guide/` (01, 02, 05, 11, 14, 17, 26, …) matches the grok-build user-guide tree |
| npm globals | `@xai-official/grok` and `@stevederico/grok-cli` are **not** installed globally on this host; this install is the `x.ai/cli/install.sh` / internal updater path |

A later implementation spike must re-pin the exact `grok` version it uses.
1.0.30 is the pin for this note, not a release qualifier.

## Two product questions (do not mix)

1. **CodexHub → Grok CLI (managed client).** Inject Gateway so Grok CLI
   consumes local CodexHub Gateway the way OpenCode, Pi, OMP, ZCode, and DSH
   do. That is this campaign.
2. **Grok CLI / xAI subscription → other clients (Maintained Provider).**
   Already exists as the `xai` provider in `config/providers.toml`
   (`upstream_format = "responses"`, `auth_capabilities = ["subscription:xai_oauth"]`).
   Do not reopen as this campaign.

Those two surfaces **coexist on one machine** and must not be looped:

- Other clients (Codex, OpenCode, Pi, …) still receive CodexHub's `xai`
  Maintained Provider through Gateway.
- Grok CLI already has the same Grok models via `grok login` / the built-in
  catalog (`grok-4.6`, `grok-4.5`, `grok-build`, …).
- The Grok CLI adapter therefore **must not project** CodexHub's `xai`
  subscription models into `~/.grok/config.toml`. Injecting
  `codexhub-xai-grok-4.6` would duplicate the native picker row and add a
  Grok → Gateway → api.x.ai hop the user did not ask for.

Do not treat community `grok-cli` as this client. Official identity is
**Grok Build**, binary **`grok`**, home **`$GROK_HOME` / `~/.grok`**.

## CodexHub contract the new client would have to meet

From ADR-0004, CONTEXT.md, and `src-tauri/src/gateway/managed_clients.rs`:

- Inject exactly one owned Injected Block (route key `codexhub`, plus the
  existing `codexhub-*` prefix used by OpenCode/Pi in
  `inject.rs` `codexhub_client_provider_id`).
- Preserve every user-owned provider, model, setting, MCP server, and plugin.
- Never flip the client's global default (`[models] default`, also
  `GROK_DEFAULT_MODEL` / `-m`). Activation is the user's action.
- Credentials are one surgical key; Detach removes only the owned tables + key.
- Readback is block-fingerprint, not whole-file byte compare.
- Adapters expose `metadata()` / `inspect()` / `plan()`; the coordinator
  publishes. DSH (`clients/dsh.rs`) is the YAML reference; OpenCode is the
  JSON provider-object reference. Grok is TOML model sections.

Current official roster in `list_gateway_clients`
(`src-tauri/src/gateway/mod.rs`): Generic (copy-only), OpenCode, ZCode, Pi,
OMP, DSH. Codex is the history-bucket exception and is not in that UI list.
Adding Grok CLI means a new adapter on the same seam, plus isolated
apply/readback (`docs/agents/real-client-e2e.md`).

Gateway endpoints the Injected Block would advertise today:

- `http://127.0.0.1:<port>/v1`
- `/v1/models`, `/v1/chat/completions`, `/v1/responses`
- Per-upstream: `/v1/providers/<id>/chat/completions` and
  `/v1/providers/<id>/responses` (`inject.rs` `gateway_client_provider_base_url`)

The client must call that loopback HTTP service **directly**. Grok's custom
`[model.*] base_url` is documented to do exactly that (Ollama /
`localhost:8080/v1` examples).

## Verified Grok CLI surfaces

### A. Identity (question 1)

| Item | Official Grok Build | Not this product |
| --- | --- | --- |
| Product name | Grok Build (docs.x.ai/build, x.ai/cli) | Community "Grok CLI" |
| Binary | `grok` (source crate `xai-grok-pager`) | `@stevederico/grok-cli` (separate npm) |
| Home | `GROK_HOME` default `~/.grok` (Windows `%USERPROFILE%\.grok`) | `GROKCLI_*` env vars |
| Config | `config.toml`, `pager.toml`, `sandbox.toml` | Community provider env map |
| Session auth | `~/.grok/auth.json` from `grok login` | N/A |
| MCP OAuth | `~/.grok/mcp_credentials.json` | N/A |
| Install | `https://x.ai/cli/install.sh` / `install.ps1`; alt `npm i -g @xai-official/grok` | `npm i -g @stevederico/grok-cli` |

Detect installed: `PATH` `grok` **or** `$GROK_HOME` / `~/.grok` (same shape as
DSH/Pi). Distinguish official by `grok --version` / `grok --help` containing
"Grok Build". Do not treat a `grok` that is the community npm shim as this
adapter without that pin.

### B. Injectable surface (question 2)

Grok has a first-party, documented, user-owned **model map**:

- Any number of `[model.<catalog-key>]` tables coexist in user `config.toml`.
- Optional `[model_providers.<name>]` holds shared `base_url` / `api_key` /
  `env_key` / `api_backend`; a model sets `model_provider = "<name>"` to
  inherit (grok-build `model_providers.rs`, local `26-config-reference.md`).
- CodexHub can add owned tables without rewriting `[ui]`, `[mcp_servers]`,
  `[plugins]`, or foreign `[model.ollama-*]`.
- Parser keeps other `[model.*]` entries if one table has a bad field
  (`config_model_override_parse.rs`).

That is the OpenCode/DSH analog, flattened because Grok has no nested
`provider.models[]` array. It is **not** Cursor BYOK (one overwrite slot).

### C. Protocol (question 3)

From 1.0.30 `11-custom-models.md` and sampling tests:

| `api_backend` | Wire | Default for custom models |
| --- | --- | --- |
| `chat_completions` | `{base_url}/chat/completions` | Yes when omitted |
| `responses` | `{base_url}/responses` | Official hosted Grok models |
| `messages` | Anthropic `/v1/messages` | Not a Gateway inbound surface |

Headers: `Authorization: Bearer` from `api_key` / `env_key`. `extra_headers`
are sent verbatim. Required xAI-only headers are **not** required for a
custom `base_url`.

`GROK_MODELS_BASE_URL` / `[endpoints] models_base_url`:

- Fetches `{base}/models` and uses that URL as the **inference** base
  (`EndpointsConfig::resolve_inference_base_url`).
- `resolve_model_list` **skips built-in defaults** when
  `has_custom_endpoint()` is true.
- Auth switches to API key; `grok login` is not required.

That replaces the whole catalog, including official Grok. **Managed Takeover.
Reject as the official Apply path.**

Per-model `base_url` is the Provider Injection fit: official `grok-4.6` stays
on xAI; CodexHub rows call `127.0.0.1`.

`GROK_CONFIG` / `GROK_CONFIG_PATH` cannot inject `[model.*]` (allowlist of
soft settings only, 05-configuration.md). Do not use the overlay as Apply.

### D. Credentials (question 4)

Order (11-custom-models.md / enterprise / 02-authentication.md):

1. Per-model `api_key`
2. `env_key` (process environment; string or array)
3. Signed-in session token (`~/.grok/auth.json`)
4. `XAI_API_KEY` (global fallback)

There is **no** DSH-style `.credentials.yaml` that `grok` loads. `auth.json`
is session login only — CodexHub must **never** read or write it.

`env_key = "CODEXHUB_API_KEY"` only works if that variable is in the `grok`
**process** environment. `[session] load_envrc` injects `.envrc` into **bash
tools**, not into Grok's own HTTP client. CodexHub cannot honestly guarantee a
user-launched TUI sees a sidecar env var.

OpenCode and Pi already write `settings.gateway_client_key` inline on the
owned provider. Do the same on owned `[model_providers.codexhub-*] api_key`.
Detach removes the tables, so the key leaves the file. Mask in previews
(`injection.rs` `MaskedSecret`).

Prefer that over `env_key` for Apply. Isolated E2E may additionally pass
`CODEXHUB_API_KEY` in the runner process.

Never put the Gateway key in `[models] extra_headers` (that would attach to
every model, including official Grok). Never set `XAI_API_KEY` as Apply.

grok-build tests: a custom `base_url` without a credential is BYOK; the session
JWT must not leak. Still write an explicit Gateway key so Apply does not
depend on that fail-closed path.

### E. Activation (question 5)

Global default is `[models] default` (also `GROK_DEFAULT_MODEL`, `-m`, TUI
`/model` / `/m`, Ctrl+M picker). Apply must not write it.

After Connect, the user selects a `codexhub-*` catalog key in a **new**
session. That matches ADR-0004: Apply is inject/detach; pointing at Gateway is
the user's action.

### F. Hot reload / restart (question 6)

- `auth.json` hot-reloads (02-authentication.md "Hot Reload").
- Model/toolset config is read at **session start**; in-flight sessions
  snapshot (05-configuration.md; grok-build `agent_ops.rs` `/new` re-resolves).
- Several `/settings` keys and `[ui.status_line]` require a process restart
  (25-status-line.md).

Disclose **restart Grok CLI** (quit and relaunch, or `/new` after a full
restart if the running process already loaded the old file). DSH's
`restart_required = "none"` does not apply. User-feedback rule: name the
exact process (`docs/agents/user-feedback.md`).

### G. Session history (question 7)

Sessions live under `~/.grok/sessions/<encoded-cwd>/<session-id>/`
(17-sessions.md). Resume is by session id / cwd, not by `model_provider`.
`/model` can switch mid-session. **No Codex history-bucket exception.**

### H. Tools / agent protocol (question 8)

Grok is an agent harness. Client-side tools (bash, edit, search, MCP) are
sent to the custom endpoint as ordinary function tools on Chat Completions or
Responses. Gateway already accepts those inbound shapes.

`supports_backend_search = true` asks for **Grok-hosted server-side search**
(inline `server_tool_use` / `web_search_tool_result`, 14-headless-mode.md).
Gateway is not that backend. Owned models must omit it or set `false`.

If Gateway is Chat-only for that upstream, set `api_backend = "chat_completions"`.
If Responses, set `responses`. Never `messages` (fold Anthropic through
Gateway's OpenAI-compatible path, same as Pi/OpenCode
`GatewayClientEndpointSelection::AnthropicMessages` → Chat).

`stream_tool_calls`: some BYOK endpoints need `false` (11-custom-models.md).
Do not set global `[models] stream_tool_calls`. Probe per owned model after
isolated apply; default unset.

`reasoning_summary` on Responses custom models defaults to `concise`
(26-config-reference.md). Some gateways reject it; spike before merge.

### I. Project vs global (question 9)

Project `.grok/config.toml` contributes only `[mcp_servers]`, `[plugins]`, and
`[permission]` (settings + 05-configuration.md). `[model.*]` and
`[model_providers.*]` load only from user `$GROK_HOME/config.toml`. Project
files will not fight a global Injected Block.

Enterprise `requirements.toml` `allowed_models` can **hide** injected ids
(fleet pin is not a union). Disclose; do not edit requirements.

### J. Catalog projection (question 11)

ADR-0004: project the full enabled set into the Injected Block. Grok has no
nested models list. Options:

| Option | Fits Provider Injection? |
| --- | --- |
| `GROK_MODELS_BASE_URL` / `[endpoints] models_base_url` | **No.** Steals official Grok catalog. Takeover. |
| One `[model.codexhub]` only | One picker row; does not project the enabled set. Copy-only quality. |
| One `[model.codexhub-<slug>]` per exported model, **no** shared provider | Projects the catalog, but N unrelated tables; detach is a prefix delete. |
| One owned `[model_providers.codexhub]` + picker rows with `model_provider = "codexhub"` | Closest to DSH's one provider + models list. Mixed Responses/Chat must set `api_backend` per row. |
| OpenCode-shaped: one `[model_providers.codexhub-<upstream>]` per `gateway_client_provider_groups` + `[model."codexhub-<upstream>-<short-id>"]` rows | Matches existing CodexHub catalog projection (`inject.rs`). Owned prefix is the Injected Block. |

**Recommended official Apply:** the OpenCode-shaped owned prefix. That is one
owned **set** (CONTEXT.md: "the exact set of entries CodexHub owns"), not
takeover. Detach removes every `model_providers` / `model` table whose id
starts with `codexhub` / `codexhub-` and that matches the local-Gateway
adoption rule (`is_managed_codexhub_provider_entry` analog).

A single shared `[model_providers.codexhub]` pointing at Gateway `/v1` is
admissible if a spike proves mixed upstreams can share one `api_backend`.
Do not land that as the first path; reuse `gateway_client_provider_groups`
**after a Grok-only filter** (next subsection).

**Rejected Apply shapes:** `[endpoints]`; writing `[models] default`;
overriding `[model.grok-4.6] base_url` (hijacks official Grok);
projecting CodexHub's `xai` subscription into Grok CLI (duplicates the
native catalog).

### J.1 Native Grok catalog is excluded from this client's projection

ADR-0004's "full enabled model set" is the set CodexHub would export to a
client that has **no** native Grok subscription. Grok CLI is not that
client. Its built-in catalog *is* the Grok subscription. The adapter projects
"full enabled set minus models this client already owns natively."

This filter is **Grok-adapter-local**. `gateway_client_provider_groups`
stays unchanged so OpenCode / Pi / OMP / ZCode / DSH still receive `xai`
through Gateway.

Skip a Gateway provider when **either** is true:

| Predicate | Why |
| --- | --- |
| `provider.id == "xai"` | Bundled Maintained Provider in `config/providers.toml`. |
| `auth_capabilities` contains `subscription:xai_oauth` | Same CodexHub Grok 订阅 login, even if a future preset id drifts. |

Do **not** skip by model-id prefix `grok-*` on other providers. Official
OpenAI, OpenCode Go, Ollama, custom OpenAI-compatible endpoints, and every
non-`xai` Maintained Provider still inject.

Consequences:

- No `[model_providers.codexhub-xai]` and no `[model.codexhub-xai-*]`.
- Native picker rows (`grok-4.6`, `grok-build`, …) stay Grok's. CodexHub
  never writes `[model.grok-4.6]`.
- Republish / Detach still remove any stale `codexhub-xai*` tables left by
  an older adapter (owned-prefix cleanup).
- If the remaining groups are empty (only `xai` exported, official models
  off), Connect does not invent fake picker rows. Status should say Grok
  订阅仍走客户端原生目录；启用其他 Provider 后再出现 CodexHub 条目.
  Prefer still recording the client as hub-bound so catalog republish
  can add non-xai rows later (sentinel owned `[model_providers.codexhub]`
  with no model tables is admissible; do not point it at `api.x.ai`).

## Fit against ADR-0004

| ADR-0004 rule | Grok CLI today |
| --- | --- |
| Inject one owned block, keep foreign providers | Yes: owned `[model_providers.codexhub-*]` + `[model.codexhub-*]`. Foreign tables stay. |
| Never activate | `[models] default` is a separate key. Leave it. |
| Surgical credential | Inline `api_key` on owned provider tables (no sidecar store). Detach removes it. |
| Loopback Gateway traffic | Yes. Per-model `base_url` is dialed by the local `grok` process. |
| Block-fingerprint readback | Fingerprint owned tables only (base_url, api_backend, model ids). |
| Isolated E2E apply | Yes: `GROK_HOME=<isolated>/grok` is a first-party writable root. |

This is closer to OpenCode/DSH than to Cursor or Claude Code.

## Admission table

| # | Question | Result |
| --- | --- | --- |
| 1 | Identity | Official Grok Build / `grok` / `~/.grok`. Not community grok-cli. |
| 2 | Injectable map | Yes. `[model.*]` + `[model_providers.*]` in user TOML. |
| 3 | Protocol | Chat Completions and Responses against `{base_url}`. `/models` catalog fetch is global takeover — reject as Apply. |
| 4 | Credentials | `api_key` on owned tables for Apply. No `.credentials.yaml`. Never `auth.json`. |
| 5 | Activation | `[models] default`. Apply must not change it. |
| 6 | Restart | Disclose restart Grok CLI / new process. Not DSH hot reload. |
| 7 | History bucket | No. Sessions keyed by cwd + id. |
| 8 | Tools | Client-side tools OK. `supports_backend_search` must be false. |
| 9 | Project config | Cannot contain `[model.*]`. No fight. |
| 10 | Admission bar | **Met.** First-party documented custom endpoint the local process calls, coexistence, surgical detach, no impersonation of `api.x.ai`. |
| 11 | Catalog | Owned prefix of picker rows + `model_providers`, not `GROK_MODELS_BASE_URL`. **Omit CodexHub `xai` / `subscription:xai_oauth`.** |
| 12 | Local pin | 1.0.30 on this host. |

## Recommended operation

**Admit Grok CLI to the official managed-client roster** as a native Provider
Injection client (`auto_apply_supported: true` when installed), not copy-only
and not takeover.

Copy-only remains available via Generic for a manual `[model.*]` paste. That
is not the product path once the adapter lands.

- Adapter id: **`grok`** (binary and `$GROK_HOME` name).
- Display name: **Grok CLI**.
- Kind: **Terminal client** (same class as OpenCode).
- Do not use `grok-cli` as the id (collides with the community package).

Revisit only if xAI removes per-model `base_url` or makes custom inference go
through `cli-chat-proxy.grok.com`. Until then, Cursor-style rejection does not
apply.

## Implementation plan (not authorized by this note)

### Files to add / touch

| Area | Change |
| --- | --- |
| `src-tauri/src/gateway/clients/grok.rs` | New adapter: detect, inspect, plan. |
| `src-tauri/src/gateway/clients/mod.rs` | `mod grok`. |
| `src-tauri/src/gateway/managed_clients.rs` | `GrokAdapter` in `adapter_for`; `config_present_for`; native apply/preview/restore/readback/isolated arms. |
| `src-tauri/src/gateway/mod.rs` | `list_gateway_clients` roster row. |
| `src-tauri/src/gateway/isolated.rs` | Add `"grok"` to `ISOLATED_CLIENTS`. |
| `src-tauri/src/injection.rs` | `ISOLATED_MANAGED_CLIENTS` entry `files: &["grok/config.toml"]`. Do **not** stretch the YAML `InjectionDescriptor` engine; Grok is OpenCode-class native TOML, not DSH's single YAML provider. `ConfigFormat::Toml` exists but `require_yaml` still fail-closes. |
| Frontend | `ui-contract.json` `gatewayClients`; `GatewayClientId`; `clientKind.grok` i18n; `PrototypeData.ts` fixture; `GatewayClientCard` icon. |
| Tests | Coordinator + `clients/grok.rs` mirroring OpenCode; isolated apply under caller root. Must assert `xai` / `subscription:xai_oauth` groups are omitted while `openai` (and other non-xai) groups are written; OpenCode/Pi projection is unchanged. |

### Injected Block shape

Reuse `gateway_client_provider_groups`, then drop every group whose
upstream is `xai` or `subscription:xai_oauth` (section J.1). Example of
what **is** written (redacted). Note there is no `codexhub-xai` table:

```toml
[model_providers.codexhub-openai]
base_url = "http://127.0.0.1:<port>/v1/providers/openai"
api_key = "<gateway_client_key>"
api_backend = "responses"

[model."codexhub-openai-gpt-5.5"]
model = "gpt-5.5"
name = "CodexHub 5.5"
model_provider = "codexhub-openai"
context_window = 258400
supports_backend_search = false
```

- Catalog keys use hyphens (`-m codexhub-openai-gpt-5.5`). Grok table keys
  must not contain slashes.
- `model` is the id sent to Gateway (the short id OpenCode puts inside the
  provider).
- `api_backend` from `GatewayClientEndpointSelection` (`responses` or
  `chat_completions`). Never `messages`.
- Repeat one `model_providers.codexhub-<upstream>` per **non-xai** group
  and one `[model."…"]` per remaining exported model. Republish rewrites
  the owned set and must delete stale `codexhub-xai*` if present.
- Never write `[models] default`, `[models] extra_headers`, `[endpoints]`,
  `auth.json`.
- `activation_touched = false`.
- `restart_required = "Grok CLI"`.

### Credential strategy

Surgical `api_key` on each owned `model_providers` table (same secret as
OpenCode's provider `apiKey`). Detach removes those tables. Isolated apply
may set `GROK_HOME` and optionally `CODEXHUB_API_KEY`; live Apply must not
require the user to export env.

### Activation rule

Never write `[models] default` or `GROK_DEFAULT_MODEL`. UI copy: after
Connect, pick a CodexHub row with `/model` or `-m` in a new session.

### Restart disclosure

Toast names **Grok CLI**. Config is not DSH hot-reload.

### Isolated apply

- Writable path: `<isolated>/grok/config.toml`.
- Runner env: `GROK_HOME=<isolated>/grok` so the binary never sees `~/.grok`.
- Honor `GROK_HOME` in detect when set; else `~/.grok` /
  `%USERPROFILE%\.grok`.
- Do not copy host `auth.json` into the isolated root.

### Real-client E2E pin

Do **not** add Grok to the eight CLI Responses cases in
`docs/agents/real-client-e2e.md` without a dedicated credential contract and a
new Issue (same rule the doc already states for xAI-as-upstream). First gate:
isolated `managed-client-config` preview/apply/readback. A later live
`grok --no-auto-update -p … -m codexhub-…` against loopback Gateway is a
version-recorded smoke, not this campaign's merge bar.

### UI roster

- `list_gateway_clients`: `id = "grok"`, `name = "Grok CLI"`,
  `kind = "Terminal client"`, `auto_apply_supported` when installed,
  `config_path = <GROK_HOME>/config.toml`, route detection from owned `base_url`
  (`is_local_gateway_url`).
- Connection toggle is inject/detach (`switchGatewayClientRoute`). Do not add
  a DSH-only connect command.
- Status should mention restart, not "hot reload".

### Verification class

`strict`: persisted third-party home, credentials, routing
(`docs/agents/verification-policy.md`). Targeted Rust tests during development;
`cargo test` / `cargo clippy` in `src-tauri` plus frontend ui-contract if the
roster UI changed.

## Open questions (spike before merge, not admission blockers)

1. **`reasoning_summary`.** Default `concise` on Responses custom models. Confirm
   Gateway / Official Responses accept it; if 400, set `none` on owned rows.
2. **`stream_tool_calls`.** Probe Gateway streaming tool-call shape with one
   isolated `grok -p` after apply. Do not guess; do not set the global knob.
3. **Comment preservation.** serde `toml` vs `toml_edit` for `config.toml`.
   OpenCode JSON already drops comments. Optional, not a blocker.
4. **Fleet `allowed_models`.** Surface in status if injected keys are pinned
   out. Do not edit `/etc/grok/requirements.toml`.
5. **Official catalog id collisions.** Never name an owned table `grok-4.6`.
   Always `codexhub-` prefix. Independently, never inject the `xai` provider
   (J.1); the prefix rule alone is not enough, because `codexhub-xai-grok-4.6`
   would still duplicate the native row.
6. **One vs N `model_providers`.** Spike whether Gateway `/v1` plus per-row
   `api_backend` is enough; default to per-upstream groups.

## Non-goals

- Consuming Grok / xAI subscription models as a Maintained Provider (already
  a separate surface; do not mix).
- Projecting that same `xai` Maintained Provider back into Grok CLI. Native
  `grok login` is the Grok subscription surface for this client.
- Impersonating `api.x.ai`, `cli-chat-proxy.grok.com`, or rewriting `auth.json`.
- `GROK_MODELS_BASE_URL` / `[endpoints]` Apply.
- Activating `[models] default`.
- ACP hosting (`grok agent stdio`) as a CodexHub Gateway client.
- Adding Grok to the eight-case real-client E2E matrix in the same Issue.
- Treating `stevederico/grok-cli` or other community CLIs as this client.

## Decision

**Admit Grok CLI (`id = grok`) to the officially maintained managed-client list.**
The first-party `[model.*]` / `[model_providers.*]` map is a surgical Injected
Block the local process dials. Implement as an OpenCode-class native adapter
that writes owned `codexhub-*` tables, never the default model, never the
catalog-wide models endpoint, and never CodexHub's `xai` subscription models
(Grok CLI already has them natively). Restart disclosure is Grok CLI.
Isolated apply first; live CLI E2E later.
