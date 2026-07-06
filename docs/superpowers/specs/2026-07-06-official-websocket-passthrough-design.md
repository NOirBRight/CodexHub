# Official WebSocket Passthrough Design

## Goal

CodexHub should keep one unified Codex App model list where official OpenAI models and third-party models can be selected side by side, while official models behave as close as possible to Codex App's native official provider path.

The primary fix is to add a WebSocket-capable local provider path. Official models should use near-native WebSocket passthrough. Third-party models should continue using CodexHub's existing HTTP/SSE gateway adaptation, request rewriting, tool compatibility, compact handling, and retry logic.

## Current Problem

The existing transparent proxy mode routes Codex App through `http://127.0.0.1:9099/v1` with `supports_websockets = false`. That forces Codex App onto HTTP/SSE even for official models. In that mode, official `openai/gpt-*` requests pass through the same Python gateway machinery used for third-party providers:

- request body normalization
- forced official streaming fields
- SSE terminal-event handling
- upstream open retry
- stream retry before downstream output
- optional downstream retry notices
- image proxy and browser-context guidance paths

Those behaviors are useful for third-party model compatibility. They are not desired for official Codex App GPT models because Codex App already owns official retry, reconnect, and streaming behavior.

## Design

CodexHub remains the active Codex App provider when the user wants official and third-party models in the same model picker.

The proxy mode config should eventually advertise WebSocket support:

```toml
model_provider = "custom"
model_catalog_json = "model-catalogs/codexhub-model-catalog.json"

[model_providers.custom]
name = "Codex Proxy"
base_url = "http://127.0.0.1:9099/v1"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = true
```

CodexHub then routes requests by behavior profile:

| Profile | Request Source | Model Scope | Behavior |
| --- | --- | --- | --- |
| `official_codex_app_ws_passthrough` | Codex App WebSocket | `openai/gpt-*`, `gpt-*` | WebSocket reverse proxy to official Codex backend with minimal auth/header/model mapping. |
| `official_codex_app_http_passthrough` | Codex App HTTP/SSE fallback | `openai/gpt-*`, `gpt-*` | Near-native HTTP/SSE passthrough with gateway retry/rewrite disabled. |
| `official_gateway_compat` | Third-party clients using CodexHub endpoints | official models | Existing compatibility behavior where Chat/Responses conversion may be required. |
| `external_provider_gateway` | Any client | third-party models | Existing gateway behavior: protocol conversion, tool adaptation, compact handling, image proxy, and retry. |

## Official WebSocket Passthrough

For official Codex App WebSocket traffic, CodexHub should behave like a transport relay, not a model adapter.

Allowed work:

- accept the local WebSocket upgrade from Codex App
- authenticate to the official Codex backend using existing Codex auth
- forward frames in both directions
- map `openai/gpt-5.5` to `gpt-5.5` if the upstream protocol requires the unprefixed model id
- map fast variants to upstream model plus `service_tier = "priority"` if the protocol exposes service tier in a frame or request payload
- log connection lifecycle metadata without logging prompt or secret contents

Disabled work:

- no gateway automatic retry
- no stream retry before downstream output
- no third-party tool schema rewriting
- no CodexHub explicit tool injection
- no compact request detection or compact retry
- no image proxy
- no browser-context guidance injection
- no synthetic terminal events
- no conversion between Responses and Chat Completions

## Third-Party Model Path

Third-party models keep the current gateway design because they need compatibility behavior:

- `responses`, `chat_completions`, and `anthropic_messages` endpoint selection
- request and response conversion
- tool protocol adaptation for providers that do not support Codex native tools
- compact text-only handling
- empty compact response detection
- upstream retry for unreliable providers
- SSE terminal guards

If Codex App sends third-party model requests over WebSocket after `supports_websockets = true`, CodexHub should bridge that WebSocket request into the existing HTTP/SSE gateway implementation. The external provider does not need to support WebSocket.

## Required Probe Before Implementation

Before implementing the reverse proxy, add a gated WebSocket recorder for Codex App traffic. The recorder must capture only protocol metadata:

- upgrade path
- query string keys
- selected subprotocol
- non-secret header names
- frame direction
- frame type
- frame byte length
- whether a frame appears to contain JSON
- JSON top-level keys when safe to parse
- close code and close reason length

The recorder must redact authorization headers, cookies, account ids, prompts, tool arguments, file contents, and full frame bodies.

The probe must answer:

- whether Codex App upgrades `/v1/responses`, `/v1/responses/{id}`, or another path
- where the model id appears
- whether Codex App uses WebSocket for all models or only official models
- how reconnect attempts are represented
- whether the official upstream WebSocket endpoint uses the same path and subprotocol
- whether service tier or fast-model selection appears in the initial request or a later frame

## Configuration Migration

Proxy mode should not immediately force WebSocket on for all users until the implementation is verified. Add an explicit setting:

```json
{
  "gateway_official_websocket_passthrough_enabled": false
}
```

When disabled, keep current HTTP/SSE behavior. When enabled:

- `config_overlay.py` writes `supports_websockets = true`
- it no longer forces `responses_websockets = false` and `responses_websockets_v2 = false`
- official Codex App models use the WebSocket passthrough profile
- third-party models continue through the gateway profile

After manual validation shows lower GPT reconnect stalls, the default can be changed in a separate release.

## Error Handling

Official WebSocket passthrough should not hide or replay official failures. On upstream failure:

- close the downstream WebSocket with a protocol-appropriate close code
- log a redacted `official_ws_passthrough_error` event
- let Codex App perform its own reconnect or recovery

Third-party gateway errors remain unchanged and continue to use existing retry and downstream error behavior.

## Testing Strategy

Unit tests:

- proxy mode config writes `supports_websockets = true` only when the new setting is enabled
- official Codex App requests choose `official_codex_app_ws_passthrough`
- official WebSocket passthrough disables gateway retry/rewrite flags
- third-party requests still choose `external_provider_gateway`
- third-party WebSocket requests bridge to the existing gateway profile

Integration tests:

- WebSocket recorder captures handshake metadata without prompt text
- official WebSocket relay forwards client and upstream frames in both directions
- upstream close propagates downstream close without gateway retry
- HTTP/SSE fallback for official models uses one upstream attempt
- third-party HTTP/SSE regression tests continue to pass

Manual validation:

- Codex App can select `openai/gpt-5.5` and a third-party model from the same model list
- `openai/gpt-5.5` requests no longer emit gateway retry events
- third-party failures still emit retry events when retry is enabled
- GPT reconnect stalls are compared against current proxy mode using `codex-proxy-events.jsonl` and Codex App visible behavior

## Non-Goals

- Do not remove third-party request rewriting.
- Do not disable third-party automatic retry.
- Do not require third-party providers to implement WebSocket.
- Do not make official model passthrough completely bypass CodexHub in the first implementation, because that would break the unified model picker goal.
- Do not log full WebSocket frame bodies during probing or production operation.

## Risks

The largest risk is that Codex App's WebSocket protocol may require stateful behavior that is not obvious from the HTTP/SSE path. The recorder step is mandatory because guessing the frame contract could create a worse reconnect failure than the current SSE proxy.

The second risk is that `supports_websockets = true` may cause Codex App to send third-party model traffic over WebSocket too. The design handles this by bridging third-party WebSocket requests back into the existing HTTP/SSE gateway, but this must be verified by the probe.

The third risk is accidental regression of unified history behavior. The new proxy mode must remain compatible with the current unified `custom` provider history strategy.
