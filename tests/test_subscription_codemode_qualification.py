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
    result = qualification.assess_case("code-mode", turns, [], [session()], "nonce", [1])
    assert result["passed"]
    fake = session()
    fake["effects"][0]["call_sha256"] = "different-call"
    assert not qualification.assess_case("code-mode", turns, [], [fake], "nonce", [1])["passed"]
    turns[0]["finals"] = ["nonce\u200b"]
    assert not qualification.assess_case("code-mode", turns, [], [session()], "nonce", [1])["passed"]


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
