# Third-Party Transparent Metered Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish official thin passthrough hardening first, then split Codex App third-party adaptation from third-party client transparent proxying while preserving metering and conservative retry.

**Architecture:** Keep official Codex App Responses traffic on the existing thin `official_codex_app_http_passthrough` path, with no gateway compatibility mutation and with async usage projected into Gateway usage. Add a route decision object that separately selects behavior profile, wire-format adapter, Codex App semantic adapter, request-kind policy, retry policy, and usage policy. Then enable same-format transparent proxying for third-party clients and use lightweight Responses/Chat conversion only when caller and upstream endpoint formats differ.

**Tech Stack:** Python proxy in `src-python/codex_proxy.py`, Python telemetry in `src-python/proxy_telemetry.py`, Rust/Tauri telemetry ingestion in `src-tauri/src/gateway.rs`, Rust official usage bridge in `src-tauri/src/openai_usage.rs`, Python unit tests in `tests/test_routing.py`, `tests/test_chat_completions_gateway.py`, and `tests/test_proxy_event_logging.py`, Rust tests in `src-tauri`.

## Global Constraints

- Do not touch or depend on the in-progress subagent implementation branch. As of 2026-07-07, thread `019f39bd-9ef9-7aa1-8e8f-33cddc8e0eef` is still `inProgress` in `C:\Users\noirb\.codex\worktrees\f11c\CodexHub` on branch `codex/subagent-protocol-fix`.
- Treat Codex App subagent repair as a `CodexAppSemanticAdapter` extension point until that branch has a clean handoff or lands.
- Do not remove Codex App third-party adapter behavior.
- Do not run Codex-specific rewrite, compact handling, subagent repair, browser guidance, image proxy, or synthetic SSE repair on transparent third-party client paths.
- Prefer same-format upstream passthrough. Do not convert Chat to Responses to Chat when caller and upstream both support Chat.
- Retry for transparent paths is allowed only before downstream headers or body bytes are written.
- Usage observation must be asynchronous and must update Gateway usage aggregation by request id.
- Multi-endpoint provider config is deferred until after the first implementation.

---

## File Structure

- Modify `src-python/codex_proxy.py`: official passthrough async usage event name, route decision model, route classification, wire-format adapter selection, transparent request dispatch, transparent relay, retry policy wiring, async usage tap event emission.
- Modify `src-python/proxy_telemetry.py`: allow `usage_observed` events to update request usage fields by `request_id`.
- Modify `src-tauri/src/gateway.rs`: allow `usage_observed` events to update request usage fields during UI telemetry ingestion and JSONL backfill.
- Modify `src-tauri/src/openai_usage.rs`: bound the Codex app-server subprocess used for official account usage so the usage UI cannot hang indefinitely.
- Modify `tests/test_routing.py`: official thin passthrough regression gate, route decision tests, transparent-path no-adapter tests, conservative retry tests, compact separation tests.
- Modify `tests/test_chat_completions_gateway.py`: same-format conversion bypass tests and lightweight fallback conversion tests.
- Modify `tests/test_proxy_event_logging.py`: Python usage projection tests.
- Modify `docs/superpowers/specs/2026-07-07-third-party-transparent-metered-routing-design.md`: keep spec aligned if implementation discovers a narrower invariant.

---

## Preflight: Confirm Subagent Dependency Is Deferred

**Files:**
- Inspect only: `C:\Users\noirb\.codex\worktrees\f11c\CodexHub`
- Inspect only: Codex thread `019f39bd-9ef9-7aa1-8e8f-33cddc8e0eef`

**Interfaces:**
- Produces: implementation decision to proceed without rebasing or copying the unfinished subagent branch.

- [x] **Step 1: Read the subagent thread status**

Run through the Codex thread inspector:

```text
read_thread(threadId="019f39bd-9ef9-7aa1-8e8f-33cddc8e0eef")
```

Observed: the thread is still `inProgress`. Continue with this plan, but leave Codex App subagent integration deferred.

- [x] **Step 2: Inspect the subagent worktree without editing**

Run:

```powershell
git -C C:\Users\noirb\.codex\worktrees\f11c\CodexHub status --short
```

Observed: the subagent branch has unfinished source, test, and diagnostics changes. Do not copy those changes into this worktree.

- [x] **Step 3: Record the dependency decision**

Decision:

```text
Subagent dependency decision: native subagent repair is deferred for this implementation. This branch will define route and adapter boundaries, but will not consume codex/subagent-protocol-fix until that work has a clean handoff or lands.
```

---

## Task 0: Finish Official Thin Passthrough Hardening

**Files:**
- Modify: `src-python/codex_proxy.py`
- Modify: `src-python/proxy_telemetry.py`
- Modify: `src-tauri/src/gateway.rs`
- Modify: `src-tauri/src/openai_usage.rs`
- Test: `tests/test_routing.py`
- Test: `tests/test_proxy_event_logging.py`
- Test: `src-tauri/src/openai_usage.rs`
- Test: `src-tauri/src/gateway.rs`

**Interfaces:**
- Consumes existing behavior profile: `BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH`.
- Produces: official passthrough remains transport-thin for Codex App Responses traffic.
- Produces: official passthrough async usage is emitted as `usage_observed`, not only `official_passthrough_usage_observed`.
- Produces: Python and Rust Gateway telemetry projections consume `usage_observed`.
- Produces: Codex account usage subprocess is bounded by an explicit timeout.

- [x] **Step 1: Run the existing official passthrough regression gate**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing -q
```

Expected: PASS. This confirms current coverage for:

```text
official profile selection
single upstream open attempt
no gateway retry or downstream retry notice
no image proxy
no compact tool stripping
no browser guidance
one body parse on official passthrough
request body HMAC skipped while cache key is retained
no synthetic Codex App identity headers
raw official SSE relay
no synthetic official stream terminal/error event
third-party gateway behavior unchanged
```

- [x] **Step 2: Add the failing Python usage projection test**

In `tests/test_proxy_event_logging.py`, add:

```python
def test_usage_observed_updates_existing_gateway_request_usage(self):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "codex-proxy-telemetry.sqlite"
        proxy_telemetry.write_event_to_sqlite(
            db_path,
            {
                "ts": "2026-07-07T01:00:00Z",
                "event": "request_complete",
                "request_id": "req-usage-observed",
                "status": 200,
                "usage_source": "missing",
                "usage_missing_reason": "async_usage_pending",
            },
        )
        proxy_telemetry.write_event_to_sqlite(
            db_path,
            {
                "ts": "2026-07-07T01:00:01Z",
                "event": "usage_observed",
                "request_id": "req-usage-observed",
                "usage_source": "upstream_async",
                "usage_input_tokens": 11,
                "usage_cached_input_tokens": 3,
                "usage_output_tokens": 5,
                "usage_total_tokens": 16,
            },
        )

        connection = sqlite3.connect(db_path)
        try:
            row = connection.execute(
                "SELECT usage_source, usage_input_tokens, usage_cached_input_tokens, usage_output_tokens, usage_total_tokens FROM gateway_requests WHERE request_id = ?",
                ("req-usage-observed",),
            ).fetchone()
        finally:
            connection.close()

    self.assertEqual(row, ("upstream_async", 11, 3, 5, 16))
```

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_proxy_event_logging.ProxyEventLoggingTests.test_usage_observed_updates_existing_gateway_request_usage -q
```

Expected before implementation: FAIL because `_upsert_request()` ignores `usage_observed`.

- [x] **Step 3: Project `usage_observed` in Python telemetry**

In `src-python/proxy_telemetry.py`, allow `_upsert_request()` to process `usage_observed`:

```python
if not request_id or event not in {"request_start", "request_complete", "request_error", "usage_observed"}:
    return
```

Keep completion timestamp updates limited to terminal request events:

```python
if event == "request_start":
    values["first_ts"] = _string(payload.get("ts"))
elif event in {"request_complete", "request_error"}:
    values["completed_ts"] = _string(payload.get("ts"))
```

- [x] **Step 4: Add the failing Rust Gateway projection test**

In `src-tauri/src/gateway.rs`, add a telemetry ingestion test near existing `gateway_requests` projection tests. It should ingest:

```rust
let request_complete = r#"{"ts":"2026-07-07T01:00:00Z","event":"request_complete","request_id":"req-usage-observed-rust","status":200,"usage_source":"missing","usage_missing_reason":"async_usage_pending","upstream":"official","model":"openai/gpt-5.5"}"#;
let usage_observed = r#"{"ts":"2026-07-07T01:00:01Z","event":"usage_observed","request_id":"req-usage-observed-rust","usage_source":"upstream_async","usage_input_tokens":11,"usage_cached_input_tokens":3,"usage_output_tokens":5,"usage_total_tokens":16,"upstream":"official","model":"openai/gpt-5.5"}"#;
```

Expected assertion: the `gateway_requests` row for `req-usage-observed-rust` has `usage_source = "upstream_async"` and token fields populated.

- [x] **Step 5: Project `usage_observed` in Rust Gateway telemetry**

In `src-tauri/src/gateway.rs`, update the event allow-list:

```rust
if event != "request_start"
    && event != "request_complete"
    && event != "request_error"
    && event != "usage_observed"
{
    return Ok(());
}
```

Keep `completed_ts` updates limited to `request_complete` and `request_error`.

- [x] **Step 6: Add generic usage-observed emission helper**

In `src-python/codex_proxy.py`, add:

```python
def _write_usage_observed_event(context: Mapping[str, Any], usage: Mapping[str, Any] | None) -> None:
    if usage is None:
        return
    write_proxy_event(
        "usage_observed",
        request_id=context.get("request_id"),
        model=context.get("model"),
        upstream=context.get("upstream"),
        provider_id=context.get("provider_id") or context.get("upstream"),
        upstream_format=context.get("upstream_format"),
        inbound_format=context.get("inbound_format"),
        **_normalize_usage_for_event(usage),
    )
```

Replace the `write_proxy_event("official_passthrough_usage_observed", ...)` call in `_official_passthrough_usage_worker()` with:

```python
_write_usage_observed_event(context, usage)
```

- [x] **Step 7: Add official usage worker test**

In `tests/test_routing.py`, add:

```python
def test_official_passthrough_usage_worker_emits_usage_observed(self):
    context = {
        "request_id": "req-async-usage",
        "model": "openai/gpt-5.5",
        "upstream": "official",
        "upstream_format": "responses",
        "inbound_format": "responses",
    }
    line = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":2,"output_tokens":3,"total_tokens":5}}}\n\n'

    with patch("codex_proxy.write_proxy_event") as write_event:
        payload_bytes = codex_proxy._sse_payload_bytes(line)
        payload = json.loads(payload_bytes.decode("utf-8"))
        usage = codex_proxy._usage_from_response_event(payload)
        codex_proxy._write_usage_observed_event(context, usage)

    write_event.assert_called_once()
    self.assertEqual(write_event.call_args.args[0], "usage_observed")
    fields = write_event.call_args.kwargs
    self.assertEqual(fields["request_id"], "req-async-usage")
    self.assertEqual(fields["usage_source"], "upstream")
    self.assertEqual(fields["usage_input_tokens"], 2)
    self.assertEqual(fields["usage_output_tokens"], 3)
```

- [x] **Step 8: Add Codex account usage subprocess timeout test**

In `src-tauri/src/openai_usage.rs`, add a testable helper that can read from an app-server process with a deadline. The test should use a child process or mock reader that never emits id `2` and assert the function returns an error containing:

```text
Codex account usage timed out
```

The timeout should be small in the test, for example `Duration::from_millis(50)`.

- [x] **Step 9: Implement the account usage timeout**

In `src-tauri/src/openai_usage.rs`, add:

```rust
const CODEX_ACCOUNT_USAGE_TIMEOUT: Duration = Duration::from_secs(8);
```

Use the deadline while waiting for the `account/usage/read` response. If the deadline expires, kill and wait on the child process and return:

```rust
Err("Codex account usage timed out.".to_string())
```

Implementation may use a reader thread plus `recv_timeout`, or a helper that checks elapsed time around nonblocking child output. It must not leave the app-server child running on timeout.

- [x] **Step 10: Verify Task 0**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing tests.test_proxy_event_logging -q
cd src-tauri
cargo test -q gateway:: --lib
cargo test -q openai_usage:: --lib
cd ..
```

Expected: PASS.

---

## Task 1: Introduce Route Decision Policies Without Behavior Change

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_routing.py`

**Interfaces:**
- Produces: `RouteDecision` dataclass.
- Produces: `route_decision_for_request(upstream, request_context, inbound_format, provider_hint=None) -> RouteDecision`.
- Produces constants: `BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER`, `BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED`, `WIRE_TRANSPARENT`, `WIRE_RESPONSES_TO_CHAT`, `WIRE_CHAT_TO_RESPONSES`, `CODEX_SEMANTIC_EXTERNAL_ADAPTER`, `CODEX_SEMANTIC_NONE`, `REQUEST_KIND_GATEWAY`, `REQUEST_KIND_TRANSPARENT`, `RETRY_GATEWAY_FULL`, `RETRY_CONSERVATIVE_PRE_OUTPUT`, `USAGE_SYNC_CAPTURE`, `USAGE_ASYNC_TAP`.
- Produces only policy names for Codex App subagent repair. It must not import or duplicate unfinished code from `codex/subagent-protocol-fix`.

- [x] **Step 1: Write failing route decision tests**

Already added in this worktree:

```text
tests.test_routing.RoutingTests.test_route_decision_codex_app_third_party_chat_upstream_uses_codex_adapter_and_wire_conversion
tests.test_routing.RoutingTests.test_route_decision_third_party_app_provider_same_format_is_transparent_metered
tests.test_routing.RoutingTests.test_route_decision_third_party_app_official_responses_is_transparent_metered
```

- [x] **Step 2: Run tests to verify failure**

Observed before implementation: FAIL with `AttributeError: module 'codex_proxy' has no attribute 'route_decision_for_request'`.

- [x] **Step 3: Add the route decision model**

Already added in this worktree:

```python
@dataclass(frozen=True)
class RouteDecision:
    behavior_profile: str
    selected_upstream_format: str
    wire_format_adapter: str
    codex_semantic_adapter: str
    request_kind_policy: str
    retry_policy: str
    usage_policy: str
    repair_policy: str
```

- [x] **Step 4: Run route tests**

Observed:

```text
python -m unittest tests.test_routing -q
Ran 217 tests
OK
```

- [x] **Step 5: Keep these changes uncommitted until Task 0 is complete**

Do not commit or stage Task 1 before the restored official passthrough Task 0 has passed.

---

## Task 2: Add Same-Format Transparent Metered Dispatch

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_routing.py`
- Test: `tests/test_chat_completions_gateway.py`

**Interfaces:**
- Consumes: `RouteDecision`.
- Produces: transparent path bypasses `compatible_request_body`, `_chat_completions_request_to_responses_body`, `_responses_request_to_chat_completion_body`, `apply_image_proxy_to_responses_payload`, and Codex-specific SSE repair when `wire_format_adapter == WIRE_TRANSPARENT`.

- [x] **Step 1: Write failing same-format bypass test**

In `tests/test_chat_completions_gateway.py`, add a provider-scoped Chat request to `/v1/providers/volc/chat/completions`. Patch the conversion and Codex adapter functions with `AssertionError`, patch `urlopen` to return a Chat response, and assert the downstream body contains the upstream Chat response id.

- [x] **Step 2: Consume route decision before request conversion**

In `CodexProxyHandler._proxy_post_request()`, compute `route_decision` before compatibility mutation and use:

```python
behavior_profile = route_decision.behavior_profile
selected_upstream_format = route_decision.selected_upstream_format
```

Only run Codex App compatibility mutation when:

```python
route_decision.codex_semantic_adapter == CODEX_SEMANTIC_EXTERNAL_ADAPTER
```

- [x] **Step 3: Add transparent response relay branch**

In `_relay_upstream_response()`, add a branch for:

```python
behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
and upstream_format == inbound_format
```

This branch must copy upstream headers and bytes/SSE lines to downstream without rewriting them. It may parse copied data only after forwarding to emit `usage_observed`.

- [x] **Step 4: Verify Task 2**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_chat_completions_gateway tests.test_routing -q
```

Expected: PASS.

---

## Task 3: Add Conservative Pre-Output Retry For Transparent Paths

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_routing.py`

**Interfaces:**
- Consumes: `RETRY_CONSERVATIVE_PRE_OUTPUT`.
- Produces: transparent retry before downstream headers/body only.

- [x] **Step 1: Write failing pre-output retry test**

Add a test that calls `_open_upstream_response(..., retry_policy=RETRY_CONSERVATIVE_PRE_OUTPUT)` with `urlopen` side effects `[URLError(...), success]` and asserts exactly two upstream opens before any downstream output exists.

- [x] **Step 2: Write failing after-output no-replay test**

Add a streaming transparent relay test where the first SSE line is written and the next read raises `ConnectionResetError`. Assert the partial line reached downstream and no replay occurred.

- [x] **Step 3: Add retry policy parameter**

Update `_open_upstream_response()` with:

```python
retry_policy: str = RETRY_GATEWAY_FULL
```

If `retry_policy == RETRY_CONSERVATIVE_PRE_OUTPUT`, allow open retry but do not emit downstream retry notices.

- [x] **Step 4: Ensure transparent relay does not defer stream errors after output**

When relaying transparent paths, set:

```python
defer_stream_errors = False
```

Do not replay after headers or body bytes have been written.

- [x] **Step 5: Verify Task 3**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing -q
```

Expected: PASS.

---

## Task 4: Keep Compact Scoped To Gateway Compatibility

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_routing.py`
- Test: `tests/test_chat_completions_gateway.py`

**Interfaces:**
- Consumes: `REQUEST_KIND_GATEWAY` and `REQUEST_KIND_TRANSPARENT`.
- Produces: transparent route does not strip tools or classify prompt heuristics unless a future explicit setting enables it.

- [x] **Step 1: Write failing transparent compact bypass test**

Add a provider-scoped third-party Chat request containing compact-like text and tools. Patch `_strip_tools_for_compact_payload` with `AssertionError` and assert the transparent request completes.

- [x] **Step 2: Gate compact handling by request-kind policy**

Only call compact prompt heuristics and `_strip_tools_for_compact_payload()` when:

```python
route_decision.request_kind_policy == REQUEST_KIND_GATEWAY
```

For transparent paths, set:

```python
request_kind = RETRY_REQUEST_MAIN_GENERATION
```

- [x] **Step 3: Verify Codex App compact behavior still works**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_chat_completions_gateway.ChatRequestToResponsesTests.test_compact_prompt_detection_strips_tools_before_conversion tests.test_chat_completions_gateway.ChatCompletionsGatewayTests.test_compact_empty_response_uses_compact_retry_budget -q
```

Expected: PASS.

---

## Task 5: Add Lightweight Wire Format Fallback

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_chat_completions_gateway.py`

**Interfaces:**
- Consumes: `WIRE_RESPONSES_TO_CHAT` and `WIRE_CHAT_TO_RESPONSES`.
- Produces: mismatched third-party client routes use conversion without Codex semantic adapter.

- [x] **Step 1: Write failing Chat caller to Responses upstream fallback test**

Add a third-party Chat request whose selected upstream is Responses-only. Patch `compatible_request_body` with `AssertionError`, return a Responses body from `urlopen`, and assert downstream is Chat-shaped.

- [x] **Step 2: Implement fallback request body selection**

For transparent fallback:

```python
if route_decision.wire_format_adapter == WIRE_CHAT_TO_RESPONSES:
    upstream_body = _chat_completions_request_to_responses_body(body)
    selected_upstream_format = "responses"
elif route_decision.wire_format_adapter == WIRE_RESPONSES_TO_CHAT:
    upstream_body = _responses_request_to_chat_completion_body(body)
    selected_upstream_format = "chat_completions"
```

Do not call `compatible_request_body()` for transparent fallback.

- [x] **Step 3: Implement fallback response conversion**

For transparent fallback:

```text
Chat caller + Responses upstream -> Chat downstream
Responses caller + Chat upstream -> Responses downstream
```

Reuse existing Chat/Responses conversion helpers, but do not run Codex semantic repair.

- [x] **Step 4: Verify Task 5**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_chat_completions_gateway tests.test_routing -q
```

Expected: PASS.

---

## Task 6: Full Regression And Documentation Gate

**Files:**
- Modify only files required by failing tests.
- Verify: `docs/superpowers/specs/2026-07-07-third-party-transparent-metered-routing-design.md`

**Interfaces:**
- Consumes all interfaces from Tasks 0 through 5.
- Produces release-ready test evidence.

- [x] **Step 1: Run focused Python tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing tests.test_chat_completions_gateway tests.test_proxy_event_logging -q
```

Expected: PASS.

- [x] **Step 2: Run full Python discovery**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest discover -s tests -q
```

Expected: PASS.

- [x] **Step 3: Run Rust tests**

Run:

```powershell
cd src-tauri
cargo test -q
cd ..
```

Expected: PASS.

- [x] **Step 4: Verify official passthrough telemetry and thin behavior**

Expected official passthrough event properties:

```json
{
  "behavior_profile": "official_codex_app_http_passthrough"
}
```

Expected absent events:

```text
upstream_retry
sse_retry_notice
image_proxy_applied
image_proxy_failed
browser_context_guidance_injected
compact_text_only_tools_stripped
upstream_stream_incomplete_synthesized_terminal
```

- [x] **Step 5: Verify third-party transparent path telemetry**

Expected event properties:

```json
{
  "behavior_profile": "third_party_app_transparent_metered",
  "wire_format_adapter": "transparent",
  "codex_semantic_adapter": "none",
  "retry_policy": "conservative_pre_output",
  "usage_policy": "async_tap"
}
```

- [x] **Step 6: Verify Codex App third-party path remains adapter-owned**

Expected event properties:

```json
{
  "behavior_profile": "codex_app_external_adapter",
  "codex_semantic_adapter": "codex_app_external_adapter"
}
```

Do not require native subagent branch E2E for this transparent-path implementation unless that branch has landed.

- [x] **Step 7: Run subagent gate only after branch integration**

After threads `019f3038-028d-7891-9c68-6fe6825046e7` and `019f39bd-9ef9-7aa1-8e8f-33cddc8e0eef` have a clean handoff or land, run:

```powershell
$env:PYTHONPATH='src-python'; python diagnostics/subagent-e2e/run_level12_e2e.py --level all --models glm52,k2_7,m3 --endpoints responses,chat --jobs 2
```

If the subagent branch is still `inProgress`, skip this gate for the transparent-path implementation and record:

```text
Subagent gate skipped: codex/subagent-protocol-fix has not produced a clean handoff or landed.
```

Observed during this implementation: subagent gate skipped because `codex/subagent-protocol-fix` has not produced a clean handoff or landed.

- [x] **Step 8: Final diff check**

Run:

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors. Status shows only intentional source, test, and documentation changes.

---

## Review Follow-Up: Fix Merge Blockers And Add Vision Proxy Overlay

**Files:**
- Modify: `src-python/codex_proxy.py`
- Modify: `src-python/proxy_telemetry.py`
- Modify: `src-tauri/src/gateway.rs`
- Test: `tests/test_routing.py`
- Test: `tests/test_chat_completions_gateway.py`
- Test: `tests/test_proxy_event_logging.py`
- Test: `src-tauri/src/gateway.rs`
- Verify: `docs/superpowers/specs/2026-07-07-third-party-transparent-metered-routing-design.md`

**Interfaces:**
- Produces: order-tolerant `usage_observed` projection.
- Produces: official passthrough raw SSE behavior for `stream:false` and HTTP error event streams.
- Produces: explicit third-party official `/v1/chat/completions` lightweight Chat-to-Responses fallback.
- Produces: pure response-side wire-format conversion for transparent fallback.
- Produces: telemetry fields from `RouteDecision`: `wire_format_adapter`, `codex_semantic_adapter`, `request_kind_policy`, `retry_policy`, `usage_policy`, `repair_policy`, and `vision_proxy_policy`.
- Produces: `VisionProxyPolicy`/`VisionProxyAdapter` boundary so vision proxy can be used by Codex App third-party adapter and optionally by third-party transparent routes without becoming part of the transparent core.

- [x] **Step 1: Add failing usage projection event-order tests**

Add Python and Rust tests where `usage_observed` is ingested before `request_complete`. Expected before the fix: `usage_source` is downgraded to `missing`.

- [x] **Step 2: Make usage projection order-tolerant**

In Python and Rust telemetry projection, do not let `request_complete` or `request_error` overwrite existing non-missing usage fields with `missing` or `async_usage_pending`.

- [x] **Step 3: Add failing official passthrough SSE tests**

Cover official Codex App Responses passthrough when upstream returns event-stream with caller `stream:false`, and when upstream returns an event-stream `HTTPError`. Expected: raw SSE relay and official passthrough behavior profile.

- [x] **Step 4: Preserve official passthrough raw event-stream behavior**

Pass `behavior_profile` into HTTPError relay and prioritize official passthrough raw SSE relay before buffer/convert logic, regardless of caller stream mode.

- [x] **Step 5: Add failing official Chat fallback test**

Add explicit third-party client `/v1/chat/completions` request for an official model. Expected behavior: Chat request converts to official Responses upstream; official Responses body converts back to Chat downstream; Codex semantic adapter does not run.

- [x] **Step 6: Enable official Chat-to-Responses fallback**

Allow explicit third-party identity + official upstream + `WIRE_CHAT_TO_RESPONSES` to use `third_party_app_transparent_metered` with lightweight fallback.

- [x] **Step 6a: Add thin official endpoint compatibility for transparent metered traffic**

E2E with OpenCode/OMP showed the official Codex backend rejects some public Responses-compatible request shapes on transparent paths. Keep `codex_semantic_adapter=none`, but normalize only the official endpoint contract before the upstream call:

- set `store=false`
- normalize string `input` into a Responses message-list shape
- remove unsupported `max_output_tokens`

Also convert upstream `{detail: ...}` Responses errors into Chat error payloads during Chat fallback instead of returning an empty assistant message.

- [x] **Step 7: Add failing response-side semantic bypass tests**

Patch `compatible_response_body`, `_normalize_third_party_tool_call`, `_downgrade_invalid_third_party_tool_calls`, and `_guard_duplicate_multi_agent_spawn_calls` in transparent fallback tests. Expected before the fix: at least one Codex semantic repair helper is called.

- [x] **Step 8: Add pure transparent fallback response conversion**

For `third_party_app_transparent_metered`, perform only wire-format conversion on fallback responses. Do not run Codex response repair, tool-call repair, duplicate subagent guard, browser guidance, compact repair, or synthetic terminal repair.

- [x] **Step 9: Run live E2E matrix and extended capability checks**

Observed on 2026-07-07:

```text
protocol matrix: OpenCode/OMP x official Responses, official Chat fallback, Ollama Responses, Ollama Chat, Chat->Responses, Responses->Chat; GLM and K2.7; 20/20 passed
protocol matrix: ZCode/Pi x official Responses, official Chat fallback, Ollama Responses, Ollama Chat, Chat->Responses, Responses->Chat; GLM and K2.7; 20/20 passed
protocol matrix smoke prompt was shortened to avoid GLM spending the small output budget on reasoning summary before assistant text; follow-up OpenCode/OMP GLM Responses rerun passed after two transient GLM Responses failures in a broad rerun
Codex CLI: OpenAI official Responses passthrough passed
Codex CLI: Ollama GLM/K2.7 Responses passthrough passed
Codex CLI: Ollama GLM/K2.7 Chat-only fallback passed
OpenCode real CLI: 10/10 route cases passed
OMP real CLI: 10/10 route cases passed
Pi real CLI: 10/10 route cases passed
ZCode real CLI: not executed because no zcode command-line shim or headless executable is available in PATH or the discovered ZCode/Zed install directories; protocol-level ZCode config E2E passed instead
Tool E2E: Codex CLI + OpenAI and Codex CLI + Ollama GLM shell tool calls passed
Vision Proxy E2E: transparent third-party image request emitted image_proxy_vision_request_complete and image_proxy_applied with vision_proxy_policy=transparent_overlay
Compact E2E: compact request emitted compact_text_only_tools_stripped and completed with behavior_profile=codex_app_external_adapter
Python unittest discovery: 463 tests OK
Rust cargo test: 153 tests OK
```

- [x] **Step 9: Add failing RouteDecision/telemetry tests**

Assert official unknown client route decisions return official gateway compatibility, while provider-scoped and explicit third-party official routes return transparent policies. Assert `request_start` and `request_complete` include policy fields from `RouteDecision`.

- [x] **Step 10: Make RouteDecision authoritative**

Move official unknown-client compatibility into `route_decision_for_request()` and project all route policy fields into request telemetry events.

- [x] **Step 11: Add failing Vision Proxy policy tests**

Cover Codex App third-party text-only image request preserving current vision proxy behavior, transparent third-party image request with vision proxy disabled not applying image proxy, and transparent third-party image request with vision proxy enabled applying only the vision overlay.

- [x] **Step 12: Extract and wire Vision Proxy overlay**

Introduce `VisionProxyPolicy` constants and an `apply_vision_proxy_adapter()` helper boundary. Keep current implementation in `codex_proxy.py` for the first pass, but call it only through the policy. Default transparent policy is disabled; Codex App third-party adapter policy is enabled; transparent policy can be enabled by configuration.

- [x] **Step 13: Run regression gates**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing tests.test_chat_completions_gateway tests.test_proxy_event_logging -q
$env:PYTHONPATH='src-python'; python -m unittest discover -s tests -q
cd src-tauri
cargo test -q
cargo fmt --check
cd ..
git diff --check
git status --short
```

Expected: all tests pass; `git diff --check` has no whitespace errors.

---

## Self-Review

- Spec coverage: official thin passthrough hardening, route decision, same-format transparency, lightweight format fallback, async usage projection, conservative retry, Codex App semantic adapter separation, Vision Proxy overlay, Compact scoping, subagent dependency deferral, and subagent regression gating are each covered.
- Previous official passthrough plan coverage: profile selection, mutation ordering, official compatibility, retry/image/browser/compact bypass, request body processing, header preservation, raw SSE relay, async usage tap, and regression gate are represented in Task 0.
- Placeholder scan: this plan contains no placeholder markers.
- Type consistency: policy names and helper names are consistent across tasks.

---

## Second Review Follow-Up: Fix Transparent Routing Merge Blockers

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_routing.py`
- Test: `tests/test_chat_completions_gateway.py`
- Verify: `docs/superpowers/specs/2026-07-07-third-party-transparent-metered-routing-design.md`

**Interfaces:**
- Produces: explicit third-party client standard third-party model routes enter `third_party_app_transparent_metered`.
- Produces: unknown-client standard third-party model routes remain on the legacy gateway profile and do not emit false transparent policy telemetry.
- Produces: transparent lightweight fallback never retries after downstream SSE headers/body can have been written.
- Produces: transparent lightweight fallback usage remains `async_usage_pending` on `request_complete` and observed usage goes through `usage_observed`.
- Produces: request telemetry distinguishes caller request body from final upstream body after format conversion, model mapping, and Vision Proxy.
- Produces: transparent lightweight fallback streams are converted incrementally instead of buffering the entire upstream stream before emitting converted deltas.

- [x] **Step 1: Add failing coverage for second review blockers**

Added tests for:

```text
unknown standard third-party route decision stays gateway profile
explicit third-party standard Chat route is transparent metered
transparent fallback request_complete records async_usage_pending
transparent fallback stream does not retry after downstream headers
transparent fallback Vision Proxy telemetry separates caller/upstream body hashes
Responses-to-Chat streaming fallback emits Chat deltas before upstream completion
Chat-to-Responses streaming fallback emits Responses deltas before upstream completion
```

- [x] **Step 2: Fix route decision and runtime gate**

Standard non-provider-scoped third-party model routes now require an explicit third-party client identity to enter transparent metered routing. Provider-scoped routes still express third-party provider intent without an extra client header.

- [x] **Step 3: Fix transparent retry semantics**

Transparent metered relay disables deferred stream errors so the gateway retry loop cannot replay after downstream output begins. Upstream-open retry remains governed by `RETRY_CONSERVATIVE_PRE_OUTPUT`.

- [x] **Step 4: Fix usage sidecar behavior**

Transparent same-format and lightweight fallback paths now enqueue usage observation work and leave request completion usage as `async_usage_pending`.

- [x] **Step 5: Fix request observability semantics**

Events now expose caller request body hashes and upstream request body hashes separately. The legacy `request_body_hmac` field represents the final upstream body.

- [x] **Step 6: Fix streaming fallback buffering**

Transparent lightweight streaming fallback now converts Responses SSE to Chat chunks and Chat chunks to Responses SSE incrementally.

- [x] **Step 7: Verify second review follow-up**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing tests.test_chat_completions_gateway tests.test_proxy_event_logging -q
$env:PYTHONPATH='src-python'; python -m unittest discover -s tests -q
cd src-tauri
cargo test -q
cargo fmt --check
cd ..
git diff --check
```

Observed:

```text
focused Python: 304 tests OK
full Python discovery: 451 tests OK
Rust: 153 tests OK
cargo fmt --check: OK
git diff --check: OK, with existing CRLF conversion warnings only
```
