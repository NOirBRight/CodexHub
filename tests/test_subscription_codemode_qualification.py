"""Public evidence oracle tests: synthetic data cannot count as live success."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("qualification", ROOT / "scripts/qualify_subscription_codemode.py")
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


def test_observer_does_not_mutate_history_or_expose_description():
    payload = {"model": "cursor-subscription/exact", "tools": [{"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "exec", "description": "SECRET-DYNAMIC-DESCRIPTION", "format": {"type": "grammar"}}]}],
               "input": [{"type": "agent_message", "author": "/root", "recipient": "/root/child", "content": [{"type": "input_text", "text": "SECRET-TASK"}]},
                         {"type": "custom_tool_call_output", "call_id": "stable-call", "id": "typed-item", "output": "random-fixture"}]}
    original = json.dumps(payload, sort_keys=True)
    observed = qualification.observe_request(payload, 1, "random-fixture")
    assert json.dumps(payload, sort_keys=True) == original
    assert "SECRET" not in json.dumps(observed)
    assert observed["tool_outputs"][0]["call_sha256"] == qualification.digest("stable-call")
    assert observed["declarations"][0]["type"] == "custom"
    assert observed["declarations"][0]["namespace"] == "functions"


def session(child=False, reads=1, spawn=False, followup=False, wait=False):
    return {"is_child": child, "calls": [{"type": "custom_tool_call", "name": "exec", "reads_fixture": bool(reads), "call_sha256": "call", "spawn": spawn, "followup": followup, "wait": wait}] * reads,
            "effects": [{"call_sha256": "call", "fixture_present": True}], "assistant_returned_fixture": True, "assistant_returned_reversed_fixture": True}


def test_code_mode_requires_actual_custom_effect_and_exact_final():
    turns = [{"exit": 0, "timed_out": False, "errors": [], "finals": ["nonce"], "pid": 10}]
    result = qualification.assess_case("code-mode", turns, [{"model": qualification.CURSOR_MODEL}], [session()], "nonce", [1])
    assert result["passed"]
    fake = session()
    fake["effects"][0]["call_sha256"] = "different-call"
    assert not qualification.assess_case("code-mode", turns, [{"model": qualification.CURSOR_MODEL}], [fake], "nonce", [1])["passed"]
    turns[0]["finals"] = ["nonce\u200b"]
    assert not qualification.assess_case("code-mode", turns, [{"model": qualification.CURSOR_MODEL}], [session()], "nonce", [1])["passed"]


def test_restart_requires_new_gateway_and_caller_and_same_history_calls():
    turns = [{"exit": 0, "timed_out": False, "errors": [], "finals": [value], "pid": pid} for value, pid in [("nonce", 10), ("ecnon", 11)]]
    traces = [{"epoch": epoch, "tool_outputs": [{"call_sha256": "call"}], "model": qualification.CURSOR_MODEL} for epoch in (0, 1)]
    assert qualification.assess_case("completed-history-restart", turns, traces, [session()], "nonce", [1, 2])["passed"]
    assert not qualification.assess_case("completed-history-restart", turns, traces, [session()], "nonce", [1, 1])["passed"]
    traces[1]["tool_outputs"][0]["call_sha256"] = "fabricated-new-call"
    assert not qualification.assess_case("completed-history-restart", turns, traces, [session()], "nonce", [1, 2])["passed"]


def test_v2_requires_same_single_child_and_real_followup_wait_and_direction():
    turns = [{"exit": 0, "timed_out": False, "errors": [], "finals": [value], "pid": pid} for value, pid in [("nonce\necnon", 10), ("ecnon", 11)]]
    traces = [{"epoch": epoch, "tool_outputs": [{"call_sha256": "call"}], "model": model} for epoch, model in [(0, qualification.OFFICIAL_MODEL), (0, qualification.CURSOR_MODEL), (1, qualification.OFFICIAL_MODEL)]]
    sessions = [session(reads=0), session(child=True, reads=2)]
    sessions[0]["calls"] = [{"type": "function_call", "name": "spawn_agent", "reads_fixture": False, "call_sha256": "spawn", "spawn": True, "followup": True, "wait": True, "spawn_target_sha256": "reader", "target_sha256": "reader"}]
    assert qualification.assess_case("official-to-cursor", turns, traces, sessions, "nonce", [1, 2])["passed"]
    assert not qualification.assess_case("official-to-cursor", turns, traces, sessions + [session(child=True)], "nonce", [1, 2])["passed"]
    sessions[0]["calls"][0]["reads_fixture"] = True
    assert not qualification.assess_case("official-to-cursor", turns, traces, sessions, "nonce", [1, 2])["passed"]


def test_bad_finals_keep_zero_width_evidence_and_raw_text_hash():
    stdout = '\n'.join(json.dumps(event) for event in [{"type": "item.completed", "item": {"type": "agent_message", "text": "commentary"}},
                                                       {"type": "item.completed", "item": {"type": "agent_message", "text": "nonce\u200b"}}])
    result = qualification.parse_turn(stdout, SimpleNamespace(returncode=0, pid=1), False, "nonce")
    assert result["finals"] == [{"sha256": qualification.digest("nonce\u200b"), "length": 6, "zero_width_count": 1}]


def test_private_rollout_summary_proves_child_execution_with_call_identity(tmp_path):
    path = tmp_path / "rollout.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": "PRIVATE-CHILD-ID", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "PRIVATE-PARENT"}}}}},
            {"type": "turn_context", "payload": {"model": qualification.CURSOR_MODEL}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "namespace": "functions", "id": "typed", "call_id": "call", "input": 'text(await tools.exec_command({cmd:"cat probe-value.txt"}))'}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call", "output": "nonce"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "nonce"}]}}]
    path.write_text('\n'.join(json.dumps(row) for row in rows))
    result = qualification.summarize_rollouts([path], "nonce")
    assert result[0]["is_child"] and result[0]["calls"][0]["reads_fixture"]
    assert result[0]["effects"][0]["call_sha256"] == qualification.digest("call")
    assert "PRIVATE" not in json.dumps(result)


def test_harness_rejects_unbounded_timeout_before_any_run(tmp_path):
    with pytest.raises(SystemExit):
        qualification.main(["--case-timeout", "181", "--output", str(tmp_path / "result.json")])



def test_frozen_candidate_restart_bytes_do_not_follow_worker_edits(tmp_path):
    source, frozen = tmp_path / "source", tmp_path / "frozen"
    for name in ("src-python", "config"):
        (source / name).mkdir(parents=True)
        (source / name / "sample.txt").write_text("candidate")
    digest = qualification.freeze_candidate(source, frozen)
    (source / "src-python/sample.txt").write_text("worker edit")
    assert (frozen / "src-python/sample.txt").read_text() == "candidate"
    assert len(digest) == 64



def test_harness_rejects_duplicate_cases_before_any_candidate_or_inference(tmp_path):
    with pytest.raises(SystemExit):
        qualification.main(["--case", "code-mode", "--case", "code-mode", "--output", str(tmp_path / "result.json")])


def test_public_case_smoke_uses_real_gateway_but_no_inference_and_checks_cleanup(tmp_path, monkeypatch):
    import os
    if os.name == "nt":
        pytest.skip("POSIX fake executable fixture")
    source, user, frozen = tmp_path / "codex-source", tmp_path / "user", tmp_path / "candidate"
    (source / "model-catalogs").mkdir(parents=True)
    (user / ".config/cursor").mkdir(parents=True)
    (source / "model-catalogs/codexhub-model-catalog.json").write_text(json.dumps({"models": [{"slug": qualification.OFFICIAL_MODEL, "display_name": "official fixture", "visibility": "list", "supported_in_api": True, "tool_mode": "code_mode", "multi_agent_version": "v2"}]}))
    (source / "auth.json").write_text('{"auth_mode":"chatgpt","tokens":{"access_token":"dummy-never-used","refresh_token":"dummy-never-used","id_token":"dummy-never-used"}}')
    (user / ".config/cursor/auth.json").write_text('{"accessToken":"dummy-never-used"}')
    installed = tmp_path / "installed" / "2026.09.28-64d2043"
    installed.mkdir(parents=True)
    cursor = installed / "cursor-agent"
    cursor.write_text("#!/bin/sh\nexit 1\n")
    cursor.chmod(0o700)
    fake = tmp_path / "fake-codex"
    fake.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then printf "codex-cli 0.159.3\\n"; else printf \'{"type":"turn.failed","error":{"message":"fake caller no request"}}\\n\'; exit 1; fi\n')
    fake.chmod(0o700)
    qualification.freeze_candidate(ROOT, frozen)
    monkeypatch.syspath_prepend(str(frozen / "src-python"))
    monkeypatch.setenv("PATH", str(installed) + os.pathsep + os.environ.get("PATH", os.defpath))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    result = qualification.run_case("code-mode", frozen, source, user, fake, 30)
    assert len(result["gateway_pids"]) == 1
    assert result["requests"] == []  # no model request, even to a fake server
    assert result["failure_class"] is None
    assert not result["checks"]["passed"]
    assert result["private_tree_removed"]



def test_public_oracle_rejects_a_model_fallback_even_when_fixture_matches():
    turns = [{"exit": 0, "timed_out": False, "errors": [], "finals": ["nonce"], "pid": 1}]
    wrong = qualification.assess_case("code-mode", turns, [{"model": "nearest-family-low"}], [session()], "nonce", [2])
    assert not wrong["exact_selected_model_identity"] and not wrong["passed"]


def test_observer_preserves_payload_and_records_safe_selection_controls():
    request = {"model": qualification.CURSOR_MODEL, "reasoning": {"effort": "high"}, "parallel_tool_calls": True}
    before = json.dumps(request)
    result = qualification.observe_request(request, 0, "nonce")
    assert result["requested_reasoning_effort"] == "high"
    assert result["parallel_tool_calls"] is True
    assert json.dumps(request) == before
    result = qualification.observe_request({"reasoning_effort": "PRIVATE-DYNAMIC-STRING", "parallel_tool_calls": 1}, 0, "nonce")
    assert result["requested_reasoning_effort"] == "unsupported"
    assert result["parallel_tool_calls"] == "invalid"
    assert "PRIVATE" not in json.dumps(result)
