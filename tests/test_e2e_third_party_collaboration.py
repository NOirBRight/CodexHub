from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "e2e_third_party_collaboration.py"


def _runner_module():
    spec = importlib.util.spec_from_file_location("third_party_collaboration_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(item: dict[str, object]) -> str:
    return json.dumps({"type": "response_item", "payload": item})


def test_e2e_muse_default_reasoning_matches_the_maintained_provider_capability() -> None:
    """The real-provider matrix must not advertise an unsupported effort."""
    runner = _runner_module()
    from maintained_catalog import resolve_model

    muse = resolve_model("opencode-go", "muse-spark-1.3-contributor")

    assert runner.DEFAULT_REASONING["opencode-go/muse-spark-1.3-contributor"] == (
        muse.default_reasoning_level
    )


def test_initial_fixture_is_a_real_failing_test_not_a_missing_module(tmp_path) -> None:
    runner = _runner_module()
    runner._write_parent_fixture(tmp_path)
    assert (tmp_path / "parent_task.py").is_file()
    assert (tmp_path / "test_parent_task.py").is_file()
    checked = runner._verify_parent_fixture(tmp_path)
    assert checked["host_test_exit_code"] == 1
    assert checked["parent_fixture_fixed"] is False


def test_resume_fixture_preserves_the_parent_implementation(tmp_path) -> None:
    runner = _runner_module()
    runner._write_parent_fixture(tmp_path)
    fixed = "def normalize(value):\n    return 'FIXED'\n"
    (tmp_path / "parent_task.py").write_text(fixed)
    runner._write_resume_fixture(tmp_path)
    assert (tmp_path / "parent_task.py").read_text() == fixed


def test_fixture_verdict_uses_behavior_not_quote_style_or_mutable_tests(tmp_path) -> None:
    runner = _runner_module()
    runner._write_parent_fixture(tmp_path)
    (tmp_path / "parent_task.py").write_text("def normalize(value):\n    return 'FIXED'\n")
    assert runner._verify_parent_fixture(tmp_path)["parent_fixture_fixed"] is True
    (tmp_path / "parent_task.py").write_text('def normalize(value):\n    return "BROKEN"\n')
    (tmp_path / "test_parent_task.py").write_text("# empty test must not pass acceptance\n")
    assert runner._verify_parent_fixture(tmp_path)["host_test_exit_code"] != 0


def test_user_prompts_cannot_supply_completed_results() -> None:
    runner = _runner_module()
    rows = [json.loads(_row({"type": "message", "role": "user", "content": runner.PARENT_SENTINEL}))]
    assert runner._sentinels_in_records([{"rows": rows}]) == set()


def test_spawn_prompt_mentioning_unittest_is_not_parent_test_execution() -> None:
    runner = _runner_module()
    rows = [json.loads(_row({"type": "function_call", "namespace": "collaboration", "name": "spawn_agent",
                            "arguments": '{"message":"run python -m unittest -q test_parent_task.py"}'}))]
    assert runner._parent_test_tool_call_count([{"rows": rows}]) == 0


@pytest.mark.parametrize(
    ("version", "namespace", "followup_name"),
    [
        ("v1", "multi_agent_v1", "resume_agent"),
        ("v2", "collaboration", "followup_task"),
    ],
)
def test_e2e_evidence_uses_parent_child_relationship_not_model_name(
    tmp_path, version: str, namespace: str, followup_name: str
) -> None:
    runner = _runner_module()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    model = "xai/grok-4.6"

    parent = [
        json.dumps({"type": "session_meta", "payload": {"id": "parent"}}),
        json.dumps({"type": "turn_context", "payload": {"model": model}}),
        _row({"type": "function_call", "namespace": namespace, "name": "spawn_agent", "encrypted_function_args": []}),
        _row({"type": "function_call", "namespace": namespace, "name": "wait_agent", "encrypted_function_args": []}),
        _row({"type": "function_call", "namespace": namespace, "name": followup_name, "encrypted_function_args": []}),
        _row({"type": "custom_tool_call", "input": "python -m unittest -q test_resume_turn.py"}),
        _row({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "E2E_CHILD_OK 323\nE2E_FOLLOWUP_OK 667\nE2E_NO_SUBAGENT_TURN_OK 818"}]}),
    ]
    if version == "v1":
        parent.insert(
            -1,
            _row({"type": "function_call", "namespace": namespace, "name": "close_agent"}),
        )
    child = [
        json.dumps({"type": "session_meta", "payload": {"id": "child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}}}),
        json.dumps({"type": "turn_context", "payload": {"model": model}}),
        _row({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "E2E_CHILD_OK 323\nE2E_FOLLOWUP_OK 667"}]}),
    ]
    if version == "v2":
        child[2:2] = [
            _row({"type": "agent_message", "content": [{"type": "input_text", "text": "E2E_CHILD_OK 323"}]}),
            _row({"type": "agent_message", "content": [{"type": "input_text", "text": "E2E_FOLLOWUP_OK 667"}]}),
        ]
    (sessions / "parent.jsonl").write_text("\n".join(parent) + "\n")
    (sessions / "child.jsonl").write_text("\n".join(child) + "\n")

    # This old fixture contains only names and sentinel text: no call IDs,
    # execution results, or terminal events. It must no longer qualify.
    with pytest.raises(RuntimeError, match="ambiguous_collaboration_call"):
        runner.collect_evidence(tmp_path, model, version)


def test_e2e_resume_selects_the_related_parent_not_the_last_session(tmp_path) -> None:
    runner = _runner_module()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    model = "xai/grok-4.6"
    (sessions / "parent.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "parent"}}),
                json.dumps({"type": "turn_context", "payload": {"model": model}}),
            ]
        )
        + "\n"
    )
    (sessions / "child.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}}}),
                json.dumps({"type": "turn_context", "payload": {"model": model}}),
            ]
        )
        + "\n"
    )
    (sessions / "unrelated.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "unrelated"}}),
                json.dumps({"type": "turn_context", "payload": {"model": model}}),
            ]
        )
        + "\n"
    )

    assert runner._parent_session_id(tmp_path, model) == "parent"


def test_e2e_client_turn_keeps_the_resume_in_the_isolated_working_directory(
    tmp_path, monkeypatch
) -> None:
    runner = _runner_module()
    observed: dict[str, object] = {}

    class _Client:
        def wait(self, *, timeout: int) -> int:
            assert timeout == 1
            return 0

    def fake_popen(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return _Client()

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    output = tmp_path / "client.jsonl"

    assert runner._run_client(
        ["codex", "exec", "resume", "parent"],
        environment={"CODEX_HOME": str(tmp_path / "client")},
        output=output,
        working_directory=tmp_path,
        timeout=1,
    ) == 0
    assert observed["cwd"] == tmp_path


def test_e2e_evidence_requires_the_actual_parent_model(tmp_path) -> None:
    """A catalog label cannot claim parent-model coverage without a parent record."""
    runner = _runner_module()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    parent_model = "xai/grok-4.6"

    (sessions / "parent.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "parent"}}),
                json.dumps({"type": "turn_context", "payload": {"model": parent_model}}),
                _row({"type": "function_call", "namespace": "collaboration", "name": "spawn_agent", "encrypted_function_args": []}),
                _row({"type": "function_call", "namespace": "collaboration", "name": "wait_agent", "encrypted_function_args": []}),
                _row({"type": "function_call", "namespace": "collaboration", "name": "followup_task", "encrypted_function_args": []}),
                _row({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "E2E_CHILD_OK 323\nE2E_FOLLOWUP_OK 667\nE2E_PARENT_IMPLEMENTED_OK 941"}]}),
            ]
        )
        + "\n"
    )
    (sessions / "child.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}}}),
                json.dumps({"type": "turn_context", "payload": {"model": parent_model}}),
                _row({"type": "agent_message", "content": [{"type": "input_text", "text": "E2E_CHILD_OK 323"}]}),
                _row({"type": "agent_message", "content": [{"type": "input_text", "text": "E2E_FOLLOWUP_OK 667"}]}),
                _row({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "E2E_CHILD_OK 323\nE2E_FOLLOWUP_OK 667"}]}),
            ]
        )
        + "\n"
    )

    with pytest.raises(RuntimeError, match="ambiguous_collaboration_call"):
        runner.collect_evidence(tmp_path, parent_model="opencode-go/muse-spark-1.3-contributor", child_model=parent_model)


def _complete_fixture(home, version="v2"):
    model = "xai/grok-4.6"
    namespace = "collaboration" if version == "v2" else "multi_agent_v1"
    def row(kind, payload):
        return {"type": kind, "timestamp": "2026-09-10T00:00:00Z", "payload": payload}
    def turn(identity):
        return [row("event_msg", {"type": "task_started", "turn_id": identity}),
                row("turn_context", {"turn_id": identity, "model": model, "effort": "high", "multi_agent_version": version, "sandbox_policy": {"type": "read-only"}})]
    parent = [row("session_meta", {"id": "parent"})] + turn("parent-turn")
    names = ["spawn_agent", "followup_task"] if version == "v2" else [
        "spawn_agent", "wait_agent", "close_agent", "resume_agent", "send_input", "wait_agent", "close_agent"]
    for index, name in enumerate(names):
        args = ({"task_name": "reviewer", "message": "inspect"} if name == "spawn_agent" else {"target": "reviewer", "message": "inspect again"}) if version == "v2" else (
            {"message": "inspect"} if name == "spawn_agent" else {"ids": ["child"]} if name == "wait_agent" else {"id": "child", "message": "inspect again"})
        output = ({"task_name": "/root/reviewer"} if version == "v2" else {"agent_id": "child"}) if name == "spawn_agent" else (
            {"status": {"child": {"completed": "review"}}} if name == "wait_agent" else {"previous_status": {"completed": "review"}} if version == "v1" else "")
        parent.extend([row("response_item", {"type": "function_call", "namespace": namespace, "name": name, "call_id": str(index), "arguments": json.dumps(args)}),
                       row("response_item", {"type": "function_call_output", "call_id": str(index), "output": json.dumps(output)})])
    if version == "v2":
        parent.append(row("response_item", {"type": "agent_message", "author": "/root/reviewer", "recipient": "/root", "content": [{"type": "input_text", "text": "fixture review result"}]}))
    parent.extend([row("response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "patch-call", "input": "fixture source update"}),
                   row("response_item", {"type": "custom_tool_call_output", "call_id": "patch-call", "output": "Success"}),
                   row("event_msg", {"type": "task_complete", "turn_id": "parent-turn"})])
    child = [row("session_meta", {"id": "child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent", "agent_path": "/root/reviewer"}}}})]
    for identity in ("review", "followup"):
        child += turn(identity) + [
            row("response_item", {"type": "function_call", "name": "exec_command", "call_id": identity + "-read", "arguments": '{"cmd":"cat parent_task.py child_review.txt"}'}),
            row("response_item", {"type": "function_call_output", "call_id": identity + "-read", "output": "Process exited with code 0\nfixture source"}),
            row("event_msg", {"type": "task_complete", "turn_id": identity, "last_agent_message": "fixture review result"})]
    sessions = home / "sessions"
    sessions.mkdir()
    for name, rows in (("parent", parent), ("child", child)):
        (sessions / f"{name}.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
    output = home / "cli.jsonl"
    command = {"id": "command", "type": "command_execution", "command": '"$CODEXHUB_E2E_PYTHON" -m unittest -q test_parent_task.py'}
    events = [
        {"type": "thread.started", "thread_id": "parent"},
        {"type": "item.completed", "item": {"id": "patch", "type": "file_change", "status": "completed", "changes": [{"path": "parent_task.py", "kind": "update"}]}},
        {"type": "item.started", "item": command},
        {"type": "item.completed", "item": {**command, "status": "completed", "exit_code": 0}},
        {"type": "turn.completed"},
    ]
    output.write_text("\n".join(map(json.dumps, events)) + "\n")
    return output


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_complete_correlated_evidence_accepts_same_model_parent_and_child(tmp_path, version):
    runner = _runner_module()
    output = _complete_fixture(tmp_path, version)
    evidence = runner.collect_evidence(tmp_path, "xai/grok-4.6", version, parent_effort="high", child_effort="high", client_outputs=(output,))
    assert evidence["passed"] is True
    assert evidence["parent_test_tool_call_count"] == 1
    assert evidence["parent_child_relationships"] == [{"child": "child", "parent": "parent"}]


@pytest.mark.parametrize("fault", ["exit_code", "missing_start", "duplicate_item", "missing_terminal", "wrong_thread", "no_parent_edit", "echo_only"])
def test_execution_evidence_rejects_false_success(tmp_path, fault):
    runner = _runner_module()
    output = _complete_fixture(tmp_path)
    events = [json.loads(line) for line in output.read_text().splitlines()]
    if fault == "exit_code":
        events[3]["item"].pop("exit_code")
    elif fault == "missing_start":
        events.pop(2)
    elif fault == "duplicate_item":
        events.insert(4, events[3])
    elif fault == "missing_terminal":
        events.pop()
    elif fault == "wrong_thread":
        events[0]["thread_id"] = "child"
    elif fault == "no_parent_edit":
        events.pop(1)
    else:
        events[3]["item"]["command"] = 'echo "$CODEXHUB_E2E_PYTHON -m unittest -q test_parent_task.py"'
    output.write_text("\n".join(map(json.dumps, events)))
    try:
        evidence = runner.collect_evidence(tmp_path, "xai/grok-4.6", parent_effort="high", child_effort="high", client_outputs=(output,))
    except RuntimeError:
        return
    assert evidence["passed"] is False


@pytest.mark.parametrize("fault", ["missing_result", "duplicate_id", "effort_drift", "second_turn_delegation", "bad_line"])
def test_history_evidence_rejects_gaps_and_cross_turn_delegation(tmp_path, fault):
    runner = _runner_module()
    output = _complete_fixture(tmp_path)
    path = tmp_path / "sessions/parent.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if fault == "missing_result":
        rows.pop(4)
    elif fault == "duplicate_id":
        rows.insert(4, rows[3])
    elif fault == "effort_drift":
        rows[2]["payload"]["effort"] = "low"
    elif fault == "second_turn_delegation":
        rows.extend([{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "second"}},
                     {"type": "response_item", "payload": {"type": "function_call", "namespace": "collaboration", "name": "list_agents", "call_id": "second", "arguments": "{}"}},
                     {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "second", "output": "{}"}}])
    path.write_text("\n".join(map(json.dumps, rows)) + ("\n\0\n" if fault == "bad_line" else "\n"))
    try:
        evidence = runner.collect_evidence(tmp_path, "xai/grok-4.6", parent_effort="high", child_effort="high", client_outputs=(output,))
    except RuntimeError:
        return
    assert evidence["passed"] is False


def test_read_only_label_does_not_replace_child_execution_evidence(tmp_path):
    runner = _runner_module()
    output = _complete_fixture(tmp_path)
    path = tmp_path / "sessions/child.jsonl"
    with path.open("a") as stream:
        stream.write(_row({"type": "custom_tool_call", "name": "apply_patch", "call_id": "child-write", "input": "update parent_task.py"}) + "\n")
        stream.write(_row({"type": "custom_tool_call_output", "call_id": "child-write", "output": "Success"}) + "\n")
    evidence = runner.collect_evidence(tmp_path, "xai/grok-4.6", parent_effort="high", child_effort="high", client_outputs=(output,))
    assert evidence["passed"] is False


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_second_parent_turn_can_complete_without_any_new_collaboration(tmp_path, version):
    runner = _runner_module()
    first = _complete_fixture(tmp_path, version)
    parent = tmp_path / "sessions/parent.jsonl"
    rows = [json.loads(line) for line in parent.read_text().splitlines()]
    second = json.loads(json.dumps([rows[1], rows[2], rows[-3], rows[-2], rows[-1]]))
    for row in second:
        if "turn_id" in row["payload"]:
            row["payload"]["turn_id"] = "second-turn"
        if "call_id" in row["payload"]:
            row["payload"]["call_id"] = "second-patch"
    parent.write_text("\n".join(map(json.dumps, rows + second)) + "\n")
    output = tmp_path / "cli-second.jsonl"
    output.write_text(first.read_text().replace("test_parent_task.py", "test_resume_turn.py"))
    evidence = runner.collect_evidence(tmp_path, "xai/grok-4.6", version, parent_effort="high", child_effort="high", client_outputs=(first, output))
    assert evidence["passed"] is True
    assert evidence["parent_turn_ids"] == ["parent-turn", "second-turn"]
    assert evidence["parent_resume_test_tool_call_count"] == 1


def test_no_child_is_a_recorded_lifecycle_failure_not_a_missing_parent(tmp_path):
    runner = _runner_module()
    output = _complete_fixture(tmp_path)
    (tmp_path / "sessions/child.jsonl").unlink()
    evidence = runner.collect_evidence(tmp_path, "xai/grok-4.6", parent_effort="high", child_effort="high", client_outputs=(output,))
    assert evidence["passed"] is False
    assert evidence["parent_models"] == ["xai/grok-4.6"]
    assert evidence["client_trace"][0]["terminal"] == "turn.completed"


def test_missing_parent_edit_provenance_is_unverified_not_model_failure(tmp_path):
    runner = _runner_module()
    output = _complete_fixture(tmp_path)
    events = [json.loads(line) for line in output.read_text().splitlines()]
    events = [event for event in events if event.get("item", {}).get("type") != "file_change"]
    output.write_text("\n".join(map(json.dumps, events)) + "\n")
    evidence = runner.collect_evidence(tmp_path, "xai/grok-4.6", parent_effort="high", child_effort="high", client_outputs=(output,))
    assert evidence["passed"] is False
    assert evidence["status"] == "unverified"
    assert evidence["missing_evidence"] == ["parent_source_edit_attribution"]


def test_fixture_cannot_fake_success_by_exiting_the_trusted_validator(tmp_path):
    runner = _runner_module()
    runner._write_parent_fixture(tmp_path)
    (tmp_path / "parent_task.py").write_text("import os\nos._exit(0)\ndef normalize(value):\n    return 'BROKEN'\n")
    checked = runner._verify_parent_fixture(tmp_path)
    assert checked["parent_fixture_fixed"] is False


def test_gateway_worker_rejection_is_not_attributed_to_the_provider():
    runner = _runner_module()
    signals = [{"code": "upstream.error", "source": "xai", "failure_class": "permanent", "type": "external_worker_binding_rejected"}]
    assert runner.classify_failure_signals(signals) == "gateway_collaboration_boundary"
