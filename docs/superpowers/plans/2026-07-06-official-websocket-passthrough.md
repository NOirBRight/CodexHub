# Official HTTP Passthrough Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep one CodexHub model picker for official OpenAI models and third-party models, while making official Codex App GPT HTTP/SSE traffic avoid CodexHub's third-party gateway compatibility layers.

**Architecture:** Phase 1 keeps proxy mode on HTTP/SSE with `supports_websockets = false`. Official Codex App Responses traffic receives a narrow `official_codex_app_http_passthrough` behavior profile with gateway retry, stream retry, request rewriting, image proxy, browser guidance, compact retry, and synthetic repair disabled. Third-party traffic keeps the existing `external_provider_gateway` behavior, including subagent compatibility; WebSocket production work is deferred until a later bridge can reuse that same gateway core.

**Tech Stack:** Python proxy in `src-python/codex_proxy.py`, proxy config overlay in `src-python/config_overlay.py`, Python unit tests in `tests/test_routing.py`, `tests/test_chat_completions_gateway.py`, and `tests/test_config_overlay.py`; later release validation uses `diagnostics/subagent-e2e/run_level12_e2e.py` from the subagent branch once it lands.

---

## File Structure

- Modify `src-python/codex_proxy.py`: add Phase 1 behavior profile selection, hidden official HTTP passthrough setting, profile-aware request mutation, profile-aware image proxy gating, profile-aware retry gating, and `behavior_profile` telemetry.
- Modify `tests/test_routing.py`: add profile selection tests, official passthrough mutation tests, official retry/image/compact/browser bypass tests, and third-party regression tests.
- Modify `tests/test_chat_completions_gateway.py`: add regression coverage that Chat Completions and third-party provider gateway behavior still use the compatibility path.
- Modify `tests/test_config_overlay.py`: keep proxy overlay locked to `supports_websockets = false` for Phase 1.
- Do not modify `src-python/config_overlay.py` for Phase 1 unless a regression test shows it no longer writes the existing non-WebSocket overlay.
- Do not modify Rust or React settings/UI for Phase 1. The hidden emergency setting is read by Python from runtime settings or environment only.
- Keep `src-python/websocket_transport.py` and `tests/test_websocket_transport.py` as diagnostic assets only; they are not part of the Phase 1 production path.

---

## Safety Invariants

- Proxy mode continues to write `supports_websockets = false`.
- Proxy mode continues to force `responses_websockets = false` and `responses_websockets_v2 = false`.
- No visible WebSocket production switch is added in Phase 1.
- Official passthrough applies only when all are true: upstream is `official`, client context is `codex-app`, inbound format is `responses`, and `gateway_official_http_passthrough_enabled()` is true.
- Official Chat Completions traffic, non-Codex-App traffic, and unknown-client traffic stay on `official_gateway_compat`.
- Third-party traffic always stays on `external_provider_gateway`.
- `external_provider_gateway` keeps request rewriting, tool adaptation, compact handling, image proxy, subagent state guidance, response repair, and retry.
- `official_codex_app_http_passthrough` emits no `upstream_retry`, no `sse_retry_notice`, no `image_proxy_*`, no `browser_context_guidance_injected`, no `compact_text_only_tools_stripped`, and no third-party adapter repair events.
- Release is blocked until the subagent branch has landed or this branch has rebased onto it, and the Level 1/Level 2 E2E suite passes.

---

## Task 1: Lock Phase 1 Config And Add Behavior Profiles

**Files:**
- Modify: `src-python/codex_proxy.py`
- Modify: `tests/test_routing.py`
- Modify: `tests/test_config_overlay.py`

- [ ] **Step 1: Write failing config-lock test**

In `tests/test_config_overlay.py`, add or keep an explicit test named `test_proxy_overlay_stays_non_websocket_for_phase1`.

Assertions:

```python
self.assertIn("supports_websockets = false", updated)
self.assertIn("responses_websockets = false", updated)
self.assertIn("responses_websockets_v2 = false", updated)
self.assertNotIn("supports_websockets = true", updated)
```

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_config_overlay -q
```

Expected: PASS if the existing overlay is still correct. If it fails, fix only `src-python/config_overlay.py` to restore the current non-WebSocket output.

- [ ] **Step 2: Write failing behavior profile tests**

In `tests/test_routing.py`, add tests with these names and assertions:

```python
def test_official_codex_app_responses_uses_http_passthrough_profile(self):
    upstream = {"name": "official"}
    context = {"client_id": "codex-app"}
    self.assertEqual(
        codex_proxy.behavior_profile_for_request(upstream, context, inbound_format="responses"),
        codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
    )

def test_official_chat_completions_uses_gateway_compat_profile(self):
    upstream = {"name": "official"}
    context = {"client_id": "codex-app"}
    self.assertEqual(
        codex_proxy.behavior_profile_for_request(upstream, context, inbound_format="chat_completions"),
        codex_proxy.BEHAVIOR_OFFICIAL_GATEWAY_COMPAT,
    )

def test_official_unknown_client_uses_gateway_compat_profile(self):
    upstream = {"name": "official"}
    context = {"client_id": "unknown"}
    self.assertEqual(
        codex_proxy.behavior_profile_for_request(upstream, context, inbound_format="responses"),
        codex_proxy.BEHAVIOR_OFFICIAL_GATEWAY_COMPAT,
    )

def test_third_party_always_uses_external_gateway_profile(self):
    upstream = {"name": "ollama"}
    context = {"client_id": "codex-app"}
    self.assertEqual(
        codex_proxy.behavior_profile_for_request(upstream, context, inbound_format="responses"),
        codex_proxy.BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
    )
```

Add a setting test:

```python
def test_official_http_passthrough_setting_defaults_enabled_and_env_can_disable(self):
    with patch.dict(os.environ, {}, clear=True):
        self.assertTrue(codex_proxy.gateway_official_http_passthrough_enabled())
    with patch.dict(os.environ, {"CODEX_PROXY_OFFICIAL_HTTP_PASSTHROUGH_ENABLED": "0"}, clear=True):
        self.assertFalse(codex_proxy.gateway_official_http_passthrough_enabled())
```

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing -q
```

Expected: FAIL because the constants and helper do not exist yet.

- [ ] **Step 3: Implement Phase 1 profile helpers**

In `src-python/codex_proxy.py`, near the retry/profile settings helpers, add:

```python
BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH = "official_codex_app_http_passthrough"
BEHAVIOR_OFFICIAL_GATEWAY_COMPAT = "official_gateway_compat"
BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY = "external_provider_gateway"


def gateway_official_http_passthrough_enabled() -> bool:
    return _env_or_settings_flag(
        "CODEX_PROXY_OFFICIAL_HTTP_PASSTHROUGH_ENABLED",
        "gateway_official_http_passthrough_enabled",
        True,
    )


def _is_codex_app_context(request_context: Mapping[str, str]) -> bool:
    return request_context.get("client_id") == "codex-app"


def behavior_profile_for_request(
    upstream: Mapping[str, Any],
    request_context: Mapping[str, str],
    *,
    inbound_format: str,
) -> str:
    if str(upstream.get("name")) != "official":
        return BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY
    if (
        gateway_official_http_passthrough_enabled()
        and inbound_format == "responses"
        and _is_codex_app_context(request_context)
    ):
        return BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
    return BEHAVIOR_OFFICIAL_GATEWAY_COMPAT
```

- [ ] **Step 4: Verify Task 1**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_config_overlay tests.test_routing -q
```

Expected: PASS for the new helper tests and existing routing tests.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src-python/codex_proxy.py tests/test_routing.py tests/test_config_overlay.py
git commit -m "feat: add official http passthrough profile"
```

---

## Task 2: Select Profile Before Compatibility Mutations

**Files:**
- Modify: `src-python/codex_proxy.py`
- Modify: `tests/test_routing.py`

- [ ] **Step 1: Write failing compact and telemetry tests**

In `tests/test_routing.py`, add tests proving:

- official Codex App Responses compact requests do not call `_strip_tools_for_compact_payload`
- third-party compact requests still call `_strip_tools_for_compact_payload`
- `request_start`, `request_complete`, and `request_error` events include `behavior_profile`

Use `patch("codex_proxy._strip_tools_for_compact_payload", wraps=codex_proxy._strip_tools_for_compact_payload)` for the compact tests. Use the existing fake handler helpers in `tests/test_routing.py` and assert:

```python
self.assertEqual(request_start["behavior_profile"], codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH)
```

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing -q
```

Expected: FAIL because `_proxy_post_request()` strips compact tools before it knows the behavior profile and telemetry has no `behavior_profile`.

- [ ] **Step 2: Reorder `_proxy_post_request()`**

In `CodexProxyHandler._proxy_post_request()`:

1. read and decode the body
2. parse `inbound_payload`
3. compute `request_kind`
4. extract `model_requested` from the current body
5. compute `model = provider_scoped_route_model(model_requested, provider_hint)`
6. choose `upstream`
7. compute `behavior_profile = behavior_profile_for_request(upstream, request_context, inbound_format=inbound_format)`
8. only then run compact tool stripping, Chat/Responses conversion, and profile-specific compatibility work

For inbound Chat Completions:

```python
if inbound_format == "chat_completions":
    behavior_profile = BEHAVIOR_OFFICIAL_GATEWAY_COMPAT if upstream_name == "official" else behavior_profile
    body = _chat_completions_request_to_responses_body(body)
```

For compact stripping:

```python
if (
    request_kind == RETRY_REQUEST_COMPACT
    and behavior_profile != BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
    and isinstance(inbound_payload, dict)
    and _strip_tools_for_compact_payload(...)
):
    body = json.dumps(inbound_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
```

- [ ] **Step 3: Add telemetry field**

Add `behavior_profile=behavior_profile` to every `request_start`, `request_complete`, `request_error`, `upstream_protocol_fallback`, `sse_retry_notice`, and adapter event context emitted from the routed POST path.

Set:

```python
adapter_event_context = {
    "request_id": request_id,
    "model": model_canonical,
    "behavior_profile": behavior_profile,
    **proxy_request_context,
}
```

- [ ] **Step 4: Verify Task 2**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src-python/codex_proxy.py tests/test_routing.py
git commit -m "fix: choose gateway behavior before request mutations"
```

---

## Task 3: Implement Profile-Aware Official Request Compatibility

**Files:**
- Modify: `src-python/codex_proxy.py`
- Modify: `tests/test_routing.py`
- Modify: `tests/test_chat_completions_gateway.py`

- [ ] **Step 1: Write failing body compatibility tests**

In `tests/test_routing.py`, add tests for `compatible_request_body(...)`:

```python
def test_official_http_passthrough_only_maps_model_service_tier_and_store(self):
    body = json.dumps({
        "model": "openai/gpt-5.5-fast",
        "input": [{"role": "user", "content": "Current URL: https://example.test/page"}],
        "tools": [{"type": "function", "name": "multi_agent_v1__spawn_agent"}],
        "stream": False,
        "max_output_tokens": 123,
    }).encode("utf-8")
    upstream = {"name": "official", "upstream_model": "gpt-5.5", "service_tier": "priority"}

    transformed = codex_proxy.compatible_request_body(
        body,
        upstream,
        model_id="openai/gpt-5.5-fast",
        behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
    )
    payload = json.loads(transformed)

    self.assertEqual(payload["model"], "gpt-5.5")
    self.assertEqual(payload["service_tier"], "priority")
    self.assertIs(payload["store"], False)
    self.assertIs(payload["stream"], False)
    self.assertEqual(payload["max_output_tokens"], 123)
    self.assertNotIn("Codex browser context detected.", json.dumps(payload))
    self.assertEqual(payload["tools"], [{"type": "function", "name": "multi_agent_v1__spawn_agent"}])
```

Add a regression test proving `BEHAVIOR_OFFICIAL_GATEWAY_COMPAT` still performs the existing official compatibility mutations.

In `tests/test_chat_completions_gateway.py`, add or update a test proving official Chat Completions traffic uses `official_gateway_compat`, not `official_codex_app_http_passthrough`.

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing tests.test_chat_completions_gateway -q
```

Expected: FAIL because `compatible_request_body()` is not profile-aware yet.

- [ ] **Step 2: Add `behavior_profile` parameter**

Change the signature:

```python
def compatible_request_body(
    body: bytes,
    upstream: Mapping[str, Any],
    model_id: str | None = None,
    event_context: Mapping[str, Any] | None = None,
    inject_codex_tools: bool = True,
    behavior_profile: str = BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
) -> bytes:
```

Update every call site to pass the computed profile:

```python
body = compatible_request_body(
    body,
    upstream,
    model_id=model,
    event_context=adapter_event_context,
    inject_codex_tools=request_kind != RETRY_REQUEST_COMPACT,
    behavior_profile=behavior_profile,
)
```

- [ ] **Step 3: Add the official passthrough branch**

At the top of the parsed JSON branch, before the current `if upstream_name == "official":` compatibility block, add:

```python
if behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH:
    if isinstance(upstream_model, str) and upstream_model and payload.get("model") != upstream_model:
        payload["model"] = upstream_model
        changed = True
    service_tier = upstream.get("service_tier")
    if isinstance(service_tier, str) and service_tier and payload.get("service_tier") != service_tier:
        payload["service_tier"] = service_tier
        changed = True
    if payload.get("store") is not False:
        payload["store"] = False
        changed = True
    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
```

Do not call these helpers in that branch:

- `_normalize_responses_message_input_items`
- `_normalize_responses_string_input`
- `_sanitize_official_system_messages`
- `_sanitize_official_invalid_tool_calls`
- `_inject_browser_context_guidance`
- `_rewrite_structured_tool_input_items`
- `_rewrite_internal_input_items`
- `_inject_explicit_codex_tools`

- [ ] **Step 4: Verify Task 3**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing tests.test_chat_completions_gateway -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```powershell
git add src-python/codex_proxy.py tests/test_routing.py tests/test_chat_completions_gateway.py
git commit -m "fix: make official responses passthrough profile minimal"
```

---

## Task 4: Disable Gateway Retry, Stream Retry, Image Proxy, And Retry Notices For Official Passthrough

**Files:**
- Modify: `src-python/codex_proxy.py`
- Modify: `tests/test_routing.py`

- [ ] **Step 1: Write failing retry and image-proxy tests**

In `tests/test_routing.py`, add tests proving that official Codex App Responses passthrough:

- calls `_open_upstream_response(..., max_attempts=1)`
- calls `_relay_upstream_response(..., defer_stream_errors=False)`
- does not call `apply_image_proxy_to_responses_payload`
- does not emit `upstream_retry`
- does not emit `sse_retry_notice`

Use existing fake response helpers and `patch(...)` around `_open_upstream_response`, `_relay_upstream_response`, and `apply_image_proxy_to_responses_payload`.

Add third-party regression tests proving:

- external provider requests still call `apply_image_proxy_to_responses_payload` when image proxy is enabled
- external provider retry still emits `upstream_retry` when the first upstream open fails with a transient retryable error

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing -q
```

Expected: FAIL because official traffic still uses shared retry and image proxy behavior.

- [ ] **Step 2: Gate image proxy by profile**

In `_proxy_post_request()`, compute:

```python
is_official_http_passthrough = behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
```

Wrap image proxy application:

```python
if (
    not is_official_http_passthrough
    and isinstance(image_proxy_payload, dict)
    and apply_image_proxy_to_responses_payload(...)
):
    body = json.dumps(image_proxy_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
```

- [ ] **Step 3: Gate downstream retry notices by profile**

Change:

```python
emit_retry_to_downstream = (
    caller_stream
    and inbound_format == "responses"
    and gateway_downstream_retry_notice_enabled()
)
```

to:

```python
emit_retry_to_downstream = (
    not is_official_http_passthrough
    and caller_stream
    and inbound_format == "responses"
    and gateway_downstream_retry_notice_enabled()
)
```

- [ ] **Step 4: Gate upstream open retry and stream retry by profile**

Before calling `_open_upstream_response(...)`, compute:

```python
open_max_attempts = 1 if is_official_http_passthrough else None
base_relay_attempts = 1 if is_official_http_passthrough else _upstream_retry_attempts(request_kind)
```

Pass:

```python
max_attempts=open_max_attempts,
```

to `_open_upstream_response(...)`.

Set relay deferral:

```python
defer_stream_errors=False if is_official_http_passthrough else relay_attempt < relay_attempts
```

When `is_official_http_passthrough` is true, do not call `_emit_upstream_retry_event(...)`, do not call `emit_downstream_retry(...)`, and do not sleep/replay the request. The simplest implementation is to keep `base_relay_attempts = 1`, which makes the existing retry branch raise on the first failure.

- [ ] **Step 5: Verify Task 4**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```powershell
git add src-python/codex_proxy.py tests/test_routing.py
git commit -m "fix: bypass gateway retry for official passthrough"
```

---

## Task 5: Phase 1 Regression And Release Gate

**Files:**
- Modify only files required by failing tests within the Phase 1 scope.

- [ ] **Step 1: Run Python regression suite**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_config_overlay tests.test_websocket_transport tests.test_routing tests.test_chat_completions_gateway tests.test_proxy_event_logging tests.test_subagent_state -q
```

Expected: PASS.

- [ ] **Step 2: Run full Python unit discovery**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest discover -s tests -q
```

Expected: PASS.

- [ ] **Step 3: Rebase or merge after subagent branch lands**

Wait until the work from Codex thread `019f3038-028d-7891-9c68-6fe6825046e7` is available on the target integration branch. Rebase or merge this Phase 1 branch onto that branch and resolve conflicts in:

- `src-python/codex_proxy.py`
- `tests/test_routing.py`
- `tests/test_chat_completions_gateway.py`
- `tests/test_subagent_state.py`

Do not resolve conflicts by dropping subagent behavior. Preserve worker detection, subagent state guidance, malformed multi-agent tool repair, required tool-choice repair, and lifecycle-complete finalization from the subagent branch.

- [ ] **Step 4: Run subagent E2E gate**

After the subagent runner exists in this branch, run with a valid `OLLAMA_API_KEY`:

```powershell
$env:PYTHONPATH='src-python'; python diagnostics/subagent-e2e/run_level12_e2e.py --level all --models glm52,k2_7,m3 --endpoints responses,chat --jobs 2
```

Expected: command exits `0`, and the generated `diagnostics/subagent-e2e/level12-e2e-*/summary.md` marks every Level 1 and Level 2 case as passing.

- [ ] **Step 5: Manual Phase 1 validation**

With proxy mode active:

1. Inspect `~/.codex/config.toml`.
2. Confirm `supports_websockets = false`.
3. Confirm `responses_websockets = false` and `responses_websockets_v2 = false`.
4. Select `openai/gpt-5.5` in Codex App and send a small non-sensitive request.
5. Confirm `codex-proxy-events.jsonl` contains `behavior_profile = "official_codex_app_http_passthrough"`.
6. Confirm the official request emits no `upstream_retry`, `sse_retry_notice`, `image_proxy_*`, `browser_context_guidance_injected`, `compact_text_only_tools_stripped`, or third-party adapter repair events.
7. Select a third-party model and send a small non-sensitive request.
8. Confirm third-party events contain `behavior_profile = "external_provider_gateway"` and keep normal retry/adapter behavior.

- [ ] **Step 6: Final checks**

Run:

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors. Remaining untracked diagnostics are either intentionally ignored or moved out of the release branch.

- [ ] **Step 7: Commit final Phase 1 fixes**

```powershell
git add src-python/codex_proxy.py tests/test_routing.py tests/test_chat_completions_gateway.py tests/test_config_overlay.py
git commit -m "test: gate official passthrough against third-party subagents"
```

---

## Phase 2 Backlog: WebSocket Transport Refactor

Do not start these tasks until Phase 1 has shipped or is stable on the integration branch.

### Phase 2 Task A: Capture Desktop Codex App WebSocket Contract

Use the gated recorder only. Capture:

- upgrade path
- query keys
- selected subprotocol
- non-secret header names
- first-frame opcode and byte length
- JSON top-level keys
- model location
- close code behavior

Do not capture prompt text, tool arguments, frame bodies, authorization values, cookies, account ids, file contents, or raw headers.

Save findings to:

- `docs/superpowers/findings/YYYY-MM-DD-codex-app-websocket-contract.md`
- `tests/fixtures/codex_app_websocket_contract.json`

### Phase 2 Task B: Extract Shared Gateway Core

Refactor third-party HTTP/SSE gateway dispatch so a later WebSocket bridge can use the same request rewriting, retry, subagent guidance, and response repair logic.

The bridge must not duplicate subagent behavior.

### Phase 2 Task C: Implement Official WebSocket Relay

Implement official relay only after the contract fixture defines:

- first request frame shape
- model mapping location
- service-tier location
- upstream close propagation behavior

Official relay must not use gateway retry or third-party compatibility mutations.

### Phase 2 Task D: Implement Third-Party WebSocket Bridge

Before setting `supports_websockets = true`, implement a bridge from Codex App WebSocket request frames into the existing third-party gateway core and back to the Codex App WebSocket response envelope.

The bridge must pass:

```powershell
$env:PYTHONPATH='src-python'; python diagnostics/subagent-e2e/run_level12_e2e.py --level all --models glm52,k2_7,m3 --endpoints responses,chat --jobs 2
```

### Phase 2 Task E: Expose Production WebSocket Switch

Only after official relay and third-party bridge pass tests:

- add a visible advanced UI switch
- allow `config_overlay.py` to write `supports_websockets = true`
- remove stale `responses_websockets = false` and `responses_websockets_v2 = false`
- document rollback to Phase 1 HTTP/SSE mode

---

## Expected Outcome

After Phase 1:

- CodexHub keeps official and third-party models in one custom provider model picker.
- Official Codex App GPT traffic stays on HTTP/SSE but no longer receives CodexHub gateway retry or third-party compatibility mutations.
- Proxy mode keeps `supports_websockets = false`, so third-party models are not forced onto WebSocket.
- Third-party models keep the existing compatibility gateway, retry behavior, and subagent support.
- The first release can ship without WebSocket relay/bridge work, but only after the subagent branch passes its E2E gate.

After Phase 2:

- WebSocket protocol understanding is pinned in tests and sanitized documentation before production relay code depends on it.
- Official Codex App GPT traffic can use WebSocket transport through CodexHub without gateway retry or third-party compatibility mutations.
- Third-party WebSocket requests bridge into the same compatibility gateway core used by HTTP/SSE, preserving subagent behavior.
