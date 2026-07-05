# Gateway Stream Compact Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CodexHub Gateway treat compact as a text-only request kind and reject incomplete upstream streams instead of synthesizing successful responses.

**Architecture:** Add request-kind detection before request conversion, then apply compact-specific tool stripping and response validation. Add model-agnostic SSE terminal detection helpers and use them in every buffered or converted stream path so the Gateway only emits finish chunks, `[DONE]`, or non-stream completed JSON after an upstream terminal signal.

**Tech Stack:** Python Gateway in `src-python/codex_proxy.py`, Python unit tests in `tests/test_chat_completions_gateway.py` and `tests/test_routing.py`, catalog sync in `src-python/catalog_sync.py`, Tauri/Rust gateway metadata in `src-tauri/src/gateway.rs`.

---

## File Structure

- Modify `src-python/codex_proxy.py`: request-kind detection, compact tool stripping, compact empty-response guard, stream terminal helpers, relay guards, telemetry.
- Modify `tests/test_chat_completions_gateway.py`: pure tests for compact detection, compact tool stripping, Responses SSE completion guards, Chat Completions completion guards.
- Modify `tests/test_routing.py`: relay-level tests for buffered SSE, converted SSE, passthrough SSE, compact empty responses, and header-provided request kind.
- Modify `src-python/catalog_sync.py`: set GPT-5.5 and GPT-5.5-fast context windows to `258400`.
- Modify `src-tauri/src/gateway.rs`: set built-in GPT-5.5 and GPT-5.5-fast context windows to `258400`.
- Modify `tests/test_catalog_sync.py`: update context-window assertions.

---

### Task 1: Add Stream Terminal Helpers

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_chat_completions_gateway.py`

- [ ] **Step 1: Write failing pure helper tests**

Add these imports in `tests/test_chat_completions_gateway.py`:

```python
from codex_proxy import (
    UpstreamStreamIncompleteError,
    _chat_stream_chunks_have_terminal,
    _events_to_responses_body,
    _response_events_to_chat_stream_chunks,
    _responses_events_have_terminal,
)
```

If a symbol is already imported, extend the existing import list instead of duplicating it.

Add these tests to `ResponseEventsToChatStreamTests`:

```python
def test_responses_events_terminal_detection_requires_completed_or_failure(self):
    self.assertFalse(_responses_events_have_terminal([]))
    self.assertFalse(_responses_events_have_terminal([
        {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5.5"}},
        {"type": "response.output_text.delta", "delta": "partial"},
    ]))
    self.assertTrue(_responses_events_have_terminal([
        {"type": "response.completed", "response": {"id": "resp_1", "model": "gpt-5.5", "output": []}},
    ]))
    self.assertTrue(_responses_events_have_terminal([
        {"type": "response.failed", "response": {"id": "resp_1", "model": "gpt-5.5"}},
    ]))


def test_events_to_responses_body_can_require_completed_event(self):
    with self.assertRaises(UpstreamStreamIncompleteError):
        _events_to_responses_body([
            {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5.5"}},
            {"type": "response.output_text.delta", "delta": "partial"},
        ], require_completed=True)


def test_response_events_to_chat_stream_chunks_can_require_completed_event(self):
    with self.assertRaises(UpstreamStreamIncompleteError):
        _response_events_to_chat_stream_chunks([
            {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5.5"}},
            {"type": "response.output_text.delta", "delta": "partial"},
        ], require_completed=True)


def test_chat_stream_chunks_terminal_detection_accepts_done_or_finish_reason(self):
    self.assertFalse(_chat_stream_chunks_have_terminal([]))
    self.assertFalse(_chat_stream_chunks_have_terminal([
        {"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]},
    ]))
    self.assertTrue(_chat_stream_chunks_have_terminal([
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]))
    self.assertTrue(_chat_stream_chunks_have_terminal(["[DONE]"]))
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_chat_completions_gateway.ResponseEventsToChatStreamTests -q
```

Expected: FAIL with import errors for the new helper names.

- [ ] **Step 3: Add helper implementation**

Add this code in `src-python/codex_proxy.py` before `_response_events_to_chat_stream_chunks`:

```python
class UpstreamStreamIncompleteError(RuntimeError):
    """Raised when an upstream stream ends without a terminal event."""


RESPONSES_TERMINAL_EVENT_TYPES = {
    "response.completed",
    "response.failed",
    "response.incomplete",
    "error",
}


def _responses_events_have_terminal(events: list[Mapping[str, Any]]) -> bool:
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type in RESPONSES_TERMINAL_EVENT_TYPES:
            return True
    return False


def _responses_events_have_completed(events: list[Mapping[str, Any]]) -> bool:
    for event in events:
        if isinstance(event, Mapping) and event.get("type") == "response.completed":
            return True
    return False


def _chat_stream_chunk_has_finish(chunk: Mapping[str, Any]) -> bool:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if isinstance(choice, Mapping) and choice.get("finish_reason") is not None:
            return True
    return False


def _chat_stream_chunks_have_terminal(chunks: list[Mapping[str, Any] | str]) -> bool:
    for chunk in chunks:
        if chunk == "[DONE]":
            return True
        if isinstance(chunk, Mapping) and _chat_stream_chunk_has_finish(chunk):
            return True
    return False
```

Change `_response_events_to_chat_stream_chunks` signature and terminal handling:

```python
def _response_events_to_chat_stream_chunks(
    events: list[Mapping[str, Any]],
    *,
    require_completed: bool = False,
) -> list[dict[str, Any]]:
    if require_completed and not _responses_events_have_completed(events):
        raise UpstreamStreamIncompleteError("Responses stream ended before response.completed")
```

Keep the existing body after that guard.

Change `_events_to_responses_body` signature and add the same completed guard at the top:

```python
def _events_to_responses_body(
    events: list[Mapping[str, Any]],
    *,
    require_completed: bool = False,
) -> bytes:
    if require_completed and not _responses_events_have_completed(events):
        raise UpstreamStreamIncompleteError("Responses stream ended before response.completed")
```

Keep the existing reconstruction logic after the guard.

- [ ] **Step 4: Run pure helper tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_chat_completions_gateway.ResponseEventsToChatStreamTests -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src-python/codex_proxy.py tests/test_chat_completions_gateway.py
git commit -m "Add upstream stream terminal helpers"
```

---

### Task 2: Guard Buffered Responses SSE to Non-Stream JSON

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Write failing relay test**

Add `UpstreamStreamIncompleteError` to the `from codex_proxy import (...)` list only if the test file references it directly.

Add this test near the existing `_relay_upstream_response` tests in `tests/test_routing.py`:

```python
def test_buffered_responses_sse_without_completed_returns_502_error(self):
    handler = FakeHandler()
    response = FakeSseResponse([
        b'data: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5.5"}}\n\n',
        b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
        b"",
    ])

    status = CodexProxyHandler._relay_upstream_response(
        handler,
        response,
        "official",
        request_id="req_incomplete_buffer",
        model="openai/gpt-5.5",
        upstream_format="responses",
        inbound_format="responses",
        caller_stream=False,
    )

    self.assertEqual(status, 502)
    self.assertEqual(handler.status, 502)
    payload = json.loads(handler.wfile.writes[0])
    self.assertEqual(payload["error"]["type"], "upstream_stream_incomplete")
    self.assertEqual(payload["error"]["code"], "upstream_stream_incomplete")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing.RoutingTests.test_buffered_responses_sse_without_completed_returns_502_error -q
```

Expected: FAIL because the Gateway currently synthesizes a completed response.

- [ ] **Step 3: Add JSON error helper for incomplete streams**

Add this helper after `_downstream_json_error_body` in `src-python/codex_proxy.py`:

```python
def _incomplete_stream_json_error_body(upstream_name: str) -> bytes:
    return _downstream_json_error_body(
        message="Upstream stream ended before a terminal event.",
        error_type="upstream_stream_incomplete",
        code="upstream_stream_incomplete",
        upstream_name=upstream_name,
    )
```

In `_relay_upstream_response`, inside the `if buffer_sse_to_json:` branch, replace:

```python
body = _events_to_responses_body(events)
```

with:

```python
try:
    body = _events_to_responses_body(events, require_completed=True)
except UpstreamStreamIncompleteError:
    status = 502
    body = _incomplete_stream_json_error_body(upstream_name)
    write_proxy_event(
        "upstream_stream_incomplete",
        request_id=request_id,
        model=model,
        upstream=upstream_name,
        status=status,
        upstream_format=upstream_format,
        inbound_format=inbound_format,
    )
```

Leave `_capture_usage(usage_capture, _usage_from_json_body(body))` after this block.

- [ ] **Step 4: Run buffered relay test**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing.RoutingTests.test_buffered_responses_sse_without_completed_returns_502_error -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src-python/codex_proxy.py tests/test_routing.py
git commit -m "Reject incomplete buffered response streams"
```

---

### Task 3: Guard Responses SSE Converted to Chat Completions Stream

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Write failing conversion test**

Add this test in `tests/test_routing.py`:

```python
def test_responses_sse_to_chat_stream_without_completed_writes_sse_error(self):
    handler = FakeHandler()
    response = FakeSseResponse([
        b'data: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5.5"}}\n\n',
        b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
        b"",
    ])

    status = CodexProxyHandler._relay_upstream_response(
        handler,
        response,
        "official",
        request_id="req_incomplete_chat_convert",
        model="openai/gpt-5.5",
        upstream_format="responses",
        inbound_format="chat_completions",
        caller_stream=True,
    )

    self.assertEqual(status, 502)
    data = b"".join(handler.wfile.writes)
    self.assertIn(b"upstream_stream_incomplete", data)
    self.assertNotIn(b"finish_reason", data)
    self.assertNotIn(b"data: [DONE]", data)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing.RoutingTests.test_responses_sse_to_chat_stream_without_completed_writes_sse_error -q
```

Expected: FAIL because the Gateway currently writes a finish chunk and `[DONE]`.

- [ ] **Step 3: Require completion before writing converted Chat chunks**

In `_relay_upstream_response`, inside the branch:

```python
if want_chat_output and upstream_format != "chat_completions":
```

replace:

```python
for chunk in _response_events_to_chat_stream_chunks(events):
```

with:

```python
try:
    chunks = _response_events_to_chat_stream_chunks(events, require_completed=True)
except UpstreamStreamIncompleteError:
    self.close_connection = True
    write_proxy_event(
        "upstream_stream_incomplete",
        request_id=request_id,
        model=model,
        upstream=upstream_name,
        status=502,
        upstream_format=upstream_format,
        inbound_format=inbound_format,
    )
    self._write_downstream_sse_error(
        inbound_format=inbound_format,
        upstream_name=upstream_name,
        status=502,
        error="upstream_stream_incomplete",
        detail="Upstream stream ended before response.completed.",
    )
    _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
    return 502

for chunk in chunks:
```

- [ ] **Step 4: Run conversion test**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing.RoutingTests.test_responses_sse_to_chat_stream_without_completed_writes_sse_error -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src-python/codex_proxy.py tests/test_routing.py
git commit -m "Guard converted chat streams against incomplete upstreams"
```

---

### Task 4: Guard Chat Completions SSE Terminal Detection

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Write failing Chat SSE test**

Add this test in `tests/test_routing.py`:

```python
def test_chat_sse_without_finish_or_done_writes_sse_error(self):
    handler = FakeHandler()
    response = FakeSseResponse([
        b'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n',
        b"",
    ])

    status = CodexProxyHandler._relay_upstream_response(
        handler,
        response,
        "ollama_cloud",
        request_id="req_incomplete_chat_sse",
        model="ollama-cloud/glm-5.2",
        upstream_format="chat_completions",
        inbound_format="chat_completions",
        caller_stream=True,
    )

    self.assertEqual(status, 502)
    data = b"".join(handler.wfile.writes)
    self.assertIn(b"upstream_stream_incomplete", data)
    self.assertNotIn(b"data: [DONE]", data)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing.RoutingTests.test_chat_sse_without_finish_or_done_writes_sse_error -q
```

Expected: FAIL because the Gateway currently writes `[DONE]` after EOF.

- [ ] **Step 3: Preserve `[DONE]` while collecting Chat SSE chunks**

In the `if upstream_format == "chat_completions":` branch, change:

```python
payload_bytes = _sse_payload_bytes(line)
if payload_bytes is None:
    continue
try:
    payload = json.loads(payload_bytes.decode("utf-8-sig"))
except (UnicodeDecodeError, json.JSONDecodeError):
    continue
if isinstance(payload, dict):
    chunks.append(payload)
    _capture_usage(usage_capture, _usage_from_payload(payload))
```

to:

```python
payload_bytes = _sse_payload_bytes(line)
if payload_bytes is None:
    continue
if payload_bytes == b"[DONE]":
    chunks.append("[DONE]")
    continue
try:
    payload = json.loads(payload_bytes.decode("utf-8-sig"))
except (UnicodeDecodeError, json.JSONDecodeError):
    continue
if isinstance(payload, dict):
    chunks.append(payload)
    _capture_usage(usage_capture, _usage_from_payload(payload))
```

Immediately after the `except` block and before `if want_chat_output:`, add:

```python
if not _chat_stream_chunks_have_terminal(chunks):
    self.close_connection = True
    write_proxy_event(
        "upstream_stream_incomplete",
        request_id=request_id,
        model=model,
        upstream=upstream_name,
        status=502,
        upstream_format=upstream_format,
        inbound_format=inbound_format,
    )
    self._write_downstream_sse_error(
        inbound_format=inbound_format,
        upstream_name=upstream_name,
        status=502,
        error="upstream_stream_incomplete",
        detail="Upstream Chat Completions stream ended without finish_reason or [DONE].",
    )
    _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
    return 502
```

When writing Chat Completions passthrough chunks, replace:

```python
for chunk in chunks:
    self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n\n")
    self.wfile.flush()
```

with:

```python
for chunk in chunks:
    if chunk == "[DONE]":
        continue
    self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n\n")
    self.wfile.flush()
```

Keep the final single `self.wfile.write(b"data: [DONE]\n\n")` so the downstream receives exactly one terminal marker.

- [ ] **Step 4: Run Chat SSE test**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing.RoutingTests.test_chat_sse_without_finish_or_done_writes_sse_error -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src-python/codex_proxy.py tests/test_routing.py
git commit -m "Guard chat streams against missing terminal chunks"
```

---

### Task 5: Guard Responses SSE Passthrough Terminal Detection

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Write failing passthrough test**

Add this test in `tests/test_routing.py`:

```python
def test_responses_sse_passthrough_without_terminal_writes_sse_error(self):
    handler = FakeHandler()
    response = FakeSseResponse([
        b'data: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5.5"}}\n\n',
        b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
        b"",
    ])

    status = CodexProxyHandler._relay_upstream_response(
        handler,
        response,
        "official",
        request_id="req_incomplete_responses_passthrough",
        model="openai/gpt-5.5",
        upstream_format="responses",
        inbound_format="responses",
        caller_stream=True,
    )

    self.assertEqual(status, 502)
    data = b"".join(handler.wfile.writes)
    self.assertIn(b"upstream_stream_incomplete", data)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing.RoutingTests.test_responses_sse_passthrough_without_terminal_writes_sse_error -q
```

Expected: FAIL because EOF currently closes the stream without an explicit downstream error.

- [ ] **Step 3: Track terminal event in Responses SSE passthrough**

In the final Responses SSE branch of `_relay_upstream_response`, before the `try:` loop, add:

```python
saw_terminal_event = False
```

Inside the loop, after `usage_payload = _parse_sse_json_payload(line)`, add:

```python
if isinstance(usage_payload, Mapping) and _responses_events_have_terminal([usage_payload]):
    saw_terminal_event = True
```

After the `except` block and before the `sse_reasoning_summary` event, add:

```python
if status < 400 and not saw_terminal_event:
    self.close_connection = True
    write_proxy_event(
        "upstream_stream_incomplete",
        request_id=request_id,
        model=model,
        upstream=upstream_name,
        status=502,
        upstream_format=upstream_format,
        inbound_format=inbound_format,
    )
    self._write_downstream_sse_error(
        inbound_format=inbound_format,
        upstream_name=upstream_name,
        status=502,
        error="upstream_stream_incomplete",
        detail="Upstream Responses stream ended without a terminal event.",
    )
    _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
    return 502
```

- [ ] **Step 4: Run passthrough test**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing.RoutingTests.test_responses_sse_passthrough_without_terminal_writes_sse_error -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src-python/codex_proxy.py tests/test_routing.py
git commit -m "Guard responses streams against missing terminal events"
```

---

### Task 6: Add Explicit Compact Request Kind Detection

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_chat_completions_gateway.py`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Write failing request-kind tests**

Add this import in `tests/test_chat_completions_gateway.py`:

```python
from codex_proxy import _request_kind_from_headers_and_payload
```

Add this test class:

```python
class RequestKindDetectionTests(unittest.TestCase):
    def test_compact_header_marks_request_kind_without_prompt_heuristic(self):
        payload = {"model": "gpt-5.5", "input": "summarize"}

        request_kind = _request_kind_from_headers_and_payload(
            {"x-query-source": "compact"},
            payload,
            "responses",
        )

        self.assertEqual(request_kind, "compact")

    def test_compact_prompt_heuristic_marks_request_kind(self):
        payload = {
            "model": "glm-5.2",
            "messages": [{
                "role": "user",
                "content": (
                    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
                    "Your task is to create a detailed summary of the conversation so far.\n"
                    "Return an <analysis> block followed by a <summary> block."
                ),
            }],
        }

        request_kind = _request_kind_from_headers_and_payload({}, payload, "chat_completions")

        self.assertEqual(request_kind, "compact")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_chat_completions_gateway.RequestKindDetectionTests -q
```

Expected: FAIL because `_request_kind_from_headers_and_payload` does not exist.

- [ ] **Step 3: Implement request-kind detection**

Add this helper after `_is_compact_summary_payload` in `src-python/codex_proxy.py`:

```python
def _request_kind_from_headers_and_payload(
    headers: Mapping[str, str] | Any,
    payload: Mapping[str, Any] | None,
    inbound_format: str,
) -> str:
    for header_name in ("x-request-kind", "x-query-source"):
        header_value = _get_header(headers, header_name)
        if isinstance(header_value, str) and header_value.strip().lower() == RETRY_REQUEST_COMPACT:
            return RETRY_REQUEST_COMPACT
    if isinstance(payload, Mapping) and _is_compact_summary_payload(payload, inbound_format):
        return RETRY_REQUEST_COMPACT
    return RETRY_REQUEST_MAIN_GENERATION
```

In `_proxy_post_request`, replace the inline compact detection:

```python
if isinstance(inbound_payload, dict) and _is_compact_summary_payload(inbound_payload, inbound_format):
    request_kind = RETRY_REQUEST_COMPACT
```

with:

```python
request_kind = _request_kind_from_headers_and_payload(self.headers, inbound_payload, inbound_format)
if request_kind == RETRY_REQUEST_COMPACT:
```

Keep the existing `proxy_request_context = _event_context_with_request_kind(...)` update inside the compact branch.

- [ ] **Step 4: Run request-kind tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_chat_completions_gateway.RequestKindDetectionTests -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src-python/codex_proxy.py tests/test_chat_completions_gateway.py
git commit -m "Detect compact request kind explicitly"
```

---

### Task 7: Strip Tools and Disable Tool Injection for Compact

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_chat_completions_gateway.py`

- [ ] **Step 1: Write failing compact tool stripping test**

Add this test to `ChatRequestToResponsesTests`:

```python
def test_compact_prompt_detection_strips_tools_before_conversion(self):
    payload = {
        "model": "glm-5.2",
        "messages": [
            {"role": "assistant", "content": "previous work"},
            {
                "role": "user",
                "content": (
                    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
                    "Your task is to create a detailed summary of the conversation so far.\n"
                    "Your response must include an <analysis> block followed by a <summary> block."
                ),
            },
        ],
        "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {"type": "object"}}}],
        "tool_choice": "auto",
    }

    self.assertTrue(_is_compact_summary_payload(payload, "chat_completions"))
    self.assertTrue(_strip_tools_for_compact_payload(payload))
    self.assertNotIn("tools", payload)
    self.assertNotIn("tool_choice", payload)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_chat_completions_gateway.ChatRequestToResponsesTests.test_compact_prompt_detection_strips_tools_before_conversion -q
```

Expected: FAIL if `_strip_tools_for_compact_payload` is missing or does not remove `tool_choice`.

- [ ] **Step 3: Implement compact tool stripping and disable injection**

Add this helper after `_request_kind_from_headers_and_payload`:

```python
def _strip_tools_for_compact_payload(
    payload: dict[str, Any],
    *,
    event_context: Mapping[str, Any] | None = None,
    upstream_name: str | None = None,
) -> bool:
    removed_tools = payload.pop("tools", None)
    removed_tool_choice = payload.pop("tool_choice", None)
    if removed_tools is None and removed_tool_choice is None:
        return False

    removed_tool_count = len(removed_tools) if isinstance(removed_tools, list) else 0
    _write_adapter_event(
        event_context,
        "compact_text_only_tools_stripped",
        upstream=upstream_name,
        model=payload.get("model") if isinstance(payload.get("model"), str) else None,
        removed_tool_count=removed_tool_count,
        removed_tool_choice=removed_tool_choice if isinstance(removed_tool_choice, str) else None,
    )
    return True
```

In `_proxy_post_request`, when calling `compatible_request_body`, use:

```python
body = compatible_request_body(
    body,
    upstream,
    model_id=model,
    event_context=adapter_event_context,
    inject_codex_tools=request_kind != RETRY_REQUEST_COMPACT,
)
```

Verify `compatible_request_body` already accepts `inject_codex_tools`; if it does not, add the parameter:

```python
def compatible_request_body(
    body: bytes,
    upstream: Mapping[str, Any],
    model_id: str | None = None,
    event_context: Mapping[str, Any] | None = None,
    inject_codex_tools: bool = True,
) -> bytes:
```

Wrap the explicit Codex tool injection block with `inject_codex_tools`:

```python
if inject_codex_tools:
    tool_names_before = _function_tool_names(payload.get("tools"))
    if _inject_explicit_codex_tools(
        payload,
        include_tool_search=include_tool_search,
        include_multi_agent_tools=not lifecycle_complete,
        include_spawn_agent=include_spawn_agent,
        include_wait_agent=include_wait_agent,
        include_close_agent=include_close_agent,
        include_node_repl_tools=not node_repl_single_step_complete,
        open_agent_ids=open_agent_ids,
        wait_agent_ids=wait_agent_ids,
        close_agent_ids=close_agent_ids,
    ):
        added_tool_names = sorted(_function_tool_names(payload.get("tools")) - tool_names_before)
        _write_adapter_event(
            event_context,
            "explicit_codex_tools_injected",
            upstream=upstream_name,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
            added_tool_count=len(added_tool_names),
            added_tool_names=added_tool_names,
        )
        changed = True
```

- [ ] **Step 4: Run compact stripping test**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_chat_completions_gateway.ChatRequestToResponsesTests.test_compact_prompt_detection_strips_tools_before_conversion -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src-python/codex_proxy.py tests/test_chat_completions_gateway.py
git commit -m "Strip tools from compact summary requests"
```

---

### Task 8: Reject Empty Compact Responses

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Write failing compact empty response relay test**

Add this test in `tests/test_routing.py`:

```python
def test_compact_non_sse_empty_chat_response_becomes_retryable_error(self):
    handler = FakeHandler()
    body = json.dumps({
        "id": "resp_empty",
        "object": "response",
        "status": "completed",
        "model": "glm-5.2",
        "output": [],
    }).encode("utf-8")
    response = FakeResponse(body)

    status = CodexProxyHandler._relay_upstream_response(
        handler,
        response,
        "ollama_cloud",
        upstream_format="responses",
        inbound_format="chat_completions",
        caller_stream=False,
        request_kind=RETRY_REQUEST_COMPACT,
    )

    self.assertEqual(status, 502)
    self.assertEqual(handler.status, 502)
    payload = json.loads(handler.wfile.writes[0])
    self.assertEqual(payload["error"]["type"], "compact_empty_response")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing.RoutingTests.test_compact_non_sse_empty_chat_response_becomes_retryable_error -q
```

Expected: FAIL because empty compact responses are currently treated as successful responses.

- [ ] **Step 3: Implement compact empty-response guard**

Add these helpers after `_strip_tools_for_compact_payload`:

```python
def _chat_completion_body_is_empty(body: bytes) -> bool:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or "error" in payload:
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return True
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        message = choice.get("message")
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return False
        if not isinstance(content, str) and _chat_content_text(content).strip():
            return False
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            return False
    return True


def _responses_body_is_empty(body: bytes) -> bool:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or "error" in payload:
        return False
    output = payload.get("output")
    if not isinstance(output, list) or not output:
        return True
    for item in output:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "function_call":
            return False
        if item.get("type") != "message":
            continue
        if _chat_content_text(item.get("content")).strip():
            return False
    return True


def _compact_response_body_is_empty(body: bytes, inbound_format: str) -> bool:
    if inbound_format == "chat_completions":
        return _chat_completion_body_is_empty(body)
    return _responses_body_is_empty(body)
```

In `_relay_upstream_response`, after response format conversion and before headers are sent, add:

```python
if status < 400 and request_kind == RETRY_REQUEST_COMPACT and _compact_response_body_is_empty(body, inbound_format):
    status = 502
    body = _downstream_json_error_body(
        message="Upstream returned an empty compact summary.",
        error_type="compact_empty_response",
        code="compact_empty_response",
        upstream_name=upstream_name,
    )
    event_fields = dict(event_context) if event_context else {}
    event_fields.pop("request_id", None)
    event_fields.pop("model", None)
    event_fields.pop("upstream", None)
    write_proxy_event(
        "compact_empty_response",
        request_id=request_id,
        model=model,
        upstream=upstream_name,
        status=status,
        upstream_format=upstream_format,
        inbound_format=inbound_format,
        **event_fields,
    )
    _capture_usage(usage_capture, None, missing_reason="compact_empty_response")
```

- [ ] **Step 4: Run compact empty-response test**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing.RoutingTests.test_compact_non_sse_empty_chat_response_becomes_retryable_error -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src-python/codex_proxy.py tests/test_routing.py
git commit -m "Reject empty compact summary responses"
```

---

### Task 9: Add Empty Assistant Telemetry for Non-Compact Responses

**Files:**
- Modify: `src-python/codex_proxy.py`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Write failing telemetry test**

Add this test in `tests/test_routing.py`:

```python
def test_non_compact_empty_assistant_response_logs_telemetry_but_stays_successful(self):
    handler = FakeHandler()
    body = json.dumps({
        "id": "resp_empty",
        "object": "response",
        "status": "completed",
        "model": "gpt-5.5",
        "output": [],
    }).encode("utf-8")
    response = FakeResponse(body)

    with patch("codex_proxy.write_proxy_event") as write_event:
        status = CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "official",
            request_id="req_empty_non_compact",
            model="openai/gpt-5.5",
            upstream_format="responses",
            inbound_format="responses",
            caller_stream=False,
            request_kind="main_generation",
        )

    self.assertEqual(status, 200)
    self.assertEqual(handler.status, 200)
    event_names = [call.args[0] for call in write_event.call_args_list]
    self.assertIn("empty_assistant_response", event_names)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing.RoutingTests.test_non_compact_empty_assistant_response_logs_telemetry_but_stays_successful -q
```

Expected: FAIL because non-compact empty assistant responses are not logged separately.

- [ ] **Step 3: Add non-compact empty telemetry**

After the compact empty-response guard in `_relay_upstream_response`, add:

```python
elif status < 400 and request_kind != RETRY_REQUEST_COMPACT and _responses_body_is_empty(body):
    event_fields = dict(event_context) if event_context else {}
    event_fields.pop("request_id", None)
    event_fields.pop("model", None)
    event_fields.pop("upstream", None)
    write_proxy_event(
        "empty_assistant_response",
        request_id=request_id,
        model=model,
        upstream=upstream_name,
        status=status,
        upstream_format=upstream_format,
        inbound_format=inbound_format,
        **event_fields,
    )
```

Use `_chat_completion_body_is_empty(body)` instead when `inbound_format == "chat_completions"`:

```python
empty_non_compact = (
    _chat_completion_body_is_empty(body)
    if inbound_format == "chat_completions"
    else _responses_body_is_empty(body)
)
elif status < 400 and request_kind != RETRY_REQUEST_COMPACT and empty_non_compact:
```

- [ ] **Step 4: Run telemetry test**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing.RoutingTests.test_non_compact_empty_assistant_response_logs_telemetry_but_stays_successful -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src-python/codex_proxy.py tests/test_routing.py
git commit -m "Log empty non-compact assistant responses"
```

---

### Task 10: Align GPT-5.5 Context Window Metadata

**Files:**
- Modify: `src-python/catalog_sync.py`
- Modify: `src-tauri/src/gateway.rs`
- Test: `tests/test_catalog_sync.py`
- Test: `src-tauri/src/gateway.rs`

- [ ] **Step 1: Write failing catalog assertion**

In `tests/test_catalog_sync.py`, change the GPT-5.5 assertions to:

```python
self.assertEqual(by_slug["openai/gpt-5.5"]["context_window"], 258400)
self.assertEqual(by_slug["openai/gpt-5.5"]["max_context_window"], 258400)
```

Also add this assertion for the fast variant if the test does not already cover it:

```python
self.assertEqual(by_slug["openai/gpt-5.5-fast"]["context_window"], 258400)
self.assertEqual(by_slug["openai/gpt-5.5-fast"]["max_context_window"], 258400)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_catalog_sync.CatalogSyncTests -q
```

Expected: FAIL while `catalog_sync.py` still reports `272000`.

- [ ] **Step 3: Change Python catalog defaults**

In `src-python/catalog_sync.py`, set both GPT-5.5 defaults to `258400`:

```python
OFFICIAL_MODEL_DEFAULTS: dict[str, dict[str, Any]] = {
    "gpt-5.5": {
        "context_window": 258400,
        "max_context_window": 258400,
        "additional_speed_tiers": ["fast"],
        "service_tiers": OFFICIAL_FAST_SERVICE_TIERS,
        "default_reasoning_level": "medium",
    },
    "gpt-5.5-fast": {
        "context_window": 258400,
        "max_context_window": 258400,
        "default_reasoning_level": "medium",
    },
```

- [ ] **Step 4: Change Tauri built-in model metadata**

In `src-tauri/src/gateway.rs`, change:

```rust
("openai/gpt-5.5", "OpenAI GPT-5.5", 272000),
```

to:

```rust
("openai/gpt-5.5", "OpenAI GPT-5.5", 258400),
```

And change the GPT-5.5 fast tuple from:

```rust
272000,
```

to:

```rust
258400,
```

- [ ] **Step 5: Run catalog and Rust tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_catalog_sync.CatalogSyncTests -q
cd src-tauri
cargo test -q
cd ..
```

Expected: PASS for Python catalog tests and Rust tests.

- [ ] **Step 6: Commit**

```powershell
git add src-python/catalog_sync.py src-tauri/src/gateway.rs tests/test_catalog_sync.py
git commit -m "Align GPT-5.5 context metadata"
```

---

### Task 11: Run Full Verification

**Files:**
- Verify: `src-python/codex_proxy.py`
- Verify: `src-python/catalog_sync.py`
- Verify: `src-tauri/src/gateway.rs`
- Verify: `tests/test_chat_completions_gateway.py`
- Verify: `tests/test_routing.py`
- Verify: `tests/test_catalog_sync.py`

- [ ] **Step 1: Run focused Gateway tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_chat_completions_gateway tests.test_routing tests.test_catalog_sync -q
```

Expected: PASS.

- [ ] **Step 2: Run full Python test suite**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest discover -s tests -q
```

Expected: PASS.

- [ ] **Step 3: Run Rust test suite**

Run:

```powershell
cd src-tauri
cargo test -q
cd ..
```

Expected: PASS.

- [ ] **Step 4: Inspect changed files**

Run:

```powershell
git diff --check
git status --short
```

Expected: `git diff --check` exits 0. `git status --short` shows only the intended Gateway, tests, catalog, Tauri, and docs files plus any unrelated pre-existing user changes.

- [ ] **Step 5: Commit verification updates**

If Task 11 required no file changes, do not create a commit. If a verification-only fix was necessary, commit it:

```powershell
git add src-python/codex_proxy.py tests/test_chat_completions_gateway.py tests/test_routing.py tests/test_catalog_sync.py src-python/catalog_sync.py src-tauri/src/gateway.rs
git commit -m "Verify stream and compact hardening"
```

---

### Task 12: Deploy to the Active Runtime Gateway

**Files:**
- Source runtime: `D:\Workstation\CodexHub\src-python\codex_proxy.py`
- Current worktree: `C:\Users\noirb\.codex\worktrees\2d78\CodexHub`
- Runtime process: `python.exe D:\Workstation\CodexHub\src-python\codex_proxy.py --port 9099`

- [ ] **Step 1: Confirm active runtime process**

Run:

```powershell
Get-CimInstance Win32_Process -Filter "CommandLine LIKE '%codex_proxy.py --port 9099%'" |
  Select-Object ProcessId,ExecutablePath,CommandLine |
  Format-List
```

Expected: one active Gateway process using `D:\Workstation\CodexHub\src-python\codex_proxy.py --port 9099`.

- [ ] **Step 2: Apply the committed changes to `D:\Workstation\CodexHub`**

Use the team's normal merge/cherry-pick flow. If this worktree branch has commits, run from the runtime repo:

```powershell
git -C D:\Workstation\CodexHub fetch C:\Users\noirb\.codex\worktrees\2d78\CodexHub HEAD
git -C D:\Workstation\CodexHub cherry-pick FETCH_HEAD
```

Expected: the runtime repo contains the same Gateway hardening commits. If multiple commits were created, cherry-pick the commit range in order.

- [ ] **Step 3: Restart Gateway**

Run:

```powershell
$proc = Get-CimInstance Win32_Process -Filter "CommandLine LIKE '%codex_proxy.py --port 9099%'" | Select-Object -First 1
if ($proc) { Stop-Process -Id $proc.ProcessId -Force }
Start-Process -WindowStyle Hidden -FilePath "C:\Users\noirb\AppData\Local\Programs\Python\Python313\python.exe" -ArgumentList "D:\Workstation\CodexHub\src-python\codex_proxy.py --port 9099"
```

Expected: a new hidden Python process is listening on port `9099`.

- [ ] **Step 4: Verify runtime health**

Run:

```powershell
Get-CimInstance Win32_Process -Filter "CommandLine LIKE '%codex_proxy.py --port 9099%'" |
  Select-Object ProcessId,ExecutablePath,CommandLine |
  Format-List
Get-Content -LiteralPath "C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl" -Tail 5
```

Expected: active Gateway process exists and the event log remains writable.

- [ ] **Step 5: Commit deployment note**

If deployment updates a tracked runtime repo, commit there according to that repo's branch policy. If deployment only restarts an already updated runtime, no commit is needed.

---

## Self-Review

- Spec coverage: compact request kind, tool stripping, empty compact rejection, model-agnostic SSE completion guards, GPT-5.5 context metadata, runtime deployment, and full verification are covered.
- Placeholder scan: the plan contains no placeholder markers and no unspecified test steps.
- Type consistency: helper names are consistent across tasks: `_responses_events_have_terminal`, `_responses_events_have_completed`, `_chat_stream_chunks_have_terminal`, `UpstreamStreamIncompleteError`, `_request_kind_from_headers_and_payload`, `_compact_response_body_is_empty`.
