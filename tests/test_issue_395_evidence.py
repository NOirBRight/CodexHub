from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "docs" / "evidence" / "issue-395"
SUMMARY_PATH = EVIDENCE_DIR / "cli-chat" / "summary.json"
README_PATH = EVIDENCE_DIR / "README.md"
EXPECTED_TOOLS = {
    "followup_task",
    "interrupt_agent",
    "list_agents",
    "send_message",
    "spawn_agent",
    "wait_agent",
}


def _summary() -> dict:
    return json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))


def test_issue_395_real_cli_chat_evidence_is_candidate_and_protocol_bound() -> None:
    summary = _summary()
    readme = README_PATH.read_text(encoding="utf-8")

    assert summary["schema"] == "codexhub.issue395.cli-chat-v2-lifecycle.v1"
    assert summary["qualification_status"] == "passed"
    assert re.fullmatch(r"[0-9a-f]{40}", summary["candidate_sha"])
    assert summary["candidate_sha"] in readme
    assert summary["cli_version"].startswith("codex-cli ")
    assert summary["route"]["upstream_format"] == "chat_completions"
    assert summary["route"]["selected_provider"] == summary["route"]["upstream_provider_id"]


def test_issue_395_evidence_covers_all_v2_phases_without_v1_or_fallback() -> None:
    summary = _summary()
    observations = summary["phase_observations"]

    assert all(observations["phases"].values())
    assert set(observations["observed_tools"]) == EXPECTED_TOOLS
    assert observations["observed_namespaces"] == ["collaboration"]
    assert observations["v1_namespace_seen"] is False
    assert observations["v1_tools_seen"] == []
    assert observations["errors"] == []
    assert observations["fallback_count"] == 0
    assert summary["gateway_log_summary"]["fallback_count"] == 0
    assert summary["gateway_log_summary"]["errors"] == []
    assert summary["gateway_log_summary"]["has_v1_observation"] is False
    assert summary["gateway_log_summary"]["has_v2_observation"] is True


def test_issue_395_evidence_records_progressive_chat_and_same_home_readback() -> None:
    summary = _summary()
    wire = summary["chat_wire_summary"]
    restart = summary["same_home_restart_check"]

    assert wire["tool_call_responses"] == 6
    assert wire["request_count"] == wire["root_request_count"] + wire["child_request_count"]
    assert wire["child_request_count"] >= 1
    assert wire["six_alias_requests"] >= 1
    assert wire["progressive_two_chunk_responses"] == wire["request_count"]
    assert wire["raw_aliases_retained"] is False
    assert wire["raw_messages_retained"] is False

    assert restart == {
        "agents_configuration_preserved": True,
        "config_preserved": True,
        "passed": True,
        "requested": True,
        "resume_exit_code": 0,
        "resume_terminal_event": "turn.completed",
        "resumed_request_count": 1,
        "task_identity_preserved": True,
    }


def test_issue_395_committed_evidence_is_bounded_and_sanitized() -> None:
    summary = _summary()
    serialized = json.dumps(summary, ensure_ascii=True, sort_keys=True)

    assert summary["sanitization"] == {
        "absolute_paths_retained": False,
        "credentials_retained": False,
        "opaque_ids_retained": False,
        "raw_prompts_retained": False,
    }
    for forbidden in (
        "/home/",
        "Authorization",
        "api_key",
        "x-codex-turn-metadata",
        "__codexhub_ns_",
        "__codexhub_agent_message_v2__:",
        "encrypted_content",
    ):
        assert forbidden not in serialized
