"""Validate the bounded, sanitized Issue #251 Code Mode replay fixture.

This validator exercises the request-local compatibility seam only.  It does
not launch Codex, open a network connection, read credentials, execute a tool,
or call the production HTTP routing path.  The fixture is deliberately a
protocol-controlled local replay; a real CLI/manual capture remains a separate
Beta3 gate.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "issue_251_code_mode_evidence.json"
PENDING_CANDIDATE_SHA = "PENDING_CANDIDATE_SHA"
SCHEMA = "codexhub.issue251.code_mode_evidence.v1"

_SHA256 = re.compile(r"^[0-9a-f]{40}$")
_SENSITIVE = re.compile(r"(?i)(?:api[_-]?key|authorization|bearer|password|secret|sk-[a-z0-9]|ghp_)")
_LOCAL_PATH = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\users\\|/users/|/home/)")
_RAW_CONTENT = re.compile(r"(?i)(?:\*\*\* begin patch|\*\*\* update file:|raw prompt text)")


class EvidenceValidationError(ValueError):
    """A bounded, non-sensitive evidence failure."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise EvidenceValidationError(code)


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_strings(key)
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _assert_sanitized(value: Any) -> None:
    for text in _iter_strings(value):
        _require(_SENSITIVE.search(text) is None, "evidence_sensitive_value")
        _require(_LOCAL_PATH.search(text) is None, "evidence_local_path")
        _require(_RAW_CONTENT.search(text) is None, "evidence_raw_content")


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_fixture(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceValidationError("fixture_unavailable_or_invalid") from error
    _require(isinstance(payload, dict), "fixture_root_invalid")
    _assert_sanitized(payload)
    _require(payload.get("schema") == SCHEMA, "fixture_schema_invalid")
    _require(payload.get("sanitized") is True, "fixture_not_sanitized")
    return payload


def _load_runtime():
    source = ROOT / "src-python"
    _require(source.is_dir(), "workspace_source_unavailable")
    sys.path.insert(0, str(source))
    try:
        from runtime_tool_compatibility import (  # type: ignore[import-not-found]
            ADAPT,
            CUSTOM_FREEFORM,
            NATIVE,
            CompatibilityStreamState,
            ProtocolCapabilities,
            ToolCompatibilityError,
            build_tool_compatibility_plan,
        )
    finally:
        sys.path.pop(0)
    return {
        "ADAPT": ADAPT,
        "CUSTOM_FREEFORM": CUSTOM_FREEFORM,
        "NATIVE": NATIVE,
        "CompatibilityStreamState": CompatibilityStreamState,
        "ProtocolCapabilities": ProtocolCapabilities,
        "ToolCompatibilityError": ToolCompatibilityError,
        "build_tool_compatibility_plan": build_tool_compatibility_plan,
    }


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _assert_route_identity(case: dict[str, Any]) -> None:
    route = case.get("route_identity")
    _require(isinstance(route, dict), "route_identity_invalid")
    _require(set(route) == {"provider", "model", "endpoint", "protocol"}, "route_identity_invalid")
    _require(all(isinstance(value, str) and value for value in route.values()), "route_identity_invalid")
    _require(route["protocol"] == case.get("protocol"), "route_protocol_mismatch")


def _build_plan(fixture: dict[str, Any], case: dict[str, Any], runtime: dict[str, Any]):
    protocol = runtime["ProtocolCapabilities"]
    capabilities = protocol.responses_structured() if case["capabilities"] == "responses_structured" else protocol.chat_tools()
    return runtime["build_tool_compatibility_plan"](
        _copy(fixture["declarations"]),
        selected_protocol=case["protocol"],
        protocol_capabilities=capabilities,
        request_token=case["request_token"],
    )


def _history_fixture(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    history = _copy(fixture["history"])
    _require(set(history) == {"items", "outputs"}, "history_schema_invalid")
    items = history["items"]
    outputs = history["outputs"]
    _require(isinstance(items, list) and isinstance(outputs, list), "history_schema_invalid")
    _require(len(items) == 3 and len(outputs) == 3, "history_shape_invalid")
    return items + outputs


def _assert_ids_and_order(items: list[dict[str, Any]], fixture: dict[str, Any]) -> None:
    expected_ids = [
        "item_read",
        "item_patch",
        "item_verify",
        "result_read",
        "result_patch",
        "result_verify",
    ]
    _require([item.get("id") for item in items] == expected_ids, "history_item_order_changed")
    expected_calls = [
        "call_read",
        "call_patch",
        "call_verify",
        "call_read",
        "call_patch",
        "call_verify",
    ]
    _require([item.get("call_id") for item in items] == expected_calls, "history_call_identity_changed")
    _require(
        fixture["workflow"]["history_order"] == ["item_read", "item_patch", "item_verify"],
        "workflow_order_claim_invalid",
    )


def _exercise_history(plan: Any, fixture: dict[str, Any]) -> None:
    history = _history_fixture(fixture)
    encoded = plan.encode_payload({"tools": _copy(fixture["declarations"]), "input": history})
    _require(isinstance(encoded.get("tools"), list), "encoded_tools_invalid")
    custom_tool = encoded["tools"][1]
    if plan.entries[1].disposition == "native":
        _require(custom_tool == fixture["declarations"][1], "native_custom_declaration_changed")
    else:
        _require(
            isinstance(custom_tool, dict)
            and custom_tool.get("type") == "function"
            and custom_tool.get("name") == plan.entries[1].aliases[0],
            "adapted_custom_declaration_invalid",
        )
        _require(
            set(custom_tool.get("parameters", {}).get("properties", {})) == {"__codexhub_custom_input"},
            "adapted_custom_envelope_invalid",
        )
    decoded = plan.decode_payload({"output": encoded["input"]})
    result = decoded.get("output")
    _require(isinstance(result, list), "decoded_history_invalid")
    _require(result == history, "history_inverse_changed")
    _assert_ids_and_order(result, fixture)


def _stream_item(alias: str | None, *, input_value: str) -> dict[str, Any]:
    if alias is None:
        return {
            "type": "custom_tool_call",
            "id": "item_patch",
            "call_id": "call_patch",
            "name": "apply_patch",
            "input": input_value,
        }
    return {
        "type": "function_call",
        "id": "item_patch",
        "call_id": "call_patch",
        "name": alias,
        "arguments": "",
    }


def _native_stream_events(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    value = fixture["opaque_values"]["input"]
    item = _stream_item(None, input_value="")
    completed = {**item, "input": value}
    return [
        {"type": "response.output_item.added", "item": item},
        {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "item_patch",
            "call_id": "call_patch",
            "delta": value,
        },
        {
            "type": "response.custom_tool_call_input.done",
            "item_id": "item_patch",
            "call_id": "call_patch",
            "input": value,
        },
        {"type": "response.output_item.done", "item": completed},
        {"type": "response.completed", "response": {"output": [completed]}},
    ]


def _adapted_stream_events(plan: Any, fixture: dict[str, Any]) -> list[dict[str, Any]]:
    value = fixture["opaque_values"]["input"]
    alias = plan.entries[1].aliases[0]
    envelope = json.dumps({"__codexhub_custom_input": value}, separators=(",", ":"))
    item = _stream_item(alias, input_value="")
    completed = {**item, "arguments": envelope}
    return [
        {"type": "response.output_item.added", "item": item},
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item_patch",
            "call_id": "call_patch",
            "delta": envelope,
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "item_patch",
            "call_id": "call_patch",
            "arguments": envelope,
        },
        {"type": "response.output_item.done", "item": completed},
        {"type": "response.completed", "response": {"output": [completed]}},
    ]


def _exercise_stream(plan: Any, fixture: dict[str, Any], stream_mode: str, runtime: dict[str, Any]) -> dict[str, Any]:
    events = _native_stream_events(fixture) if stream_mode == "native" else _adapted_stream_events(plan, fixture)
    state = runtime["CompatibilityStreamState"](plan)
    decoded = state.decode_events(events)
    _require(state.terminal, "stream_not_terminal")
    _require(decoded[-1]["type"] == "response.completed", "stream_terminal_changed")
    if stream_mode == "native":
        _require(decoded == events, "native_stream_not_identity")
        return {"mode": "native", "event_count": len(decoded), "output_types": [item["type"] for item in decoded]}
    event_types = [item["type"] for item in decoded]
    _require(
        event_types
        == [
            "response.output_item.added",
            "response.custom_tool_call_input.delta",
            "response.custom_tool_call_input.done",
            "response.output_item.done",
            "response.completed",
        ],
        "adapted_stream_inverse_changed",
    )
    patch_item = decoded[3]["item"]
    _require(patch_item["type"] == "custom_tool_call", "adapted_stream_item_family_changed")
    _require(patch_item["name"] == "apply_patch", "adapted_stream_item_name_changed")
    _require(patch_item["input"] == fixture["opaque_values"]["input"], "adapted_stream_input_changed")
    _require(patch_item["id"] == "item_patch" and patch_item["call_id"] == "call_patch", "adapted_stream_identity_changed")
    return {"mode": "adapted", "event_count": len(decoded), "output_types": event_types}


def _expect_failure(action: Callable[[], Any], expected: str | None, runtime: dict[str, Any]) -> str:
    try:
        action()
    except runtime["ToolCompatibilityError"] as error:
        if expected is not None:
            _require(error.classification == expected, "negative_control_classification_changed")
        return str(error.classification)
    raise EvidenceValidationError("negative_control_did_not_fail")


def _exercise_controls(plan: Any, fixture: dict[str, Any], runtime: dict[str, Any]) -> dict[str, str]:
    alias = plan.entries[1].aliases[0]
    value = fixture["opaque_values"]["input"]
    controls: dict[str, str] = {}

    def fresh_state():
        # History and stream replay bind call identities.  Each negative
        # control gets a fresh attempt ledger while retaining the same
        # request-scoped aliases.
        return runtime["CompatibilityStreamState"](plan.new_attempt())

    controls["unknown_alias"] = _expect_failure(
        lambda: fresh_state().decode_events_for_event(
            {
                "type": "response.output_item.added",
                "item": {**_stream_item("__codexhub_custom_unknown", input_value=""), "id": "item_unknown"},
            }
        ),
        "unknown_alias",
        runtime,
    )

    def malformed() -> None:
        state = fresh_state()
        state.decode_events_for_event({"type": "response.output_item.added", "item": _stream_item(alias, input_value="")})
        state.decode_events_for_event(
            {
                "type": "response.function_call_arguments.done",
                "item_id": "item_patch",
                "call_id": "call_patch",
                "arguments": json.dumps({"unexpected": value}, separators=(",", ":")),
            }
        )

    controls["malformed_envelope"] = _expect_failure(malformed, "invalid_envelope", runtime)

    controls["missing_identity"] = _expect_failure(
        lambda: fresh_state().decode_events_for_event(
            {"type": "response.output_item.added", "item": {"type": "function_call", "name": alias, "arguments": ""}}
        ),
        "invalid_custom_stream_identity",
        runtime,
    )

    def duplicate() -> None:
        state = fresh_state()
        added = {"type": "response.output_item.added", "item": _stream_item(alias, input_value="")}
        state.decode_events_for_event(added)
        state.decode_events_for_event(added)

    controls["duplicate_identity"] = _expect_failure(duplicate, "invalid_custom_stream_identity", runtime)

    def incomplete() -> None:
        state = fresh_state()
        state.decode_events_for_event({"type": "response.output_item.added", "item": _stream_item(alias, input_value="")})
        state.decode_events_for_event(
            {
                "type": "response.incomplete",
                "response": {"output": []},
            }
        )

    controls["incomplete_stream"] = _expect_failure(incomplete, "incomplete_stream", runtime)

    def after_terminal() -> None:
        state = fresh_state()
        state.decode_events(_adapted_stream_events(plan, fixture))
        state.decode_events_for_event({"type": "response.function_call_arguments.delta", "item_id": "item_patch", "delta": value})

    controls["stream_after_terminal"] = _expect_failure(after_terminal, "stream_after_terminal", runtime)

    _require(set(controls) == set(fixture["controls"]), "controls_schema_invalid")
    return controls


def validate_fixture(payload: dict[str, Any], *, candidate_sha: str | None = None) -> dict[str, Any]:
    _require(payload["evidence_status"] == "protocol_controlled_local_replay", "evidence_status_invalid")
    binding = payload.get("candidate_binding")
    _require(isinstance(binding, dict), "candidate_binding_invalid")
    bound_sha = binding.get("candidate_sha")
    _require(isinstance(bound_sha, str), "candidate_binding_invalid")
    if candidate_sha is not None:
        _require(_SHA256.fullmatch(candidate_sha) is not None, "candidate_sha_invalid")
        _require(bound_sha == candidate_sha, "candidate_sha_mismatch")
    elif bound_sha != PENDING_CANDIDATE_SHA:
        _require(_SHA256.fullmatch(bound_sha) is not None, "candidate_sha_invalid")

    declarations = payload.get("declarations")
    _require(isinstance(declarations, list) and len(declarations) == 3, "declarations_invalid")
    _require(
        [item.get("name") for item in declarations if isinstance(item, dict)] == ["read", "apply_patch", "verify"],
        "declarations_invalid",
    )
    workflow = payload.get("workflow")
    _require(
        isinstance(workflow, dict)
        and workflow.get("steps") == ["read", "apply_patch", "verify"]
        and workflow.get("apply_patch_scope") == "target_scoped"
        and workflow.get("execution_owner") == "codex_client"
        and workflow.get("gateway_executes_tools") is False,
        "workflow_contract_invalid",
    )
    runtime = _load_runtime()
    summaries: dict[str, Any] = {}
    for case in payload.get("cases", []):
        _require(isinstance(case, dict), "case_invalid")
        _assert_route_identity(case)
        plan = _build_plan(payload, case, runtime)
        custom_entry = plan.entries[1]
        _require(custom_entry.family == runtime["CUSTOM_FREEFORM"], "custom_family_invalid")
        _require(custom_entry.disposition == case["expected_disposition"], "custom_disposition_changed")
        if case["stream_mode"] == "adapted":
            _require(len(custom_entry.aliases) == 1 and custom_entry.aliases[0].startswith("__codexhub_custom_"), "custom_alias_invalid")
            _require(custom_entry.aliases[0] not in {"read", "apply_patch", "verify"}, "custom_alias_collision")
            second_plan = _build_plan(payload, {**case, "request_token": f"{case['request_token']}-other"}, runtime)
            _require(second_plan.entries[1].aliases[0] != custom_entry.aliases[0], "custom_alias_not_request_scoped")
            _require(plan.diagnostics.as_dict().get("failure_classifications") == [], "diagnostics_failure_unexpected")
            _require("__codexhub_" not in json.dumps(plan.diagnostics.as_dict()), "diagnostics_alias_leak")
        else:
            _require(custom_entry.aliases == (), "native_custom_alias_unexpected")
        _exercise_history(plan, payload)
        stream_summary = _exercise_stream(plan, payload, case["stream_mode"], runtime)
        controls = _exercise_controls(plan, payload, runtime) if case["stream_mode"] == "adapted" else {}
        summaries[case["id"]] = {
            "disposition": custom_entry.disposition,
            "stream": stream_summary,
            "controls": controls,
            "route_digest": _canonical_digest(case["route_identity"]),
        }
    _require(set(summaries) == {"native_custom", "adapted_custom"}, "case_coverage_incomplete")
    return {
        "schema": SCHEMA,
        "candidate_sha": bound_sha,
        "fixture_sha256": _canonical_digest(payload),
        "cases": summaries,
        "status": "pass",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--candidate-sha", help="Require the fixture to be bound to this reviewed 40-hex SHA.")
    args = parser.parse_args(argv)
    try:
        result = validate_fixture(_load_fixture(args.fixture), candidate_sha=args.candidate_sha)
    except EvidenceValidationError as error:
        print(f"ISSUE_251_EVIDENCE_FAIL: {error}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
