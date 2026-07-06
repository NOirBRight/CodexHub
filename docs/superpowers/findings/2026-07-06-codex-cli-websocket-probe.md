# Codex CLI WebSocket Probe Findings

Date: 2026-07-06

Scope: isolated Codex CLI probe using a temporary `CODEX_HOME`, a temporary local CodexHub proxy, dummy API key auth, and recorder-only WebSocket handling. The current Codex App config and runtime were not modified.

## Setup

- Codex CLI version: `codex-cli 0.142.2`
- Proxy ports: `19199` for official model-name probe, `19200` for third-party model-name probe
- Provider config:
  - `wire_api = "responses"`
  - `requires_openai_auth = false`
  - `supports_websockets = true`
  - local `base_url = "http://127.0.0.1:<port>/v1"`
- Recorder config:
  - `gateway_websocket_recorder_enabled = true`
  - `gateway_websocket_recorder_max_frames = 4`
  - `gateway_websocket_recorder_idle_timeout_seconds = 0.75`

## Official Model-Name Probe

Model: `openai/gpt-5.5`

Observed WebSocket handshake:

- Upgrade path: `/v1/responses`
- Query keys: none
- Selected subprotocol: none
- Header names:
  - `authorization`
  - `connection`
  - `host`
  - `openai-beta`
  - `originator`
  - `sec-websocket-extensions`
  - `sec-websocket-key`
  - `sec-websocket-version`
  - `session-id`
  - `thread-id`
  - `upgrade`
  - `user-agent`
  - `x-client-request-id`
  - `x-codex-beta-features`
  - `x-codex-turn-metadata`
  - `x-codex-window-id`

Observed first frame:

- Direction: client to proxy
- Opcode: `1` text
- JSON: yes
- Model location: top-level `model` key
- Top-level keys:
  - `client_metadata`
  - `generate`
  - `include`
  - `input`
  - `instructions`
  - `model`
  - `parallel_tool_calls`
  - `prompt_cache_key`
  - `reasoning`
  - `store`
  - `stream`
  - `text`
  - `tool_choice`
  - `tools`
  - `type`

Retry behavior observed after the recorder closed the socket:

- CLI logged `stream disconnected - retrying sampling request`
- Retry sequence in the sampled run reached `5/5`
- Retry delays were sub-second to several seconds
- Each retry opened a new WebSocket connection to `/v1/responses`

## Third-Party Model-Name Probe

Model: `volc/glm-5.2`

Observed result:

- The CLI still upgraded to WebSocket at `/v1/responses`.
- Query keys remained empty.
- Selected subprotocol remained absent.
- First frame remained a text JSON frame.
- The first frame contained top-level `model`.

This indicates that, at least in Codex CLI `0.142.2`, `supports_websockets = true` behaves as a provider-level transport choice. Third-party model ids are sent over WebSocket too when they are selected through that provider.

## Conclusions

- The local proxy must handle WebSocket on `/v1/responses`.
- The model id is in the first client text frame as a top-level JSON `model` field for the sampled CLI paths.
- `supports_websockets = true` cannot be enabled for the unified CodexHub provider unless third-party WebSocket handling exists.
- Official WebSocket relay alone is not enough for a unified model picker.
- Disconnect/reconnect behavior is client-owned: the CLI retries sampling requests after the server closes before `response.completed`.

## Redaction

The recorder did not persist header values, authorization values, cookies, prompt text, tool arguments, or full frame bodies. This finding records only structural metadata.

## Limitations

- This probe used Codex CLI, not the desktop Codex App UI.
- The CLI user agent was inferred as `codex-app` by the existing proxy heuristic.
- The probe intentionally closed the socket without forwarding to an upstream server, so it verifies request-side protocol shape and retry attempts, not successful upstream response framing.
