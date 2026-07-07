# Third-Party Client Transparent Metered Routing Design

Date: 2026-07-07

## Reader And Action

This design is for CodexHub engineers who need to implement the next routing refactor without breaking the existing Codex App third-party model path. After reading it, they should be able to separate Codex App protocol adaptation from third-party app transparent proxying, then plan the implementation in safe phases.

## Goal

CodexHub should support two different external model access use cases without forcing them through the same behavior:

1. Codex App selecting third-party models from the unified CodexHub catalog.
2. Third-party clients such as ZCode, OpenCode, Pi, and OMP using CodexHub as a local proxy for official and third-party models.

The first use case needs a Codex-aware adapter. The second use case should default to transparent mapped proxying with asynchronous usage metering and conservative retry, whether the upstream is a third-party provider or the official Responses endpoint.

## Decision Summary

Keep the existing Codex App third-party compatibility path, but rename and isolate it conceptually as `codex_app_external_adapter`.

Separate wire-format conversion from Codex App semantic compatibility. Wire-format conversion handles only transport/API shape differences such as Responses to Chat Completions, Chat Completions to Responses, and the future WebSocket bridge. Codex App semantic compatibility handles Codex-specific behavior such as tool rewrite, compact handling, subagent repair, browser guidance, image proxy, and provider parameter compatibility.

Add a separate third-party client path named `third_party_app_transparent_metered`. This path should cover third-party clients calling either third-party providers or the official Responses endpoint. It should not perform Codex-specific request rewriting, tool rewriting, subagent repair, compact handling, or synthetic stream repair. It may map provider-scoped URLs, inject upstream authentication, map configured model aliases, observe usage asynchronously, and retry only before any downstream bytes have been written.

When a caller request and an upstream provider endpoint do not use the same wire format, CodexHub should prefer a lightweight format fallback over failing immediately. This fallback is not the default path; it is only used to connect otherwise-compatible clients and providers. When the caller and selected upstream endpoint already share a wire format, CodexHub should preserve that format and avoid conversion.

For official `/v1/responses`, the transparent metered path requires an explicit third-party client identity from header, metadata, or user-agent inference. Requests with unknown client identity remain on the existing official gateway compatibility path for backward compatibility. Provider-scoped URLs already express third-party provider intent, so they may use transparent metered routing without an additional client header.

For standard non-provider-scoped third-party model routes, transparent metered routing also requires an explicit third-party client identity. Unknown-client standard third-party routes remain on the existing gateway compatibility profile so old callers are not silently moved to the transparent path while telemetry claims no semantic adapter ran.

Future provider configuration should support multiple endpoint capabilities per provider, but this first refactor should not implement the multi-endpoint schema.

## Subagent Branch Dependency

The native subagent implementation is currently separate from this design. As of 2026-07-07, Codex thread `019f39bd-9ef9-7aa1-8e8f-33cddc8e0eef` is still `inProgress` in worktree `C:\Users\noirb\.codex\worktrees\f11c\CodexHub` on branch `codex/subagent-protocol-fix`. The latest inspected state had focused Python regression passing and several GLM/M3/K2 focused E2E cases improving, but K2 chat focused was still running and no final handoff had been produced.

This design must therefore treat subagent repair as an integration dependency, not as a first-scope implementation input.

Rules:

- Do not copy or depend on unfinished subagent implementation details from the `f11c` worktree.
- Define the Codex App semantic adapter boundary now, including where subagent repair plugs in.
- Keep transparent third-party app paths independent of subagent repair.
- Integrate the concrete subagent repair implementation only after the subagent branch has a clean handoff or lands.
- Before enabling the Codex App external adapter refactor on top of that branch, rerun focused Python regression, GLM/M3/K2 focused E2E, and the Level 1/Level 2 assisted subagent gate.

## Current Problem

The current external provider path mixes several distinct responsibilities:

- route model names to upstream providers
- map provider-scoped client URLs to canonical model ids
- convert between Responses and Chat Completions even when the caller and upstream could have used the same format directly
- rewrite tool schemas and tool results
- repair Codex subagent lifecycle events
- handle compact requests
- proxy images for text-only models
- retry upstream opens and streams
- parse or repair SSE streams
- extract usage synchronously from response bodies or stream events
- write telemetry used by the Gateway usage UI

That behavior is necessary when Codex App talks to third-party models because Codex App sends Codex-native request and stream semantics. It is too invasive when third-party clients talk to official or third-party providers, where CodexHub should behave like a local provider proxy rather than a Codex protocol adapter.

## Behavioral Profiles

### `codex_app_external_adapter`

Use when the caller is Codex App and the selected upstream is a third-party provider.

This profile keeps the current compatibility behavior:

- request and response wire-format conversion where needed
- model alias to upstream model mapping
- tool schema adaptation
- tool output repair
- subagent state guidance and lifecycle repair
- compact request handling
- image proxy support
- provider-specific parameter compatibility
- SSE terminal and malformed event repair
- full gateway retry behavior

For this design's first implementation, this profile should be represented as a policy boundary and preserved behaviorally. The concrete subagent repair implementation remains owned by the active subagent branch until that branch has passed its gate. Transparent third-party client work must not depend on that branch being ready.

Codex App speaks the Responses contract to CodexHub. If a selected third-party provider only supports Chat Completions, this profile must convert Codex App Responses requests to Chat Completions upstream requests, then convert Chat Completions bodies or streams back to Responses for Codex App.

This profile remains necessary after WebSocket support. If Codex App sends third-party model requests over WebSocket, CodexHub still must bridge the Codex-native WebSocket contract into the external provider's supported transport and reuse the same subagent behavior. WebSocket support is therefore another wire-format adapter behind the Codex App semantic adapter, not a replacement for the adapter.

### `third_party_app_transparent_metered`

Use when the caller is a third-party client and the selected upstream can accept the incoming wire format directly.

This profile has two first-scope variants:

- third-party client to third-party provider, using the provider's configured compatible endpoint
- third-party client to official Responses, using the official transparent Responses passthrough

This profile should preserve request and stream semantics:

- preserve request body except for configured model mapping and explicitly documented upstream endpoint compatibility normalization
- preserve caller-selected wire format
- preserve upstream response bytes or SSE lines
- inject or replace upstream authentication
- route to provider-scoped endpoints
- apply official account and model mapping when the upstream is official
- observe usage asynchronously after forwarding data downstream
- record request telemetry
- apply conservative retry when enabled

For official Responses traffic, transparent metering is still routed through the ChatGPT Codex backend. That backend is not byte-for-byte compatible with public Responses clients, so this path may apply a thin official endpoint compatibility normalization without becoming a Codex semantic adapter:

- set `store` to `false`
- normalize string `input` into a Responses message-list shape
- remove `max_output_tokens` when the official backend rejects it

This normalization must not inject Codex tools, run Compact handling, run subagent repair, apply browser guidance, or perform synthetic stream repair.

When transparent routing must use lightweight wire-format fallback, streaming responses should still be converted incrementally. The fallback must not wait for the entire upstream stream before sending the first converted downstream delta.

This profile must not perform Codex-specific behavior:

- no subagent repair
- no Codex tool injection
- no tool schema rewrite
- no compact request special handling
- no browser guidance injection
- no inline image proxy as part of the transparent core
- no Codex-specific SSE terminal repair
- no synthetic downstream retry event

Vision support is a capability overlay, not part of the transparent core. A route may opt into `VisionProxyAdapter` when the caller sends images and the selected upstream lacks image input capability. That overlay must be explicit in policy and telemetry because it deliberately changes request content.

### Lightweight Format Fallback

Use when the caller is a third-party client and the selected upstream endpoint cannot accept the incoming wire format.

Examples:

- caller sends Chat Completions, provider endpoint is Responses-only
- caller sends Responses, provider endpoint is Chat Completions-only

The fallback may convert between request and response formats, but should stay lightweight. For official upstreams, the convertible target format is Responses.

- no Codex subagent repair
- no Codex tool injection
- no compact handling
- no Codex-specific SSE repair beyond what is required to produce the target wire format
- no Codex-specific response normalization, tool-call repair, or subagent duplicate-call guard

The intent is "connect when reasonable", not "pretend every provider is Codex-compatible".

## Wire Format Conversion Rules

Wire-format conversion is a separate policy from behavior profile selection.

Codex App traffic:

- inbound contract is Responses
- official upstream should use Responses passthrough
- third-party Responses-capable upstream should receive Responses unless provider settings choose another endpoint
- third-party Chat-only upstream requires request-side Responses to Chat Completions conversion and response-side Chat Completions to Responses conversion
- future WebSocket traffic requires a WebSocket-to-upstream bridge while preserving the same Codex App semantic adapter

Gateway-facing HTTP routes:

- `/v1/responses` means the caller expects Responses on the downstream side
- `/v1/chat/completions` means the caller expects Chat Completions on the downstream side
- if the selected upstream endpoint supports the same format, CodexHub should proxy that format directly
- if the selected upstream endpoint supports only the opposite format, CodexHub may use the lightweight Responses to Chat Completions or Chat Completions to Responses fallback

Third-party app traffic:

- provider-scoped route and model mapping are allowed
- same-format upstreams should be transparent
- mismatched upstreams may use lightweight format fallback
- no Codex App semantic adapter should run on this path

This rule avoids the current unnecessary shape of Chat client to Chat upstream going through Chat to Responses to Chat.

## Route Matrix

| Caller | Upstream | Normal Profile | Notes |
| --- | --- | --- | --- |
| Codex App | official | official thin passthrough | Existing Phase 1 path, with remaining hardening fixes tracked separately. |
| Codex App | third-party | `codex_app_external_adapter` | Required for Codex-native tools, subagents, compact, and later WebSocket bridge. |
| Third-party app | third-party, same wire format | `third_party_app_transparent_metered` | Default target for ZCode, OpenCode, Pi, OMP. |
| Third-party app | third-party, mismatched wire format | lightweight format fallback | Prefer format conversion over failure. |
| Third-party app | official Responses | `third_party_app_transparent_metered` | Responses transparent passthrough with thin official endpoint compatibility, sidecar usage, and conservative retry. |
| Third-party app | official, non-Responses inbound format | lightweight format fallback | For `/v1/chat/completions`, convert Chat to official Responses upstream and convert the Responses result back to Chat without Codex semantic repair. |

## Policy Modules

The implementation should move toward small policy modules with explicit interfaces. They do not all need to become separate files in the first implementation, but callers should make decisions through these concepts rather than scattered inline conditions.

### `RoutePolicy`

Chooses the behavior profile from:

- caller identity
- upstream provider type
- inbound wire format
- provider-scoped path
- configured provider capabilities
- emergency settings

The interface should return a single route decision object containing the behavior profile, selected upstream format, wire-format adapter policy, Codex semantic adapter policy, request-kind policy, retry policy, usage policy, and repair policy.

### `WireFormatAdapter`

Controls only request and response wire shape:

- `transparent`: caller format and upstream format match
- `responses_to_chat`: caller expects Responses, upstream accepts Chat Completions
- `chat_to_responses`: caller expects Chat Completions, upstream accepts Responses
- `websocket_bridge`: future Codex App WebSocket to selected upstream transport

Wire-format adapter selection should be independent of retry and usage capture.

### `CodexAppSemanticAdapter`

Controls Codex-specific semantic transformation:

- `codex_app_external_adapter`: Codex App to third-party provider traffic
- `none`: third-party client transparent traffic and official passthrough traffic

This adapter owns:

- Codex tool rewrite
- compact request handling
- subagent repair and state guidance
- browser guidance injection
- image proxy
- provider-specific parameter compatibility
- Codex-specific stream repair

It must not run for `third_party_app_transparent_metered`.

Subagent repair inside this adapter is an extension point for the native subagent branch. Until that branch is complete, this adapter should keep existing behavior intact and expose policy names only where needed by routing tests.

### `VisionProxyAdapter`

Controls image capability compensation when a caller sends image input and the selected upstream cannot accept images.

This adapter is independent from the Codex App semantic adapter:

- Codex App to official passthrough does not use CodexHub vision proxy; official upstream owns its own multimodal behavior.
- Codex App to third-party text-only providers may use the existing vision proxy behavior through an explicit `vision_proxy` policy value.
- Third-party clients on transparent metered routes may opt into vision proxy as a capability overlay when provider policy enables it.
- Vision proxy must not be implied by `third_party_app_transparent_metered`; default transparent behavior remains no image mutation.

Trigger conditions:

- inbound payload contains image content in Responses or Chat Completions shape
- selected upstream `input_modalities` does not include `image`
- `VisionProxyPolicy` is enabled for the caller/upstream route
- a configured vision proxy model supports image input

The adapter may replace or augment image parts with text descriptions before the selected upstream request is sent. It must write telemetry such as `vision_proxy_applied` or `vision_proxy_failed` with `behavior_profile`, `vision_proxy_policy`, selected upstream, proxy model, and inbound format. For transparent routes, the request should still keep `codex_semantic_adapter = none`; only `vision_proxy_policy` should indicate the overlay.

### `RequestKindPolicy`

Controls request-kind recognition and text-only summary behavior.

`compact` remains a real request kind. It is not explained by retry length alone. Live evidence showed compact requests carrying many tools while also instructing the model to respond with text only, and gateway tool injection made that conflict worse. Retry helps recover empty or interrupted compact attempts, but it does not remove the semantic conflict.

For first implementation:

- Codex App third-party adapter keeps compact handling.
- Existing gateway compatibility paths keep compact handling until replaced.
- Transparent third-party paths do not apply compact special handling by default.
- A future explicit setting may allow minimal text-only tool stripping for clients that send `x-request-kind: compact` or `x-query-source: compact`.

### `RetryPolicy`

Controls whether and when CodexHub may retry.

Profiles:

- `gateway_full`: existing compatibility retry for Codex App third-party traffic.
- `conservative_pre_output`: default for third-party transparent metered traffic.
- `off`: emergency or provider-specific opt-out.

`conservative_pre_output` may retry upstream open failures or stream failures only before downstream headers or body bytes have been written. After any downstream write, CodexHub must not replay the request; it should close the stream and record telemetry.

### `UsagePolicy`

Controls how usage is observed:

- `sync_capture`: existing gateway-compatible capture from parsed bodies or parsed SSE.
- `async_tap`: observe copied response data after it is forwarded downstream.
- `none`: disabled or unsupported.

Third-party transparent metered traffic should use `async_tap` for both official and third-party upstreams. Queue pressure, parse errors, or stream interruption may drop token usage, but must not delay the user-visible response. The async tap must write an event that the Gateway usage projection consumes; a side-channel event that never updates usage aggregation is not sufficient.

### `RepairPolicy`

Controls stream and tool repair:

- `codex_subagent_repair`: Codex App third-party traffic only.
- `generic_transport_guard`: minimal connection handling without semantic repair.
- `none`: raw forwarding.

Third-party transparent metered traffic should use `generic_transport_guard` or `none`, not `codex_subagent_repair`.

## Usage Projection

The existing asynchronous usage side-channel is not enough for third-party transparent metering unless the Gateway usage projection consumes it.

Add a first-class usage observation event, conceptually `usage_observed`, with:

- request id
- upstream provider
- model
- inbound format
- usage fields
- usage source
- observation timing

The telemetry projection should update the existing request row by request id in the first implementation. A separate usage observations table can be added later if multiple observations per request become necessary.

The design should tolerate observation arriving after request completion. A request may first appear as missing usage and later become metered after the asynchronous observer writes usage.

The design must also tolerate observation arriving before request completion. Request completion must not later downgrade a non-missing usage observation to `missing` or `async_usage_pending`.

Current `official_passthrough_usage_observed`-style events are not enough unless Python and Rust telemetry ingestion treat them as usage projection inputs.

Transparent metered routes should mark `request_complete` usage as `missing` with `usage_missing_reason = async_usage_pending` when usage is expected from the sidecar observer. Observed usage should be emitted through `usage_observed`; request completion should not synchronously become the usage source for transparent traffic.

Request body observability must distinguish caller body from final upstream body. When format conversion, model mapping, or Vision Proxy changes the payload, events should expose separate caller-body and upstream-body hashes. The legacy `request_body_hmac` field represents the final upstream body after all gateway mutations.

## Endpoint Capabilities

Current provider config has one base URL and one active upstream format per provider, plus discovered available formats. That is enough for the first implementation when route decisions prefer the active format and avoid unnecessary conversion.

Future config should allow multiple endpoints per provider:

```toml
[[providers.endpoints]]
format = "chat_completions"
base_url = "https://provider.example/v1"

[[providers.endpoints]]
format = "responses"
base_url = "https://provider.example/responses/v1"

[[providers.endpoints]]
format = "anthropic_messages"
base_url = "https://provider.example/anthropic"
```

Route selection should eventually choose the endpoint matching the caller's inbound format. If no matching endpoint exists, it may choose a convertible endpoint and use lightweight format fallback.

This multi-endpoint schema is intentionally out of scope for the first implementation, but the route decision object should avoid assuming one provider has only one endpoint forever.

## Error Handling

For third-party transparent metered traffic, including official Responses passthrough:

- upstream open failures before downstream output may retry according to `conservative_pre_output`
- upstream failures after downstream output must close downstream without replaying
- downstream retry notices must not be written
- CodexHub-specific SSE events must not be injected
- errors should be logged with behavior profile, caller, upstream, selected format, and retry outcome

For Codex App third-party adapter traffic, keep existing gateway retry and repair behavior until the subagent branch has landed and the E2E suite proves an equivalent refactor.

## Implementation Phases

### Dependency Gate: Keep Subagent Integration Out Of The First Transparent Path

Before implementation starts, confirm whether thread `019f39bd-9ef9-7aa1-8e8f-33cddc8e0eef` has produced a final handoff. If it has not, proceed only with changes that do not consume its implementation.

Expected result: third-party transparent metered routing can be built and tested without rebasing onto `codex/subagent-protocol-fix`, while the Codex App external adapter remains a stable boundary for later integration.

### Phase 0: Finish Official Thin Passthrough Hardening

Before changing third-party paths, finish the known official passthrough gaps:

- raw relay for official non-stream SSE
- raw relay for official SSE HTTP errors
- stricter Codex App detection for passthrough
- timeout for official account usage subprocess
- usage observation projection for official async usage in Gateway usage

### Phase 1: Introduce Route Decision And Policy Names

Add the route decision shape and policy names while preserving current behavior.

Expected result: Codex App third-party traffic still behaves exactly as before, but tests can assert it is classified as `codex_app_external_adapter` with `CodexAppSemanticAdapter=codex_app_external_adapter`. This phase must not import or reimplement unfinished subagent repair logic.

### Phase 2: Add Usage Observation Projection

Implement `usage_observed` projection before transparent third-party proxying depends on asynchronous usage.

Expected result: asynchronous usage observations can update or join with completed request telemetry. Projection must be order-tolerant: `request_complete -> usage_observed` and `usage_observed -> request_complete` both leave the request row with non-missing usage.

### Phase 3: Add Transparent Metered Paths

Enable `third_party_app_transparent_metered` for provider-scoped third-party app traffic when inbound format matches configured upstream format. Enable the same policy for third-party app traffic to official Responses.

Expected result: ZCode, OpenCode, Pi, and OMP can use official and third-party providers through CodexHub without Codex-specific rewriting, while usage is measured asynchronously and retry is conservative. Same-format requests should not be converted through an intermediate format.

This phase is independent of native subagent readiness.

### Phase 4: Add Lightweight Format Fallback

When a provider cannot accept the incoming wire format, use lightweight conversion rather than failing immediately.

Expected result: mismatched Chat/Responses pairs still work when conversion is straightforward. Official `/v1/chat/completions` requests from explicit third-party clients are included: convert Chat requests to official Responses upstream requests, then convert the official Responses body or stream back to Chat Completions without Codex semantic repair.

Official fallback requests must pass through the same thin official endpoint compatibility normalization as official Responses traffic: `store=false`, string `input` normalized to a Responses message-list shape, and unsupported output-token fields removed before the official upstream call.

### Phase 5: Add Vision Proxy Overlay

Extract the current image proxy decision into a `VisionProxyPolicy` boundary and route it through a `VisionProxyAdapter` helper. The first implementation may keep the helper in `codex_proxy.py`, but it must be called through a named policy boundary.

Expected behavior:

- Codex App third-party text-only models preserve current vision proxy support.
- Third-party transparent routes do not run vision proxy unless `VisionProxyPolicy` says enabled.
- When enabled for transparent routes, vision proxy is the only semantic overlay; Compact, subagent repair, browser guidance, and Codex response repair still stay disabled.
- Official Codex App passthrough still bypasses CodexHub vision proxy.

### Phase 6: Prepare Multi-Endpoint Provider Config

Add provider endpoint capabilities only after the first transparent metered path is stable.

Expected result: provider configs can route Chat, Responses, and Anthropic Messages to different upstream endpoint roots.

## Testing Strategy

Unit tests should cover:

- route profile selection by caller and upstream type
- route decision exposes behavior profile, wire-format adapter, Codex semantic adapter, request-kind policy, retry policy, and usage policy
- Codex App third-party traffic still uses Codex adapter behavior
- Codex App third-party Chat-only upstream converts Responses to Chat upstream and Chat back to Responses downstream
- third-party app matching wire format uses transparent metered behavior
- third-party app matching wire format does not call Chat to Responses or Responses to Chat conversion
- third-party app official Responses traffic uses transparent metered behavior
- explicit third-party app official Chat Completions traffic uses lightweight Chat to Responses fallback
- transparent path does not call Codex rewrite, subagent repair, compact handling, implicit image proxy, or synthetic SSE repair
- transparent fallback response conversion does not call Codex response normalization, third-party tool repair, or subagent duplicate-call guards
- Vision Proxy overlay is off by default for transparent paths and can be enabled independently from Codex semantic adapter
- conservative retry retries only before downstream output
- after downstream output, transparent path does not replay
- async usage observation updates Gateway usage aggregation regardless of whether it is observed before or after `request_complete`
- lightweight format fallback is used only when inbound and upstream formats differ
- compact handling remains request-kind scoped and is not attributed only to retry timing

Integration tests should cover:

- third-party client provider-scoped Chat Completions request to a Chat endpoint
- third-party client provider-scoped Responses request to a Responses endpoint
- third-party client official Responses request to the official Responses endpoint
- third-party client official Chat Completions request converted to official Responses upstream
- third-party transparent image request to a text-only provider with Vision Proxy disabled and enabled
- third-party client streaming request with usage observation
- Codex App third-party model request after the subagent branch has a final handoff or lands
- Level 1 and Level 2 subagent E2E suite after rebasing onto the completed subagent branch

## Non-Goals

- Do not remove the Codex App third-party adapter.
- Do not make third-party transparent mode handle Codex subagent semantics.
- Do not make same-format transparent traffic convert through Responses or Chat Completions unnecessarily.
- Do not enable production WebSocket support in this refactor.
- Do not implement multi-endpoint provider config in the first phase.
- Do not change third-party app client config exports unless needed to select the new route behavior.

## Decisions For First Implementation

- Transparent metered routing should become the default for provider-scoped third-party app traffic and third-party app official Responses traffic once the route decision, usage projection, and conservative retry tests pass. During development it may be guarded by a hidden emergency setting so operators can fall back to the current adapter path.
- Conservative retry is enabled by default for transparent metered traffic, but only before downstream output starts. After any downstream write, retry is forbidden.
- Asynchronous usage observations should first update the existing request projection by request id. A separate observations table can be added later if multiple usage observations per request become necessary.
- Lightweight format fallback should be automatic when configured inbound and upstream formats differ. No live provider probing is required for the first implementation.
- Compact stays as a request-kind policy for Codex App external adapter and existing compatibility paths. It should not be part of the transparent path by default.
