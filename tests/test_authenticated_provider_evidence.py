from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "authenticated-provider"
EXPECTED_MODELS = {"glm-5.2", "kimi-k2.7-code", "deepseek-v4-flash:0731"}
EXPECTED_TOOLS = {
    "followup_task",
    "interrupt_agent",
    "list_agents",
    "send_message",
    "spawn_agent",
    "wait_agent",
}


def _load(name: str) -> dict:
    return json.loads((EVIDENCE / name / "summary.json").read_text(encoding="utf-8"))


def test_authenticated_provider_evidence_is_candidate_and_client_bound() -> None:
    chat = _load("chat")
    readme = (EVIDENCE / "README.md").read_text(encoding="utf-8")

    assert chat["schema"] == "codexhub.authenticated-provider-cli.v1"
    assert chat["qualification_status"] == "passed"
    assert re.fullmatch(r"[0-9a-f]{40}", chat["candidate_sha"])
    assert chat["candidate_sha"] in readme
    assert chat["cli_version"].startswith("codex-cli ")
    assert chat["provider_id"] == "ollama-cloud"


def test_all_authenticated_chat_cells_cover_text_patch_and_v2() -> None:
    summary = _load("chat")
    assert {cell["model"] for cell in summary["cells"]} == EXPECTED_MODELS
    assert len(summary["cells"]) == 3

    for cell in summary["cells"]:
        assert cell["status"] == "passed"
        assert cell["protocol"] == "chat_completions"
        assert cell["provider_id"] == "ollama-cloud"
        assert cell["identity_bound"] is True
        assert cell["provider_probe"]["status"] == "passed"
        assert cell["provider_probe"]["http_status"] == 200
        assert cell["provider_probe"]["has_choices"] is True

        scenarios = cell["scenarios"]
        assert set(scenarios) == {"identity_text", "file_workflow", "collaboration"}
        assert all(value["status"] == "passed" for value in scenarios.values())
        assert scenarios["identity_text"]["terminal_event"] == "turn.completed"
        assert scenarios["identity_text"]["sentinel_observed"] is True
        assert scenarios["identity_text"]["multi_turn_history"]["passed"] is True
        assert scenarios["identity_text"]["multi_turn_history"]["resume_item_types"] == [
            "agent_message"
        ]
        assert scenarios["file_workflow"]["file_verified"] is True
        assert {"command_execution", "file_change"} <= set(
            scenarios["file_workflow"]["item_types"]
        )
        assert scenarios["file_workflow"]["standard_function_lifecycle"][
            "exact_call_result_identity"
        ] is True
        assert scenarios["file_workflow"]["custom_tool_lifecycle"][
            "exact_call_result_identity"
        ] is True
        collaboration = scenarios["collaboration"]
        assert set(collaboration["observed_tools"]) == EXPECTED_TOOLS
        assert collaboration["qualified_phase_sequence"] == [
            "spawn_agent",
            "wait_agent",
            "followup_task",
            "send_message",
            "list_agents",
            "interrupt_agent",
        ]
        assert set(collaboration["lifecycle_phases"]) == {
            "followup_task",
            "send_message",
            "list_agents",
            "interrupt_agent",
        }
        for phase in collaboration["lifecycle_phases"].values():
            assert phase == {
                "exit_code": 0,
                "terminal_event": "turn.completed",
                "sentinel_observed": True,
                "target_call_observed": True,
            }
        assert collaboration["phase1"]["terminal_event"] == "turn.completed"
        assert collaboration["phase1"]["sentinel_observed"] is True
        assert collaboration["exact_call_result_history_identity"] is True
        assert collaboration["canonical_task_identity_observed"] is True
        assert collaboration["child_result_delivery_observed"] is True
        recovery = collaboration["child_delivery_recovery"]
        assert recovery["passed"] is True
        assert recovery["attempt_count"] in {0, 1, 2}
        if recovery["attempt_count"]:
            assert recovery["terminal_event"] == "turn.completed"
            assert recovery["sentinel_observed"] is True
            assert recovery["target_call_observed"] is True
        assert collaboration["result_shapes"] == {
            "wait": True,
            "list_status": True,
            "interrupt": True,
        }
        assert collaboration["same_home_restart"]["passed"] is True
        assert collaboration["same_home_restart"]["config_preserved"] is True
        assert collaboration["same_home_restart"]["agents_configuration_preserved"] is True
        assert collaboration["same_home_restart"]["no_new_spawn"] is True
        assert collaboration["cross_home_rejected_before_gateway_request"] is True

        gateway = cell["gateway"]
        assert gateway["providers"] == ["ollama-cloud"]
        assert gateway["models"] == [cell["model"]]
        assert gateway["protocols"] == ["chat_completions"]
        assert gateway["fallback_count"] == 0
        assert gateway["has_v1_observation"] is False
        assert gateway["has_v2_observation"] is True
        assert gateway["event_types"]["chat_stream_shape_summary"] > 0
        assert gateway["event_types"]["chat_to_responses_event_summary"] > 0
        assert gateway["event_types"]["chat_reasoning_extensions_suppressed"] > 0
        assert gateway["event_types"]["runtime_tool_adapter_response"] >= 6
        assert gateway["reasoning_policies"] == ["explicit"]
        assert gateway["streaming"]["progressive_text_stream_count"] >= 1
        assert gateway["streaming"]["text_delta_source_count"] >= 2
        assert cell["protocol_observations_passed"] is True
        for failure in gateway["failures"]:
            assert failure["category"] in {
                "client_cancellation",
                "request_or_semantic_rejection",
                "upstream_transport_or_availability",
                "upstream_or_gateway_failure",
                "other_non_success",
            }
            assert set(failure) <= {
                "event",
                "status",
                "failure_class",
                "failure_phase",
                "retry_safety",
                "category",
            }


def test_authenticated_provider_evidence_is_bounded_and_sanitized() -> None:
    for name in ("chat",):
        summary = _load(name)
        assert summary["sanitization"] == {
            "absolute_paths_retained": False,
            "credentials_retained": False,
            "opaque_ids_retained": False,
            "raw_prompts_retained": False,
            "raw_provider_output_retained": False,
            "request_bodies_retained": False,
        }
        serialized = json.dumps(summary, sort_keys=True)
        for forbidden in (
            "/home/",
            "Authorization",
            "api_key",
            "CODEXHUB_AUTH_QUAL_KEY",
            "request_body",
            "__codexhub_ns_",
            "__codexhub_custom_",
            "encrypted_content",
        ):
            assert forbidden not in serialized
