# Official Passthrough Design

## Goal

CodexHub should keep one unified Codex App model picker where official OpenAI models and third-party models can be selected side by side, while official models behave as close as possible to Codex App's native official provider path.

The first release should reduce official-model divergence without changing the transport contract exposed to Codex App. WebSocket support is a later transport project, not a dependency for the first release.

## Decision Summary

Ship this in two phases:

1. **Phase 1: non-WebSocket official HTTP/SSE passthrough.** Keep proxy mode configured with `supports_websockets = false`. For official Codex App Responses requests, disable CodexHub gateway retry, stream retry, request rewriting, image proxy, browser-context guidance, compact retry, and synthetic terminal repair. Third-party models keep the existing gateway compatibility path.
2. **Phase 2: WebSocket transport refactor.** Resume WebSocket production work only after Phase 1 is stable and the external-model subagent branch has landed. Do not advertise `supports_websockets = true` until third-party WebSocket-to-gateway bridging preserves the same subagent behavior as the HTTP/SSE path.

Phase 1 may start now, but the first public release gate depends on the subagent compatibility branch passing its Level 1 and Level 2 E2E suite after rebasing/merging.

## Current Problem

The existing transparent proxy mode routes Codex App through `http://127.0.0.1:9099/v1` with `supports_websockets = false`. That forces Codex App onto HTTP/SSE. In that mode, official `openai/gpt-*` requests pass through the same Python gateway machinery used for third-party providers:

- request body normalization
- official backend compatibility mutations
- compact request tool stripping
- browser-context guidance injection
- optional image proxying
- upstream open retry
- stream retry before downstream output
- optional downstream retry notices
- SSE terminal-event repair and guards

Those behaviors are useful for third-party compatibility. They are risky for official Codex App GPT models because Codex App already owns official retry, reconnect, and streaming behavior. Layering CodexHub retry/replay over Codex's native retry can plausibly increase reconnect stalls.

## Probe Findings

An isolated Codex CLI probe was completed without touching the user's running Codex App. The sanitized findings are in `docs/superpowers/findings/2026-07-06-codex-cli-websocket-probe.md`.

Observed with Codex CLI `0.142.2`:

- `supports_websockets = true` upgrades to `/v1/responses`.
- The selected WebSocket subprotocol was empty.
- The model id appears in the first text frame as a top-level `model` field.
- Official model `openai/gpt-5.5` used WebSocket.
- Third-party-looking model id `volc/glm-5.2` also used WebSocket.

The important design consequence is that Codex custom-provider WebSocket support must be treated as provider-wide. An official-only WebSocket relay is not safe while third-party traffic still depends on the HTTP/SSE gateway for request rewriting, retries, and subagent compatibility.

The probe was against Codex CLI, not the desktop Codex App UI. It establishes enough risk to avoid WebSocket in Phase 1, but it is not a complete production WebSocket contract.

## Phase 1 Design

CodexHub remains the active Codex App provider when the user wants official and third-party models in the same picker.

Proxy mode config stays non-WebSocket:

```toml
model_provider = "custom"
model_catalog_json = "model-catalogs/codexhub-model-catalog.json"

[model_providers.custom]
name = "Codex Proxy"
base_url = "http://127.0.0.1:9099/v1"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false

[features]
responses_websockets = false
responses_websockets_v2 = false
```

Phase 1 routes requests by behavior profile:

| Profile | Request Source | Model Scope | Behavior |
| --- | --- | --- | --- |
| `official_codex_app_http_passthrough` | Codex App HTTP/SSE Responses request | `openai/gpt-*`, `gpt-*` official upstream | Near-native HTTP/SSE passthrough. Disable gateway retry, stream retry, image proxy, browser guidance, compact retry, third-party tool rewriting, synthetic terminal repair, and Chat/Responses conversion. |
| `official_gateway_compat` | Non-Codex App clients, Chat Completions callers, or explicit fallback | official upstream | Existing official compatibility behavior for clients that are not native Codex App Responses traffic. |
| `external_provider_gateway` | Any client | third-party upstreams | Existing gateway behavior: endpoint selection, protocol conversion, request rewriting, tool adaptation, compact handling, image proxy, subagent state guidance, response repair, and retry. |
| `diagnostic_websocket_recorder` | WebSocket upgrade while recorder is explicitly enabled | any path | Diagnostic-only protocol metadata recorder. Default off. Not part of Phase 1 production routing. |

### Official HTTP/SSE Passthrough

For `official_codex_app_http_passthrough`, CodexHub should behave like a thin official transport adapter.

Allowed work:

- choose the official upstream
- authenticate to the official Codex backend using the existing Codex auth flow
- map `openai/gpt-5.5` to `gpt-5.5` when the upstream expects the unprefixed id
- map fast variants to upstream model plus `service_tier = "priority"` when the catalog route uses a fast pseudo-model
- add `store = false` only if the current official backend endpoint still requires it
- log routing, status, timing, and redacted request metadata

Disabled work:

- no gateway automatic retry
- no stream retry before downstream output
- no downstream retry notices
- no third-party tool schema rewriting
- no CodexHub explicit tool injection
- no compact request tool stripping
- no compact empty-summary retry
- no image proxy
- no browser-context guidance injection
- no synthetic terminal events
- no Chat/Responses protocol conversion
- no mutation of prompt, input items, tools, instructions, or file contents

If a request is official but not a Codex App Responses request, Phase 1 should route it to `official_gateway_compat`, not to passthrough.

### Third-Party Model Path

Third-party models keep the current gateway design because they need compatibility behavior:

- `responses`, `chat_completions`, and future `anthropic_messages` endpoint selection
- request and response conversion
- tool protocol adaptation for providers that do not support Codex native tools
- subagent state guidance and tool lifecycle repair
- compact text-only handling
- empty compact response detection
- image proxy for text-only models receiving image input
- upstream retry for unreliable providers
- SSE terminal guards and response repair

Phase 1 must not change this path except for adding telemetry fields that are explicitly verified not to change request or response behavior.

## Phase 2 Design

WebSocket support is still useful later because it can restore closer parity with Codex App's native official provider path and avoid SSE-specific proxy interference. It is not required for the first release.

Phase 2 must start from a stricter WebSocket contract:

- keep the gated WebSocket recorder
- capture the desktop Codex App handshake and first frames with only redacted metadata
- pin a sanitized WebSocket contract fixture
- extract the third-party gateway core so HTTP/SSE and WebSocket bridge traffic share the same subagent behavior
- implement official WebSocket relay
- implement third-party WebSocket-to-HTTP/SSE bridge before advertising WebSocket support
- expose `supports_websockets = true` only after the bridge passes the subagent E2E suite

If Codex App sends third-party model requests over WebSocket after `supports_websockets = true`, CodexHub must bridge that WebSocket request into the existing gateway implementation. The external provider does not need to support WebSocket.

## Configuration

Phase 1:

- Keep `supports_websockets = false` in proxy mode.
- Keep `responses_websockets = false` and `responses_websockets_v2 = false`.
- Do not expose a production WebSocket switch in the UI.
- Add only a hidden/emergency Phase 1 setting if needed:

```json
{
  "gateway_official_http_passthrough_enabled": true
}
```

The setting defaults to `true` for the first release because Phase 1's purpose is to stop applying third-party gateway behavior to official Codex App traffic. Operators may set it to `false` through runtime settings or `CODEX_PROXY_OFFICIAL_HTTP_PASSTHROUGH_ENABLED=0` to fall back to the current official compatibility path during emergency diagnosis.

Phase 2:

- Add a separate WebSocket production setting only after the third-party bridge exists.
- Only then may `config_overlay.py` write `supports_websockets = true`.
- Only then may stale WebSocket false feature flags be removed.

## Error Handling

Official HTTP/SSE passthrough should not hide or replay official failures. On upstream failure:

- return the official upstream failure to Codex App using existing downstream error handling
- log a redacted `request_error` event with `behavior_profile = "official_codex_app_http_passthrough"`
- do not emit `upstream_retry` or `sse_retry_notice` events
- let Codex App perform its own retry or recovery

Third-party gateway errors remain unchanged and continue to use existing retry and downstream error behavior.

## Testing Strategy

Unit tests:

- proxy mode config continues to write `supports_websockets = false`
- Phase 1 behavior profile selection picks official HTTP passthrough only for Codex App official Responses requests
- official HTTP passthrough disables gateway retry, stream retry, downstream retry notices, image proxy, browser guidance, compact stripping, and compact retry
- official HTTP passthrough still applies safe official model alias and fast service-tier mapping
- official non-Codex-App traffic and Chat Completions traffic keep the official compatibility path
- third-party requests still use `external_provider_gateway`
- third-party compact, image proxy, retry, and subagent tests continue to pass

Integration/manual validation:

- Codex App can select `openai/gpt-5.5` and third-party models from the same model list
- `openai/gpt-5.5` HTTP/SSE requests no longer emit gateway retry or third-party adapter events
- third-party failures still emit retry events when retry is enabled
- the subagent Level 1 and Level 2 E2E runner from thread `019f3038-028d-7891-9c68-6fe6825046e7` passes after the subagent branch lands

## Non-Goals

- Do not remove third-party request rewriting.
- Do not disable third-party automatic retry.
- Do not require third-party providers to implement WebSocket.
- Do not advertise WebSocket support in Phase 1.
- Do not expose a WebSocket production switch before third-party bridge support exists.
- Do not make official model passthrough completely bypass CodexHub, because that would break the unified model picker goal.
- Do not log full WebSocket frame bodies during probing or production operation.

## Risks

The main Phase 1 risk is accidental regression of third-party subagent compatibility because the same proxy file owns official routing and third-party gateway behavior. The mitigation is to keep Phase 1 edits narrow and use the subagent E2E suite as a release gate.

The main Phase 2 risk is that Codex App's WebSocket protocol is provider-wide and stateful. Guessing the frame contract could create worse reconnect failures than the current SSE proxy. The recorder and contract fixture are mandatory before relay or bridge implementation.

The remaining product risk is unified history behavior. CodexHub must continue to preserve the current unified `custom` provider history strategy while changing only the official traffic behavior profile.
