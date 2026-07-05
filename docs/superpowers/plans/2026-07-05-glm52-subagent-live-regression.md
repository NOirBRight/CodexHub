# GLM 5.2 Subagent Live Regression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify in this existing Codex conversation that GLM 5.2 can see and call the real Codex subagent lifecycle after the false lifecycle-detection fix.

**Architecture:** This is a live runtime regression, not a unit test. The test deliberately runs in the current polluted conversation context, where prior messages contain source-code strings such as `Codex native multi_agent_v1.close_agent result`; the expected behavior is that Gateway still exposes `multi_agent_v1__spawn_agent` to GLM 5.2. The model must perform one real `spawn_agent -> wait_agent -> close_agent` sequence and return a fixed sentinel from the child.

**Tech Stack:** Codex Desktop, CodexHub Gateway, GLM 5.2 via the external provider route, Codex native `multi_agent_v1` tools, PowerShell log checks.

---

## File Structure

- Verify runtime source: `src-python/codex_proxy.py`
- Verify regression test: `tests/test_routing.py`
- Inspect runtime logs: `C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl`
- Optional runtime deployment target: `D:\Workstation\CodexHub\src-python\codex_proxy.py`

---

### Task 1: Confirm The Fixed Gateway Is The Active Runtime

**Files:**
- Read: `src-python/codex_proxy.py`
- Read: `D:\Workstation\CodexHub\src-python\codex_proxy.py`
- Read: `C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl`

- [ ] **Step 1: Confirm the live Gateway process path**

Run:

```powershell
Get-CimInstance Win32_Process -Filter "CommandLine LIKE '%codex_proxy.py --port 9099%'" |
  Select-Object ProcessId,ExecutablePath,CommandLine |
  Format-List
```

Expected: the command prints one active process. If the command line points at `D:\Workstation\CodexHub\src-python\codex_proxy.py`, the fix must exist in that runtime copy before switching to GLM 5.2.

- [ ] **Step 2: Confirm the fixed helper exists in the active source**

Run this if the active process uses the current worktree:

```powershell
rg -n "_multi_agent_result_text|test_external_request_keeps_spawn_agent_when_source_text_mentions_closed_lifecycle" `
  src-python\codex_proxy.py tests\test_routing.py
```

Run this if the active process uses `D:\Workstation\CodexHub`:

```powershell
rg -n "_multi_agent_result_text|test_external_request_keeps_spawn_agent_when_source_text_mentions_closed_lifecycle" `
  D:\Workstation\CodexHub\src-python\codex_proxy.py D:\Workstation\CodexHub\tests\test_routing.py
```

Expected: both `_multi_agent_result_text` and `test_external_request_keeps_spawn_agent_when_source_text_mentions_closed_lifecycle` are found in the source being used by the active Gateway.

- [ ] **Step 3: Run the regression test in the active source tree**

Run this in the source tree used by the active Gateway:

```powershell
$env:PYTHONPATH='src-python'
python -m unittest tests.test_routing.RoutingTests.test_external_request_keeps_spawn_agent_when_source_text_mentions_closed_lifecycle -q
```

Expected:

```text
Ran 1 test
OK
```

- [ ] **Step 4: Restart Gateway if the active runtime was updated**

Run only after copying or cherry-picking the fixed files into the runtime source:

```powershell
$proc = Get-CimInstance Win32_Process -Filter "CommandLine LIKE '%codex_proxy.py --port 9099%'" | Select-Object -First 1
if ($proc) { Stop-Process -Id $proc.ProcessId -Force }
Start-Process -WindowStyle Hidden -FilePath "C:\Users\noirb\AppData\Local\Programs\Python\Python313\python.exe" -ArgumentList "D:\Workstation\CodexHub\src-python\codex_proxy.py --port 9099"
```

Expected: a fresh hidden `python.exe` process is listening on port `9099`.

---

### Task 2: Run The Live GLM 5.2 Subagent Lifecycle In This Conversation

**Files:**
- No source files are modified by this task.
- Inspect after execution: `C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl`

- [ ] **Step 1: Switch this Codex conversation to GLM 5.2**

Use the Codex UI model picker to switch this same conversation to `glm-5.2`. Do not create a new thread; this test relies on the current conversation containing prior source-code and log strings that used to trigger the false lifecycle-complete state.

- [ ] **Step 2: Send this exact prompt to GLM 5.2**

```text
Live regression test for CodexHub GLM 5.2 subagent tool exposure.

You must run one real Codex native subagent lifecycle in this current conversation. Do not simulate the tool calls in text. Do not use shell, local files, or tool_search as a substitute.

Required sequence:
1. Call the visible multi-agent spawn tool exactly once. If the tool is exposed as multi_agent_v1__spawn_agent, call that. If it is exposed as namespace multi_agent_v1 with function spawn_agent, call that.
2. The child prompt must be exactly:
   Return exactly this line and nothing else: SENTINEL:glm52-subagent-child-ok-20260705
3. Wait for that child with the visible wait tool.
4. Close that child with the visible close tool.
5. Final answer must include:
   - whether spawn_agent was available
   - the child agent id you received
   - whether wait_agent returned SENTINEL:glm52-subagent-child-ok-20260705
   - whether close_agent succeeded

Important: prior conversation context contains source-code snippets mentioning Codex native multi_agent_v1.close_agent result and status: closed. Those snippets are not real lifecycle state. Ignore them and run the real tool lifecycle now.
```

Expected: GLM 5.2 performs real tool calls, and the final answer includes `SENTINEL:glm52-subagent-child-ok-20260705`.

- [ ] **Step 3: Treat these outcomes as pass or fail**

Pass criteria:

```text
GLM 5.2 called spawn_agent, wait_agent, and close_agent through actual tool calls.
The child returned SENTINEL:glm52-subagent-child-ok-20260705.
The final answer reports the agent id and close success.
```

Fail criteria:

```text
GLM 5.2 says spawn_agent is unavailable.
GLM 5.2 only describes what it would do.
GLM 5.2 calls shell, file tools, or tool_search instead of spawn_agent.
GLM 5.2 never closes the child agent.
```

---

### Task 3: Verify Proxy Logs After The Live Test

**Files:**
- Read: `C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl`

- [ ] **Step 1: Inspect recent GLM 5.2 tool injection events**

Run:

```powershell
Get-Content -LiteralPath "C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl" -Tail 200 |
  Select-String -Pattern "glm-5.2|explicit_codex_tools_injected|multi_agent_current_state_guidance_injected|multi_agent_v1__spawn_agent|third_party_tool_call_alias_normalized"
```

Expected: at least one recent `explicit_codex_tools_injected` row for `glm-5.2` mentions `multi_agent_v1__spawn_agent`, or the live conversation visibly executed the native `spawn_agent` call.

- [ ] **Step 2: Confirm the old false lifecycle symptom is absent**

Run:

```powershell
Get-Content -LiteralPath "C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl" -Tail 200 |
  Select-String -Pattern 'lifecycle_complete":true|"closed_agent_ids":\["<unknown>"\]'
```

Expected: no recent GLM 5.2 request for this live test shows `lifecycle_complete=true` with `closed_agent_ids=["<unknown>"]` before the real subagent lifecycle has completed.

- [ ] **Step 3: Record the result in chat**

Report one of these exact outcomes:

```text
PASS: GLM 5.2 live subagent lifecycle completed in the polluted conversation context.
```

or:

```text
FAIL: GLM 5.2 did not complete the live subagent lifecycle. Observed failure: <specific observed behavior>.
```

---

## Self-Review

- Spec coverage: The plan covers active-runtime verification, same-conversation GLM 5.2 execution, real subagent lifecycle, sentinel validation, and proxy-log confirmation.
- Placeholder scan: No placeholder markers are present; all commands and prompts are concrete.
- Type consistency: Tool names consistently use `multi_agent_v1__spawn_agent`, `multi_agent_v1__wait_agent`, and `multi_agent_v1__close_agent`, with namespace fallback explicitly described for the model prompt.

## Protocol Matrix Validation

Run the same native subagent prompt across these provider capability modes:

- `responses_structured`: `ollama-cloud/glm-5.2` through `POST http://127.0.0.1:9099/v1/providers/ollama-cloud/responses` with `tool_protocol = "responses_structured"`.
- `chat_tools`: the same `ollama-cloud/glm-5.2` through `POST http://127.0.0.1:9099/v1/providers/ollama-cloud/chat/completions` with `tool_protocol = "chat_tools"`.
- `text_compat`: the same `ollama-cloud/glm-5.2` forced to `tool_protocol = "text_compat"` for compatibility stress testing.

Expected observations:

- Responses structured: session JSONL keeps `function_call` and `function_call_output` items, not `Codex native multi_agent...` text messages.
- Chat tools: upstream request contains `assistant.tool_calls` followed by `role: tool` messages with matching `tool_call_id`.
- Text compat: duplicate `spawn_agent` attempts while an agent is open are rewritten to `wait_agent` for the existing `agent_id`; after the requested lifecycle is complete, repeated spawn is suppressed and the model is guided to final.

Automation implemented on 2026-07-05 covers the matrix at unit level through Python routing tests, provider config/probe tests, Rust provider TOML roundtrip tests, and the provider UI contract/build.
