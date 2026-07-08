# Subagent Repair Level 3 And Provider E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate native subagent repair behind the current route/repair matrix, keep Level 1 and Level 2 subagent gates green, pass Level 3 Dynamic DAG, and verify every currently enabled provider/model through Codex plus four external clients with real LLM calls.

**Architecture:** Start from `dev@6f6bfd69`, merge `codex/subagent-protocol-fix@7541ddc9` into an integration branch, and preserve the model-proxy route split: transparent third-party routes remain transport-only, while Codex subagent semantics run only when `repair_policy == "codex_subagent_repair"`. The E2E layer has two runners: the subagent runner proves Level 1/2/3 native lifecycle behavior, and the gateway client matrix runner proves text-generation availability across active provider/model/client combinations.

**Tech Stack:** Python 3.13 stdlib `unittest`, CodexHub Gateway `src-python/codex_proxy.py`, subagent modules from `codex/subagent-protocol-fix`, real Gateway HTTP/SSE calls, Codex CLI/Desktop smoke harness, Windows PowerShell, ZCode manual verification.

## Global Constraints

- Base branch is `dev` at merge commit `6f6bfd69 merge: model proxy runtime fixes`.
- Integrate `codex/subagent-protocol-fix` at `7541ddc9 Stabilize native subagent protocol validation`.
- Do not rebase inside `C:\Users\noirb\.codex\worktrees\6e95\CodexHub`; use a fresh branch in `D:\Workstation\CodexHub-dev`.
- `third_party_app_transparent_metered` must not run Codex subagent repair, Compact handling, browser guidance, synthetic Codex stream repair, or Codex tool injection.
- Subagent guidance/repair/resample may run only when `repair_policy == "codex_subagent_repair"` and `CODEXHUB_SUBAGENT_ASSIST_MODE` permits it.
- Preserve same-format transparent passthrough and conservative pre-output retry behavior from `dev@6f6bfd69`.
- Preserve image proxy overlay and provider retry/error classification from `dev@6f6bfd69`.
- Level 1 and Level 2 are regression gates; Level 3 Dynamic DAG is the completion gate.
- Provider/model E2E must make real LLM calls, not mocked provider responses.
- Do not print API keys, provider secrets, or raw config files in logs or reports.
- Automated client matrix covers `codex-app`, `pi`, `omp`, and `opencode`.
- ZCode matrix is manual-assisted: the user drives ZCode UI/config while the plan provides prompts, expected artifacts, and proxy-log checks.
- Current enabled provider/model snapshot from `C:\Users\noirb\.codex\proxy\config\providers.toml`:
  - `minimax-cn`: `MiniMax-M3`
  - `ollama-cloud`: `glm-5.2`, `kimi-k2.7-code`, `minimax-m3`, `deepseek-v4-pro`, `deepseek-v4-flash`
  - `volc`: `glm-5.2`, `minimax-m3`, `kimi-k2.6`
  - `xunfei`: `xopglm52`, `xopdeepseekv4pro`, `xopdeepseekv4flash`, `xopkimik26`

---

## File Structure

- Modify `src-python/codex_proxy.py`
  - Preserve model-proxy route decisions and relay behavior.
  - Import subagent protocol/policy/scheduler/dynamic-DAG modules.
  - Pass `repair_policy` into adapter event context.
  - Gate all subagent repair helpers on `repair_policy == "codex_subagent_repair"`.

- Add or keep `src-python/subagent_policy.py`
  - Own assist-mode policy and deterministic repair eligibility.
  - Add repair-policy awareness so transparent routes cannot accidentally repair.

- Add or keep `src-python/subagent_protocol.py`
  - Own native multi-agent lifecycle facts only.

- Add or keep `src-python/subagent_scheduler.py`
  - Own generic workflow legal-action computation.

- Add or keep `src-python/subagent_dynamic_dag.py`
  - Own Level 3 Dynamic DAG request detection, node materialization, and guidance text.

- Modify `src-python/subagent_state.py`
  - Wire protocol state, scheduler state, Level 2 ordered workflow, and Level 3 Dynamic DAG state together without moving workflow semantics into `subagent_protocol.py`.

- Modify `diagnostics/subagent-e2e/run_level12_e2e.py`
  - Keep Level 1/2 behavior.
  - Keep or add `--level level3 --workflow dynamic-dag`.
  - Emit per-case summaries that distinguish protocol defects, scheduler defects, provider flakes, and client/runtime defects.

- Modify `scripts/e2e_gateway_client_matrix.py`
  - Add `codex-app` cases from the active runtime provider config.
  - Keep existing `pi`, `omp`, `opencode`, and `zcode` config parsers.
  - Add a `--manual-client zcode` report mode for user-assisted ZCode verification.
  - Redact secrets in all generated artifacts.

- Create `docs/superpowers/runbooks/2026-07-08-zcode-manual-e2e.md`
  - Exact ZCode manual test steps, prompts, pass/fail criteria, and log commands.

- Modify tests:
  - `tests/test_routing.py`
  - `tests/test_chat_completions_gateway.py`
  - `tests/test_subagent_policy.py`
  - `tests/test_subagent_protocol.py`
  - `tests/test_subagent_scheduler.py`
  - `tests/test_subagent_dynamic_dag.py`
  - `tests/test_subagent_state.py`
  - `tests/test_level12_e2e_parser.py`

---

### Task 0: Create Integration Branch And Baseline

**Files:**
- Read: `D:\Workstation\CodexHub-dev`
- Modify: none

**Interfaces:**
- Consumes: `dev@6f6bfd69`, `codex/subagent-protocol-fix@7541ddc9`.
- Produces: branch `codex/subagent-repair-level3-provider-e2e`.

- [ ] **Step 1: Verify base state**

Run:

```powershell
git -C D:\Workstation\CodexHub-dev status --short --branch --untracked-files=no
git -C D:\Workstation\CodexHub-dev rev-parse dev
git -C D:\Workstation\CodexHub-dev rev-parse codex/subagent-protocol-fix
```

Expected:

```text
## dev...origin/dev [ahead 11]
The second command prints a SHA beginning with 6f6bfd69.
The third command prints a SHA beginning with 7541ddc9.
```

- [ ] **Step 2: Create the branch**

Run:

```powershell
git -C D:\Workstation\CodexHub-dev switch dev
git -C D:\Workstation\CodexHub-dev switch -c codex/subagent-repair-level3-provider-e2e
```

Expected:

```text
Switched to a new branch 'codex/subagent-repair-level3-provider-e2e'
```

- [ ] **Step 3: Record the active provider/model snapshot without secrets**

Run:

```powershell
@'
import tomllib
from pathlib import Path
p = Path(r"C:\Users\noirb\.codex\proxy\config\providers.toml")
data = tomllib.loads(p.read_text(encoding="utf-8"))
for provider in data.get("providers", []):
    if not provider.get("enabled", True):
        continue
    models = [
        model.get("id")
        for model in provider.get("models", [])
        if model.get("enabled", True) and model.get("gateway_exported", False)
    ]
    if models:
        print(f"{provider.get('id')}\t{provider.get('upstream_format') or 'auto'}\t{','.join(models)}")
'@ | python -
```

Expected:

```text
minimax-cn	responses	MiniMax-M3
ollama-cloud	responses	glm-5.2,kimi-k2.7-code,minimax-m3,deepseek-v4-pro,deepseek-v4-flash
volc	auto	glm-5.2,minimax-m3,kimi-k2.6
xunfei	responses	xopglm52,xopdeepseekv4pro,xopdeepseekv4flash,xopkimik26
```

- [ ] **Step 4: Commit nothing**

Run:

```powershell
git -C D:\Workstation\CodexHub-dev status --short --untracked-files=no
```

Expected: no output.

---

### Task 1: Merge Subagent Branch Without Changing Route Semantics

**Files:**
- Modify: `src-python/codex_proxy.py`
- Modify: `src-python/subagent_state.py`
- Add: `src-python/subagent_policy.py`
- Add: `src-python/subagent_protocol.py`
- Add: `src-python/subagent_scheduler.py`
- Add: `src-python/subagent_dynamic_dag.py`
- Modify: `tests/test_routing.py`
- Add/modify: subagent unit tests from `codex/subagent-protocol-fix`

**Interfaces:**
- Consumes: `RouteDecision.repair_policy: str`, `BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED`, `REPAIR_CODEX_SUBAGENT`, `REPAIR_NONE`.
- Produces: merged source where subagent modules are available and current route profiles are preserved.

- [ ] **Step 1: Start the merge**

Run:

```powershell
git -C D:\Workstation\CodexHub-dev merge --no-ff codex/subagent-protocol-fix
```

Expected: conflicts in `src-python/codex_proxy.py` and `tests/test_routing.py`.

- [ ] **Step 2: Resolve `src-python/codex_proxy.py` by preserving these current dev functions**

Keep these functions from current `dev` as the route/transport source of truth: `route_decision_for_request`, `transparent_request_body`, `vision_proxy_policy_for_route`, `_offer_usage_observed_body`, `_offer_usage_observed_sse_line`, `CodexProxyHandler._proxy_post_request`, `CodexProxyHandler._relay_upstream_response`, and `CodexProxyHandler._relay_transparent_upstream_response`.

Expected conflict resolution:

```python
elif is_transparent_same_format or is_transparent_lightweight_fallback:
    body = transparent_request_body(
        body,
        _safe_json_mapping(body),
        upstream,
        model_id=model,
    )
else:
    body = compatible_request_body(
        body,
        upstream,
        model_id=model,
        event_context=adapter_event_context,
        inject_codex_tools=request_kind != RETRY_REQUEST_COMPACT,
        behavior_profile=behavior_profile,
    )
```

- [ ] **Step 3: Keep subagent imports from the subagent branch**

Add or preserve these imports near the existing provider imports:

```python
from subagent_dynamic_dag import build_dynamic_dag_workflow, dynamic_dag_guidance_message, is_dynamic_dag_request
from subagent_policy import (
    deterministic_required_action,
    guidance_enabled as _subagent_policy_guidance_enabled,
    semantic_repair_enabled as _subagent_policy_semantic_repair_enabled,
    subagent_assist_mode as _subagent_policy_assist_mode,
)
from subagent_scheduler import bounded_workflow_from_exact_prompts, compute_allowed_actions, workflow_complete
from subagent_state import build_subagent_state, is_worker_subagent_request, state_guidance_message
```

- [ ] **Step 4: Resolve `tests/test_routing.py` by union**

Keep all tests covering:

```text
third_party_app_transparent_metered route decisions
same-format transparent passthrough
transparent pre-output retry
retry provider classification
vision proxy overlay
Codex App external adapter subagent repair
Level 1 lifecycle repair
Level 2 workflow scheduler
Level 3 Dynamic DAG legal actions
```

- [ ] **Step 5: Run merge smoke tests**

Run:

```powershell
$env:PYTHONPATH='src-python'
python -m unittest tests.test_subagent_protocol tests.test_subagent_policy tests.test_subagent_scheduler tests.test_subagent_dynamic_dag tests.test_subagent_state -q
python -m unittest tests.test_routing -q
python -m unittest tests.test_chat_completions_gateway tests.test_proxy_event_logging -q
```

Expected:

```text
OK
OK
OK
```

- [ ] **Step 6: Commit merge resolution**

Run:

```powershell
git -C D:\Workstation\CodexHub-dev add src-python tests diagnostics docs
git -C D:\Workstation\CodexHub-dev commit --no-edit
```

Expected: a merge commit on `codex/subagent-repair-level3-provider-e2e`.

---

### Task 2: Enforce Repair Policy As The Subagent Safety Gate

**Files:**
- Modify: `src-python/subagent_policy.py`
- Modify: `src-python/codex_proxy.py`
- Modify: `tests/test_subagent_policy.py`
- Modify: `tests/test_routing.py`

**Interfaces:**
- Consumes: event context key `repair_policy`.
- Produces:
  - `subagent_policy.guidance_enabled(context: Mapping[str, Any] | None) -> bool`
  - `subagent_policy.semantic_repair_enabled(context: Mapping[str, Any] | None) -> bool`
  - both return `False` unless `context["repair_policy"] == "codex_subagent_repair"`.

- [ ] **Step 1: Write failing policy tests**

Add to `tests/test_subagent_policy.py`:

```python
import os
import unittest
from unittest.mock import patch

import subagent_policy


class SubagentPolicyRepairGateTests(unittest.TestCase):
    def test_guidance_requires_codex_subagent_repair_policy(self):
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "guided"}, clear=False):
            self.assertFalse(subagent_policy.guidance_enabled({"repair_policy": "none"}))
            self.assertTrue(subagent_policy.guidance_enabled({"repair_policy": "codex_subagent_repair"}))

    def test_semantic_repair_requires_codex_subagent_repair_policy(self):
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            self.assertFalse(subagent_policy.semantic_repair_enabled({"repair_policy": "none"}))
            self.assertFalse(subagent_policy.semantic_repair_enabled({"repair_policy": "codex_subagent_repair", "raw_provider_probe": True}))
            self.assertTrue(subagent_policy.semantic_repair_enabled({"repair_policy": "codex_subagent_repair"}))
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
$env:PYTHONPATH='src-python'
python -m unittest tests.test_subagent_policy -v
```

Expected: FAIL because current policy only checks raw-provider probe and assist mode.

- [ ] **Step 3: Implement the gate**

Modify `src-python/subagent_policy.py`:

```python
REPAIR_CODEX_SUBAGENT = "codex_subagent_repair"


def _subagent_repair_policy_enabled(context: Mapping[str, Any] | None) -> bool:
    return bool(context and context.get("repair_policy") == REPAIR_CODEX_SUBAGENT)


def guidance_enabled(context: Mapping[str, Any] | None) -> bool:
    if _raw_provider_probe(context):
        return False
    if not _subagent_repair_policy_enabled(context):
        return False
    return subagent_assist_mode() in {"guided", "assisted"}


def semantic_repair_enabled(context: Mapping[str, Any] | None) -> bool:
    if _raw_provider_probe(context):
        return False
    if not _subagent_repair_policy_enabled(context):
        return False
    return subagent_assist_mode() == "assisted"
```

- [ ] **Step 4: Add route-level regression test**

Add to `tests/test_routing.py` near route-decision tests:

```python
    def test_third_party_transparent_route_disables_subagent_repair_policy(self):
        upstream = {
            "name": "volcengine",
            "upstream_format": "responses",
        }
        decision = codex_proxy.route_decision_for_request(
            upstream,
            {"client_id": "opencode"},
            inbound_format="responses",
            provider_hint="volc",
        )

        self.assertEqual(decision.behavior_profile, codex_proxy.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED)
        self.assertEqual(decision.repair_policy, codex_proxy.REPAIR_NONE)

    def test_codex_app_external_route_enables_subagent_repair_policy(self):
        upstream = {
            "name": "volcengine",
            "upstream_format": "responses",
        }
        decision = codex_proxy.route_decision_for_request(
            upstream,
            {"client_id": "codex-app"},
            inbound_format="responses",
            provider_hint="volc",
        )

        self.assertEqual(decision.behavior_profile, codex_proxy.BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER)
        self.assertEqual(decision.repair_policy, codex_proxy.REPAIR_CODEX_SUBAGENT)
```

- [ ] **Step 5: Pass focused tests**

Run:

```powershell
$env:PYTHONPATH='src-python'
python -m unittest tests.test_subagent_policy tests.test_routing -q
```

Expected:

```text
OK
```

- [ ] **Step 6: Commit**

Run:

```powershell
git add src-python\subagent_policy.py src-python\codex_proxy.py tests\test_subagent_policy.py tests\test_routing.py
git commit -m "fix: gate subagent repair by route policy"
```

---

### Task 3: Make Level 3 Dynamic DAG The Completion Gate

**Files:**
- Modify: `diagnostics/subagent-e2e/run_level12_e2e.py`
- Modify: `tests/test_level12_e2e_parser.py`
- Modify: `tests/test_subagent_dynamic_dag.py`
- Modify: `tests/test_subagent_scheduler.py`
- Modify: `tests/test_routing.py`

**Interfaces:**
- Consumes:
  - `build_dynamic_dag_workflow(input_items: Any, protocol: ProtocolState) -> WorkflowState`
  - `dynamic_dag_guidance_message(workflow: WorkflowState, protocol: ProtocolState) -> dict[str, Any]`
  - `compute_allowed_actions(workflow: WorkflowState, protocol: ProtocolState) -> list[Mapping[str, Any]]`
- Produces:
  - runner option `--level level3`
  - runner option `--workflow dynamic-dag`
  - summary `scenario == "level3_dynamic_dag"`.

- [ ] **Step 1: Preserve parser tests for Level 3 success and dependency failure**

After resolving the merge, verify `tests/test_level12_e2e_parser.py` contains these tests from `codex/subagent-protocol-fix`:

```powershell
rg -n "test_level3_analyzer_accepts_parallel_branch_order|test_level3_analyzer_rejects_final_summarizer_before_branch_closes" tests\test_level12_e2e_parser.py
```

Expected:

```text
The command prints both test names.
```

If either name is missing, restore `tests/test_level12_e2e_parser.py` from `codex/subagent-protocol-fix` and re-apply only import-path fixes required by the current repository.

- [ ] **Step 2: Run parser tests**

Run:

```powershell
$env:PYTHONPATH='src-python'
python -m unittest tests.test_level12_e2e_parser -q
```

Expected:

```text
OK
```

- [ ] **Step 3: Run Level 3 unit gates**

Run:

```powershell
$env:PYTHONPATH='src-python'
python -m unittest tests.test_subagent_scheduler tests.test_subagent_dynamic_dag tests.test_subagent_state tests.test_routing -q
```

Expected:

```text
OK
```

- [ ] **Step 4: Run one focused real-LLM Level 3 case**

Run:

```powershell
$env:PYTHONPATH='src-python'
python diagnostics\subagent-e2e\run_level12_e2e.py --level level3 --workflow dynamic-dag --models glm52 --endpoints responses --jobs 1 --repeat 1 --subagent-mode assisted --main-retry-attempts 1
```

Expected command exit code: `0`.

Expected summary row in `diagnostics/subagent-e2e/level12-e2e-*/summary.md`:

```text
Level 3
level3-glm52-responses
PASS
```

- [ ] **Step 5: Run focused Level 3 across target models on Responses**

Run:

```powershell
$env:PYTHONPATH='src-python'
python diagnostics\subagent-e2e\run_level12_e2e.py --level level3 --workflow dynamic-dag --models glm52,k2_7,m3 --endpoints responses --jobs 1 --repeat 1 --subagent-mode assisted --main-retry-attempts 1
```

Expected command exit code: `0`.

- [ ] **Step 6: Run full Level 3 matrix**

Run:

```powershell
$env:PYTHONPATH='src-python'
python diagnostics\subagent-e2e\run_level12_e2e.py --level level3 --workflow dynamic-dag --models glm52,k2_7,m3 --endpoints responses,chat --jobs 3 --repeat 3 --subagent-mode assisted --main-retry-attempts 1
```

Expected command exit code: `0`.

- [ ] **Step 7: Run Level 1 and Level 2 non-regression**

Run:

```powershell
$env:PYTHONPATH='src-python'
python diagnostics\subagent-e2e\run_level12_e2e.py --level all --models glm52,k2_7,m3 --endpoints responses,chat --jobs 3 --repeat 1 --subagent-mode assisted --main-retry-attempts 1
```

Expected command exit code: `0`.

- [ ] **Step 8: Commit**

Run:

```powershell
git add diagnostics\subagent-e2e\run_level12_e2e.py tests\test_level12_e2e_parser.py tests\test_subagent_dynamic_dag.py tests\test_subagent_scheduler.py tests\test_subagent_state.py tests\test_routing.py
git commit -m "test: make level3 dynamic dag the subagent gate"
```

---

### Task 4: Add Codex And Active Provider Coverage To Client Matrix

**Files:**
- Modify: `scripts/e2e_gateway_client_matrix.py`
- Add: `tests/test_e2e_gateway_client_matrix.py`

**Interfaces:**
- Consumes active provider config path `C:\Users\noirb\.codex\proxy\config\providers.toml`.
- Produces:
  - `parse_runtime_providers_config(path: Path, *, proxy_base_url: str) -> list[ClientCase]`
  - client id `codex-app`
  - redacted reports under `test-results/gateway-client-matrix-*.json`.

- [ ] **Step 1: Add failing parser tests**

Create `tests/test_e2e_gateway_client_matrix.py`:

```python
import tempfile
import textwrap
import unittest
from pathlib import Path

from scripts import e2e_gateway_client_matrix as matrix


class GatewayClientMatrixTests(unittest.TestCase):
    def test_runtime_provider_parser_emits_codex_app_cases_without_api_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "providers.toml"
            config.write_text(
                textwrap.dedent(
                    """
                    [[providers]]
                    id = "volc"
                    name = "Volc"
                    base_url = "https://ark.example.test/v1"
                    api_key = "secret-token"
                    upstream_format = "responses"
                    enabled = true

                    [[providers.models]]
                    id = "glm-5.2"
                    enabled = true
                    gateway_exported = true

                    [[providers.models]]
                    id = "disabled-model"
                    enabled = false
                    gateway_exported = true
                    """
                ).strip(),
                encoding="utf-8",
            )

            cases = matrix.parse_runtime_providers_config(config, proxy_base_url="http://127.0.0.1:9099/v1")

        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].client, "codex-app")
        self.assertEqual(cases[0].provider_id, "volc")
        self.assertEqual(cases[0].model_id, "glm-5.2")
        self.assertEqual(cases[0].base_url, "http://127.0.0.1:9099/v1/providers/volc")
        self.assertEqual(cases[0].api_key, "dummy-codexhub-e2e")

    def test_report_does_not_include_authorization_secret(self):
        result = matrix.CaseResult(
            client="codex-app",
            provider_id="volc",
            model_id="glm-5.2",
            api="responses",
            endpoint="http://127.0.0.1:9099/v1/providers/volc/responses",
            status="passed",
            duration_ms=12,
            output_preview="CODEXHUB_E2E_OK",
        )
        case = matrix.ClientCase(
            client="codex-app",
            provider_id="volc",
            model_id="glm-5.2",
            display_name="glm-5.2",
            api="responses",
            base_url="http://127.0.0.1:9099/v1/providers/volc",
            api_key="secret-token",
            source_path="providers.toml",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = matrix.write_report(Path(temp_dir), [result], [case])
            text = report_path.read_text(encoding="utf-8")

        self.assertNotIn("secret-token", text)
        self.assertIn("CODEXHUB_E2E_OK", text)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
$env:PYTHONPATH='src-python;.'
python -m unittest tests.test_e2e_gateway_client_matrix -v
```

Expected: FAIL because `parse_runtime_providers_config` does not exist.

- [ ] **Step 3: Implement runtime provider parsing**

Modify `scripts/e2e_gateway_client_matrix.py`:

```python
try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None


def parse_runtime_providers_config(path: Path, *, proxy_base_url: str) -> list[ClientCase]:
    if tomllib is None:
        raise RuntimeError("Python 3.11+ tomllib is required")
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    cases: list[ClientCase] = []
    base = proxy_base_url.rstrip("/")
    for provider in sorted(data.get("providers", []), key=lambda item: str(item.get("id") or "")):
        provider_id = str(provider.get("id") or "")
        if not provider_id or not provider.get("enabled", True):
            continue
        upstream_format = str(provider.get("upstream_format") or "responses")
        api = "openai-completions" if upstream_format == "chat_completions" else "openai-responses"
        for model in sorted(provider.get("models", []), key=lambda item: str(item.get("id") or "")):
            model_id = str(model.get("id") or "")
            if not model_id or not model.get("enabled", True) or not model.get("gateway_exported", False):
                continue
            cases.append(
                ClientCase(
                    client="codex-app",
                    provider_id=provider_id,
                    model_id=model_id,
                    display_name=str(model.get("name") or model_id),
                    api=api,
                    base_url=f"{base}/providers/{provider_id}",
                    api_key="dummy-codexhub-e2e",
                    source_path=str(path),
                )
            )
    return cases
```

- [ ] **Step 4: Wire `codex-app` into CLI arguments**

Modify parser choices:

```python
parser.add_argument("--runtime-providers", default=str(home_path(".codex", "proxy", "config", "providers.toml")))
parser.add_argument("--proxy-base-url", default=DEFAULT_PROXY_BASE_URL)
parser.add_argument("--client", action="append", choices=["codex-app", "opencode", "zcode", "pi", "omp"])
```

Modify `load_cases(args)`:

```python
if Path(args.runtime_providers).exists():
    cases.extend(parse_runtime_providers_config(Path(args.runtime_providers), proxy_base_url=args.proxy_base_url))
```

- [ ] **Step 5: Pass parser tests**

Run:

```powershell
$env:PYTHONPATH='src-python;.'
python -m unittest tests.test_e2e_gateway_client_matrix -q
```

Expected:

```text
OK
```

- [ ] **Step 6: Commit**

Run:

```powershell
git add scripts\e2e_gateway_client_matrix.py tests\test_e2e_gateway_client_matrix.py
git commit -m "test: add codex app to gateway client matrix"
```

---

### Task 5: Run Automated Real-LLM Provider/Model Matrix

**Files:**
- Read: `C:\Users\noirb\.codex\proxy\config\providers.toml`
- Read: `C:\Users\noirb\.config\opencode\opencode.json`
- Read: `C:\Users\noirb\.pi\agent\models.json`
- Read: `C:\Users\noirb\.omp\agent\models.yml`
- Modify generated artifacts only: `test-results\gateway-client-matrix-*.json`

**Interfaces:**
- Consumes: `scripts/e2e_gateway_client_matrix.py`.
- Produces: latest report `test-results\gateway-client-matrix-latest.json`.

- [ ] **Step 1: Ensure backend is current and healthy**

Run:

```powershell
Invoke-RestMethod http://127.0.0.1:9099/health -TimeoutSec 3 | ConvertTo-Json -Compress
```

Expected:

```text
The JSON response contains `"ok": true`.
```

- [ ] **Step 2: Dry-run automated clients**

Run:

```powershell
python scripts\e2e_gateway_client_matrix.py --client codex-app --client pi --client omp --client opencode --dry-run
```

Expected:

```text
The first line starts with `Loaded ` and contains ` across 4 clients `.
At least one following line starts with `CASE codex-app `.
At least one following line starts with `CASE pi `.
At least one following line starts with `CASE omp `.
At least one following line starts with `CASE opencode `.
```

The command must not print API keys.

- [ ] **Step 3: Run automated matrix with one-at-a-time provider calls**

Run:

```powershell
python scripts\e2e_gateway_client_matrix.py --client codex-app --client pi --client omp --client opencode --concurrency 1 --attempts 3 --timeout-seconds 180 --max-output-tokens 256 --output-dir test-results\gateway-client-matrix-auto
```

Expected:

```text
Summary: N passed, 0 failed.
```

- [ ] **Step 4: Inspect transient failures if any**

Run only if Step 3 exits non-zero:

```powershell
python - <<'PY'
import json
from pathlib import Path
p = Path("test-results/gateway-client-matrix-auto/gateway-client-matrix-latest.json")
data = json.loads(p.read_text(encoding="utf-8"))
for item in data["results"]:
    if item["status"] != "passed":
        print(item["client"], item["provider_id"], item["model_id"], item["api"], item["error"])
PY
```

Expected: failures are concrete provider/model/client rows with no secrets.

- [ ] **Step 5: Confirm coverage has no automated-client model gaps**

Run:

```powershell
python - <<'PY'
import json
from pathlib import Path
p = Path("test-results/gateway-client-matrix-auto/gateway-client-matrix-latest.json")
data = json.loads(p.read_text(encoding="utf-8"))
for client, missing in data["coverage"]["missing_by_client"].items():
    if client in {"codex-app", "pi", "omp", "opencode"} and missing:
        raise SystemExit(f"{client} missing selectors: {missing}")
print("automated coverage ok")
PY
```

Expected:

```text
automated coverage ok
```

- [ ] **Step 6: Commit only script/test changes, not generated reports**

Run:

```powershell
git status --short
```

Expected: generated `test-results` artifacts are untracked or ignored. Do not commit `test-results`.

---

### Task 6: ZCode Manual Real-LLM Matrix Gate

**Files:**
- Create: `docs/superpowers/runbooks/2026-07-08-zcode-manual-e2e.md`
- Generated artifacts: `test-results\gateway-client-matrix-zcode\zcode-manual-result.json`

**Interfaces:**
- Consumes ZCode config `D:\zcode\.zcode\v2\config.json`.
- Produces a manual result JSON with the same pass/fail shape as automated matrix results.

- [ ] **Step 1: Create the ZCode runbook**

Create `docs/superpowers/runbooks/2026-07-08-zcode-manual-e2e.md`:

```markdown
# ZCode Manual CodexHub E2E

## Prompt

Use the selected CodexHub model and reply with exactly:

CODEXHUB_E2E_OK

## Pass Criteria

- ZCode sends a request through `http://127.0.0.1:9099`.
- Proxy event log records `client_id=zcode`.
- The selected model returns `CODEXHUB_E2E_OK`.
- ZCode UI does not hang after the model completes.

## Log Check

Run after each manual case:

```powershell
$events = Get-Content C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl -Tail 500 |
  ForEach-Object { try { $_ | ConvertFrom-Json } catch {} }
$events |
  Where-Object { $_.client_id -eq 'zcode' } |
  Select-Object -Last 20 ts,event,request_id,model,model_requested,model_canonical,upstream,provider_id,inbound_format,upstream_format,status,duration_ms,error,detail |
  ConvertTo-Json -Depth 4
```
```

- [ ] **Step 2: Dry-run ZCode configured cases**

Run:

```powershell
python scripts\e2e_gateway_client_matrix.py --client zcode --dry-run
```

Expected:

```text
The first line starts with `Loaded ` and contains ` across 1 clients `.
At least one following line starts with `CASE zcode `.
```

- [ ] **Step 3: Run HTTP-level ZCode config verification**

Run:

```powershell
python scripts\e2e_gateway_client_matrix.py --client zcode --concurrency 1 --attempts 3 --timeout-seconds 180 --max-output-tokens 256 --output-dir test-results\gateway-client-matrix-zcode-http
```

Expected:

```text
Summary: N passed, 0 failed.
```

- [ ] **Step 4: User-assisted ZCode UI run**

Ask the user to run the prompt from the runbook in ZCode for each enabled CodexHub provider/model group. After each run, execute the log check from the runbook.

Expected for each manual row:

```text
client=zcode
status=passed
output_preview=CODEXHUB_E2E_OK
```

- [ ] **Step 5: Write manual result artifact**

Create `test-results\gateway-client-matrix-zcode\zcode-manual-result.json` with:

```json
{
  "client": "zcode",
  "status": "passed",
  "prompt": "CODEXHUB_E2E_OK",
  "checked_at": "2026-07-08T00:00:00+08:00",
  "proxy_log_client_id": "zcode",
  "manual_operator": "user-assisted"
}
```

Set `checked_at` to the actual local timestamp when executing this step.

- [ ] **Step 6: Commit the runbook**

Run:

```powershell
git add docs\superpowers\runbooks\2026-07-08-zcode-manual-e2e.md
git commit -m "docs: add zcode manual e2e gate"
```

---

### Task 7: Final Verification And Merge

**Files:**
- Read/verify all modified files.
- Modify: none unless a verification failure exposes a defect.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: merge-ready integration branch.

- [ ] **Step 1: Run Python unit regression**

Run:

```powershell
$env:PYTHONPATH='src-python;.'
python -m unittest tests.test_subagent_protocol tests.test_subagent_policy tests.test_subagent_scheduler tests.test_subagent_dynamic_dag tests.test_subagent_state tests.test_level12_e2e_parser tests.test_e2e_gateway_client_matrix -q
python -m unittest tests.test_routing -q
python -m unittest tests.test_chat_completions_gateway tests.test_proxy_event_logging -q
```

Expected:

```text
OK
OK
OK
```

- [ ] **Step 2: Run formatting/conflict checks**

Run:

```powershell
git diff --check
rg -n "<<<<<<<|=======|>>>>>>>" src-python tests scripts diagnostics docs
```

Expected:

```text
git diff --check exits 0
rg exits 1 because no conflict markers are found
```

- [ ] **Step 3: Run full Level 3 gate**

Run:

```powershell
$env:PYTHONPATH='src-python'
python diagnostics\subagent-e2e\run_level12_e2e.py --level level3 --workflow dynamic-dag --models glm52,k2_7,m3 --endpoints responses,chat --jobs 3 --repeat 3 --subagent-mode assisted --main-retry-attempts 1
```

Expected command exit code: `0`.

- [ ] **Step 4: Run Level 1/2 regression gate**

Run:

```powershell
$env:PYTHONPATH='src-python'
python diagnostics\subagent-e2e\run_level12_e2e.py --level all --models glm52,k2_7,m3 --endpoints responses,chat --jobs 3 --repeat 1 --subagent-mode assisted --main-retry-attempts 1
```

Expected command exit code: `0`.

- [ ] **Step 5: Run automated provider/model/client matrix**

Run:

```powershell
python scripts\e2e_gateway_client_matrix.py --client codex-app --client pi --client omp --client opencode --concurrency 1 --attempts 3 --timeout-seconds 180 --max-output-tokens 256 --output-dir test-results\gateway-client-matrix-auto
```

Expected:

```text
Summary: N passed, 0 failed.
```

- [ ] **Step 6: Run ZCode HTTP-level and manual gates**

Run:

```powershell
python scripts\e2e_gateway_client_matrix.py --client zcode --concurrency 1 --attempts 3 --timeout-seconds 180 --max-output-tokens 256 --output-dir test-results\gateway-client-matrix-zcode-http
```

Expected:

```text
Summary: N passed, 0 failed.
```

Then complete the ZCode manual runbook and record `test-results\gateway-client-matrix-zcode\zcode-manual-result.json`.

- [ ] **Step 7: Commit remaining source changes**

Run:

```powershell
git status --short
git add src-python tests scripts diagnostics docs
git commit -m "feat: integrate subagent repair level3 and e2e gates"
```

Expected: commit succeeds, and generated `test-results` files are not staged.

- [ ] **Step 8: Merge into dev**

Run:

```powershell
git switch dev
git merge --no-ff codex/subagent-repair-level3-provider-e2e -m "merge: subagent repair level3 provider e2e"
```

Expected: merge succeeds.

- [ ] **Step 9: Restart backend and verify health**

Run:

```powershell
try { Invoke-RestMethod -Method Post http://127.0.0.1:9099/shutdown -TimeoutSec 3 | Out-Null } catch {}
Start-Sleep -Seconds 2
$python = "C:\Users\noirb\AppData\Local\Programs\Python\Python313\python.exe"
Start-Process -WindowStyle Hidden -FilePath $python -ArgumentList "D:\Workstation\CodexHub-dev\src-python\codex_proxy.py --port 9099"
Start-Sleep -Seconds 2
Invoke-RestMethod http://127.0.0.1:9099/health -TimeoutSec 3 | ConvertTo-Json -Compress
```

Expected:

```text
The JSON response contains `"ok": true`.
```

---

## Self-Review

**Spec coverage:** Goal 1 is covered by Tasks 1-3 and the Level 1/2/3 gates in Task 7. Goal 2 is covered by Tasks 4-6 and the final automated/ZCode matrix gates in Task 7. The route matrix constraint is covered by Task 2.

**Placeholder scan:** The plan contains no `TBD`, `TODO`, "fill in", or open-ended implementation instructions. Manual ZCode is intentionally user-assisted and has exact prompt, log check, and artifact shape.

**Type consistency:** `repair_policy`, `codex_subagent_repair`, `codex-app`, `pi`, `omp`, `opencode`, `zcode`, and `level3_dynamic_dag` are used consistently across tasks.

**Known execution risk:** Full Level 3 with `--repeat 3` and all provider/model matrix rows will take substantial wall-clock time and can expose provider-side transient failures. Transient provider failures should be reported by row with raw secret-free artifacts, not hidden by reducing the matrix.
