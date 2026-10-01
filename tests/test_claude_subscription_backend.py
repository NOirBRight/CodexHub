"""Public exchange tests cross real subprocess and private MCP boundaries."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from claude_subscription_backend import stream_chat
from claude_subscription_mcp import run_mcp
from subscription_backend_contract import BackendError


def event(kind, **values):
    return {"type": "stream_event", "event": {"type": kind, **values}}


def text_events(text="answer\u200b"):
    return [event("message_start", message={"model": "claude-exact"}),
            event("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            event("content_block_delta", index=0, delta={"type": "text_delta", "text": text}),
            event("content_block_stop", index=0),
            event("message_delta", delta={"stop_reason": "end_turn"}), event("message_stop")]


def tool_events(calls):
    result = [event("message_start", message={"model": "claude-exact"})]
    for index, call in enumerate(calls):
        raw = json.dumps(call["arguments"], ensure_ascii=False)
        result += [event("content_block_start", index=index, content_block={"type": "tool_use",
                   "id": call["id"], "name": "mcp__codexhub__" + call["name"], "input": {}}),
                   event("content_block_delta", index=index, delta={"type": "input_json_delta", "partial_json": raw[:3]}),
                   event("content_block_delta", index=index, delta={"type": "input_json_delta", "partial_json": raw[3:]}),
                   event("content_block_stop", index=index)]
    return result + [event("message_delta", delta={"stop_reason": "tool_use"}), event("message_stop")]


_FAKE_CLI = '''import json, os, subprocess, sys, time
from pathlib import Path
args = json.loads(sys.argv[1])
data = json.loads(sys.argv[2])
line = sys.stdin.readline()
Path(data["capture"]).write_text(line, encoding="utf-8")
if data.get("child"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
    Path(data["child"]).write_text(str(child.pid))
helpers = []
if data.get("calls"):
    config = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
    server = config["mcpServers"]["codexhub"]
    for call in data["calls"]:
        helper = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        helper.stdin.write(json.dumps({"jsonrpc":"2.0", "id":1, "method":"initialize"}) + "\\n")
        helper.stdin.flush()
        assert json.loads(helper.stdout.readline())["result"]["serverInfo"]["name"] == "codexhub-caller-tools"
        helper.stdin.write(json.dumps({"jsonrpc":"2.0", "id":2, "method":"tools/list"}) + "\\n")
        helper.stdin.flush()
        listed = json.loads(helper.stdout.readline())["result"]["tools"]
        assert any(row["name"] == call["name"] for row in listed)
        helper.stdin.write(json.dumps({"jsonrpc":"2.0", "id":3, "method":"tools/call", "params":{
            "name":call["name"], "arguments":call["arguments"], "_meta":{"claudecode/toolUseId":call["id"]}}}) + "\\n")
        helper.stdin.flush()
        helpers.append(helper)
    time.sleep(.15)
if data.get("change"):
    Path(data["change"]).write_text("{}")
for value in data["events"]:
    print(json.dumps(value, ensure_ascii=False), flush=True)
    time.sleep(.01)
time.sleep(3600)
'''


@pytest.fixture
def exchange(tmp_path):
    home = tmp_path / "home"
    config = home / ".claude"
    config.mkdir(parents=True)
    auth = config / ".credentials.json"
    auth.write_text(json.dumps({"claudeAiOauth": {"accessToken": "source-private-token", "refreshToken": "refresh-private",
        "expiresAt": 4_000_000_000_000, "scopes": ["user:inference"]}, "mcpOAuth": {"private": "excluded"}}))
    (config / "settings.json").write_text('{"env":{"ANTHROPIC_BASE_URL":"http://gateway.invalid"}}')
    cli = tmp_path / "fake_cli.py"
    cli.write_text(_FAKE_CLI)
    capture = tmp_path / "capture.json"
    roots, children, commands = [], [], []
    env = {"PATH": os.environ.get("PATH", ""), "ANTHROPIC_API_KEY": "ambient-private",
           "ANTHROPIC_BASE_URL": "http://gateway.invalid", "HTTP_PROXY": "http://proxy.invalid",
           "NODE_OPTIONS": "--require=bad", "CLAUDECODE": "1"}

    def status(binary, args, *, env, cwd, timeout):
        roots.append(cwd)
        assert args == ["auth", "status", "--json"]
        assert env["HOME"] == str(cwd)
        assert not any(key in env for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "HTTP_PROXY", "NODE_OPTIONS", "CLAUDECODE"))
        account = Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
        assert set(json.loads(account.read_text())) == {"claudeAiOauth"}
        assert not account.with_name("settings.json").exists()
        if os.name != "nt":
            assert account.stat().st_mode & 0o777 == 0o600
            assert cwd.stat().st_mode & 0o777 == 0o700
        return subprocess.CompletedProcess([], 0, '{"loggedIn":true,"apiProvider":"firstParty","authMethod":"claude.ai"}', "")

    def invoke(payload=None, *, events=None, calls=None, cancel=None, timeout=3, change=False, child=False):
        data = {"capture": str(capture), "events": text_events() if events is None else events,
                "calls": calls or []}
        if change:
            data["change"] = str(auth)
        if child:
            data["child"] = str(tmp_path / "child.pid")
        def factory(command, **kwargs):
            commands.append(command)
            assert "--restricted" in command and "--permission-prompts" in command
            assert not any("bypass" in arg or "dangerously" in arg for arg in command)
            assert command[command.index("--tools") + 1] == ""
            process = subprocess.Popen([sys.executable, "-u", str(cli), json.dumps(command), json.dumps(data)], **kwargs)
            children.append(process)
            return process
        return stream_chat(payload or {"model": "claude-exact", "messages": [{"role": "user", "content": "answer"}]},
                           cancel=cancel or threading.Event(), timeout=timeout, source_home=home,
                           environ=env, binary=Path(sys.executable), status_runner=status, process_factory=factory)
    return invoke, auth, capture, roots, children, commands, tmp_path


def test_text_stream_preserves_output_and_reaps(exchange):
    invoke, _, capture, roots, children, commands, _ = exchange
    rows = list(invoke())
    assert len(rows) == 2
    assert rows[0]["choices"][0]["delta"] == {"role": "assistant", "content": "answer\u200b"}
    assert rows[-1]["choices"][0]["finish_reason"] == "stop"
    assert all(row["model"] == "claude-exact" for row in rows)
    assert not any("usage" in row for row in rows)
    assert children[0].poll() is not None and not roots[0].exists()
    prompt = json.loads(json.loads(capture.read_text())["message"]["content"])
    assert prompt["messages"] == [{"role": "user", "content": "answer"}]
    assert "--no-session-persistence" in commands[0]


def tool_payload():
    return {"model": "claude-exact", "messages": [{"role": "user", "content": "use tools"}],
            "tools": [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
                      for name in ("first", "second")]}


def test_actual_private_mcp_parallel_calls_and_exact_history_restart(exchange):
    invoke, _, capture, roots, children, commands, _ = exchange
    payload = tool_payload()
    calls = [{"id": "toolu-original-1", "name": "first", "arguments": {"task": "真实任务"}},
             {"id": "toolu-original-2", "name": "second", "arguments": {"value": 2}}]
    rows = list(invoke(payload, events=tool_events(calls), calls=calls))
    returned = [row["choices"][0]["delta"]["tool_calls"][0] for row in rows[:-1]]
    assert [row["id"] for row in returned] == [call["id"] for call in calls]
    assert [json.loads(row["function"]["arguments"]) for row in returned] == [call["arguments"] for call in calls]
    assert rows[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert all(child.poll() is not None for child in children)
    for row in returned:
        row.pop("index")
    payload["messages"] += [{"role": "assistant", "content": None, "tool_calls": returned},
                            {"role": "tool", "tool_call_id": "toolu-original-1", "content": "real-first-result"},
                            {"role": "tool", "tool_call_id": "toolu-original-2", "content": "real-second-result"},
                            {"role": "user", "content": "Continue same conversation"}]
    payload["messages"].insert(0, {"role": "system", "content": "original instructions"})
    list(invoke(payload))
    history = json.loads(json.loads(capture.read_text())["message"]["content"])
    assert history["messages"] == payload["messages"]
    assert "user context" in history["adaptation"]
    assert len(children) == 2 and roots[0] != roots[1] and all(not root.exists() for root in roots)
    assert not any("--system-prompt" in command for command in commands)


def test_callback_disagrees_with_native_call_fails_without_tool_delivery(exchange):
    invoke, *_ = exchange
    native = [{"id": "toolu-id", "name": "first", "arguments": {"task": "native"}}]
    callback = [{**native[0], "arguments": {"task": "different"}}]
    with pytest.raises(BackendError) as error:
        list(invoke(tool_payload(), events=tool_events(native), calls=callback))
    assert error.value.code == "backend-contract"


@pytest.mark.parametrize("events,code", [
    ([event("message_start"), {"type": "result", "is_error": True, "result": "organization disabled source-private-token"}], "not-eligible"),
    ([{"type": "result", "is_error": True, "result": "expired token private@example.com"}], "auth-required"),
    ([{"type": "result", "is_error": True, "errors": ["quota exhausted"]}], "rate-limited"),
    ([{"type": "system", "subtype": "init", "tools": ["Bash"]}], "backend-contract"),
    ([event("content_block_start", index=0, content_block={"type": "tool_use", "id": "id", "name": "Bash"})], "backend-contract"),
])
def test_errors_before_real_first_chunk_are_bounded(exchange, events, code):
    invoke, _, _, roots, children, *_ = exchange
    with pytest.raises(BackendError) as error:
        next(invoke(events=events))
    assert error.value.code == code
    assert "private" not in str(error.value)
    assert all(child.poll() is not None for child in children) and all(not root.exists() for root in roots)


def test_account_change_interrupts_before_any_chunk(exchange):
    invoke, *_ = exchange
    with pytest.raises(BackendError) as error:
        list(invoke(change=True))
    assert error.value.code == "account-changed"


@pytest.mark.parametrize("auth,code", [({}, "auth-required"),
    ({"accessToken": "token", "expiresAt": 1, "scopes": ["user:inference"]}, "auth-expired"),
    ({"accessToken": "token", "expiresAt": 4_000_000_000_000, "scopes": []}, "not-eligible")])
def test_auth_admission_never_launches_cli(exchange, auth, code):
    invoke, path, _, _, children, *_ = exchange
    path.write_text(json.dumps({"claudeAiOauth": auth}))
    with pytest.raises(BackendError) as error:
        list(invoke())
    assert error.value.code == code and children == []


@pytest.mark.parametrize("messages", [
    [{"role": "tool", "tool_call_id": "orphan", "content": "result"}],
    [{"role": "assistant", "tool_calls": [{"id": "pending", "type": "function", "function": {"name": "first", "arguments": "{}"}}]}],
    [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://private.invalid"}}]}],
])
def test_incomplete_history_and_unsupported_content_fail_visibly(exchange, messages):
    invoke, *_, children, _, _ = exchange
    with pytest.raises(BackendError) as error:
        list(invoke({"model": "claude-exact", "messages": messages}))
    assert error.value.code == "invalid-request" and children == []


def test_cancel_while_waiting_and_close_reap_process_group(exchange):
    invoke, _, _, roots, children, _, tmp_path = exchange
    cancel = threading.Event()
    errors = []
    def read():
        try:
            list(invoke(events=[], cancel=cancel, timeout=10, child=True))
        except BackendError as error:
            errors.append(error.code)
    thread = threading.Thread(target=read)
    thread.start()
    deadline = time.monotonic() + 2
    while not (tmp_path / "child.pid").exists() and time.monotonic() < deadline:
        time.sleep(.01)
    cancel.set()
    thread.join(timeout=1)
    assert not thread.is_alive() and errors == ["cancelled"]
    assert children[0].poll() is not None and not roots[0].exists()
    iterator = invoke(events=text_events())
    next(iterator)
    iterator.close()
    assert children[-1].poll() is not None and not roots[-1].exists()


def test_timeout_during_wait_is_bounded_and_cleans(exchange):
    invoke, _, _, roots, children, *_ = exchange
    with pytest.raises(BackendError) as error:
        list(invoke(events=[], timeout=.3))
    assert error.value.code == "timeout" and all(child.poll() is not None for child in children)
    assert all(not root.exists() for root in roots)


def test_mcp_only_declares_given_tools_and_notifications_have_no_reply():
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    output = io.StringIO()
    tools = [{"name": "first", "inputSchema": {"type": "object"}}]
    run_mcp(io.StringIO("\n".join(json.dumps(row) for row in requests) + "\n"), output, tools,
            "http://127.0.0.1:9876/private")
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [row["id"] for row in replies] == [1, 2]
    assert replies[1]["result"]["tools"] == tools


def test_supplied_usage_is_preserved_without_estimates(exchange):
    invoke, *_ = exchange
    values = text_events()
    values[0]["event"]["message"]["usage"] = {"input_tokens": 7, "output_tokens": 0,
        "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2}
    values[-2]["event"]["usage"] = {"output_tokens": 9}
    result = list(invoke(events=values))[-1]
    assert result["usage"] == {"prompt_tokens": 12, "completion_tokens": 9, "total_tokens": 21,
                               "prompt_tokens_details": {"cached_tokens": 3, "cache_creation_tokens": 2}}


def test_exact_model_never_substitutes_or_falls_back(exchange):
    invoke, *_ = exchange
    values = text_events()
    values[0]["event"]["message"]["model"] = "different-model"
    with pytest.raises(BackendError) as error:
        next(invoke(events=values))
    assert error.value.code == "backend-contract"


def test_expiry_during_idle_request_fails_and_reaps(exchange):
    invoke, path, _, roots, children, *_ = exchange
    auth = json.loads(path.read_text())
    auth["claudeAiOauth"]["expiresAt"] = time.time() + .25
    path.write_text(json.dumps(auth))
    with pytest.raises(BackendError) as error:
        list(invoke(events=[], timeout=3))
    assert error.value.code == "auth-expired"
    assert all(child.poll() is not None for child in children) and all(not root.exists() for root in roots)


@pytest.mark.parametrize("history", [
    [{"role": "assistant", "tool_calls": [
        {"id": "dup", "type": "function", "function": {"name": "first", "arguments": "{}"}},
        {"id": "dup", "type": "function", "function": {"name": "second", "arguments": "{}"}}]}],
    [{"role": "assistant", "tool_calls": [{"id": "id", "type": "function", "function": {"name": "first", "arguments": "{}"}}]},
     {"role": "tool", "tool_call_id": "different-id", "content": "result"}],
])
def test_ambiguous_or_wrong_call_identity_never_becomes_success(exchange, history):
    invoke, *_ = exchange
    with pytest.raises(BackendError) as error:
        list(invoke({"model": "claude-exact", "messages": history}))
    assert error.value.code == "invalid-request"


def test_mcp_cancelled_call_never_returns_placeholder_result(exchange):
    invoke, _, _, roots, children, *_ = exchange
    calls = [{"id": "toolu-cancelled", "name": "first", "arguments": {}}]
    cancel = threading.Event()
    errors = []
    def read():
        try:
            list(invoke(tool_payload(), events=[], calls=calls, cancel=cancel, timeout=5))
        except BackendError as error:
            errors.append(error.code)
    thread = threading.Thread(target=read)
    thread.start()
    time.sleep(.4)
    cancel.set()
    thread.join(timeout=1)
    assert not thread.is_alive() and errors == ["cancelled"]
    assert all(child.poll() is not None for child in children) and all(not root.exists() for root in roots)


def test_finish_chunk_is_observed_after_private_tree_is_removed(exchange):
    invoke, _, _, roots, children, *_ = exchange
    iterator = invoke()
    next(iterator)
    final = next(iterator)
    assert final["choices"][0]["finish_reason"] == "stop"
    assert children[0].poll() is not None and not roots[0].exists()
    iterator.close()


def test_forced_tool_choice_never_claims_plain_text_success(exchange):
    invoke, *_ = exchange
    payload = tool_payload()
    payload["tool_choice"] = "required"
    with pytest.raises(BackendError) as error:
        list(invoke(payload))
    assert error.value.code == "backend-contract"


def test_exact_vendor_slash_identity_is_preserved(exchange):
    invoke, *_ = exchange
    values = text_events()
    values[0]["event"]["message"]["model"] = "vendor/claude-exact"
    rows = list(invoke({"model": "vendor/claude-exact", "messages": [{"role": "user", "content": "text"}]}, events=values))
    assert all(row["model"] == "vendor/claude-exact" for row in rows)
