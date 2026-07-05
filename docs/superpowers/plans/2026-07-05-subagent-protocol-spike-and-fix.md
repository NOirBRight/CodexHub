# Subagent Protocol Spike And Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Codex native subagent coordination reliable for external providers by preserving structured tool history when the upstream supports it, and by adding a Gateway-owned subagent state machine for compatibility paths.

**Architecture:** Split the Gateway adapter into protocol modes instead of treating every non-official provider as text compatibility. Responses-capable tool endpoints keep `function_call` / `function_call_output` structure with only function-name flattening and no compatibility guidance injection. Chat Completions endpoints use OpenAI-style `assistant.tool_calls` / `role: tool` history only after a live probe proves it works; Ollama GLM-5.2 currently needs a state-summary compatibility path instead. Compatibility paths use a Gateway-owned event-led coordinator that computes subagent state from `agent_id`, `call_id`, spawn prompt, nickname, inferred role/task, implementation epoch, requested count, and explicit append intent. It exposes useful next tools and suppresses duplicate lifecycle restarts without hard-coding a single workflow script.

**Tech Stack:** Python 3.13 stdlib `unittest`, CodexHub Python Gateway, Rust/Tauri provider config, React/TypeScript provider UI.

---

## Scope

This plan intentionally separates three provider classes:

- `responses_structured`: upstream accepts `/v1/responses` with structured function calls and function-call outputs.
- `chat_tools`: upstream only accepts `/v1/chat/completions`, and a live probe proves it supports multi-turn `assistant.tool_calls` plus `role: tool` history.
- `text_compat`: upstream cannot be trusted with structured tool transcript; Gateway must expose a tightly bounded tool set and inject state guidance.

For Ollama GLM-5.2, `chat_tools` is not considered proven: flat tool names work for single-turn tool selection, but OpenAI-style Chat tool history did not survive multi-turn subagent state replay. Route it through the compatibility coordinator until a future probe proves otherwise.

Native subagent support must be disabled for providers with no structured tool-call support and no compatibility tool-call path. Text-only models can answer normally, but must not be presented as Codex native subagent-capable.

## Inline Spike Findings

These findings were produced in `diagnostics/subagent-e2e/` on 2026-07-05 and should guide implementation order:

- `tests.test_routing.RoutingTests.test_responses_structured_provider_preserves_multi_agent_tool_history` fails today because `compatible_request_body()` still rewrites structured `function_call` history into text messages even when the provider is intended to be `responses_structured`.
- `tests.test_chat_completions_gateway.ChatRequestToResponsesTests.test_responses_to_chat_preserves_function_call_history` fails today because `_responses_input_to_chat_messages()` drops non-message Responses items.
- Direct Ollama Chat endpoint probes show flat `multi_agent_v1__spawn_agent`, `multi_agent_v1__wait_agent`, and `multi_agent_v1__close_agent` tool names work for single-turn selection. OpenAI-style `assistant.tool_calls` plus `role: tool` history fails or causes wrong next-tool selection. Text state summaries plus a single visible next tool work with `tool_choice = auto`.
- A follow-up Ollama Chat probe validated bounded multi-spawn behavior with state summaries: when `requested_count=2` and `spawned_count=1`, GLM-5.2 selected `multi_agent_v1__spawn_agent` with `{"message":"return B","nickname":"b"}`; when `requested_count=2` and `spawned_count=2`, it selected `multi_agent_v1__wait_agent` for both ids. This confirms the coordinator design can support "spawn one, then spawn the second" if the Gateway state parser computes the right next action and tool visibility.
- `superpowers:subagent-driven-development` is not a single `spawn -> wait -> close -> final` lifecycle. It intentionally creates fresh implementer, spec reviewer, code-quality reviewer, final reviewer, and sometimes sends follow-up work back to the implementer. The Gateway state machine must therefore be workflow-aware: it should block duplicate same-role/same-task spawn attempts, but allow distinct role/task spawns in the same plan execution.
- Spike `.planning/spikes/001-subagent-coordinator-policy` validated a stronger formulation: do not make this a rigid `workflow_kind = subagent-driven-development` script. Use an event-led coordinator with `(role, task_key, spawn_signature, implementation_epoch)`. A duplicate implementer spawn in the same epoch is blocked, but a same-prompt spec re-review after the implementer fixes issues is allowed because the fix advances the implementation epoch. The current global lifecycle policy incorrectly chooses `close` after implementer/reviewer waits, and a rigid "block any repeated signature forever" policy incorrectly blocks re-review.
- Ollama endpoint probes using event-led state summaries covered `glm-5.2`, `minimax-m3`, and `kimi-k2.7-code` across both `/chat/completions` and `/responses`. GLM 5.2 and MiniMax M3 passed 12/12 per endpoint with a 128-token output budget. Kimi K2.7 Code was flaky at 128 tokens (10/12 per endpoint with `tool_choice=auto`, forced `tool_choice` still only 3/9 Chat and 6/9 Responses on high-risk cases), but passed 18/18 per endpoint at a 512-token output budget with `tool_choice=auto`. Implementation must therefore tune subagent coordinator output budgets per model/provider; hiding invalid tools is necessary but not sufficient.
- Real `codex exec -m glm-5.2` through the Responses route can truly spawn, wait, and close native subagents. It then repeats the whole lifecycle instead of producing the final answer; one run reached 12 completed `spawn_agent -> wait -> close_agent` cycles before timeout.
- Existing lifecycle guard unit tests pass, but real proxy events show `lifecycle_complete = false` across repeated cycles. The missing behavior is requested-count / append-intent tracking across the whole user request, not merely per-agent open/closed tracking.
- Current `/v1/models` returns `models[].slug`, not `data[].id`; E2E scripts must query `models` and use slug `glm-5.2` for the Ollama direct model.


## File Structure

- Modify `src-python/codex_proxy.py`
  - Split external request adaptation into lightweight normalization, structured tool transcript preservation, chat-tool conversion support, and text compatibility.
  - Continue normalizing third-party flat tool-call aliases back into Codex namespace calls on model output.
  - Add response-side duplicate `spawn_agent` guard for compatibility paths.

- Create `src-python/subagent_state.py`
  - Parse input transcript into a deterministic subagent lifecycle state.
  - Track child `call_id`, `agent_id`, `nickname`, spawn prompt, inferred role/task, wait status, close status, implementation epoch, requested count, and append intent.
  - Produce tool visibility decisions and compact state hints.

- Modify `src-python/providers_config.py`
  - Persist provider `tool_protocol`.
  - Surface it through `build_external_model_index()` so Gateway routing can choose the correct adapter behavior.

- Modify `src-python/probe_upstream_format.py`
  - Return `recommended_tool_protocol` based on `responses_tool_ok`, `responses_tool_stream_ok`, `chat_tool_ok`, and `chat_tool_stream_ok`.

- Modify `src-tauri/src/main.rs`, `src-tauri/src/config.rs`, `src-tauri/src/gateway.rs`, `src-tauri/src/models.rs`
  - Carry `tool_protocol` through provider config, frontend bridge, runtime status, and endpoint probe results.

- Modify `frontend/src/lib/types.ts` and `frontend/src/pages/ProvidersPage.tsx`
  - Add `ToolProtocol` type.
  - Store the probe-recommended protocol when a provider endpoint is tested.
  - Display a compact read-only capability marker next to endpoint selection.

- Modify tests:
  - `tests/test_routing.py`
  - `tests/test_chat_completions_gateway.py`
  - `tests/test_providers_config.py`
  - `tests/test_probe_upstream_format.py`
  - `src-tauri/src/config.rs` test module
  - `src-tauri/src/gateway.rs` test module
  - `src-tauri/src/models.rs` test module

---

### Task 1: Spike Current Protocol Loss With Failing Tests

**Files:**
- Modify: `tests/test_routing.py`
- Modify: `tests/test_chat_completions_gateway.py`

- [ ] **Step 1: Add failing test for Responses structured providers preserving tool outputs**

Add this test near the existing multi-agent adapter tests in `tests/test_routing.py`:

```python
    def test_responses_structured_provider_preserves_multi_agent_tool_history(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Use one subagent."},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-ok"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child", "nickname": "child"}),
                    },
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
            },
            event_context={"request_id": "req"},
        )
        payload = json.loads(transformed)
        transcript = json.dumps(payload, ensure_ascii=True)

        self.assertEqual(payload["input"][1]["type"], "function_call")
        self.assertEqual(payload["input"][1]["name"], "multi_agent_v1__spawn_agent")
        self.assertNotIn("namespace", payload["input"][1])
        self.assertEqual(payload["input"][2]["type"], "function_call_output")
        self.assertEqual(payload["input"][2]["call_id"], "call_spawn")
        self.assertIn('"agent_id": "019f-child"', payload["input"][2]["output"])
        self.assertNotIn("Codex native multi_agent_v1.spawn_agent result", transcript)
```

- [ ] **Step 2: Run the failing routing test**

Run:

```powershell
python -m unittest tests.test_routing.RoutingTests.test_responses_structured_provider_preserves_multi_agent_tool_history -v
```

Expected: FAIL because current `compatible_request_body()` rewrites the tool result into a message containing `Codex native multi_agent_v1.spawn_agent result`.

- [ ] **Step 3: Add failing test for Responses-to-Chat tool history conversion**

Add this test in `tests/test_chat_completions_gateway.py` next to `test_responses_to_chat_stream_requests_include_usage`:

```python
    def test_responses_to_chat_preserves_function_call_history(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Use a child agent."},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return child-ok"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child"}),
                    },
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "multi_agent_v1__spawn_agent",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        ).encode("utf-8")

        payload = json.loads(_responses_request_to_chat_completion_body(body))

        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(payload["messages"][1]["role"], "assistant")
        self.assertEqual(payload["messages"][1]["tool_calls"][0]["id"], "call_spawn")
        self.assertEqual(payload["messages"][1]["tool_calls"][0]["function"]["name"], "multi_agent_v1__spawn_agent")
        self.assertEqual(payload["messages"][2]["role"], "tool")
        self.assertEqual(payload["messages"][2]["tool_call_id"], "call_spawn")
        self.assertIn("019f-child", payload["messages"][2]["content"])
```

- [ ] **Step 4: Run the failing chat conversion test**

Run:

```powershell
python -m unittest tests.test_chat_completions_gateway.ChatRequestToResponsesTests.test_responses_to_chat_preserves_function_call_history -v
```

Expected: FAIL because `_responses_input_to_chat_messages()` currently drops non-message Responses input items.

- [ ] **Step 5: Commit the spike tests**

Run:

```powershell
git add tests/test_routing.py tests/test_chat_completions_gateway.py
git commit -m "test: capture external subagent protocol gaps"
```

---

### Task 1A: Real Chat Flattening E2E Spike

**Files:**
- Create: `diagnostics/subagent-e2e/`
- No committed source changes; diagnostics output stays untracked.

- [ ] **Step 1: Create diagnostics directory**

Run:

```powershell
New-Item -ItemType Directory -Force 'diagnostics/subagent-e2e' | Out-Null
```

Expected: `diagnostics/subagent-e2e` exists.

- [ ] **Step 2: Verify a real Chat Completions provider is available through Gateway**

Run:

```powershell
$models = (Invoke-RestMethod -Uri 'http://127.0.0.1:9099/v1/models' -TimeoutSec 10).data
$models |
  Where-Object { $_.id -eq 'ollama-cloud/glm-5.2' } |
  Select-Object id, name, provider, model |
  Format-Table -AutoSize
```

Expected: `ollama-cloud/glm-5.2` is listed. Use the same Ollama provider for both this Chat endpoint spike and the Responses E2E so the test isolates protocol behavior instead of provider behavior.

- [ ] **Step 3: Prove the Chat endpoint accepts a flat `spawn_agent` function name**

Run:

```powershell
$provider = 'ollama-cloud'
$model = 'glm-5.2'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$uri = "http://127.0.0.1:9099/v1/providers/$provider/chat/completions"
$spawnOut = "diagnostics/subagent-e2e/chat-flat-spawn-$stamp.json"

$spawnPayload = @{
  model = $model
  messages = @(
    @{ role = 'user'; content = 'Call the spawn tool with message exactly: return flat spawn ok' }
  )
  tools = @(
    @{
      type = 'function'
      function = @{
        name = 'multi_agent_v1__spawn_agent'
        description = 'Spawn a Codex child agent.'
        parameters = @{
          type = 'object'
          properties = @{
            message = @{ type = 'string' }
            nickname = @{ type = 'string' }
          }
          required = @('message')
          additionalProperties = $false
        }
      }
    }
  )
  tool_choice = @{ type = 'function'; function = @{ name = 'multi_agent_v1__spawn_agent' } }
  max_tokens = 128
  stream = $false
}

$spawnJson = $spawnPayload | ConvertTo-Json -Depth 40
$spawnResp = Invoke-RestMethod -Uri $uri -Method Post -ContentType 'application/json' -Body $spawnJson -TimeoutSec 120
$spawnResp | ConvertTo-Json -Depth 60 | Set-Content -Encoding UTF8 $spawnOut

$spawnCall = $spawnResp.choices[0].message.tool_calls[0]
if ($spawnCall.function.name -ne 'multi_agent_v1__spawn_agent') {
  throw "Expected flat spawn tool name, got: $($spawnCall.function.name)"
}
$spawnArgs = $spawnCall.function.arguments | ConvertFrom-Json
if ($spawnArgs.message -notmatch 'flat spawn ok') {
  throw "Expected spawn arguments to contain flat spawn ok, got: $($spawnCall.function.arguments)"
}
"CHAT_FLAT_SPAWN_OK call_id=$($spawnCall.id)"
```

Expected: command prints `CHAT_FLAT_SPAWN_OK` and the saved JSON contains a Chat Completions `tool_calls[0].function.name` equal to `multi_agent_v1__spawn_agent`. This proves the real Ollama Chat endpoint and `glm-5.2` model accept the flattened function name shape.

- [ ] **Step 4: Prove multi-turn Chat tool history can carry flat subagent aliases**

Run:

```powershell
$waitOut = "diagnostics/subagent-e2e/chat-flat-wait-$stamp.json"

$waitPayload = @{
  model = $model
  messages = @(
    @{ role = 'user'; content = 'Call the spawn tool with message exactly: return flat spawn ok' },
    @{
      role = 'assistant'
      content = $null
      tool_calls = @($spawnCall)
    },
    @{
      role = 'tool'
      tool_call_id = $spawnCall.id
      content = '{"agent_id":"agent-flat-e2e","nickname":"flat"}'
    },
    @{ role = 'user'; content = 'Now call wait for agent-flat-e2e.' }
  )
  tools = @(
    @{
      type = 'function'
      function = @{
        name = 'multi_agent_v1__wait_agent'
        description = 'Wait for Codex child agents.'
        parameters = @{
          type = 'object'
          properties = @{
            targets = @{ type = 'array'; items = @{ type = 'string' } }
            timeout_ms = @{ type = 'integer' }
          }
          required = @('targets')
          additionalProperties = $false
        }
      }
    }
  )
  tool_choice = @{ type = 'function'; function = @{ name = 'multi_agent_v1__wait_agent' } }
  max_tokens = 128
  stream = $false
}

$waitJson = $waitPayload | ConvertTo-Json -Depth 60
$waitResp = Invoke-RestMethod -Uri $uri -Method Post -ContentType 'application/json' -Body $waitJson -TimeoutSec 120
$waitResp | ConvertTo-Json -Depth 60 | Set-Content -Encoding UTF8 $waitOut

$waitCall = $waitResp.choices[0].message.tool_calls[0]
if ($waitCall.function.name -ne 'multi_agent_v1__wait_agent') {
  throw "Expected flat wait tool name, got: $($waitCall.function.name)"
}
$waitArgs = $waitCall.function.arguments | ConvertFrom-Json
if (($waitArgs.targets -join ',') -notmatch 'agent-flat-e2e') {
  throw "Expected wait targets to contain agent-flat-e2e, got: $($waitCall.function.arguments)"
}
"CHAT_FLAT_WAIT_OK call_id=$($waitCall.id)"
```

Expected: command prints `CHAT_FLAT_WAIT_OK`. If Step 3 passes but this step fails, the flat-name route is viable for single tool selection but the current Gateway or provider does not preserve multi-turn Chat tool history correctly; keep the saved JSON as spike evidence and make Task 5 fix this command.

- [ ] **Step 5: Prove close can also flow through the same flat Chat history**

Run:

```powershell
$closeOut = "diagnostics/subagent-e2e/chat-flat-close-$stamp.json"

$closePayload = @{
  model = $model
  messages = @(
    @{ role = 'user'; content = 'Call the spawn tool with message exactly: return flat spawn ok' },
    @{ role = 'assistant'; content = $null; tool_calls = @($spawnCall) },
    @{ role = 'tool'; tool_call_id = $spawnCall.id; content = '{"agent_id":"agent-flat-e2e","nickname":"flat"}' },
    @{ role = 'user'; content = 'Now call wait for agent-flat-e2e.' },
    @{ role = 'assistant'; content = $null; tool_calls = @($waitCall) },
    @{ role = 'tool'; tool_call_id = $waitCall.id; content = '{"timed_out":false,"status":{"agent-flat-e2e":{"completed":"flat wait ok"}}}' },
    @{ role = 'user'; content = 'Now close agent-flat-e2e.' }
  )
  tools = @(
    @{
      type = 'function'
      function = @{
        name = 'multi_agent_v1__close_agent'
        description = 'Close a Codex child agent.'
        parameters = @{
          type = 'object'
          properties = @{
            target = @{ type = 'string' }
          }
          required = @('target')
          additionalProperties = $false
        }
      }
    }
  )
  tool_choice = @{ type = 'function'; function = @{ name = 'multi_agent_v1__close_agent' } }
  max_tokens = 128
  stream = $false
}

$closeJson = $closePayload | ConvertTo-Json -Depth 80
$closeResp = Invoke-RestMethod -Uri $uri -Method Post -ContentType 'application/json' -Body $closeJson -TimeoutSec 120
$closeResp | ConvertTo-Json -Depth 60 | Set-Content -Encoding UTF8 $closeOut

$closeCall = $closeResp.choices[0].message.tool_calls[0]
if ($closeCall.function.name -ne 'multi_agent_v1__close_agent') {
  throw "Expected flat close tool name, got: $($closeCall.function.name)"
}
$closeArgs = $closeCall.function.arguments | ConvertFrom-Json
if ($closeArgs.target -ne 'agent-flat-e2e') {
  throw "Expected close target agent-flat-e2e, got: $($closeCall.function.arguments)"
}
"CHAT_FLAT_CLOSE_OK call_id=$($closeCall.id)"
```

Expected: command prints `CHAT_FLAT_CLOSE_OK`. This proves the real Chat flattening route is viable for a full spawn -> wait -> close alias sequence, independent of native subagent execution.

- [ ] **Step 6: Record the Chat flattening spike summary**

Run:

```powershell
$summary = "diagnostics/subagent-e2e/chat-flat-summary-$stamp.md"
@"
# Chat Flattening E2E Spike

- Provider: $provider
- Model: $model
- Endpoint: $uri
- Spawn response: $spawnOut
- Wait response: $waitOut
- Close response: $closeOut
- Spawn tool name: $($spawnCall.function.name)
- Wait tool name: $($waitCall.function.name)
- Close tool name: $($closeCall.function.name)
- Result: flat Chat tool aliases are viable for subagent-style tool calls.
"@ | Set-Content -Encoding UTF8 $summary
Get-Content $summary -Raw
```

Expected: summary records all three flat tool names and paths to saved raw responses. Do not commit diagnostics unless explicitly requested.

---

### Task 2: Persist Provider Tool Protocol Capability

**Files:**
- Modify: `src-python/providers_config.py`
- Modify: `tests/test_providers_config.py`
- Modify: `src-python/catalog_sync.py`
- Modify: `tests/test_catalog_sync.py`

- [ ] **Step 1: Add provider config tests**

Extend `tests/test_providers_config.py::ProviderConfigTests.test_upstream_model_load_save_and_index_preserve_live_case` with these assertions and constructor field:

```python
            ProviderConfig(
                id="case-provider",
                name="Case Provider",
                base_url="https://case.example/v1",
                api_key="case-secret",
                upstream_format="chat_completions",
                available_upstream_formats=("responses", "chat_completions"),
                tool_protocol="chat_tools",
                models=[
```

Add these assertions after existing `available_upstream_formats` checks:

```python
        self.assertEqual(loaded[0].tool_protocol, "chat_tools")
        self.assertIn('tool_protocol = "chat_tools"', raw_toml)
        self.assertEqual(index["case-provider/alias-model"]["tool_protocol"], "chat_tools")
```

- [ ] **Step 2: Run the provider config test to verify it fails**

Run:

```powershell
python -m unittest tests.test_providers_config.ProviderConfigTests.test_upstream_model_load_save_and_index_preserve_live_case -v
```

Expected: FAIL because `ProviderConfig` does not yet accept or persist `tool_protocol`.

- [ ] **Step 3: Implement Python provider capability persistence**

In `src-python/providers_config.py`, add the allowed values next to `UPSTREAM_FORMATS`:

```python
TOOL_PROTOCOLS = {"auto", "responses_structured", "chat_tools", "text_compat", "none"}
```

Add the field to `ProviderConfig`:

```python
    tool_protocol: str = "auto"
```

Add it to `build_external_model_index()` entries:

```python
                "tool_protocol": provider.tool_protocol,
```

Add it to `load_providers()`:

```python
            tool_protocol=_tool_protocol_field(raw_provider.get("tool_protocol")),
```

Add it to `save_providers()` immediately after `available_upstream_formats`:

```python
        if provider.tool_protocol and provider.tool_protocol != "auto":
            chunks.append(_toml_string_line("tool_protocol", provider.tool_protocol))
```

Add this parser near `_upstream_formats_field()`:

```python
def _tool_protocol_field(value: Any) -> str:
    tool_protocol = _string_field(value, "auto").strip().lower()
    return tool_protocol if tool_protocol in TOOL_PROTOCOLS else "auto"
```

- [ ] **Step 4: Run provider config tests**

Run:

```powershell
python -m unittest tests.test_providers_config -v
```

Expected: PASS.

- [ ] **Step 5: Update catalog sync metadata tests**

In `tests/test_catalog_sync.py`, update external provider model fixtures that include `upstream_format` to also include:

```python
                "tool_protocol": "responses_structured",
```

Then assert the generated catalog metadata includes:

```python
        self.assertEqual(by_slug["ollama-cloud/glm-5.2"]["codex_proxy_metadata"]["tool_protocol"], "responses_structured")
```

- [ ] **Step 6: Implement catalog sync propagation**

In `src-python/catalog_sync.py`, add `tool_protocol` next to `upstream_format` in external model metadata:

```python
            "tool_protocol": external_model.get("tool_protocol", "auto"),
```

- [ ] **Step 7: Run catalog sync tests**

Run:

```powershell
python -m unittest tests.test_catalog_sync -v
```

Expected: PASS.

- [ ] **Step 8: Commit provider capability persistence**

Run:

```powershell
git add src-python/providers_config.py tests/test_providers_config.py src-python/catalog_sync.py tests/test_catalog_sync.py
git commit -m "feat: persist external tool protocol capability"
```

---

### Task 3: Add Probe Recommendation For Tool Protocol

**Files:**
- Modify: `src-python/probe_upstream_format.py`
- Modify: `tests/test_probe_upstream_format.py`
- Modify: `frontend/src/lib/types.ts`
- Modify: `src-tauri/src/models.rs`

- [ ] **Step 1: Add Python probe tests**

In `tests/test_probe_upstream_format.py`, add:

```python
    def test_recommends_responses_structured_when_responses_tools_work(self):
        result = {
            "responses_tool_ok": True,
            "responses_tool_stream_ok": False,
            "chat_tool_ok": True,
            "chat_tool_stream_ok": True,
        }
        self.assertEqual(recommended_tool_protocol(result), "responses_structured")

    def test_recommends_chat_tools_when_only_chat_tools_work(self):
        result = {
            "responses_tool_ok": False,
            "responses_tool_stream_ok": False,
            "chat_tool_ok": True,
            "chat_tool_stream_ok": False,
        }
        self.assertEqual(recommended_tool_protocol(result), "chat_tools")

    def test_recommends_none_without_tool_support(self):
        result = {
            "responses_tool_ok": False,
            "responses_tool_stream_ok": False,
            "chat_tool_ok": False,
            "chat_tool_stream_ok": False,
        }
        self.assertEqual(recommended_tool_protocol(result), "none")
```

Add `recommended_tool_protocol` to the import list at the top of the test file.

- [ ] **Step 2: Run the failing probe tests**

Run:

```powershell
python -m unittest tests.test_probe_upstream_format -v
```

Expected: FAIL because `recommended_tool_protocol` and the result field do not exist.

- [ ] **Step 3: Implement probe recommendation**

In `src-python/probe_upstream_format.py`, add:

```python
def recommended_tool_protocol(result: dict[str, Any]) -> str:
    if result.get("responses_tool_ok") or result.get("responses_tool_stream_ok"):
        return "responses_structured"
    if result.get("chat_tool_ok") or result.get("chat_tool_stream_ok"):
        return "chat_tools"
    return "none"
```

Initialize the result field in `probe()`:

```python
        "recommended_tool_protocol": "none",
```

After tool probes complete and before `duration_ms`, set:

```python
    result["recommended_tool_protocol"] = recommended_tool_protocol(result)
    if result["recommended_tool_protocol"] == "responses_structured":
        notes.append("Tool protocol: Responses structured tools")
    elif result["recommended_tool_protocol"] == "chat_tools":
        notes.append("Tool protocol: Chat Completions tool_calls")
    else:
        notes.append("Tool protocol: no reliable tool-call support detected")
```

- [ ] **Step 4: Update TypeScript probe type**

In `frontend/src/lib/types.ts`, add:

```ts
export type ToolProtocol = "auto" | "responses_structured" | "chat_tools" | "text_compat" | "none";
```

Add to `UpstreamFormatProbeResult`:

```ts
  recommended_tool_protocol: ToolProtocol;
```

Add to `Provider`:

```ts
  tool_protocol?: ToolProtocol | null;
```

- [ ] **Step 5: Update Rust model probe test expectations**

In `src-tauri/src/models.rs`, update the JSON shape expected by model probe tests to include:

```rust
assert_eq!(result["recommended_tool_protocol"], "responses_structured");
```

For chat-only tests, assert:

```rust
assert_eq!(result["recommended_tool_protocol"], "chat_tools");
```

- [ ] **Step 6: Run probe and Rust model tests**

Run:

```powershell
python -m unittest tests.test_probe_upstream_format -v
cargo test --manifest-path src-tauri/Cargo.toml models::tests:: -- --nocapture
```

Expected: PASS.

- [ ] **Step 7: Commit probe capability recommendation**

Run:

```powershell
git add src-python/probe_upstream_format.py tests/test_probe_upstream_format.py frontend/src/lib/types.ts src-tauri/src/models.rs
git commit -m "feat: recommend provider tool protocol from probes"
```

---

### Task 4: Preserve Structured Tool Transcript For Responses-Capable Providers

**Files:**
- Modify: `src-python/codex_proxy.py`
- Modify: `tests/test_routing.py`

- [ ] **Step 1: Add adapter-mode helpers**

In `src-python/codex_proxy.py`, add this helper near the existing multi-agent tool helpers:

```python
STRUCTURED_TOOL_PROTOCOLS = {"responses_structured", "chat_tools"}


def _external_tool_protocol(upstream: Mapping[str, Any]) -> str:
    configured = str(upstream.get("tool_protocol") or "auto").strip().lower()
    if configured in {"responses_structured", "chat_tools", "text_compat", "none"}:
        return configured
    upstream_format = str(upstream.get("upstream_format") or "responses").strip().lower()
    if upstream_format == "chat_completions":
        return "chat_tools"
    if upstream_format == "responses":
        return "responses_structured"
    return "text_compat"
```

- [ ] **Step 2: Add structured input rewrite helper**

Add:

```python
def _structured_tool_function_call_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if item.get("type") != "function_call":
        return None
    tool_name = _multi_agent_function_call_name(item)
    if tool_name is not None:
        rewritten = dict(item)
        rewritten.pop("namespace", None)
        rewritten["name"] = f"multi_agent_v1__{tool_name}"
        normalized, _, args_changed = _normalize_multi_agent_arguments(rewritten.get("arguments"), tool_name)
        if args_changed:
            rewritten["arguments"] = json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
        return rewritten
    node_name = _node_repl_function_call_name(item)
    if node_name is not None:
        rewritten = dict(item)
        rewritten.pop("namespace", None)
        rewritten["name"] = f"{NODE_REPL_NAMESPACE}__{node_name}"
        return rewritten
    return dict(item)


def _rewrite_structured_tool_input_items(
    payload: dict[str, Any],
    event_context: Mapping[str, Any] | None = None,
    upstream_name: str | None = None,
) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    changed = False
    rewritten_items: list[Any] = []
    multi_agent_search_call_ids: set[str] = set()
    for item in input_items:
        if not isinstance(item, dict):
            rewritten_items.append(item)
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            rewritten = _structured_tool_function_call_item(item)
            rewritten_items.append(rewritten if rewritten is not None else item)
            changed = changed or rewritten != item
            continue
        if item_type == "function_call_output":
            rewritten_items.append(dict(item))
            continue
        call_id = item.get("call_id")
        if (
            item_type == "tool_search_call"
            and isinstance(call_id, str)
            and _is_multi_agent_discovery_arguments(_json_object_from_arguments(item.get("arguments")))
        ):
            multi_agent_search_call_ids.add(call_id)
            replacement = _compatible_internal_message(item)
            if replacement is not None:
                rewritten_items.append(replacement)
                changed = True
            continue
        if (
            item_type == "tool_search_output"
            and isinstance(call_id, str)
            and call_id in multi_agent_search_call_ids
        ):
            replacement = _multi_agent_discovery_output_item(item)
            rewritten_items.append(_compatible_internal_message(replacement) or replacement)
            _write_adapter_event(
                event_context,
                "structured_tool_search_context_rewritten",
                upstream=upstream_name,
                call_id=call_id,
            )
            changed = True
            continue
        replacement = _compatible_internal_message(item)
        if replacement is not None:
            rewritten_items.append(replacement)
            changed = True
        else:
            rewritten_items.append(item)

    if changed:
        payload["input"] = rewritten_items
    return changed
```

- [ ] **Step 3: Route compatible request through structured mode**

In `compatible_request_body()`, replace:

```python
    changed = _rewrite_internal_input_items(payload, event_context=event_context, upstream_name=upstream_name)
```

with:

```python
    tool_protocol = _external_tool_protocol(upstream)
    if tool_protocol in STRUCTURED_TOOL_PROTOCOLS:
        changed = _rewrite_structured_tool_input_items(payload, event_context=event_context, upstream_name=upstream_name)
    elif tool_protocol == "none":
        changed = False
        payload["tools"] = [
            tool
            for tool in payload.get("tools", [])
            if not _is_multi_agent_tool_schema(tool)
        ] if isinstance(payload.get("tools"), list) else []
    else:
        changed = _rewrite_internal_input_items(payload, event_context=event_context, upstream_name=upstream_name)
```

- [ ] **Step 4: Ensure explicit tool injection follows tool protocol**

Before `_inject_explicit_codex_tools(...)`, add:

```python
    allow_codex_tools = tool_protocol != "none"
```

Then change the `if inject_codex_tools:` guard to:

```python
    if inject_codex_tools and allow_codex_tools:
```

- [ ] **Step 5: Run focused structured Responses tests**

Run:

```powershell
python -m unittest tests.test_routing.RoutingTests.test_responses_structured_provider_preserves_multi_agent_tool_history -v
python -m unittest tests.test_routing.RoutingTests.test_external_request_hides_spawn_agent_while_child_is_open -v
```

Expected: PASS. The first test proves structured providers keep structured tool outputs; the second proves existing text compatibility behavior remains intact when `tool_protocol` is not set to `responses_structured`.

- [ ] **Step 6: Commit structured Responses preservation**

Run:

```powershell
git add src-python/codex_proxy.py tests/test_routing.py
git commit -m "fix: preserve structured tool history for responses providers"
```

---

### Task 5: Gate Chat Tool History And Add State-Summary Fallback

**Files:**
- Modify: `src-python/codex_proxy.py`
- Modify: `tests/test_chat_completions_gateway.py`

- [ ] **Step 1: Keep OpenAI-style Chat history conversion behind a live capability flag**

Do not route Ollama GLM-5.2 through this path until the live probe in `diagnostics/subagent-e2e/chat-history-variants-*.md` passes the `wait-tool-history-forced` and `close-tool-history-forced` cases. The conversion below is still needed for providers that truly support Chat tool history, but `tool_protocol = "chat_tools"` must mean "probe passed", not merely "endpoint accepts `/chat/completions`".

Add this unit test in `tests/test_chat_completions_gateway.py` to define the conversion shape for providers that pass the probe:

```python
    def test_responses_to_chat_preserves_function_call_history(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Use a child agent."},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return child-ok"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child"}),
                    },
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "multi_agent_v1__spawn_agent",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        ).encode("utf-8")

        payload = json.loads(_responses_request_to_chat_completion_body(body))

        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(payload["messages"][1]["role"], "assistant")
        self.assertEqual(payload["messages"][1]["tool_calls"][0]["id"], "call_spawn")
        self.assertEqual(payload["messages"][1]["tool_calls"][0]["function"]["name"], "multi_agent_v1__spawn_agent")
        self.assertEqual(payload["messages"][2]["role"], "tool")
        self.assertEqual(payload["messages"][2]["tool_call_id"], "call_spawn")
        self.assertIn("019f-child", payload["messages"][2]["content"])
```

- [ ] **Step 2: Implement function-call item conversion to Chat messages**

In `_responses_input_to_chat_messages()` inside `src-python/codex_proxy.py`, replace the loop body with this behavior:

```python
    messages: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            name = item.get("name")
            call_id = item.get("call_id") or f"call_{uuid.uuid4().hex[:12]}"
            arguments = item.get("arguments")
            if isinstance(name, str) and name:
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": str(call_id),
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments or {}, ensure_ascii=True),
                                },
                            }
                        ],
                    }
                )
            continue
        if item_type == "function_call_output":
            call_id = item.get("call_id") or f"call_{uuid.uuid4().hex[:12]}"
            output = item.get("output")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call_id),
                    "content": output if isinstance(output, str) else json.dumps(output, ensure_ascii=True),
                }
            )
            continue
        if item_type != "message":
            continue
        role = item.get("role")
        role = role if role in {"system", "user", "assistant"} else "user"
        content = _responses_content_to_chat_content(item.get("content"))
        messages.append({"role": role, "content": content})
```

- [ ] **Step 3: Add state-summary fallback tests for Ollama-style Chat compatibility**

In `tests/test_routing.py`, add a test that proves an open child exposes only wait with a compact state message, not raw `assistant.tool_calls` / `role: tool` history:

```python
    def test_chat_state_summary_fallback_exposes_only_wait_for_open_child(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Use one subagent."},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-ok"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child", "nickname": "child"}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(
            body,
            {"name": "ollama_cloud", "upstream_format": "chat_completions", "tool_protocol": "text_compat"},
            event_context={"request_id": "req"},
        )
        payload = json.loads(transformed)
        transcript = json.dumps(payload, ensure_ascii=False)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"]}

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertIn("agent_id: 019f-child", transcript)
        self.assertIn("required_next_action: call wait_agent", transcript)
```

- [ ] **Step 4: Implement state-summary fallback routing**

In `src-python/codex_proxy.py`, make the Chat adapter choose one of two paths:

```python
if tool_protocol == "chat_tools":
    # Use _responses_input_to_chat_messages() with assistant.tool_calls and role: tool.
    chat_payload["messages"] = _responses_input_to_chat_messages(payload.get("input"))
elif tool_protocol in {"text_compat", "auto"}:
    # Keep the existing compatibility transcript: convert previous native tool calls
    # into compact state messages, expose only the next valid subagent tool, and do
    # not emit role: tool history for Ollama GLM-5.2.
    chat_payload["messages"] = _responses_input_to_chat_messages(payload.get("input"))
    chat_payload = _apply_external_compatibility_guidance(chat_payload, upstream, event_context)
```

Use the existing local helper names where they already exist; if `_apply_external_compatibility_guidance()` is not a single function today, keep the current compatibility calls in place but branch before OpenAI-style Chat history conversion.

- [ ] **Step 5: Run chat conversion and compatibility tests**

Run:

```powershell
$env:PYTHONPATH='src-python'
python -m unittest tests.test_chat_completions_gateway -v
python -m unittest tests.test_routing.RoutingTests.test_chat_state_summary_fallback_exposes_only_wait_for_open_child -v
```

Expected: PASS.

- [ ] **Step 6: Run routing regression for chat provider path**

Run:

```powershell
$env:PYTHONPATH='src-python'
python -m unittest tests.test_routing.RoutingTests.test_chat_completions_upstream_posts_to_chat_endpoint_and_body -v
```

Expected: PASS.

- [ ] **Step 7: Commit Chat tool protocol gating**

Run:

```powershell
git add src-python/codex_proxy.py tests/test_chat_completions_gateway.py
git commit -m "fix: gate chat tool history behind live capability"
```

---

### Task 6: Introduce Gateway-Owned Subagent State Parser

**Files:**
- Create: `src-python/subagent_state.py`
- Modify: `tests/test_routing.py`
- Modify: `src-python/codex_proxy.py`

- [ ] **Step 1: Define the coordinator state machine contract**

The Gateway, not the model, owns the event ledger and policy decisions. The base one-shot lifecycle is still:

```text
NoChild
  -> Spawned(call_id, agent_id, prompt, nickname)
  -> Waited(agent_id, wait_call_id, result)
  -> Closed(agent_id, close_call_id)
  -> LifecycleComplete
```

Rules:

- `agent_id` comes from `spawn_agent` output.
- `call_id` links each `function_call` to its `function_call_output`.
- `spawn_prompt` and `nickname` come from spawn arguments, with output nickname as fallback.
- `role` is inferred from spawn prompt/nickname/description: `implementer`, `spec_reviewer`, `code_quality_reviewer`, `final_reviewer`, `fixer`, or `generic`.
- `task_key` is inferred from prompt text such as `Task 1`, `Task N`, task title, or reviewer scope.
- `implementation_epoch` is tracked per `task_key`. A fresh implementer spawn starts the first epoch for that task. A completed `send_input` or `resume_agent` fix by the implementer advances the epoch. Reviewers are associated with the current task epoch at spawn time.
- `spawn_signature = (role, task_key, normalized_spawn_prompt, nickname, implementation_epoch)` is used to distinguish a duplicate retry from a new independent subagent.
- A repeated signature in the same epoch is duplicate. The same reviewer prompt after an implementer fix is not duplicate because the epoch changed.
- `requested_count` is parsed from the user's latest bounded request: "one/1/一次/一个", "two/2/两个", etc. If the user asked for a subagent workflow but did not specify a number, default to `1` after the first spawn intent is seen.
- Sequential phrasing counts as bounded multi-spawn: "spawn one, then spawn another", "先 spawn 一个，之后再 spawn 第二个", and "先开一个子代理，完成后再开第二个子代理" all mean `requested_count = 2`.
- `append_spawn_requested` is true only when a user message after the latest spawn output explicitly asks for an additional/new/another subagent.
- Planned review mode is inferred from observed role/task prompts and explicit context such as `subagent-driven-development`, but the policy is not hard-coded to that skill name. The same ledger must support equivalent external workflows.
- In planned review mode, `should_allow_spawn` remains true for the next distinct role/task/epoch agent: implementer -> spec reviewer -> code quality reviewer -> next task implementer -> final reviewer. It remains false only for duplicate same-signature same-epoch spawn attempts unless the coordinator explicitly asks for a retry/fix.
- In planned review mode, reviewer failure does not normally spawn a new implementer. It should expose `send_input` or `resume_agent` for the original implementer so the same subagent can fix spec or quality issues. After the implementer fix completes, the same reviewer role/prompt may be spawned again in the new implementation epoch.
- In one-shot mode, `should_allow_spawn` is true only when no child has been spawned yet, `spawned_count < requested_count`, or `append_spawn_requested` is true.
- In one-shot mode, `should_allow_spawn` is false after a complete `spawn -> wait -> close` lifecycle when `spawned_count >= requested_count` and there is no append request.
- `lifecycle_complete` is true when every spawned child is closed and `spawned_count >= effective_requested_count`.

- [ ] **Step 2: Add subagent state unit tests**

Create a new test class in `tests/test_routing.py`:

```python
class SubagentStateTests(unittest.TestCase):
    def test_state_tracks_spawn_prompt_nickname_wait_and_close(self):
        from subagent_state import build_subagent_state

        items = [
            {"type": "message", "role": "user", "content": "spawn two subagents"},
            {
                "type": "function_call",
                "call_id": "call_spawn_a",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": {"message": "return A", "nickname": "a"},
            },
            {
                "type": "function_call_output",
                "call_id": "call_spawn_a",
                "output": json.dumps({"agent_id": "agent-a", "nickname": "a"}),
            },
            {
                "type": "function_call",
                "call_id": "call_wait",
                "namespace": "multi_agent_v1",
                "name": "wait_agent",
                "arguments": {"targets": ["agent-a"], "timeout_ms": 60000},
            },
            {
                "type": "function_call_output",
                "call_id": "call_wait",
                "output": json.dumps({"timed_out": False, "status": {"agent-a": {"completed": "A"}}}),
            },
            {
                "type": "function_call",
                "call_id": "call_close",
                "namespace": "multi_agent_v1",
                "name": "close_agent",
                "arguments": {"target": "agent-a"},
            },
            {
                "type": "function_call_output",
                "call_id": "call_close",
                "output": json.dumps({"previous_status": {"completed": "A"}}),
            },
        ]

        state = build_subagent_state(items)

        self.assertEqual(state.requested_count, 2)
        self.assertEqual(state.spawned_agent_ids, ("agent-a",))
        self.assertEqual(state.closed_agent_ids, ("agent-a",))
        self.assertEqual(state.children[0].call_id, "call_spawn_a")
        self.assertEqual(state.children[0].spawn_prompt, "return A")
        self.assertEqual(state.children[0].nickname, "a")
        self.assertFalse(state.has_open_agents)
        self.assertFalse(state.lifecycle_complete)
        self.assertTrue(state.should_allow_spawn)

    def test_state_marks_single_closed_lifecycle_complete_and_blocks_spawn(self):
        from subagent_state import build_subagent_state

        items = [
            {"type": "message", "role": "user", "content": "Run exactly one subagent lifecycle: spawn, wait, close."},
            {
                "type": "function_call",
                "call_id": "call_spawn",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": {"message": "return child-ok", "nickname": "child"},
            },
            {
                "type": "function_call_output",
                "call_id": "call_spawn",
                "output": json.dumps({"agent_id": "agent-a", "nickname": "child"}),
            },
            {
                "type": "function_call",
                "call_id": "call_wait",
                "namespace": "multi_agent_v1",
                "name": "wait_agent",
                "arguments": {"targets": ["agent-a"]},
            },
            {
                "type": "function_call_output",
                "call_id": "call_wait",
                "output": json.dumps({"timed_out": False, "status": {"agent-a": {"completed": "child-ok"}}}),
            },
            {
                "type": "function_call",
                "call_id": "call_close",
                "namespace": "multi_agent_v1",
                "name": "close_agent",
                "arguments": {"target": "agent-a"},
            },
            {
                "type": "function_call_output",
                "call_id": "call_close",
                "output": json.dumps({"previous_status": {"completed": "child-ok"}}),
            },
        ]

        state = build_subagent_state(items)

        self.assertEqual(state.requested_count, 1)
        self.assertEqual(state.spawned_agent_ids, ("agent-a",))
        self.assertEqual(state.closed_agent_ids, ("agent-a",))
        self.assertTrue(state.lifecycle_complete)
        self.assertFalse(state.has_open_agents)
        self.assertFalse(state.should_allow_spawn)

    def test_state_allows_second_spawn_for_sequential_two_spawn_request(self):
        from subagent_state import build_subagent_state

        items = [
            {"type": "message", "role": "user", "content": "先 spawn 一个出来之后再 spawn 第二个。"},
            {
                "type": "function_call",
                "call_id": "call_spawn_a",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": {"message": "return A", "nickname": "a"},
            },
            {
                "type": "function_call_output",
                "call_id": "call_spawn_a",
                "output": json.dumps({"agent_id": "agent-a", "nickname": "a"}),
            },
        ]

        state = build_subagent_state(items)

        self.assertEqual(state.requested_count, 2)
        self.assertEqual(state.spawned_agent_ids, ("agent-a",))
        self.assertTrue(state.should_allow_spawn)
        self.assertEqual(state.next_action, "spawn")

    def test_state_waits_after_second_spawn_is_present(self):
        from subagent_state import build_subagent_state

        items = [
            {"type": "message", "role": "user", "content": "spawn one, then spawn another one"},
            {
                "type": "function_call",
                "call_id": "call_spawn_a",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": {"message": "return A", "nickname": "a"},
            },
            {"type": "function_call_output", "call_id": "call_spawn_a", "output": json.dumps({"agent_id": "agent-a"})},
            {
                "type": "function_call",
                "call_id": "call_spawn_b",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": {"message": "return B", "nickname": "b"},
            },
            {"type": "function_call_output", "call_id": "call_spawn_b", "output": json.dumps({"agent_id": "agent-b"})},
        ]

        state = build_subagent_state(items)

        self.assertEqual(state.requested_count, 2)
        self.assertFalse(state.should_allow_spawn)
        self.assertEqual(state.wait_agent_ids, ("agent-a", "agent-b"))
        self.assertEqual(state.next_action, "wait")

    def test_subagent_driven_development_allows_reviewer_after_implementer_done(self):
        from subagent_state import build_subagent_state

        items = [
            {
                "type": "message",
                "role": "user",
                "content": "Use superpowers:subagent-driven-development to execute Task 1 from the plan.",
            },
            {
                "type": "function_call",
                "call_id": "call_impl",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": {
                    "message": "You are implementing Task 1: Preserve structured Responses history.",
                    "nickname": "implementer-task-1",
                },
            },
            {
                "type": "function_call_output",
                "call_id": "call_impl",
                "output": json.dumps({"agent_id": "agent-impl-1", "nickname": "implementer-task-1"}),
            },
            {
                "type": "function_call",
                "call_id": "call_wait_impl",
                "namespace": "multi_agent_v1",
                "name": "wait_agent",
                "arguments": {"targets": ["agent-impl-1"]},
            },
            {
                "type": "function_call_output",
                "call_id": "call_wait_impl",
                "output": json.dumps({"timed_out": False, "status": {"agent-impl-1": {"completed": "Status: DONE"}}}),
            },
        ]

        state = build_subagent_state(items)

        self.assertEqual(state.workflow_kind, "plan_task_review")
        self.assertEqual(state.children[0].role, "implementer")
        self.assertEqual(state.children[0].task_key, "task-1")
        self.assertTrue(state.should_allow_spawn)
        self.assertEqual(state.next_expected_role, "spec_reviewer")
        self.assertEqual(state.next_action, "spawn")

    def test_subagent_driven_development_blocks_duplicate_implementer_spawn(self):
        from subagent_state import build_subagent_state, classify_spawn_request

        items = [
            {
                "type": "message",
                "role": "user",
                "content": "Use superpowers:subagent-driven-development to execute Task 1 from the plan.",
            },
            {
                "type": "function_call",
                "call_id": "call_impl",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": {
                    "message": "You are implementing Task 1: Preserve structured Responses history.",
                    "nickname": "implementer-task-1",
                },
            },
            {
                "type": "function_call_output",
                "call_id": "call_impl",
                "output": json.dumps({"agent_id": "agent-impl-1", "nickname": "implementer-task-1"}),
            },
        ]
        state = build_subagent_state(items)
        duplicate = classify_spawn_request(
            {"message": "You are implementing Task 1: Preserve structured Responses history.", "nickname": "implementer-task-1"}
        )

        self.assertFalse(state.allows_spawn_request(duplicate))

    def test_subagent_driven_development_routes_reviewer_issues_back_to_implementer(self):
        from subagent_state import build_subagent_state

        items = [
            {
                "type": "message",
                "role": "user",
                "content": "Use superpowers:subagent-driven-development to execute Task 1 from the plan.",
            },
            {
                "type": "function_call",
                "call_id": "call_impl",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": {
                    "message": "You are implementing Task 1: Preserve structured Responses history.",
                    "nickname": "implementer-task-1",
                },
            },
            {"type": "function_call_output", "call_id": "call_impl", "output": json.dumps({"agent_id": "agent-impl-1"})},
            {
                "type": "function_call",
                "call_id": "call_spec",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": {
                    "message": "You are reviewing spec compliance for Task 1.",
                    "nickname": "spec-reviewer-task-1",
                },
            },
            {"type": "function_call_output", "call_id": "call_spec", "output": json.dumps({"agent_id": "agent-spec-1"})},
            {
                "type": "function_call",
                "call_id": "call_wait_spec",
                "namespace": "multi_agent_v1",
                "name": "wait_agent",
                "arguments": {"targets": ["agent-spec-1"]},
            },
            {
                "type": "function_call_output",
                "call_id": "call_wait_spec",
                "output": json.dumps({"timed_out": False, "status": {"agent-spec-1": {"completed": "❌ Issues found: missing test"}}}),
            },
        ]

        state = build_subagent_state(items)

        self.assertEqual(state.next_action, "send_input")
        self.assertEqual(state.send_input_target, "agent-impl-1")
        self.assertFalse(state.should_allow_spawn)

    def test_subagent_driven_development_allows_spec_rereview_after_implementer_fix(self):
        from subagent_state import build_subagent_state, classify_spawn_request

        items = [
            {
                "type": "message",
                "role": "user",
                "content": "Use subagent-driven-development to execute Task 1 from the plan.",
            },
            {
                "type": "function_call",
                "call_id": "call_impl",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": {
                    "message": "You are implementing Task 1: Preserve structured Responses history.",
                    "nickname": "implementer-task-1",
                },
            },
            {"type": "function_call_output", "call_id": "call_impl", "output": json.dumps({"agent_id": "agent-impl-1"})},
            {
                "type": "function_call",
                "call_id": "call_spec_1",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": {
                    "message": "You are reviewing spec compliance for Task 1.",
                    "nickname": "spec-reviewer-task-1",
                },
            },
            {"type": "function_call_output", "call_id": "call_spec_1", "output": json.dumps({"agent_id": "agent-spec-1"})},
            {
                "type": "function_call",
                "call_id": "call_wait_spec_1",
                "namespace": "multi_agent_v1",
                "name": "wait_agent",
                "arguments": {"targets": ["agent-spec-1"]},
            },
            {
                "type": "function_call_output",
                "call_id": "call_wait_spec_1",
                "output": json.dumps({"timed_out": False, "status": {"agent-spec-1": {"completed": "❌ Issues found: missing test"}}}),
            },
            {
                "type": "function_call",
                "call_id": "call_fix",
                "namespace": "multi_agent_v1",
                "name": "send_input",
                "arguments": {"target": "agent-impl-1", "message": "Fix the missing test."},
            },
            {"type": "function_call_output", "call_id": "call_fix", "output": json.dumps({"status": "sent"})},
            {
                "type": "function_call",
                "call_id": "call_wait_impl_fix",
                "namespace": "multi_agent_v1",
                "name": "wait_agent",
                "arguments": {"targets": ["agent-impl-1"]},
            },
            {
                "type": "function_call_output",
                "call_id": "call_wait_impl_fix",
                "output": json.dumps({"timed_out": False, "status": {"agent-impl-1": {"completed": "Status: DONE fixed"}}}),
            },
        ]

        state = build_subagent_state(items)
        rereview = classify_spawn_request(
            {"message": "You are reviewing spec compliance for Task 1.", "nickname": "spec-reviewer-task-1"}
        )

        self.assertEqual(state.next_action, "spawn")
        self.assertEqual(state.next_expected_role, "spec_reviewer")
        self.assertTrue(state.allows_spawn_request(rereview))

    def test_state_detects_append_request_after_existing_spawn(self):
        from subagent_state import build_subagent_state

        items = [
            {
                "type": "function_call",
                "call_id": "call_spawn_a",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": {"message": "return A"},
            },
            {"type": "function_call_output", "call_id": "call_spawn_a", "output": json.dumps({"agent_id": "agent-a"})},
            {"type": "message", "role": "user", "content": "再追加一个新的独立子代理处理 B。"},
        ]

        state = build_subagent_state(items)

        self.assertTrue(state.append_spawn_requested)
        self.assertTrue(state.should_allow_spawn)
        self.assertEqual(state.spawned_agent_ids, ("agent-a",))
```

- [ ] **Step 3: Run state tests to verify they fail**

Run:

```powershell
$env:PYTHONPATH='src-python'
python -m unittest tests.test_routing.SubagentStateTests -v
```

Expected: FAIL because `src-python/subagent_state.py` does not exist.

- [ ] **Step 4: Implement state parser**

Use the behavior tests above and the validated spike at `.planning/spikes/001-subagent-coordinator-policy/spike_policy_sim.py` as the source of truth. The implementation must be event-led and epoch-aware; do not copy an older 4-field `spawn_signature` design that blocks all repeated reviewer prompts forever.

Create `src-python/subagent_state.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping


APPEND_RE = re.compile(
    r"(?:再|追加|新增|另(?:外)?|之后再|再开|再创建|再启动|再\s*spawn|then\s+spawn|then\s+start|another|additional|append|new|第二个|第\s*2\s*个|second)",
    re.IGNORECASE,
)
SPAWN_WORD_RE = re.compile(r"(?:spawn|创建|启动|派发|调用|开|生成|子代理|subagent|agent)", re.IGNORECASE)
ORDINAL_SECOND_RE = re.compile(r"(?:第二个|第\s*2\s*个|second|another)", re.IGNORECASE)
TASK_KEY_RE = re.compile(r"\bTask\s*(?P<num>\d+)\b|任务\s*(?P<cnum>\d+)", re.IGNORECASE)
PLAN_TASK_REVIEW_RE = re.compile(r"subagent-driven-development|spec compliance|code quality reviewer|implementer subagent", re.IGNORECASE)
RETRY_RE = re.compile(r"retry|redo|rerun|fix|修复|重试|重新执行", re.IGNORECASE)
REVIEW_ISSUE_RE = re.compile(r"❌|issues? found|missing|extra|not compliant|问题|缺失|不符合", re.IGNORECASE)
COUNT_RE = re.compile(
    r"(?:spawn|spawns|创建|启动|派发|调用|开|生成|同步\s*spawn|exactly|正好|只(?:执行|调用|创建)?)\s*(?P<count>\d{1,2}|one|two|three|一个|一次|两个|三个)\s*(?:个|名|位|次)?\s*(?:subagents?|agents?|子代理|lifecycle|生命周期)?",
    re.IGNORECASE,
)
SPAWN_INTENT_RE = re.compile(r"(?:subagent|子代理|spawn|multi_agent|协作测试|lifecycle|生命周期)", re.IGNORECASE)
COUNT_WORDS = {
    "one": 1,
    "一个": 1,
    "一次": 1,
    "two": 2,
    "两个": 2,
    "three": 3,
    "三个": 3,
}


@dataclass(frozen=True)
class SubagentChild:
    call_id: str
    agent_id: str | None
    spawn_prompt: str
    nickname: str | None
    spawn_index: int
    role: str
    task_key: str | None
    implementation_epoch: int
    spawn_signature: tuple[str, str | None, str, str | None, int]
    wait_call_id: str | None = None
    close_call_id: str | None = None
    result_text: str = ""
    wait_completed: bool = False
    closed: bool = False


@dataclass(frozen=True)
class SpawnRequest:
    role: str
    task_key: str | None
    spawn_prompt: str
    nickname: str | None
    implementation_epoch: int = 0
    retry_requested: bool = False

    @property
    def signature(self) -> tuple[str, str | None, str, str | None, int]:
        return (self.role, self.task_key, _normalize_prompt(self.spawn_prompt), self.nickname, self.implementation_epoch)


@dataclass(frozen=True)
class SubagentState:
    children: tuple[SubagentChild, ...]
    requested_count: int | None
    append_spawn_requested: bool
    spawn_intent_seen: bool
    workflow_kind: str

    @property
    def spawned_agent_ids(self) -> tuple[str, ...]:
        return tuple(child.agent_id for child in self.children if child.agent_id)

    @property
    def open_agent_ids(self) -> tuple[str, ...]:
        return tuple(child.agent_id for child in self.children if child.agent_id and not child.closed)

    @property
    def wait_agent_ids(self) -> tuple[str, ...]:
        return tuple(child.agent_id for child in self.children if child.agent_id and not child.closed and not child.wait_completed)

    @property
    def close_agent_ids(self) -> tuple[str, ...]:
        return tuple(child.agent_id for child in self.children if child.agent_id and not child.closed and child.wait_completed)

    @property
    def closed_agent_ids(self) -> tuple[str, ...]:
        return tuple(child.agent_id for child in self.children if child.agent_id and child.closed)

    @property
    def has_open_agents(self) -> bool:
        return bool(self.open_agent_ids)

    @property
    def effective_requested_count(self) -> int:
        if self.requested_count is not None:
            return self.requested_count
        if self.spawn_intent_seen:
            return max(1, len(self.spawned_agent_ids))
        return len(self.spawned_agent_ids)

    @property
    def lifecycle_complete(self) -> bool:
        return bool(
            self.spawned_agent_ids
            and len(self.spawned_agent_ids) >= self.effective_requested_count
            and len(self.closed_agent_ids) >= len(self.spawned_agent_ids)
            and not self.append_spawn_requested
        )

    @property
    def should_allow_spawn(self) -> bool:
        if self.workflow_kind == "plan_task_review":
            return self.next_expected_role is not None
        if self.lifecycle_complete:
            return False
        if self.requested_count is not None and len(self.spawned_agent_ids) < self.requested_count:
            return True
        if self.append_spawn_requested:
            return True
        return not self.spawned_agent_ids and not self.has_open_agents

    @property
    def next_action(self) -> str:
        if self.send_input_target:
            return "send_input"
        if self.should_allow_spawn:
            return "spawn"
        if self.wait_agent_ids:
            return "wait"
        if self.close_agent_ids:
            return "close"
        if self.lifecycle_complete:
            return "final"
        return "none"

    @property
    def send_input_target(self) -> str | None:
        if self.workflow_kind != "plan_task_review":
            return None
        failed_review = next(
            (
                child
                for child in reversed(self.children)
                if child.role in {"spec_reviewer", "code_quality_reviewer"} and REVIEW_ISSUE_RE.search(child.result_text)
            ),
            None,
        )
        if failed_review is None:
            return None
        implementer = next(
            (
                child
                for child in reversed(self.children)
                if child.role == "implementer" and child.task_key == failed_review.task_key and child.agent_id
            ),
            None,
        )
        return implementer.agent_id if implementer else None

    @property
    def next_expected_role(self) -> str | None:
        if self.workflow_kind != "plan_task_review":
            return None
        roles_by_task: dict[str | None, set[str]] = {}
        for child in self.children:
            roles_by_task.setdefault(child.task_key, set()).add(child.role)
        latest_task_key = self.children[-1].task_key if self.children else None
        roles = roles_by_task.get(latest_task_key, set())
        if "implementer" not in roles:
            return "implementer"
        if "spec_reviewer" not in roles:
            return "spec_reviewer"
        if "code_quality_reviewer" not in roles:
            return "code_quality_reviewer"
        return "implementer" if _has_more_plan_tasks(latest_task_key) else "final_reviewer"

    def allows_spawn_request(self, request: SpawnRequest) -> bool:
        if request.retry_requested:
            return True
        existing = {child.spawn_signature for child in self.children}
        if request.signature in existing:
            return False
        if self.workflow_kind == "plan_task_review":
            expected = self.next_expected_role
            return expected is not None and (request.role == expected or request.role == "fixer")
        return self.should_allow_spawn


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(_text(item) for item in value.values())
    return ""


def _tool_name(item: Mapping[str, Any]) -> str | None:
    if item.get("type") != "function_call":
        return None
    namespace = item.get("namespace")
    name = item.get("name")
    if namespace == "multi_agent_v1" and isinstance(name, str):
        return name
    if isinstance(name, str) and name.startswith("multi_agent_v1__"):
        return name.removeprefix("multi_agent_v1__")
    return None


def _requested_count(items: list[Any]) -> int | None:
    text = _text(items)
    counts: list[int] = []
    for match in COUNT_RE.finditer(text):
        raw_count = match.group("count").lower()
        count = COUNT_WORDS.get(raw_count)
        if count is None:
            count = int(raw_count)
        if 1 <= count <= 20:
            counts.append(count)
    if ORDINAL_SECOND_RE.search(text) and SPAWN_WORD_RE.search(text):
        counts.append(2)
    if not counts:
        return None
    return max(counts)


def _normalize_prompt(value: str) -> str:
    return " ".join(value.lower().split())[:500]


def _task_key(text: str) -> str | None:
    match = TASK_KEY_RE.search(text)
    if not match:
        return None
    number = match.group("num") or match.group("cnum")
    return f"task-{number}" if number else None


def _role(text: str, nickname: str | None) -> str:
    haystack = f"{text}\n{nickname or ''}".lower()
    if "spec compliance" in haystack or "spec reviewer" in haystack:
        return "spec_reviewer"
    if "code quality" in haystack or "quality reviewer" in haystack:
        return "code_quality_reviewer"
    if "final code reviewer" in haystack or "final reviewer" in haystack:
        return "final_reviewer"
    if "fix" in haystack or "修复" in haystack:
        return "fixer"
    if "implement" in haystack or "implementer" in haystack or "实现" in haystack:
        return "implementer"
    return "generic"


def _workflow_kind(items: list[Any]) -> str:
    return "plan_task_review" if PLAN_TASK_REVIEW_RE.search(_text(items)) else "one_shot"


def _has_more_plan_tasks(current_task_key: str | None) -> bool:
    # Task discovery is wired in Task 7 from plan context. Default false keeps this parser conservative.
    return False


def classify_spawn_request(args: Mapping[str, Any]) -> SpawnRequest:
    prompt = str(args.get("message") or "")
    nickname = args.get("nickname")
    nickname = nickname if isinstance(nickname, str) and nickname else None
    return SpawnRequest(
        role=_role(prompt, nickname),
        task_key=_task_key(prompt),
        spawn_prompt=prompt,
        nickname=nickname,
        retry_requested=RETRY_RE.search(prompt) is not None,
    )


def build_subagent_state(input_items: Any) -> SubagentState:
    items = input_items if isinstance(input_items, list) else []
    spawn_calls: dict[str, dict[str, Any]] = {}
    children_by_call_id: dict[str, SubagentChild] = {}
    wait_completed_ids: set[str] = set()
    closed_ids: set[str] = set()
    result_text_by_agent: dict[str, str] = {}
    wait_call_ids_by_agent: dict[str, str] = {}
    close_call_ids_by_agent: dict[str, str] = {}
    close_targets_by_call_id: dict[str, str] = {}
    latest_spawn_output_index = -1
    latest_append_message_index = -1
    spawn_intent_seen = SPAWN_INTENT_RE.search(_text(items)) is not None

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message" and APPEND_RE.search(_text(item.get("content"))):
            latest_append_message_index = index
        if item_type == "function_call":
            call_id = item.get("call_id")
            tool_name = _tool_name(item)
            args = _json_object(item.get("arguments"))
            if isinstance(call_id, str) and tool_name == "spawn_agent":
                spawn_calls[call_id] = {"args": args, "index": index}
            if isinstance(call_id, str) and tool_name == "wait_agent":
                for agent_id in args.get("targets") or []:
                    if isinstance(agent_id, str):
                        wait_call_ids_by_agent[agent_id] = call_id
            if isinstance(call_id, str) and tool_name == "close_agent":
                target = args.get("target")
                if isinstance(target, str):
                    close_call_ids_by_agent[target] = call_id
                    close_targets_by_call_id[call_id] = target
            continue
        if item_type != "function_call_output":
            continue
        call_id = item.get("call_id")
        if not isinstance(call_id, str):
            continue
        output = _json_object(item.get("output"))
        spawn = spawn_calls.get(call_id)
        if spawn is not None:
            args = spawn["args"]
            agent_id = output.get("agent_id")
            nickname = output.get("nickname") or args.get("nickname")
            if isinstance(agent_id, str) and agent_id:
                request = classify_spawn_request(args)
                children_by_call_id[call_id] = SubagentChild(
                    call_id=call_id,
                    agent_id=agent_id,
                    spawn_prompt=str(args.get("message") or ""),
                    nickname=nickname if isinstance(nickname, str) and nickname else None,
                    spawn_index=int(spawn["index"]),
                    role=request.role,
                    task_key=request.task_key,
                    spawn_signature=request.signature,
                )
                latest_spawn_output_index = index
            continue
        status = output.get("status")
        if isinstance(status, dict):
            for agent_id, value in status.items():
                if isinstance(agent_id, str) and isinstance(value, dict) and "completed" in value:
                    wait_completed_ids.add(agent_id)
                    result_text_by_agent[agent_id] = _text(value.get("completed"))
        previous_status = output.get("previous_status")
        if isinstance(previous_status, dict) and call_id in close_targets_by_call_id:
            closed_ids.add(close_targets_by_call_id[call_id])

    children = []
    for child in children_by_call_id.values():
        children.append(
                SubagentChild(
                    call_id=child.call_id,
                    agent_id=child.agent_id,
                    spawn_prompt=child.spawn_prompt,
                    nickname=child.nickname,
                    spawn_index=child.spawn_index,
                    role=child.role,
                    task_key=child.task_key,
                    spawn_signature=child.spawn_signature,
                    wait_call_id=wait_call_ids_by_agent.get(child.agent_id or ""),
                    close_call_id=close_call_ids_by_agent.get(child.agent_id or ""),
                    result_text=result_text_by_agent.get(child.agent_id or ""),
                    wait_completed=bool(child.agent_id and child.agent_id in wait_completed_ids),
                    closed=bool(child.agent_id and child.agent_id in closed_ids),
                )
        )

    children.sort(key=lambda child: child.spawn_index)
    append_requested = latest_append_message_index > latest_spawn_output_index >= 0
    return SubagentState(
        children=tuple(children),
        requested_count=_requested_count(items),
        append_spawn_requested=append_requested,
        spawn_intent_seen=spawn_intent_seen,
        workflow_kind=_workflow_kind(items),
    )
```

- [ ] **Step 5: Wire state parser into `codex_proxy.py`**

At the top of `src-python/codex_proxy.py`, import:

```python
from subagent_state import build_subagent_state
```

In `compatible_request_body()`, after `input_items = payload.get("input")`, compute:

```python
    subagent_state = build_subagent_state(input_items)
```

Replace existing state lists:

```python
    spawned_agent_ids = list(subagent_state.spawned_agent_ids)
    open_agent_ids = list(subagent_state.open_agent_ids)
    completed_wait_agent_ids = set(agent_id for agent_id in subagent_state.spawned_agent_ids if agent_id not in subagent_state.wait_agent_ids)
    closed_agent_ids = list(subagent_state.closed_agent_ids)
    wait_agent_ids = list(subagent_state.wait_agent_ids)
    close_agent_ids = list(subagent_state.close_agent_ids)
    has_open_agent = subagent_state.has_open_agents
    requested_spawn_count = subagent_state.requested_count
    lifecycle_complete = subagent_state.lifecycle_complete
    next_subagent_action = subagent_state.next_action
```

Then set:

```python
    append_spawn_required = subagent_state.append_spawn_requested and len(subagent_state.spawned_agent_ids) == len(spawned_agent_ids)
```

Update `spawn_more_required`:

```python
    spawn_more_required = (
        requested_spawn_count is not None and len(spawned_agent_ids) < requested_spawn_count
    ) or append_spawn_required
```

When `lifecycle_complete` is true, remove every `multi_agent_v1__*` tool from the payload and inject only final-answer guidance:

```python
if lifecycle_complete:
    tools = [
        tool for tool in tools
        if not (isinstance(tool, dict) and str(tool.get("name", "")).startswith("multi_agent_v1__"))
    ]
    _inject_developer_message(
        payload,
        "Codex native subagent lifecycle is complete. required_next_action: write the final concise report now. Do not call spawn_agent, wait_agent, close_agent, resume_agent, or send_input.",
    )
```

Use the existing local helper for developer-message injection if its name differs.

For non-complete lifecycle states, expose only the tool matching `next_subagent_action`:

```python
allowed_multi_agent_tools = {
    "spawn": {"multi_agent_v1__spawn_agent"},
    "wait": {"multi_agent_v1__wait_agent"},
    "close": {"multi_agent_v1__close_agent"},
    "send_input": {"multi_agent_v1__send_input"},
    "resume": {"multi_agent_v1__resume_agent"},
    "final": set(),
    "none": {"multi_agent_v1__spawn_agent"},
}[next_subagent_action]
tools = [
    tool for tool in tools
    if not (
        isinstance(tool, dict)
        and str(tool.get("name", "")).startswith("multi_agent_v1__")
        and tool.get("name") not in allowed_multi_agent_tools
    )
]
```

Inject a compact coordinator state message that names the next action:

```python
ACTION_TOOL_NAME = {
    "spawn": "spawn_agent",
    "wait": "wait_agent",
    "close": "close_agent",
    "send_input": "send_input",
    "resume": "resume_agent",
    "final": "final_answer",
    "none": "none",
}
_inject_developer_message(
    payload,
    (
        "Codex native subagent coordinator state: "
        f"requested_count={subagent_state.effective_requested_count}; "
        f"spawned_agent_ids={list(subagent_state.spawned_agent_ids)}; "
        f"wait_agent_ids={list(subagent_state.wait_agent_ids)}; "
        f"close_agent_ids={list(subagent_state.close_agent_ids)}; "
        f"closed_agent_ids={list(subagent_state.closed_agent_ids)}; "
        f"required_next_action: call {ACTION_TOOL_NAME[next_subagent_action]}."
    ),
)
```

For `next_subagent_action == "spawn"`, the state message must include the next spawn index and, when available, the corresponding prompt/nickname from the user request so the model can spawn the second child without inventing a task.

- [ ] **Step 6: Run state and existing multi-agent tests**

Run:

```powershell
$env:PYTHONPATH='src-python'
python -m unittest tests.test_routing.SubagentStateTests -v
python -m unittest tests.test_routing.RoutingTests.test_external_request_bounded_multi_spawn_allows_next_spawn_before_wait -v
python -m unittest tests.test_routing.RoutingTests.test_external_request_single_loop_hides_send_input_after_spawn_result -v
python -m unittest tests.test_routing.RoutingTests.test_external_request_hides_multi_agent_tools_after_single_loop_close -v
```

Expected: PASS.

- [ ] **Step 7: Commit state parser**

Run:

```powershell
git add src-python/subagent_state.py src-python/codex_proxy.py tests/test_routing.py
git commit -m "feat: track subagent lifecycle state in gateway"
```

---

### Task 7: Add Compatibility Coordinator Guard For Duplicate Spawn Calls

**Files:**
- Modify: `src-python/codex_proxy.py`
- Modify: `tests/test_routing.py`

- [ ] **Step 1: Add duplicate spawn response tests**

Add to `tests/test_routing.py`:

```python
    def test_text_compat_rewrites_duplicate_spawn_call_to_wait_existing_agent(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_duplicate_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return child-ok"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(
            body,
            "ollama_cloud",
            event_context={
                "request_id": "req",
                "tool_protocol": "text_compat",
                "subagent_open_agent_ids": ["019f-child"],
                "subagent_spawn_allowed": False,
            },
        )
        call = json.loads(transformed)["output"][0]

        self.assertEqual(call["name"], "wait_agent")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(json.loads(call["arguments"])["targets"], ["019f-child"])
        self.assertEqual(json.loads(call["arguments"])["timeout_ms"], 60000)

    def test_text_compat_allows_append_spawn_when_state_allows_spawn(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_append_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return child-b"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(
            body,
            "ollama_cloud",
            event_context={
                "request_id": "req",
                "tool_protocol": "text_compat",
                "subagent_open_agent_ids": ["019f-child-a"],
                "subagent_spawn_allowed": True,
            },
        )
        call = json.loads(transformed)["output"][0]

        self.assertEqual(call["name"], "spawn_agent")
        self.assertEqual(call["namespace"], "multi_agent_v1")

    def test_text_compat_suppresses_spawn_after_lifecycle_complete(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_repeated_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return child-again"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(
            body,
            "ollama_cloud",
            event_context={
                "request_id": "req",
                "tool_protocol": "text_compat",
                "subagent_open_agent_ids": [],
                "subagent_spawn_allowed": False,
                "subagent_lifecycle_complete": True,
            },
        )
        payload = json.loads(transformed)
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("spawn_agent", transcript)
        self.assertIn("required_next_action", transcript)
        self.assertIn("final", transcript.lower())
```

- [ ] **Step 2: Run duplicate spawn tests to verify they fail**

Run:

```powershell
$env:PYTHONPATH='src-python'
python -m unittest tests.test_routing.RoutingTests.test_text_compat_rewrites_duplicate_spawn_call_to_wait_existing_agent -v
python -m unittest tests.test_routing.RoutingTests.test_text_compat_allows_append_spawn_when_state_allows_spawn -v
python -m unittest tests.test_routing.RoutingTests.test_text_compat_suppresses_spawn_after_lifecycle_complete -v
```

Expected: FAIL because no response-side duplicate spawn guard exists.

- [ ] **Step 3: Store subagent state in response event context**

In `compatible_request_body()`, after `subagent_state` is computed, mutate the event context only when it is a dict:

```python
    if isinstance(event_context, dict):
        event_context["tool_protocol"] = tool_protocol
        event_context["subagent_open_agent_ids"] = list(subagent_state.open_agent_ids)
        event_context["subagent_spawn_allowed"] = bool(subagent_state.should_allow_spawn)
        event_context["subagent_lifecycle_complete"] = bool(subagent_state.lifecycle_complete)
```

- [ ] **Step 4: Add duplicate spawn guard**

In `src-python/codex_proxy.py`, add:

```python
def _guard_duplicate_multi_agent_spawn_calls(value: Any, event_context: Mapping[str, Any] | None) -> tuple[Any, bool]:
    if not isinstance(value, dict):
        return value, False
    tool_protocol = str((event_context or {}).get("tool_protocol") or "")
    if tool_protocol not in {"text_compat", "chat_tools"}:
        return value, False
    open_agent_ids = (event_context or {}).get("subagent_open_agent_ids")
    spawn_allowed = bool((event_context or {}).get("subagent_spawn_allowed"))
    lifecycle_complete = bool((event_context or {}).get("subagent_lifecycle_complete"))
    if spawn_allowed:
        return value, False
    changed = False
    rewritten = json.loads(json.dumps(value))
    for item in _iter_dict_items(rewritten):
        if item.get("type") != "function_call":
            continue
        name = item.get("name")
        if name not in {"multi_agent_v1__spawn_agent", "spawn_agent"}:
            continue
        if lifecycle_complete:
            item.clear()
            item.update(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "required_next_action: write the final concise report now. The requested subagent lifecycle is already complete.",
                }
            )
            changed = True
            continue
        if not isinstance(open_agent_ids, list) or not open_agent_ids:
            continue
        item["namespace"] = "multi_agent_v1"
        item["name"] = "wait_agent"
        item["arguments"] = json.dumps({"targets": open_agent_ids, "timeout_ms": 60000}, ensure_ascii=True, separators=(",", ":"))
        changed = True
    return rewritten, changed
```

If `_iter_dict_items()` does not exist, add:

```python
def _iter_dict_items(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dict_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dict_items(child)
```

- [ ] **Step 5: Call the guard in body and SSE adapters**

In `compatible_response_body()`, after `_downgrade_invalid_third_party_tool_calls(payload)`, add:

```python
    payload, duplicate_spawn_changed = _guard_duplicate_multi_agent_spawn_calls(payload, event_context)
    changed = changed or duplicate_spawn_changed
```

In `compatible_sse_line()`, after `_downgrade_invalid_third_party_tool_calls(payload)`, add the same two lines.

- [ ] **Step 6: Run duplicate spawn tests and multi-agent regression tests**

Run:

```powershell
python -m unittest tests.test_routing.RoutingTests.test_text_compat_rewrites_duplicate_spawn_call_to_wait_existing_agent -v
python -m unittest tests.test_routing.RoutingTests.test_text_compat_allows_append_spawn_when_state_allows_spawn -v
python -m unittest tests.test_routing.RoutingTests.test_external_response_normalizes_multi_agent_wait_alias_and_arguments -v
```

Expected: PASS.

- [ ] **Step 7: Commit duplicate spawn guard**

Run:

```powershell
git add src-python/codex_proxy.py tests/test_routing.py
git commit -m "fix: guard duplicate compatibility subagent spawns"
```

---

### Task 8: Wire Tool Protocol Through Rust Config And Frontend

**Files:**
- Modify: `src-tauri/src/main.rs`
- Modify: `src-tauri/src/config.rs`
- Modify: `src-tauri/src/gateway.rs`
- Modify: `frontend/src/pages/ProvidersPage.tsx`
- Modify: `frontend/src/lib/types.ts`

- [ ] **Step 1: Add Rust enum and provider field**

In `src-tauri/src/main.rs`, add:

```rust
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ToolProtocol {
    Auto,
    ResponsesStructured,
    ChatTools,
    TextCompat,
    None,
}
```

Add to `Provider`:

```rust
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_protocol: Option<ToolProtocol>,
```

- [ ] **Step 2: Update config TOML roundtrip test**

In `src-tauri/src/config.rs::tests::providers_toml_roundtrip_preserves_all_provider_and_model_fields`, set:

```rust
            tool_protocol: Some(ToolProtocol::ChatTools),
```

Assert:

```rust
        assert!(written.contains("tool_protocol = \"chat_tools\""));
        assert_eq!(loaded[0].tool_protocol, Some(ToolProtocol::ChatTools));
```

- [ ] **Step 3: Implement Rust TOML read/write**

In `src-tauri/src/config.rs`, include `tool_protocol` wherever `upstream_format` and `available_upstream_formats` are serialized/deserialized:

```rust
tool_protocol: provider.tool_protocol.clone(),
```

and when writing:

```rust
if let Some(tool_protocol) = &provider.tool_protocol {
    lines.push(format!("tool_protocol = \"{}\"", toml_enum_value(tool_protocol)));
}
```

Use the same enum string helper pattern already used for `UpstreamFormat`.

- [ ] **Step 4: Update provider probe application in frontend**

In `frontend/src/pages/ProvidersPage.tsx`, when applying `probe_upstream_format` result, set:

```ts
tool_protocol: result.recommended_tool_protocol,
```

Apply this in both create-provider and edit-provider flows where `upstream_format` and `available_upstream_formats` are currently updated.

- [ ] **Step 5: Add compact capability label**

In `ProvidersPage.tsx`, add:

```ts
function toolProtocolLabel(value?: ToolProtocol | null) {
  if (value === "responses_structured") return "Structured Responses tools";
  if (value === "chat_tools") return "Chat tool calls";
  if (value === "text_compat") return "Gateway compatibility";
  if (value === "none") return "Tools unavailable";
  return "Auto";
}
```

Render this near endpoint selection:

```tsx
<span className="text-xs text-slate-500">{toolProtocolLabel(form.tool_protocol)}</span>
```

- [ ] **Step 6: Run frontend and Rust checks**

Run:

```powershell
cargo test --manifest-path src-tauri/Cargo.toml config::tests::providers_toml_roundtrip_preserves_all_provider_and_model_fields -- --nocapture
npm --prefix frontend run build
```

Expected: PASS.

- [ ] **Step 7: Commit Rust/frontend capability wiring**

Run:

```powershell
git add src-tauri/src/main.rs src-tauri/src/config.rs src-tauri/src/gateway.rs frontend/src/pages/ProvidersPage.tsx frontend/src/lib/types.ts
git commit -m "feat: wire provider tool protocol capability"
```

---

### Task 9: Full Regression And Live Test Prompts

**Files:**
- Modify: `docs/superpowers/plans/2026-07-05-glm52-subagent-live-regression.md`

- [ ] **Step 1: Add live validation matrix to the existing regression doc**

Append:

```markdown
## Protocol Matrix Validation

Run the same native subagent prompt across:

- `responses_structured`: `ollama-cloud/glm-5.2` through `POST http://127.0.0.1:9099/v1/providers/ollama-cloud/responses` with `tool_protocol = "responses_structured"`.
- `chat_tools`: the same `ollama-cloud/glm-5.2` through `POST http://127.0.0.1:9099/v1/providers/ollama-cloud/chat/completions` with `tool_protocol = "chat_tools"`.
- `text_compat`: the same `ollama-cloud/glm-5.2` forced to `tool_protocol = "text_compat"` for compatibility stress testing.

Expected observations:

- Responses structured: session JSONL keeps `function_call` and `function_call_output` items, not `Codex native multi_agent...` text messages.
- Chat tools: upstream request contains `assistant.tool_calls` followed by `role: tool` messages with matching `tool_call_id`.
- Text compat: duplicate `spawn_agent` attempts while an agent is open are rewritten to `wait_agent` for the existing `agent_id`.
```

- [ ] **Step 2: Run Python tests**

Run:

```powershell
python -m unittest discover -s tests -q
```

Expected: all tests pass.

- [ ] **Step 3: Run frontend build**

Run:

```powershell
npm --prefix frontend run build
```

Expected: build succeeds.

- [ ] **Step 4: Run Rust tests scoped to changed modules**

Run:

```powershell
cargo test --manifest-path src-tauri/Cargo.toml config::tests:: gateway::tests:: models::tests:: -- --nocapture
```

Expected: tests pass.

- [ ] **Step 5: Commit docs and final regression**

Run:

```powershell
git add docs/superpowers/plans/2026-07-05-glm52-subagent-live-regression.md
git commit -m "docs: add subagent protocol validation matrix"
```

---

### Task 10: Real Codex CLI E2E With External LLMs

**Files:**
- Create: `diagnostics/subagent-e2e/`
- No committed source changes unless the test exposes a bug.

- [ ] **Step 1: Verify Gateway and candidate models are available**

Run:

```powershell
$models = (Invoke-RestMethod -Uri 'http://127.0.0.1:9099/v1/models' -TimeoutSec 10).data
$models |
  Where-Object { $_.id -eq 'ollama-cloud/glm-5.2' } |
  Select-Object id, name, provider, model |
  Format-Table -AutoSize
```

Expected: `ollama-cloud/glm-5.2` is present. This is the primary E2E model for both Responses and Chat-path validation; use other providers only as fallback evidence if Ollama is unavailable.

- [ ] **Step 2: Create diagnostics output directory**

Run:

```powershell
New-Item -ItemType Directory -Force 'diagnostics/subagent-e2e' | Out-Null
```

Expected: directory exists. Do not commit files under `diagnostics/`.

- [ ] **Step 3: Run real Codex CLI E2E for the Responses-structured route**

Run this with `ollama-cloud/glm-5.2`:

```powershell
$model = 'ollama-cloud/glm-5.2'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$out = "diagnostics/subagent-e2e/responses-structured-$stamp.jsonl"
$final = "diagnostics/subagent-e2e/responses-structured-$stamp.final.txt"
$prompt = @'
请执行一次真实 Codex native subagent 协作测试。

你的角色是 coordinator。你必须调用真实 Codex native subagent 工具，不要用 shell、文件读取、tool_search 或文字模拟替代。

步骤：
1. spawn 一个子代理，子代理 prompt 必须是：只返回这一行：SENTINEL:e2e-responses-subagent-ok-20260705
2. wait 这个子代理。
3. close 这个子代理。

最终只返回：
SPAWNED: yes/no
AGENT_ID: <id>
SENTINEL_SEEN: yes/no
CLOSED: yes/no
'@

& codex exec `
  -m $model `
  --json `
  --dangerously-bypass-approvals-and-sandbox `
  --cd C:\Users\noirb\.codex\worktrees\f11c\CodexHub `
  --output-last-message $final `
  $prompt *> $out

$LASTEXITCODE
Get-Content $final -Raw
```

Expected: exit code `0`, final output says `SPAWNED: yes`, includes one `AGENT_ID`, says `SENTINEL_SEEN: yes`, and says `CLOSED: yes`.

- [ ] **Step 4: Run real Codex CLI E2E for the Chat-tools or text-compat route**

Run this with the same `ollama-cloud/glm-5.2` model. Task 1A covers the direct `POST http://127.0.0.1:9099/v1/providers/ollama-cloud/chat/completions` Chat flattening E2E; this `codex exec` step verifies the configured Gateway route that Codex actually selects. For compatibility stress testing, temporarily set the same Ollama provider to `tool_protocol = "text_compat"` in the runtime provider config, run this step, then restore the config.

```powershell
$model = 'ollama-cloud/glm-5.2'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$out = "diagnostics/subagent-e2e/chat-or-compat-$stamp.jsonl"
$final = "diagnostics/subagent-e2e/chat-or-compat-$stamp.final.txt"
$prompt = @'
请执行一次真实 Codex native subagent 综合协作测试。

你的角色是 coordinator。你必须调用真实 Codex native subagent 工具，不要用 shell、文件读取、tool_search 或文字模拟替代。

场景 A：
1. spawn 一个子代理，子代理 prompt 必须是：只返回这一行：SENTINEL:e2e-single-ok-20260705
2. wait 这个子代理。
3. close 这个子代理。

场景 B：
1. spawn 两个子代理。
2. 第一个子代理 prompt 必须是：只返回这一行：SENTINEL:e2e-multi-a-ok-20260705
3. 第二个子代理 prompt 必须是：只返回这一行：SENTINEL:e2e-multi-b-ok-20260705
4. 等两个子代理都 spawn 后，wait 两个 agent id。
5. close 两个 agent id。

最终只返回：
SCENARIO_A_SPAWNED: yes/no
SCENARIO_A_SENTINEL: yes/no
SCENARIO_A_CLOSED: yes/no
SCENARIO_B_AGENT_IDS: <id1>, <id2>
SCENARIO_B_SENTINELS: yes/no
SCENARIO_B_CLOSED: yes/no
EXTRA_SPAWN: yes/no
'@

& codex exec `
  -m $model `
  --json `
  --dangerously-bypass-approvals-and-sandbox `
  --cd C:\Users\noirb\.codex\worktrees\f11c\CodexHub `
  --output-last-message $final `
  $prompt *> $out

$LASTEXITCODE
Get-Content $final -Raw
```

Expected: exit code `0`; scenario A and B close successfully; both B sentinels are seen; `EXTRA_SPAWN: no`.

- [ ] **Step 5: Inspect CLI JSONL for real tool calls**

Run:

```powershell
Get-ChildItem 'diagnostics/subagent-e2e/*.jsonl' |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 2 |
  ForEach-Object {
    "`n=== $($_.Name) ==="
    Select-String -Path $_.FullName -Pattern 'multi_agent_v1|spawn_agent|wait_agent|close_agent|SENTINEL:e2e' |
      Select-Object -First 80 |
      ForEach-Object { $_.Line }
  }
```

Expected: each run contains real `spawn_agent`, `wait_agent`, and `close_agent` events or their normalized aliases. If the final answer claims success but JSONL has no real tool call evidence, the E2E test fails.

- [ ] **Step 6: Inspect Gateway proxy events**

Run:

```powershell
$eventsPath = 'C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl'
Get-Content $eventsPath -Tail 8000 |
  ForEach-Object {
    try { $_ | ConvertFrom-Json } catch { $null }
  } |
  Where-Object {
    $_ -and (
      $_.event -match 'multi_agent|tool_call|explicit_codex_tools|request_' -or
      $_.model -eq 'ollama-cloud/glm-5.2'
    )
  } |
  Select-Object event, model, upstream, upstream_format, added_tool_names, wait_agent_ids, close_agent_ids, closed_agent_ids, lifecycle_complete |
  Format-Table -AutoSize
```

Expected:

- Responses-structured run does not rely on `Codex native multi_agent... result` text guidance for normal tool history.
- Chat-tools run shows converted tool history with matching tool call IDs when inspected through upstream request captures or debug logs.
- Text-compat run shows state guidance and duplicate spawn guard only when the compatibility path is selected.

- [ ] **Step 7: Inspect session JSONL for duplicate spawns**

Run:

```powershell
$sessionsRoot = 'C:\Users\noirb\.codex\sessions'
Get-ChildItem $sessionsRoot -Recurse -Filter '*.jsonl' |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 5 |
  ForEach-Object {
    $text = Get-Content $_.FullName -Raw
    $spawnCount = ([regex]::Matches($text, 'spawn_agent')).Count
    $waitCount = ([regex]::Matches($text, 'wait_agent')).Count
    $closeCount = ([regex]::Matches($text, 'close_agent')).Count
    [pscustomobject]@{
      file = $_.FullName
      spawn_agent = $spawnCount
      wait_agent = $waitCount
      close_agent = $closeCount
      has_e2e_sentinel = $text.Contains('SENTINEL:e2e')
    }
  } |
  Format-Table -AutoSize
```

Expected: the newest E2E sessions contain sentinel text and the spawn count matches the prompt intent: one spawn for single-agent scenarios, two spawns for bounded multi-agent scenarios, and no extra repeated spawn after close.

- [ ] **Step 8: Record E2E result summary**

Create `diagnostics/subagent-e2e/summary-<timestamp>.md` with:

```markdown
# Subagent E2E Summary

- Date:
- Gateway PID:
- Gateway source path:
- Responses model tested:
- Chat/compat model tested:
- CLI command exit codes:
- Agent ids observed:
- Sentinels observed:
- Extra spawn observed:
- Proxy event evidence:
- Session JSONL evidence:
- Failures or skipped provider classes:
```

Expected: the summary contains enough evidence to distinguish unit-test success from real Codex CLI + real LLM behavior. Do not commit the summary unless explicitly requested.

---

## Self-Review

**Spec coverage:** This plan covers both user-requested tracks: a Spike for supported Responses endpoints to determine and preserve lightweight normalization, a real Chat Completions flattening E2E spike to prove flat tool aliases are viable, fixes for unsupported Responses paths through Chat tools and text compatibility coordinator behavior, and real Codex CLI E2E validation against actual external LLMs.

**Placeholder scan:** No task uses an unresolved placeholder, unspecified error handling, or a generic test-writing instruction without concrete test code.

**Type consistency:** The provider field is consistently named `tool_protocol`; protocol values are `auto`, `responses_structured`, `chat_tools`, `text_compat`, and `none` across Python, Rust, and TypeScript.

## Execution Choice

Plan complete and saved to `docs/superpowers/plans/2026-07-05-subagent-protocol-spike-and-fix.md`. Two execution options:

**1. Subagent-Driven (recommended)** - dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** - execute tasks in this session using executing-plans, batch execution with checkpoints.

## Inline Implementation Status

Updated on 2026-07-05 after inline execution:

- Implemented protocol split in `src-python/codex_proxy.py`: `responses_structured` and `chat_tools` preserve structured tool history; `text_compat` keeps the Gateway coordinator guidance; `none` removes Codex native tools.
- Added `src-python/subagent_state.py` as the event-led coordinator. It tracks `agent_id`, `call_id`, prompt/nickname, role/task signature, bounded requested count, wait/close state, reviewer-to-implementer fix routing, and implementation epochs.
- Added compatibility response guard for repeated `spawn_agent` calls. In `text_compat`/`chat_tools`, duplicate spawn while an agent is open is rewritten to `wait_agent`; duplicate spawn after lifecycle completion is replaced with final-answer guidance.
- Added provider `tool_protocol` persistence and probe recommendation through Python config/catalog/probe, Rust/Tauri provider config, and the React provider UI.
- Verified with focused Python, Rust, frontend contract, and frontend build checks. Real Codex CLI E2E remains a separate live validation step because it depends on the currently running Gateway/provider configuration.
