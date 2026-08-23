"""Gateway compatibility application pipeline."""

from __future__ import annotations

from collections.abc import Iterable as IterableABC
from pathlib import Path
from typing import Any, Iterable, Mapping, NoReturn

import hashlib
import json
import re
import uuid

from apply_patch_adapter import (
    ApplyPatchAdapter,
    ApplyPatchFacts,
    _ThirdPartyApplyPatchStreamAdapter as _ApplyPatchStreamAdapterImpl,
)
from collaboration_adapter import (
    CollaborationAdapter,
    CollaborationFacts,
    PathBindingSigner,
    WORKER_REQUESTED_BINDING_FIELD,
)
from codex_semantic_adapter import (
    COLLABORATION_V1 as _COLLABORATION_V1,
    COLLABORATION_V2 as _COLLABORATION_V2,
    COLLABORATION_V2_NAMESPACE as _COLLABORATION_V2_NAMESPACE,
    multi_agent_discovery_arguments as _semantic_multi_agent_discovery_arguments,
    normalize_multi_agent_arguments as _semantic_normalize_multi_agent_arguments,
    normalize_tool_search_arguments as _semantic_normalize_tool_search_arguments,
)
from gateway_errors import UpstreamProtocolTranslationError
from gateway_sse import _sse_line_ending, _sse_payload_bytes
from protocol_translation import UnsupportedProtocolTranslationError
from runtime_tool_compatibility import (
    HostedCapabilityFacts as RuntimeHostedCapabilityFacts,
    ProtocolCapabilities as RuntimeProtocolCapabilities,
    ToolCompatibilityError as RuntimeToolCompatibilityError,
    ToolCompatibilityPlan as RuntimeToolCompatibilityPlan,
    build_tool_compatibility_plan,
)
from subagent_policy import deterministic_required_action
from gateway_settings import subagent_guidance_enabled, subagent_semantic_repair_enabled
from subagent_scheduler import bounded_workflow_from_exact_prompts, compute_allowed_actions
from subagent_state import build_subagent_state, is_worker_subagent_request, state_guidance_message
from tool_surface_adapter import (
    APPLY_PATCH_FUNCTION_NAME,
    INTERNAL_INPUT_ITEM_TYPES,
    MULTI_AGENT_DISCOVERY_TOOLS,
    MULTI_AGENT_NAMESPACE_ALIASES,
    NODE_REPL_NAMESPACE,
    TOOL_SEARCH_EMPTY_MISS_BOUND,
    TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL,
    TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION,
    TOOL_SEARCH_UNAVAILABLE_STATUS,
    ToolSurfaceAdapter,
    ToolSurfaceFacts,
)
from route_plan import (
    NATIVE_RESPONSES_TOOL_CODEC_ERROR_CODE,
    TOOL_SURFACE_STRATEGY_ERROR_CODE,
    _external_native_responses_tool_codec,
    _external_tool_protocol,
    _external_tool_surface_strategy,
)
from route_primitives import (
    BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER,
    BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
    BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
)

from . import official_passthrough as _official_passthrough
from . import host

LIFECYCLE_FINAL_RETRY_GUIDANCE = """Codex native subagent final report correction
status: lifecycle_complete_final_retry
previous_attempt_status: the previous lifecycle-complete assistant response did not satisfy the requested visible final format.
visible_response_required: re-emit only the final report requested by the user, as ordinary assistant message content.
final_format_required: the first visible output token must be the first token of that requested final report. Do not include headings, bullets, summaries, markdown fences, or prose before or after the report.
tool_calls_forbidden: the subagent lifecycle already completed via real current-turn tool executions; do not call tool_search, node_repl, local tools, or any multi_agent_v1 tool again.
source_of_truth: use only the observed current-turn agent ids, sentinels, wait results, and close state already present in the transcript.
"""


def _lifecycle_final_retry_guidance_message(reason: str) -> dict[str, str]:
    return _official_passthrough._developer_text_message(LIFECYCLE_FINAL_RETRY_GUIDANCE + f"retry_reason: {reason}")


def _responses_body_with_lifecycle_final_retry_guidance(body: bytes, reason: str) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    input_items = payload.get("input")
    guidance = _lifecycle_final_retry_guidance_message(reason)
    if isinstance(input_items, list):
        payload["input"] = list(input_items) + [guidance]
    elif isinstance(input_items, str):
        payload["input"] = [_user_text_message(input_items), guidance]
    else:
        payload["input"] = [guidance]
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


WORKER_SUBAGENT_FINALIZATION_GUIDANCE = """Codex native worker subagent finalization guidance
status: worker_subagent_finalization_required
visible_response_required: after completing any required tool work, emit the worker result as ordinary assistant message content, not only reasoning, hidden notes, or tool arguments. If you emit an empty message, the coordinator receives no result and will treat the worker as incomplete.
allowed_status_prefixes: DONE, DONE_WITH_CONCERNS, NEEDS_CONTEXT, BLOCKED, PASS, FAIL
required_next_action_after_tools: use the exact report format requested by the worker task. For diagnostic implementer/reviewer tasks, the first visible output token should usually be DONE, PASS, FAIL, or BLOCKED.
do_not_spawn_subagents: this is a worker subagent request, not a coordinator request.
"""


def _worker_subagent_finalization_message() -> dict[str, str]:
    return _official_passthrough._developer_text_message(WORKER_SUBAGENT_FINALIZATION_GUIDANCE)


def _has_worker_subagent_finalization_guidance(value: Any) -> bool:
    return any(
        "worker_subagent_finalization_required" in fragment
        for fragment in _collect_text_fragments(value)
    )


def _multi_agent_explicit_function_tools(
    include_spawn_agent: bool = True,
    include_wait_agent: bool = True,
    include_close_agent: bool = True,
    include_resume_agent: bool = True,
    include_send_input: bool = True,
    open_agent_ids: list[str] | None = None,
    wait_agent_ids: list[str] | None = None,
    close_agent_ids: list[str] | None = None,
    worker_selector_values: tuple[str, ...] = ("worker", "default"),
) -> list[dict[str, Any]]:
    return host._tool_surface_adapter().multi_agent_explicit_function_tools(
        include_spawn_agent=include_spawn_agent,
        include_wait_agent=include_wait_agent,
        include_close_agent=include_close_agent,
        include_resume_agent=include_resume_agent,
        include_send_input=include_send_input,
        open_agent_ids=open_agent_ids,
        wait_agent_ids=wait_agent_ids,
        close_agent_ids=close_agent_ids,
        worker_selector_values=worker_selector_values,
    )


def _is_multi_agent_namespace_name(name: str | None) -> bool:
    return host._tool_surface_adapter().is_multi_agent_namespace_name(name)


def _is_multi_agent_explicit_tool_name(name: str) -> bool:
    return host._tool_surface_adapter().is_multi_agent_explicit_tool_name(name)


def _multi_agent_alias_tool_name(name: Any) -> str | None:
    return host._tool_surface_adapter().multi_agent_alias_tool_name(name)


def _is_multi_agent_tool_schema(value: Any) -> bool:
    return host._tool_surface_adapter().is_multi_agent_tool_schema(value)


def _is_node_repl_explicit_tool_name(name: str) -> bool:
    return host._tool_surface_adapter().is_node_repl_explicit_tool_name(name)


def _is_node_repl_tool_schema(value: Any) -> bool:
    return host._tool_surface_adapter().is_node_repl_tool_schema(value)


def _multi_agent_function_call_name(item: Mapping[str, Any]) -> str | None:
    return host._tool_surface_adapter().multi_agent_function_call_name(item)


def _node_repl_function_call_name(item: Mapping[str, Any]) -> str | None:
    return host._tool_surface_adapter().node_repl_function_call_name(item)


def _same_selected_v1_collaboration_function_call(
    item: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
) -> bool:
    return host._tool_surface_adapter().same_selected_v1_collaboration_function_call(item, event_context)


def _restore_deferred_core_node_repl_namespace(
    payload: dict[str, Any],
    source_tools: list[Any] | None,
) -> bool:
    return host._tool_surface_adapter().restore_deferred_core_node_repl_namespace(payload, source_tools)


def _filter_tools_for_subagent_coordinator(
    payload: dict[str, Any],
    *,
    include_node_repl_tools: bool,
    compatibility_plan: RuntimeToolCompatibilityPlan | None = None,
) -> bool:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    filtered_tools = [
        tool
        for tool in tools
        if _is_multi_agent_tool_schema(tool)
        or _official_passthrough._runtime_alias_matches_namespace(compatibility_plan, tool, "multi_agent_v1")
        or (
            include_node_repl_tools
            and (
                _is_node_repl_tool_schema(tool)
                or _official_passthrough._runtime_alias_matches_namespace(compatibility_plan, tool, NODE_REPL_NAMESPACE)
            )
        )
    ]
    if len(filtered_tools) == len(tools):
        return False
    payload["tools"] = filtered_tools
    return True


def _filter_tools_for_subagent_worker(
    payload: dict[str, Any],
    *,
    compatibility_plan: RuntimeToolCompatibilityPlan | None = None,
) -> bool:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    filtered_tools = [
        tool
        for tool in tools
        if not _is_multi_agent_tool_schema(tool)
        and not _official_passthrough._is_mcp_or_codex_app_tool_schema(tool)
        and not any(
            _official_passthrough._runtime_alias_matches_namespace(compatibility_plan, tool, namespace)
            for namespace in (
                NODE_REPL_NAMESPACE,
                "multi_agent_v1",
                "mcp__multi_agent_v1",
            )
        )
        and _official_passthrough._tool_schema_name(tool) != TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL["name"]
    ]
    if len(filtered_tools) == len(tools):
        return False
    payload["tools"] = filtered_tools
    return True


def _hide_tools_for_completed_subagent_lifecycle(payload: dict[str, Any]) -> bool:
    changed = False
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        payload["tools"] = []
        changed = True
    elif "tools" not in payload:
        payload["tools"] = []
        changed = True
    if payload.pop("tool_choice", None) is not None:
        changed = True
    return changed


def _required_subagent_tool_choice(
    *,
    tool_protocol: str,
    lifecycle_complete: bool,
    include_spawn_agent: bool,
    include_wait_agent: bool,
    include_close_agent: bool,
    include_resume_agent: bool,
    include_send_input: bool,
    include_node_repl_for_subagent_workflow: bool,
) -> str | None:
    if tool_protocol not in {"chat_tools", "responses_structured"} or lifecycle_complete:
        return None
    if include_node_repl_for_subagent_workflow:
        return None
    candidates: list[str] = []
    if include_spawn_agent:
        candidates.append("multi_agent_v1__spawn_agent")
    if include_wait_agent:
        candidates.append("multi_agent_v1__wait_agent")
    if include_close_agent:
        candidates.append("multi_agent_v1__close_agent")
    if include_send_input:
        candidates.append("multi_agent_v1__send_input")
    elif include_resume_agent:
        candidates.append("multi_agent_v1__resume_agent")
    return candidates[0] if len(candidates) == 1 else None


def _set_required_subagent_tool_choice(
    payload: dict[str, Any],
    tool_name: str | None,
    *,
    event_context: Mapping[str, Any] | None,
    upstream: Any,
) -> bool:
    if not tool_name:
        return False
    desired = {"type": "function", "name": tool_name}
    if payload.get("tool_choice") == desired:
        return False
    payload["tool_choice"] = desired
    host._write_adapter_event(
        event_context,
        "required_subagent_tool_choice_set",
        upstream=upstream if isinstance(upstream, str) else None,
        tool_name=tool_name,
    )
    return True


def _normalize_tool_search_arguments(value: Any) -> dict[str, Any] | None:
    return _semantic_normalize_tool_search_arguments(value)


def _bounded_empty_tool_search_terminal_calls(value: Any) -> dict[str, tuple[str, int]]:
    return host._tool_surface_adapter().bounded_empty_tool_search_terminal_calls(value)


def _terminalize_bounded_empty_tool_search_misses(
    payload: dict[str, Any],
    terminal_calls: Mapping[str, tuple[str, int]],
) -> bool:
    return host._tool_surface_adapter().terminalize_bounded_empty_tool_search_misses(payload, terminal_calls)


def _restrict_bounded_tool_search_queries(payload: dict[str, Any], bounded_queries: set[str]) -> bool:
    return host._tool_surface_adapter().restrict_bounded_tool_search_queries(payload, bounded_queries)


def _tool_search_query_digest(query: str) -> bytes:
    return host._tool_surface_adapter().tool_search_query_digest(query)


def _bounded_tool_search_query_digests(event_context: Mapping[str, Any] | None) -> set[bytes]:
    return host._tool_surface_adapter().bounded_tool_search_query_digests(event_context)


def _tool_search_call_arguments(
    value: Mapping[str, Any],
    *,
    candidate_item_ids: set[str] | None = None,
    allow_legacy_function: bool = False,
) -> dict[str, Any] | None:
    return host._tool_surface_adapter().tool_search_call_arguments(
        value,
        candidate_item_ids=candidate_item_ids,
        allow_legacy_function=allow_legacy_function,
    )


def _bounded_tool_search_unavailable_message(item: Mapping[str, Any]) -> dict[str, Any]:
    return host._tool_surface_adapter().bounded_tool_search_unavailable_message(item)


def _suppress_bounded_tool_search_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    return host._tool_surface_adapter().suppress_bounded_tool_search_calls(value, event_context)


def _suppress_bounded_tool_search_calls_inner(
    value: Any,
    bounded_digests: set[bytes],
    candidate_item_ids: set[str],
    suppressed_item_ids: set[str],
    allow_legacy_function: bool,
) -> tuple[Any, bool]:
    return host._tool_surface_adapter()._suppress_bounded_tool_search_calls_inner(
        value,
        bounded_digests,
        candidate_item_ids,
        suppressed_item_ids,
        allow_legacy_function,
    )


def _is_multi_agent_discovery_arguments(arguments: Mapping[str, Any] | None) -> bool:
    return host._tool_surface_adapter().is_multi_agent_discovery_arguments(arguments)


def _multi_agent_discovery_arguments(value: Any) -> dict[str, Any] | None:
    return _semantic_multi_agent_discovery_arguments(value)


def _normalize_multi_agent_arguments(
    value: Any,
    tool_name: str | None,
) -> tuple[Any, str | None, bool]:
    return _semantic_normalize_multi_agent_arguments(value, tool_name)


def _raise_worker_contract_error(
    *,
    event: str,
    error_code: str,
    classification: str,
    surface: str | None = None,
) -> None:
    host._collaboration_adapter().raise_worker_contract_error(
        event=event,
        error_code=error_code,
        classification=classification,
        surface=surface,
    )


def _validate_external_worker_selectors(
    value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
) -> None:
    host._collaboration_adapter().validate_external_worker_selectors(
        value,
        event_context,
        surface=surface,
    )


def _worker_caller_carrier_supported(event_context: Mapping[str, Any] | None) -> bool:
    return host._collaboration_adapter().worker_caller_carrier_supported(event_context)


def _worker_requested_binding_signature_payload(binding: Mapping[str, Any], call_id: str) -> bytes:
    return host._collaboration_adapter().requested_binding_signature_payload(binding, call_id)


def _requested_worker_binding_signature(binding: Mapping[str, Any], call_id: str) -> str:
    return host._collaboration_adapter().requested_binding_signature(binding, call_id)


def _worker_requested_binding_sidecar(
    requested: Mapping[str, Any],
    call_id: str,
) -> dict[str, Any]:
    return host._collaboration_adapter().requested_binding_sidecar(requested, call_id)


def _verified_worker_requested_binding(
    value: Any,
    call_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    return host._collaboration_adapter().verified_requested_binding(value, call_id)


def _is_legacy_native_worker_spawn_call(
    item: Mapping[str, Any],
    arguments: Mapping[str, Any] | None,
) -> bool:
    return host._collaboration_adapter().is_legacy_native_worker_spawn_call(item, arguments)


def _is_legacy_native_worker_spawn_readback(value: Any) -> bool:
    return host._collaboration_adapter().is_legacy_native_worker_spawn_readback(value)


def _attach_worker_requested_binding_sidecars(
    value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    capture_stream_event: bool = True,
) -> tuple[Any, bool]:
    return host._collaboration_adapter().attach_requested_binding_sidecars(
        value,
        event_context,
        capture_stream_event=capture_stream_event,
    )


def _apply_external_worker_response_contract(
    value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
    validate_selectors: bool = True,
    attach_sidecars: bool = True,
    capture_stream_event: bool = True,
) -> tuple[Any, bool]:
    return host._collaboration_adapter().apply_external_worker_response_contract(
        value,
        event_context,
        surface=surface,
        validate_selectors=validate_selectors,
        attach_sidecars=attach_sidecars,
        capture_stream_event=capture_stream_event,
    )


def _validate_worker_binding_history(
    payload: Mapping[str, Any],
) -> bool:
    return host._collaboration_adapter().validate_worker_binding_history(payload)


def _compatible_multi_agent_call_message(item: Mapping[str, Any], tool_name: str) -> dict[str, str]:
    lines = [f"Previous real Codex native multi_agent_v1.{tool_name} call transcript"]
    value = _official_passthrough._stringify_internal_field(item.get("call_id"))
    if value:
        lines.append(f"call_id: {value}")
    _official_passthrough._append_internal_field(lines, "arguments", item.get("arguments"))
    return _official_passthrough._developer_text_message("\n".join(lines))


def _has_multi_agent_discovery_tools(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("type") == "namespace"
        and item.get("name") == "multi_agent_v1"
        for item in value
    )


def _text_contains_multi_agent_discovery(value: Any) -> bool:
    if isinstance(value, str):
        return "discovered_codex_native_multi_agent_tools" in value
    if isinstance(value, Mapping):
        return any(_text_contains_multi_agent_discovery(child) for child in value.values())
    if isinstance(value, list):
        return any(_text_contains_multi_agent_discovery(child) for child in value)
    return False


def _has_multi_agent_discovery_context(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "tool_search_output" and _has_multi_agent_discovery_tools(item.get("tools")):
            return True
        if item.get("type") == "message" and _text_contains_multi_agent_discovery(item.get("content")):
            return True
    return False


def _required_spawn_arguments_for_state(input_items: Any, subagent_state: Any | None) -> dict[str, Any] | None:
    if subagent_state is None or getattr(subagent_state, "next_action", None) != "spawn":
        return None
    text = _official_passthrough._active_user_request_text(input_items)
    prompts = _official_passthrough._exact_child_prompts_from_request_text(text)
    if not prompts:
        return _required_workflow_spawn_arguments(text, subagent_state)
    index = len(getattr(subagent_state, "agents", {}) or {})
    if index >= len(prompts):
        return None
    prompt = prompts[index]
    if not prompt:
        return None
    return {"message": prompt, "fork_context": False}


def _required_workflow_spawn_arguments(text: str, subagent_state: Any) -> dict[str, Any] | None:
    if not bool(getattr(subagent_state, "workflow_intent", False)):
        return None
    if not bool(getattr(subagent_state, "workflow_plan_read", False)):
        return None
    role = getattr(subagent_state, "next_expected_role", None)
    if role not in {"implementer", "spec_reviewer", "code_quality_reviewer"}:
        return None

    output_path = _official_passthrough._line_value(text, "OUTPUT_PATH=")
    sentinel = _official_passthrough._line_value(text, "SENTINEL=")
    model = _official_passthrough._line_value(text, "MODEL_UNDER_TEST=") or _official_passthrough._line_value(text, "MODEL=")
    endpoint = _official_passthrough._line_value(text, "ENDPOINT_UNDER_TEST=") or _official_passthrough._line_value(text, "ENDPOINT=")
    case_name = _official_passthrough._line_value(text, "CASE=")
    if not all(isinstance(value, str) and value for value in (output_path, sentinel, model, endpoint, case_name)):
        return None

    baseline_status = _official_passthrough._workflow_baseline_status(text)
    artifact_text = "\n".join(
        [
            f"case: {case_name}",
            f"model: {model}",
            f"endpoint: {endpoint}",
            sentinel,
            "artifact: ok",
        ]
    )
    run_dir = str(Path(output_path).parent)
    if role == "implementer":
        message = f"""You are the IMPLEMENTER subagent in a Subagent-Driven Development workflow.

Your job is the single, minimal task described below. Do exactly this and nothing else.

Create exactly one UTF-8 text artifact at this absolute path:
  OUTPUT_PATH = {output_path}

Required file content, exactly five lines plus a trailing newline:
{artifact_text}

Hard constraints:
1. Create exactly one file: OUTPUT_PATH above.
2. Do not modify product-source files and do not commit anything.
3. Do not use local_tool_gateway or mcp__codex_apps__local_tool_gateway tools.
4. After writing, read the file back and confirm it matches the required content exactly.

Report back with only:
Status: DONE
Artifact path: {output_path}
Bytes written: <integer>
File ends with newline: <yes/no>
Other files created: <none, list if any>
"""
        return {"message": message, "nickname": "implementer", "fork_context": False}

    if role == "spec_reviewer":
        message = f"""You are the SPEC REVIEWER subagent in a Subagent-Driven Development workflow.

Your single job is to verify the diagnostic artifact matches its specification exactly. Do not modify or create files.

Artifact path:
  {output_path}

Required file content, exactly five lines plus a trailing newline:
{artifact_text}

Verification steps:
1. Read the artifact using native shell/file-read tools.
2. Confirm the file exists, is UTF-8 text, and ends with a trailing newline.
3. Confirm all five lines above are present in exact order with no extra content.
4. Do not use local_tool_gateway or mcp__codex_apps__local_tool_gateway tools.

Report back with only:
Verdict: PASS | FAIL
Checks: <one-line summary>
Failures: <none, or specific failures>
"""
        return {"message": message, "nickname": "spec-reviewer", "fork_context": False}

    message = f"""You are the CODE-QUALITY REVIEWER subagent in a Subagent-Driven Development workflow.

Your single job is to verify the implementer's work is minimal. Do not modify or create files.

Expected artifact:
  {output_path}

Coordinator-owned scaffolding to ignore:
  {run_dir}

Baseline git status entries allowed for this case:
```text
{baseline_status or "<none>"}
```
These baseline entries are pre-existing coordinator-owned changes. Do not report baseline-listed paths as product-source modifications introduced by the implementer.

Verification steps:
1. Run git status --porcelain=v1 -uall.
2. Confirm the expected artifact exists and is non-empty.
3. Ignore coordinator-owned files under the scaffolding path above.
4. Fail only for implementer-owned extra files or product-source modifications not listed in the baseline block above.
5. Do not use local_tool_gateway or mcp__codex_apps__local_tool_gateway tools.

Report back with only:
Verdict: PASS | FAIL
Artifact present: <yes/no>
Product-source modifications introduced: <none, or paths>
Extra implementer-owned files: <none, or paths>
Runner-owned scaffolding files observed: <short summary>
"""
    return {"message": message, "nickname": "quality-reviewer", "fork_context": False}


def _multi_agent_result_text(item: Mapping[str, Any], tool_name: str) -> str | None:
    if item.get("type") != "message":
        return None
    text = _official_passthrough._joined_text(item.get("content"))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header = f"Codex native multi_agent_v1.{tool_name} result"
    if not lines or lines[0] != header:
        return None
    return "\n".join(lines)


def _open_multi_agent_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    open_agent_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        spawn_text = _multi_agent_result_text(item, "spawn_agent")
        if spawn_text is not None and "status: succeeded" in spawn_text:
            agent_id = _official_passthrough._line_value(spawn_text, "agent_id:")
            if agent_id:
                open_agent_ids.add(agent_id)
        close_text = _multi_agent_result_text(item, "close_agent")
        if close_text is not None and "status: closed" in close_text:
            closed_agent_id = _official_passthrough._line_value(close_text, "closed_agent_id:")
            if closed_agent_id:
                open_agent_ids.discard(closed_agent_id)
            else:
                open_agent_ids.clear()
    return sorted(open_agent_ids)


def _spawned_multi_agent_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    spawned_agent_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        spawn_text = _multi_agent_result_text(item, "spawn_agent")
        if spawn_text is not None and "status: succeeded" in spawn_text:
            agent_id = _official_passthrough._line_value(spawn_text, "agent_id:")
            if agent_id:
                spawned_agent_ids.add(agent_id)
    return sorted(spawned_agent_ids)


def _completed_multi_agent_wait_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    completed_agent_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        text = _multi_agent_result_text(item, "wait_agent")
        if text is None or "status: completed" not in text:
            continue
        for agent_id in _official_passthrough._split_agent_id_list(_official_passthrough._line_value(text, "completed_agent_ids:")):
            completed_agent_ids.add(agent_id)
    return sorted(completed_agent_ids)


def _closed_multi_agent_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    closed_agent_ids: set[str] = set()
    closed_unknown = False
    for item in value:
        if not isinstance(item, Mapping):
            continue
        text = _multi_agent_result_text(item, "close_agent")
        if text is None or "status: closed" not in text:
            continue
        closed_agent_id = _official_passthrough._line_value(text, "closed_agent_id:")
        if closed_agent_id:
            closed_agent_ids.add(closed_agent_id)
        else:
            closed_unknown = True
    if closed_unknown and not closed_agent_ids:
        return ["<unknown>"]
    return sorted(closed_agent_ids)


def _has_single_loop_multi_agent_request(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    text = _official_passthrough._joined_text(value).lower()
    if not any(token in text for token in ("spawn_agent", "multi_agent", "subagent", "子代理")):
        return False
    return any(
        token in text
        for token in (
            "只执行一次",
            "执行一次真实",
            "一次真实",
            "一个子代理",
            "最终回复",
            "不要再 spawn",
            "不要重复验证",
            "不要重复",
            "only once",
            "single spawn",
            "single loop",
            "single lifecycle",
            "exactly one",
            "one lifecycle",
            "do not spawn again",
            "don't spawn again",
            "do not repeat",
        )
    )


def _requested_multi_agent_spawn_count(value: Any) -> int | None:
    if not isinstance(value, list):
        return None
    text = _official_passthrough._joined_text(value).lower()
    if not any(token in text for token in ("spawn_agent", "multi_agent", "subagent", "子代理")):
        return None

    for pattern in (
        r"(?:spawn|spawns|创建|启动|派发|调用|开|生成)\s*(?<!第)(\d{1,2})\s*(?:个|名|位)?\s*(?:subagents?|agents?|子代理)",
        r"(?<!第)(\d{1,2})\s*(?:个|名|位)?\s*(?:subagents?|agents?|子代理)",
    ):
        match = re.search(pattern, text)
        if match:
            count = int(match.group(1))
            return count if 0 < count <= 20 else None

    chinese_numbers = {
        "一个": 1,
        "一": 1,
        "两个": 2,
        "两": 2,
        "二个": 2,
        "二": 2,
        "三个": 3,
        "三": 3,
        "四个": 4,
        "四": 4,
        "五个": 5,
        "五": 5,
        "六个": 6,
        "六": 6,
        "七个": 7,
        "七": 7,
        "八个": 8,
        "八": 8,
        "九个": 9,
        "九": 9,
        "十个": 10,
        "十": 10,
    }
    chinese_pattern = "|".join(sorted((re.escape(key) for key in chinese_numbers), key=len, reverse=True))
    match = re.search(rf"(?<!第)({chinese_pattern})\s*(?:subagents?|agents?|子代理)", text)
    if match:
        return chinese_numbers[match.group(1)]
    return None


def _has_single_step_node_repl_request(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    text = _official_passthrough._joined_text(value).lower()
    if not any(token in text for token in ("mcp__node_repl", "node_repl")):
        return False
    return any(
        token in text
        for token in (
            "exactly once",
            "one tool result",
            "stop tool use",
            "single-step",
            "single step",
            "只调用一次",
            "只执行一次",
            "不要重复",
        )
    )


def _has_completed_single_step_node_repl_context(value: Any) -> bool:
    if host._has_browser_context_signal(value) or not _has_single_step_node_repl_request(value):
        return False
    text = _official_passthrough._joined_text(value).lower()
    return "codex native mcp__node_repl.js result" in text and "status: completed" in text


def _looks_like_subagent_workflow_plan_text(text: str) -> bool:
    lowered = text.lower()
    if "# short subagent development e2e plan" in lowered:
        return True
    return (
        "output_path" in lowered
        and "sentinel" in lowered
        and "implementer" in lowered
        and ("spec reviewer" in lowered or "spec compliance" in lowered)
        and ("quality reviewer" in lowered or "code quality" in lowered)
    )


def _has_node_repl_subagent_plan_read_context(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    node_repl_call_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        call_id = item.get("call_id")
        if item_type == "function_call" and isinstance(call_id, str):
            if _node_repl_function_call_name(item) is not None:
                node_repl_call_ids.add(call_id)
            continue
        if item_type == "function_call_output" and isinstance(call_id, str) and call_id in node_repl_call_ids:
            if _looks_like_subagent_workflow_plan_text(_official_passthrough._joined_text(item.get("output"))):
                return True
            continue
        if item_type == "message":
            text = _official_passthrough._joined_text(item.get("content"))
            if "codex native mcp__node_repl.js result" in text.lower() and _looks_like_subagent_workflow_plan_text(text):
                return True
    return False


def _node_repl_single_step_complete_message() -> dict[str, str]:
    return _official_passthrough._developer_text_message(
        "\n".join(
            [
                "Codex native mcp__node_repl.js current state",
                "status: single_step_complete",
                "completed_tool_alias: mcp__node_repl__js",
                "completed_native_tool: mcp__node_repl.js",
                "required_next_action: write the final answer now. The node_repl tool call already completed successfully; do not infer hidden tools were unavailable, and do not call mcp__node_repl__js, mcp__node_repl.js, or tool_search again for this single-step request.",
            ]
        )
    )


def _has_completed_single_loop_multi_agent_context(value: Any) -> bool:
    return _has_single_loop_multi_agent_request(value) and bool(_closed_multi_agent_ids(value)) and not _has_open_multi_agent_context(value)


def _has_open_multi_agent_context(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    if _open_multi_agent_ids(value):
        return True
    unknown_open_agent = False
    for item in value:
        if not isinstance(item, Mapping):
            continue
        spawn_text = _multi_agent_result_text(item, "spawn_agent")
        if spawn_text is not None and "status: succeeded" in spawn_text:
            if not _official_passthrough._line_value(spawn_text, "agent_id:"):
                unknown_open_agent = True
        close_text = _multi_agent_result_text(item, "close_agent")
        if close_text is not None and "status: closed" in close_text:
            if not _official_passthrough._line_value(close_text, "closed_agent_id:"):
                unknown_open_agent = False
    return unknown_open_agent


def _multi_agent_lifecycle_complete_message(closed_agent_ids: list[str]) -> dict[str, str]:
    lines = ["Codex native multi_agent_v1 current state"]
    lines.append("status: lifecycle_complete")
    if closed_agent_ids:
        lines.append(f"closed_agent_ids: {', '.join(closed_agent_ids)}")
    lines.append("completed_tool_aliases: multi_agent_v1__spawn_agent, multi_agent_v1__wait_agent, multi_agent_v1__close_agent")
    lines.append(
        "visible_response_required: emit the final report as ordinary assistant message content, not only reasoning, analysis, hidden notes, or tool arguments. If you emit only reasoning, the user receives an empty final answer."
    )
    lines.append(
        "empty_final_forbidden: the next assistant response must contain visible text; stopping with zero visible output is a task failure."
    )
    lines.append(
        "final_format_required: use exactly the final response format requested by the user; the first visible output token must be the first token of that requested final report, with no prose preface."
    )
    lines.append(
        "required_next_action: write the final concise report now from the observed agent ids, wait sentinels, and close state in the current-turn transcript. The lifecycle already completed via real Codex native tool executions; hidden tools after close indicate lifecycle complete, not unavailable. Do not call tool_search or any multi_agent_v1 tool again for this completed request."
    )
    return _official_passthrough._developer_text_message("\n".join(lines))


def _multi_agent_spawn_more_message(spawned_agent_ids: list[str], requested_count: int) -> dict[str, str]:
    remaining_count = max(0, requested_count - len(spawned_agent_ids))
    lines = ["Codex native multi_agent_v1 current state"]
    lines.append("status: spawn_more_required")
    lines.append(f"requested_spawn_count: {requested_count}")
    lines.append(f"completed_spawn_count: {len(spawned_agent_ids)}")
    lines.append(f"remaining_spawn_count: {remaining_count}")
    if spawned_agent_ids:
        lines.append(f"already_spawned_agent_ids: {', '.join(spawned_agent_ids)}")
    lines.append(
        "required_next_action: call multi_agent_v1__spawn_agent for the next not-yet-created child agent before waiting or closing any child agents."
    )
    return _official_passthrough._developer_text_message("\n".join(lines))


def _multi_agent_current_state_message(
    wait_agent_ids: list[str],
    close_agent_ids: list[str],
) -> dict[str, str] | None:
    lines = ["Codex native multi_agent_v1 current state"]
    if wait_agent_ids:
        ids_text = ", ".join(wait_agent_ids)
        lines.append("status: spawned_child_wait_required")
        lines.append(f"open_agent_ids_requiring_wait: {ids_text}")
        lines.append(
            "required_next_action: call multi_agent_v1__wait_agent with targets set to these agent_id values and timeout_ms=60000 before writing the final report."
        )
        lines.append(
            "note: spawn_agent already succeeded; spawn_agent is intentionally hidden while a child agent is open."
        )
        return _official_passthrough._developer_text_message("\n".join(lines))
    if close_agent_ids:
        ids_text = ", ".join(close_agent_ids)
        lines.append("status: wait_completed_close_required")
        lines.append(f"open_agent_ids_requiring_close: {ids_text}")
        lines.append(
            "required_next_action: call multi_agent_v1__close_agent with target set to one listed agent_id. "
            "Do not write the final report until every listed agent_id has been closed."
        )
        return _official_passthrough._developer_text_message("\n".join(lines))
    return None


def _compatible_multi_agent_output_message(
    item: Mapping[str, Any],
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> dict[str, str]:
    lines = [f"Codex native multi_agent_v1.{tool_name} result"]
    call_id = _official_passthrough._single_line_internal_field(item.get("call_id"))
    if call_id:
        lines.append(f"call_id: {call_id}")

    output = item.get("output")
    output_object = _official_passthrough._json_object_from_arguments(output)

    if tool_name == "spawn_agent":
        agent_id = output_object.get("agent_id") if output_object else None
        if isinstance(agent_id, str) and agent_id:
            lines.append("status: succeeded")
            lines.append(f"agent_id: {agent_id}")
            nickname = output_object.get("nickname")
            if isinstance(nickname, str) and nickname:
                lines.append(f"nickname: {nickname}")
            lines.append(
                "next_action: call multi_agent_v1__wait_agent with this agent_id when you need the child result; do not spawn another agent for the same child request."
            )
        elif isinstance(output, str) and "agent thread limit reached" in output.lower():
            lines.append("status: failed")
            lines.append("reason: agent thread limit reached")
            lines.append("next_action: wait or close an existing agent before spawning another one.")

    elif tool_name == "wait_agent":
        timed_out = output_object.get("timed_out") if output_object else None
        status = output_object.get("status") if output_object else None
        completed_agent_ids = _official_passthrough._status_completed_agent_ids(status)
        not_found_agent_ids = _official_passthrough._status_not_found_agent_ids(status)
        if timed_out is False and completed_agent_ids:
            lines.append("status: completed")
            lines.append(f"completed_agent_ids: {', '.join(completed_agent_ids)}")
            lines.append("next_action: call multi_agent_v1__close_agent for completed agents when they are no longer needed.")
        elif timed_out is True:
            lines.append("status: timed_out")
            lines.append("next_action: call multi_agent_v1__wait_agent again for the same target if the child result is still needed.")
        elif not_found_agent_ids:
            lines.append("status: not_found")
            lines.append(f"not_found_agent_ids: {', '.join(not_found_agent_ids)}")
            lines.append("next_action: do not wait for these not_found agents again; use a known open agent_id or continue.")

    elif tool_name == "close_agent":
        target = arguments.get("target") if arguments else None
        if output_object and "previous_status" in output_object:
            lines.append("status: closed")
            if isinstance(target, str) and target:
                lines.append(f"closed_agent_id: {target}")
            lines.append("next_action: do not wait or close this agent again.")
        elif isinstance(output, str) and "not found" in output.lower():
            lines.append("status: not_found")
            if isinstance(target, str) and target:
                lines.append(f"target_agent_id: {target}")
            lines.append("next_action: do not retry close for this same target; if it was already closed, continue.")

    _official_passthrough._append_internal_field(lines, "raw_output", output)
    return _official_passthrough._developer_text_message("\n".join(lines))


def _compatible_node_repl_call_message(item: Mapping[str, Any]) -> dict[str, str]:
    lines = ["Previous real Codex native mcp__node_repl.js call transcript"]
    value = _official_passthrough._stringify_internal_field(item.get("call_id"))
    if value:
        lines.append(f"call_id: {value}")
    _official_passthrough._append_internal_field(lines, "arguments", item.get("arguments"))
    return _official_passthrough._developer_text_message("\n".join(lines))


def _compatible_node_repl_output_message(item: Mapping[str, Any], *, enforce_final: bool) -> dict[str, str]:
    lines = ["Codex native mcp__node_repl.js result"]
    value = _official_passthrough._stringify_internal_field(item.get("call_id"))
    if value:
        lines.append(f"call_id: {value}")
    lines.append("status: completed")
    if enforce_final:
        lines.append("completed_tool_alias: mcp__node_repl__js")
        lines.append("completed_native_tool: mcp__node_repl.js")
        lines.append(
            "required_next_action: write the final answer now. The node_repl tool call already completed successfully; do not infer hidden tools were unavailable, and do not call mcp__node_repl__js or tool_search again for this single-step request."
        )
    _official_passthrough._append_internal_field(lines, "raw_output", item.get("output"))
    return _official_passthrough._developer_text_message("\n".join(lines))


def _multi_agent_discovery_output_item(item: Mapping[str, Any]) -> dict[str, Any]:
    rewritten = dict(item)
    rewritten["tools"] = MULTI_AGENT_DISCOVERY_TOOLS
    rewritten.setdefault("status", "completed")
    rewritten.setdefault("execution", "client")
    return rewritten


def _worker_multi_agent_suppressed_message(item: Mapping[str, Any]) -> dict[str, Any]:
    tool_name = _multi_agent_function_call_name(item) or "multi_agent_tool"
    message: dict[str, Any] = {
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": (
                    "worker_subagent_multi_agent_call_suppressed: this request is already running inside a "
                    "worker subagent, so nested Codex multi-agent tools are unavailable. "
                    f"Suppressed attempted tool: multi_agent_v1.{tool_name}. "
                    "Use the worker's available native file/shell tools if present; otherwise report BLOCKED "
                    "with the missing tool capability instead of spawning another subagent."
                ),
            }
        ],
    }
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        message["id"] = item_id
    return message


def _looks_like_unknown_multi_agent_function_call(item: Mapping[str, Any]) -> bool:
    if item.get("type") != "function_call":
        return False
    if _multi_agent_function_call_name(item) is not None:
        return False
    namespace = item.get("namespace")
    name = item.get("name")
    if isinstance(namespace, str) and namespace in MULTI_AGENT_NAMESPACE_ALIASES:
        return True
    if not isinstance(name, str):
        return False
    return (
        name.startswith("multi_agent_v1__")
        or name.startswith("multi_agent_v1.")
        or name.startswith("mcp__multi_agent_v1__")
        or name.startswith("mcp__multi_agent_v1.")
        or (name.startswith("multi_agent_v1") and len(name) > len("multi_agent_v1"))
    )


def _looks_like_coordinator_local_function_call(
    item: Mapping[str, Any],
    *,
    allow_plan_read_node_repl: bool,
) -> bool:
    if item.get("type") != "function_call":
        return False
    if _multi_agent_function_call_name(item) is not None:
        return False
    if allow_plan_read_node_repl and _node_repl_function_call_name(item) is not None:
        return False
    name = item.get("name")
    return isinstance(name, str) and bool(name)


def _coordinator_forbidden_tool_suppressed_message(
    item: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    return _official_passthrough._assistant_transcript_message(f"subagent_coordinator_tool_call_suppressed: {reason}", item)


def _mark_lifecycle_final_seen_if_present(value: Mapping[str, Any], state: dict[str, Any]) -> None:
    if not state["lifecycle_complete"]:
        return
    text = ""
    if value.get("type") == "message":
        text = _official_passthrough._message_item_visible_text(value)
    elif value.get("type") == "response.output_item.done":
        item = value.get("item")
        if isinstance(item, Mapping):
            text = _official_passthrough._message_item_visible_text(item)
    elif value.get("type") == "response.output_text.done":
        event_text = value.get("text")
        text = event_text if isinstance(event_text, str) else ""
    if text and host._text_contains_lifecycle_final_report(text):
        state["final_seen"] = True


def _post_final_multi_agent_suppressed_item_id(value: Mapping[str, Any]) -> str | None:
    item_id = value.get("id")
    return item_id if isinstance(item_id, str) and item_id else None


def _suppress_multi_agent_calls_after_lifecycle_final(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    context = event_context or {}
    if _official_passthrough._is_raw_provider_probe_context(context) or host._is_collaboration_v2_context(context):
        return value, False
    tool_protocol = str(context.get("tool_protocol") or "")
    if tool_protocol not in {"text_compat", "chat_tools", "responses_structured"}:
        return value, False
    if not bool(context.get("subagent_lifecycle_complete")) and not bool(
        context.get("_subagent_lifecycle_final_seen")
    ):
        return value, False

    if isinstance(event_context, dict):
        stored_ids = event_context.setdefault("_post_final_suppressed_multi_agent_item_ids", set())
        suppressed_item_ids = stored_ids if isinstance(stored_ids, set) else set()
        event_context["_post_final_suppressed_multi_agent_item_ids"] = suppressed_item_ids
        final_seen = bool(event_context.get("_subagent_lifecycle_final_seen"))
    else:
        suppressed_item_ids = set()
        final_seen = False
    state = {
        "lifecycle_complete": bool(context.get("subagent_lifecycle_complete")),
        "final_seen": final_seen,
        "suppressed_item_ids": suppressed_item_ids,
        "event_context": event_context,
    }

    rewritten, changed = _suppress_multi_agent_calls_after_lifecycle_final_inner(value, state)
    if isinstance(event_context, dict) and state["final_seen"]:
        event_context["_subagent_lifecycle_final_seen"] = True
    return rewritten, changed


def _suppress_multi_agent_calls_after_lifecycle_final_inner(
    value: Any,
    state: dict[str, Any],
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _suppress_multi_agent_calls_after_lifecycle_final_inner(item, state)
            if replacement is None:
                changed = True
                continue
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    event_type = value.get("type")
    if event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
        item_id = value.get("item_id")
        if isinstance(item_id, str) and item_id in state["suppressed_item_ids"]:
            return None, True
        return value, False

    direct_tool_name = _multi_agent_function_call_name(value)
    if state["final_seen"] and direct_tool_name is not None:
        item_id = _post_final_multi_agent_suppressed_item_id(value)
        if item_id:
            state["suppressed_item_ids"].add(item_id)
        host._write_adapter_event(
            state["event_context"],
            "subagent_post_final_multi_agent_call_suppressed",
            tool=direct_tool_name,
        )
        return None, True

    event_item = value.get("item")
    if (
        state["final_seen"]
        and event_type in {"response.output_item.added", "response.output_item.done"}
        and isinstance(event_item, Mapping)
    ):
        event_tool_name = _multi_agent_function_call_name(event_item)
        if event_tool_name is not None:
            item_id = _post_final_multi_agent_suppressed_item_id(event_item)
            if item_id:
                state["suppressed_item_ids"].add(item_id)
            host._write_adapter_event(
                state["event_context"],
                "subagent_post_final_multi_agent_call_suppressed",
                tool=event_tool_name,
            )
            return None, True

    changed = False
    rewritten = dict(value)
    response = rewritten.get("response")
    if isinstance(response, Mapping) and isinstance(response.get("output"), list):
        response_rewritten = dict(response)
        output, output_changed = _suppress_multi_agent_calls_after_lifecycle_final_inner(
            response_rewritten["output"],
            state,
        )
        response_rewritten["output"] = output
        if output_changed:
            rewritten["response"] = response_rewritten
            changed = True

    output = rewritten.get("output")
    if isinstance(output, list):
        output, output_changed = _suppress_multi_agent_calls_after_lifecycle_final_inner(output, state)
        if output_changed:
            rewritten["output"] = output
            changed = True

    for key, item in list(rewritten.items()):
        if key in {"response", "output"}:
            continue
        replacement, item_changed = _suppress_multi_agent_calls_after_lifecycle_final_inner(item, state)
        if replacement is None:
            rewritten.pop(key, None)
            changed = True
            continue
        if item_changed:
            rewritten[key] = replacement
            changed = True

    _mark_lifecycle_final_seen_if_present(rewritten, state)
    return (rewritten if changed else value), changed


def _suppress_coordinator_forbidden_tool_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    context = event_context or {}
    if (
        bool(context.get("subagent_worker_context"))
        or _official_passthrough._is_raw_provider_probe_context(context)
        or host._is_collaboration_v2_context(context)
    ):
        return value, False
    tool_protocol = str(context.get("tool_protocol") or "")
    if tool_protocol not in {"text_compat", "chat_tools", "responses_structured"}:
        return value, False

    plan_read_required = bool(context.get("subagent_workflow_plan_read_required"))
    subagent_state = context.get("_subagent_state")
    state_has_agents = bool(getattr(subagent_state, "agents", {}))
    active = (
        state_has_agents
        or bool(_official_passthrough._string_list(context.get("subagent_open_agent_ids")))
        or bool(_official_passthrough._string_list(context.get("subagent_wait_agent_ids")))
        or bool(_official_passthrough._string_list(context.get("subagent_close_agent_ids")))
        or bool(_official_passthrough._string_list(context.get("subagent_closed_agent_ids")))
        or bool(context.get("subagent_lifecycle_complete"))
        or (
            bool(context.get("subagent_workflow_active"))
            and bool(context.get("subagent_workflow_plan_read_complete"))
        )
    )
    if not active and not plan_read_required:
        return value, False

    if isinstance(event_context, dict):
        suppressed = event_context.setdefault("_coordinator_suppressed_tool_item_ids", set())
        suppressed_item_ids = suppressed if isinstance(suppressed, set) else set()
        event_context["_coordinator_suppressed_tool_item_ids"] = suppressed_item_ids
    else:
        suppressed_item_ids = set()
    return _suppress_coordinator_forbidden_tool_calls_inner(
        value,
        event_context,
        suppressed_item_ids,
        allow_plan_read_node_repl=plan_read_required,
    )


def _suppress_coordinator_forbidden_tool_calls_inner(
    value: Any,
    event_context: Mapping[str, Any] | None,
    suppressed_item_ids: set[str],
    *,
    allow_plan_read_node_repl: bool,
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _suppress_coordinator_forbidden_tool_calls_inner(
                item,
                event_context,
                suppressed_item_ids,
                allow_plan_read_node_repl=allow_plan_read_node_repl,
            )
            if replacement is None:
                changed = True
                continue
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    event_type = value.get("type")
    if event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
        item_id = value.get("item_id")
        if isinstance(item_id, str) and item_id in suppressed_item_ids:
            return None, True
        return value, False

    reason = None
    if event_type == "tool_search_call":
        reason = "tool_search_unavailable_during_subagent_workflow"
    elif _looks_like_unknown_multi_agent_function_call(value):
        reason = "unknown_multi_agent_tool_unavailable"
    elif _node_repl_function_call_name(value) is not None:
        if not allow_plan_read_node_repl:
            reason = "node_repl_unavailable_after_subagent_plan_read"
    elif _official_passthrough._is_mcp_or_codex_app_function_call(value):
        reason = "mcp_or_codex_app_tool_unavailable_during_subagent_workflow"
    elif _looks_like_coordinator_local_function_call(
        value,
        allow_plan_read_node_repl=allow_plan_read_node_repl,
    ):
        reason = "coordinator_tool_unavailable_during_subagent_workflow"

    if reason is not None:
        item_id = value.get("id")
        if isinstance(item_id, str) and item_id:
            suppressed_item_ids.add(item_id)
        host._write_adapter_event(
            event_context,
            "subagent_coordinator_tool_call_suppressed",
            reason=reason,
            tool=value.get("name") if isinstance(value.get("name"), str) else None,
            namespace=value.get("namespace") if isinstance(value.get("namespace"), str) else None,
        )
        return _coordinator_forbidden_tool_suppressed_message(value, reason=reason), True

    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _suppress_coordinator_forbidden_tool_calls_inner(
            item,
            event_context,
            suppressed_item_ids,
            allow_plan_read_node_repl=allow_plan_read_node_repl,
        )
        if replacement is None:
            rewritten.pop(key, None)
            changed = True
            continue
        if item_changed:
            rewritten[key] = replacement
            changed = True
    return (rewritten if changed else value), changed


def _suppress_worker_multi_agent_tool_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    if host._is_collaboration_v2_context(event_context) or not bool((event_context or {}).get("subagent_worker_context")):
        return value, False
    if isinstance(event_context, dict):
        suppressed = event_context.setdefault("_worker_suppressed_multi_agent_item_ids", set())
        suppressed_item_ids = suppressed if isinstance(suppressed, set) else set()
        event_context["_worker_suppressed_multi_agent_item_ids"] = suppressed_item_ids
    else:
        suppressed_item_ids = set()
    return _suppress_worker_multi_agent_tool_calls_inner(value, event_context, suppressed_item_ids)


def _suppress_worker_multi_agent_tool_calls_inner(
    value: Any,
    event_context: Mapping[str, Any] | None,
    suppressed_item_ids: set[str],
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _suppress_worker_multi_agent_tool_calls_inner(
                item,
                event_context,
                suppressed_item_ids,
            )
            if replacement is None:
                changed = True
                continue
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    event_type = value.get("type")
    if event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
        item_id = value.get("item_id")
        if isinstance(item_id, str) and item_id in suppressed_item_ids:
            return None, True
        return value, False

    if _multi_agent_function_call_name(value) is not None:
        item_id = value.get("id")
        if isinstance(item_id, str) and item_id:
            suppressed_item_ids.add(item_id)
        host._write_adapter_event(
            event_context,
            "worker_subagent_multi_agent_call_suppressed",
            tool=_multi_agent_function_call_name(value),
        )
        return _worker_multi_agent_suppressed_message(value), True

    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _suppress_worker_multi_agent_tool_calls_inner(
            item,
            event_context,
            suppressed_item_ids,
        )
        if replacement is None:
            rewritten.pop(key, None)
            changed = True
            continue
        if item_changed:
            rewritten[key] = replacement
            changed = True
    return (rewritten if changed else value), changed


def _guard_duplicate_multi_agent_spawn_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    if not subagent_semantic_repair_enabled(event_context):
        return value, False

    tool_protocol = str((event_context or {}).get("tool_protocol") or "")
    if tool_protocol not in {"text_compat", "chat_tools", "responses_structured"}:
        return value, False

    spawn_allowed = bool((event_context or {}).get("subagent_spawn_allowed"))
    subagent_state = (event_context or {}).get("_subagent_state")
    if spawn_allowed and subagent_state is None:
        return value, False

    lifecycle_complete = bool((event_context or {}).get("subagent_lifecycle_complete"))
    wait_agent_ids_value = (event_context or {}).get("subagent_wait_agent_ids")
    wait_agent_ids = [agent_id for agent_id in wait_agent_ids_value if isinstance(agent_id, str)] if isinstance(wait_agent_ids_value, list) else []
    open_agent_ids_value = (event_context or {}).get("subagent_open_agent_ids")
    open_agent_ids = [agent_id for agent_id in open_agent_ids_value if isinstance(agent_id, str)] if isinstance(open_agent_ids_value, list) else []
    accepted_workflow_spawn: list[bool] = []

    return _guard_duplicate_multi_agent_spawn_calls_inner(
        value,
        event_context=event_context,
        spawn_allowed=spawn_allowed,
        subagent_state=subagent_state,
        lifecycle_complete=lifecycle_complete,
        wait_agent_ids=wait_agent_ids,
        open_agent_ids=open_agent_ids,
        accepted_workflow_spawn=accepted_workflow_spawn,
    )


def _guard_duplicate_multi_agent_spawn_calls_inner(
    value: Any,
    *,
    event_context: Mapping[str, Any] | None,
    spawn_allowed: bool,
    subagent_state: Any | None,
    lifecycle_complete: bool,
    wait_agent_ids: list[str],
    open_agent_ids: list[str],
    accepted_workflow_spawn: list[bool],
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _guard_duplicate_multi_agent_spawn_calls_inner(
                item,
                event_context=event_context,
                spawn_allowed=spawn_allowed,
                subagent_state=subagent_state,
                lifecycle_complete=lifecycle_complete,
                wait_agent_ids=wait_agent_ids,
                open_agent_ids=open_agent_ids,
                accepted_workflow_spawn=accepted_workflow_spawn,
            )
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    if _is_multi_agent_spawn_function_call(value):
        blocked_by_state = False
        if subagent_state is not None:
            arguments = _official_passthrough._json_object_from_arguments(value.get("arguments")) or {}
            try:
                if subagent_state.allows_spawn_request(arguments):
                    if (
                        not getattr(subagent_state, "bounded_request", False)
                        and not getattr(subagent_state, "requested_append", False)
                    ):
                        if accepted_workflow_spawn:
                            blocked_by_state = True
                        else:
                            accepted_workflow_spawn.append(True)
                            return value, False
                    else:
                        return value, False
                else:
                    blocked_by_state = True
            except Exception:
                if spawn_allowed:
                    return value, False
        elif spawn_allowed:
            return value, False
        if lifecycle_complete:
            return (
                {
                    "type": "message",
                    "role": "assistant",
                    "content": (
                        "visible_response_required: emit the final report as ordinary assistant message content, not only reasoning, analysis, hidden notes, or tool arguments. "
                        "If you emit only reasoning, the user receives an empty final answer. "
                        "empty_final_forbidden: the next assistant response must contain visible text; stopping with zero visible output is a task failure. "
                        "final_format_required: use exactly the final response format requested by the user; the first visible output token must be the first token of that requested final report, with no prose preface. "
                        "required_next_action: write the final concise report now from the observed agent ids, wait sentinels, and close state in the current-turn transcript. "
                        "The requested subagent lifecycle already completed via real Codex native tool executions; hidden tools after close indicate lifecycle complete, not unavailable."
                    ),
                },
                True,
            )
        replacement_wait_ids = wait_agent_ids or ([] if blocked_by_state else open_agent_ids)
        if replacement_wait_ids:
            rewritten = dict(value)
            rewritten["namespace"] = "multi_agent_v1"
            rewritten["name"] = "wait_agent"
            rewritten["arguments"] = json.dumps(
                {"targets": replacement_wait_ids, "timeout_ms": 60000},
                ensure_ascii=True,
                separators=(",", ":"),
            )
            return rewritten, True
        return _suppressed_duplicate_spawn_message(subagent_state), True

    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _guard_duplicate_multi_agent_spawn_calls_inner(
            item,
            event_context=event_context,
            spawn_allowed=spawn_allowed,
            subagent_state=subagent_state,
            lifecycle_complete=lifecycle_complete,
            wait_agent_ids=wait_agent_ids,
            open_agent_ids=open_agent_ids,
            accepted_workflow_spawn=accepted_workflow_spawn,
        )
        if item_changed:
            rewritten[key] = replacement
            changed = True
    return (rewritten if changed else value), changed


def _suppressed_duplicate_spawn_message(subagent_state: Any | None) -> dict[str, Any]:
    expected_role = getattr(subagent_state, "next_expected_role", None)
    expected_task = getattr(subagent_state, "next_expected_task", None)
    parts = [
        "required_next_action: the attempted multi_agent_v1.spawn_agent call was suppressed because it repeats an already spawned role/task.",
        "Call multi_agent_v1.spawn_agent for the distinct role/task that is currently expected.",
    ]
    if expected_role:
        parts.append(f"next_expected_role: {expected_role}")
    if expected_task:
        parts.append(f"next_expected_task: {expected_task}")
    return {
        "type": "message",
        "role": "assistant",
        "content": "\n".join(parts),
    }


def _is_multi_agent_spawn_function_call(value: Mapping[str, Any]) -> bool:
    if value.get("type") != "function_call":
        return False
    name = value.get("name")
    namespace = value.get("namespace")
    if namespace == "multi_agent_v1" and name == "spawn_agent":
        return True
    return name == "multi_agent_v1__spawn_agent"
