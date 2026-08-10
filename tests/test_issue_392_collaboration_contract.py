from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_issue_392_collaboration_contract.py"
ARTIFACT = ROOT / "docs" / "evidence" / "issue-392" / "collaboration-runtime-contract.json"


def load_module():
    spec = importlib.util.spec_from_file_location("issue392_contract", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def declaration(module, version: str) -> dict[str, object]:
    namespace = module.V1_NAMESPACE if version == module.V1 else module.V2_NAMESPACE
    children = []
    for name, parameter_schema in module.EXPECTED_PARAMETER_SCHEMAS[version].items():
        parameters = copy.deepcopy(parameter_schema)
        if not parameters["required"]:
            del parameters["required"]
        children.append(
            {
                "type": "function",
                "name": name,
                "description": "sanitized",
                "strict": False,
                "parameters": parameters,
            }
        )
    return {
        "type": "namespace",
        "name": namespace,
        "description": "sanitized",
        "tools": children,
    }


def request(module, version: str) -> dict[str, object]:
    return {"tool_choice": "auto", "tools": [declaration(module, version)]}


def test_artifact_binds_both_frozen_runtime_binaries_and_sources() -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert payload["schema"] == "codexhub.issue392.collaboration-runtime-contract.v1"
    assert payload["qualification_status"] == (
        "accepted_request_call_result_agent_message_and_readback"
    )
    assert payload["candidate_revision"] == "be10f62f44b22fa8c84510238250ae11fb3ecab4"

    by_client = {entry["client"]: entry for entry in payload["runtimes"]}
    assert by_client["codex_cli"] | {
        "source_files": by_client["codex_cli"]["source_files"],
        "observations": by_client["codex_cli"]["observations"],
    } == by_client["codex_cli"]
    assert by_client["codex_cli"]["client_version"] == "0.146.1"
    assert by_client["codex_cli"]["source_commit"] == "79b4f03d35962b005b007a015113b38930711665"
    assert by_client["codex_cli"]["binary_sha256"] == (
        "ae9d865f3d346a1a2a60c4e84775622d74e3e7ef53e0dede9c68b81eab306cca"
    )
    assert by_client["codex_desktop"]["client_version"] == "26.803.5235.0"
    assert by_client["codex_desktop"]["runtime_version"] == "0.147.0-alpha.6.5"
    assert by_client["codex_desktop"]["source_commit"] == "618b8e9111da9f57fe380b09d0f6516e3f343536"
    assert by_client["codex_desktop"]["binary_sha256"] == (
        "fb5c760e14cf8fe86e12e49e8a3e7f237af06082d6b9fe1e411e463b7229c916"
    )
    for runtime in by_client.values():
        assert set(runtime["source_files"]) == {
            "multi_agent_tool_schemas",
            "multi_agent_output_serialization",
            "v2_spawn_handler",
            "v2_message_handler",
            "v2_wait_handler",
            "v2_interrupt_handler",
            "v2_list_handler",
            "tool_planning_and_namespace_override",
            "version_selection_and_namespace_default",
            "responses_request_and_tool_choice",
            "responses_stream_parser",
            "responses_items",
            "agent_message_and_rollout",
            "desktop_thread_items",
            "desktop_event_mapping",
        }
        assert set(runtime["observations"].values()).issubset(
            {"observed", "not_applicable"}
        )
    assert by_client["codex_cli"]["observations"][
        "desktop_thread_items_and_notifications"
    ] == "not_applicable"
    assert by_client["codex_desktop"]["observations"][
        "desktop_thread_items_and_notifications"
    ] == "observed"


def test_selection_uses_complete_namespace_schema_not_shared_names() -> None:
    module = load_module()
    v1 = declaration(module, module.V1)
    v2 = declaration(module, module.V2)

    assert module.classify_request({"tool_choice": "auto", "tools": [v1]}) == module.V1
    assert module.classify_request({"tool_choice": "auto", "tools": [v2]}) == module.V2

    direct_functions = [
        child for child in copy.deepcopy(v2["tools"]) if isinstance(child, dict)
    ]
    with pytest.raises(module.ContractValidationError, match="collaboration_marker_missing"):
        module.classify_request({"tool_choice": "auto", "tools": direct_functions})
    with pytest.raises(module.ContractValidationError, match="collaboration_marker_missing"):
        module.classify_request(
            {
                "tool_choice": "auto",
                "tools": [
                    {
                        "type": "function",
                        "name": "spawn_agent",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda module, value: value["tools"].pop(), "namespace_child_set_invalid"),
        (
            lambda module, value: value["tools"].append(copy.deepcopy(value["tools"][0])),
            "namespace_child_duplicate",
        ),
        (
            lambda module, value: value["tools"][0].__setitem__("type", "custom"),
            "namespace_child_type_invalid",
        ),
        (
            lambda module, value: value["tools"][0]["parameters"].__setitem__(
                "required", ["unknown"]
            ),
            "namespace_child_parameter_schema_mismatch",
        ),
        (
            lambda module, value: value["tools"][0]["parameters"].__setitem__(
                "additionalProperties", True
            ),
            "namespace_child_parameter_schema_mismatch",
        ),
        (
            lambda module, value: value["tools"][0].__setitem__("strict", True),
            "namespace_child_strict_invalid",
        ),
        (
            lambda module, value: value["tools"][0].__setitem__(
                "output_schema", {"type": "object"}
            ),
            "namespace_child_fields_invalid",
        ),
    ],
)
def test_selection_fails_closed_on_incomplete_or_mutated_schema(mutate, code: str) -> None:
    module = load_module()
    value = declaration(module, module.V2)
    mutate(module, value)
    with pytest.raises(module.ContractValidationError, match=code):
        module.classify_request({"tool_choice": "auto", "tools": [value]})


def test_selection_matches_exact_types_encryption_and_frozen_v2_spawn_fields() -> None:
    module = load_module()
    value = declaration(module, module.V2)
    by_name = {child["name"]: child for child in value["tools"]}

    assert "service_tier" not in by_name["spawn_agent"]["parameters"]["properties"]
    assert by_name["spawn_agent"]["parameters"]["properties"]["message"] == {
        "type": "string",
        "encrypted": True,
    }

    for mutate in (
        lambda: by_name["spawn_agent"]["parameters"]["properties"].__setitem__(
            "service_tier", {"type": "string"}
        ),
        lambda: by_name["spawn_agent"]["parameters"]["properties"]["message"].pop(
            "encrypted"
        ),
        lambda: by_name["wait_agent"]["parameters"]["properties"]["timeout_ms"].__setitem__(
            "type", "string"
        ),
    ):
        candidate = copy.deepcopy(value)
        candidate_by_name = {child["name"]: child for child in candidate["tools"]}
        by_name = candidate_by_name
        mutate()
        with pytest.raises(
            module.ContractValidationError,
            match="namespace_child_parameter_schema_mismatch",
        ):
            module.classify_request({"tool_choice": "auto", "tools": [candidate]})


def test_selection_fails_closed_on_mixed_duplicate_and_conflicting_signals() -> None:
    module = load_module()
    v1 = declaration(module, module.V1)
    v2 = declaration(module, module.V2)

    with pytest.raises(
        module.ContractValidationError, match="collaboration_marker_duplicate_or_mixed"
    ):
        module.classify_request({"tool_choice": "auto", "tools": [v1, v2]})
    with pytest.raises(
        module.ContractValidationError, match="collaboration_marker_duplicate_or_mixed"
    ):
        module.classify_request(
            {"tool_choice": "auto", "tools": [v2, copy.deepcopy(v2)]}
        )
    with pytest.raises(
        module.ContractValidationError, match="collaboration_version_signal_unexpected"
    ):
        module.classify_request(
            {
                "tool_choice": "auto",
                "tools": [v2],
                "features": {"multi_agent_version": "v1"},
            }
        )
    with pytest.raises(
        module.ContractValidationError, match="collaboration_version_signal_unexpected"
    ):
        module.classify_request(
            {
                "tool_choice": "auto",
                "tools": [v1],
                "metadata": {"multi_agent_version": "v1"},
            }
        )
    with pytest.raises(module.ContractValidationError, match="tool_choice_invalid"):
        module.classify_request({"tool_choice": "required", "tools": [v2]})


def test_task_identity_and_protocol_layers_are_assigned_to_exact_layers() -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    v2 = payload["protocols"]["collaboration_v2"]
    assert v2["result"]["spawn_identity"] == "task_name"
    assert v2["canonical_task_identity"] == "agent_path_serialized_as_task_name"
    assert v2["continuation_id_field"] == "not_present"
    assert v2["call"]["identity_fields"] == ["id", "call_id"]

    layers = payload["protocol_layers"]
    assert layers["client_dispatch"]["recipient_namespace"] == "collaboration"
    assert layers["client_dispatch"]["wire_discriminator_by_itself"] is False
    assert layers["responses_declaration_and_call"]["namespace"] == "collaboration"
    assert layers["model_input_agent_message"] == {
        "type": "agent_message",
        "fields": ["type", "id", "author", "recipient", "content"],
        "task_identity_fields": ["author", "recipient"],
        "content_variants": {
            "input_text": ["type", "text"],
            "encrypted_content": ["type", "encrypted_content"],
        },
        "source_optional_field": "internal_chat_message_metadata_passthrough",
        "function_result": False,
    }
    assert layers["rollout_metadata"]["sent_as_request_metadata"] is False
    assert set(layers["desktop_app"]["thread_items"]) == {
        "agentMessage",
        "collabAgentToolCall",
        "subAgentActivity",
    }
    assert layers["desktop_app"]["wire_item_equivalent"] is False


def test_outputs_stream_terminal_and_same_home_shapes_are_complete() -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    v2 = payload["protocols"]["collaboration_v2"]
    output_schemas = v2["result"]["tool_output_schemas"]
    assert output_schemas["spawn_agent"]["required"] == ["task_name"]
    assert output_schemas["wait_agent"]["required"] == ["message", "timed_out"]
    assert output_schemas["send_message"] is None
    assert output_schemas["followup_task"] is None

    lifecycle = payload["wire_lifecycle"]
    assert lifecycle["function_call_stream"]["event_order"] == [
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
    ]
    assert lifecycle["terminal"]["events"] == [
        "response.completed",
        "response.incomplete",
        "response.failed",
    ]
    readback = lifecycle["same_home_readback"]
    assert readback["clients"] == ["codex_cli", "codex_desktop"]
    assert readback["call_output_order_preserved"] is True
    assert readback["agent_message_author_recipient_and_content_kind_preserved"] is True


def test_contract_is_sanitized_and_never_references_existing_tasks() -> None:
    text = ARTIFACT.read_text(encoding="utf-8")
    assert "019fb82d-f601-7812-a339-e9c5f675e2e8" not in text
    assert not re.search(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        text,
        re.IGNORECASE,
    )
    lowered = text.lower()
    for forbidden in ("api_key", "bearer ", "prompt_text", "task_path_value", "call_fixture"):
        assert forbidden not in lowered
    scope = json.loads(text)["capture_scope"]
    assert scope["existing_user_home_read"] is False
    assert scope["existing_user_task_read"] is False
    assert scope["known_crash_task_read"] is False


def test_builder_check_and_negative_reconciliation() -> None:
    module = load_module()
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert module.reconcile_contract(payload) == {"reconciled": True, "mismatches": []}
    for case in ("mutation", "deletion", "loss"):
        report = module.reconcile_contract(module.replay_contract(payload, case))
        assert report["reconciled"] is False
        assert report["mismatches"] == ["contract_content_invalid"]

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
