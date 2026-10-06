"""Qualification observes real terminal/identity facts and exposes only hashes."""
from __future__ import annotations

import io
import json
from pathlib import Path
import re
import subprocess

import pytest

from scripts import qualify_cli_subscriptions as qualify


@pytest.mark.parametrize("protocol", qualify.PROTOCOLS)
def test_native_protocol_requests_keep_model_history_and_declared_tools(protocol):
    history = [{"role": "user", "content": "private-prompt"}]
    tools = [{"name": "real", "parameters": {"type": "object"}}]
    body = qualify.request_for(protocol, "cursor-subscription/exact/high-fast", history, tools=tools, stream=True)
    assert body["model"] == "cursor-subscription/exact/high-fast"
    assert body["input" if protocol == "responses" else "messages"] is history
    if protocol == "messages":
        assert body["max_tokens"] == 1024
        assert body["tools"][0]["input_schema"] == tools[0]["parameters"]
    else:
        assert "max_tokens" not in body


@pytest.mark.parametrize("protocol,payload", [
    ("chat", {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-original", "type": "function", "function": {"name": "real", "arguments": '{"value":1}'}}]}, "finish_reason": "tool_calls"}]}),
    ("responses", {"status": "completed", "output": [{"type": "function_call", "id": "item-distinct",
        "call_id": "call-original", "name": "real", "arguments": '{"value":1}'}]}),
    ("messages", {"content": [{"type": "tool_use", "id": "call-original", "name": "real", "input": {"value": 1}}], "stop_reason": "tool_use"}),
])
def test_original_call_identity_and_typed_items_round_trip_without_fabrication(protocol, payload):
    reply = qualify.decode_reply(protocol, payload)
    assert reply.calls[0].id == "call-original"
    assert reply.calls[0].arguments == {"value": 1}
    if protocol == "responses":
        assert reply.calls[0].item_id == "item-distinct"
        assert reply.history_items == payload["output"]
    result = qualify.tool_result_item(protocol, reply.calls[0], "actual-executed-result")
    serialized = json.dumps(result)
    assert "call-original" in serialized and "item-distinct" not in serialized
    assert "actual-executed-result" in serialized
    assert not reply.usage_present


@pytest.mark.parametrize("protocol,events", [
    ("chat", [{"choices": [{"delta": {"content": "answer\u200b"}, "finish_reason": "stop"}]}, "[DONE]"]),
    ("responses", [{"type": "response.output_text.delta", "delta": "answer\u200b"},
                   {"type": "response.completed", "response": {"status": "completed"}}]),
    ("messages", [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "answer\u200b"}},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"}]),
])
def test_terminal_events_and_exact_output_are_required(protocol, events):
    result = qualify.decode_stream(protocol, iter(events))
    assert result.text == "answer\u200b"
    assert not result.usage_present
    with pytest.raises(qualify.QualificationFailure, match="incomplete-stream"):
        qualify.decode_stream(protocol, iter(events[:-1]))


def test_sse_errors_are_bounded_and_do_not_disclose_raw_upstream_content():
    stream = io.BytesIO(b'data: {"type":"error","error":{"code":"not-eligible","message":"secret-token private@example.com"}}\n\n')
    with pytest.raises(qualify.QualificationFailure) as error:
        list(qualify.sse_events(stream))
    assert error.value.code == "not-eligible" and "secret" not in str(error.value)
    malformed = io.BytesIO(b'data: {"message":"private"}\n')
    with pytest.raises(qualify.QualificationFailure, match="incomplete-sse-frame"):
        list(qualify.sse_events(malformed))


def test_reply_evidence_has_hashes_without_prompts_or_results():
    reply = qualify.Reply("private-result", [qualify.CallerTool("opaque-call", "read", {"secret": "private-argument"})], [], False, "stop")
    evidence = json.dumps(reply.evidence())
    assert "private-result" not in evidence and "private-argument" not in evidence and "opaque-call" not in evidence
    assert qualify.fingerprint("private-result") in evidence


class FakeGateway:
    """Caller-observable fixture port, without testing private Gateway methods."""
    denied = False
    starts = 0

    def __init__(self, repo, root, provider, model, timeout):
        self.root, self.timeout = root, timeout
        self.process = type("Process", (), {"pid": 100})()
        self.completed = {}

    def start(self):
        type(self).starts += 1

    def stop(self):
        pass

    def restart(self):
        self.process = type("Process", (), {"pid": 101})()
        self.start()

    def exchange(self, protocol, payload):
        if self.denied:
            raise qualify.QualificationFailure("not-eligible", 403)
        history = payload["input" if protocol == "responses" else "messages"]
        prompt = history[-1].get("content", "")
        if isinstance(prompt, str) and prompt.startswith("Reply exactly: "):
            text = prompt.split(": ", 1)[1]
            return qualify.Reply(text, [], [{"role": "assistant", "content": text}], False, "stop", 3 if payload["stream"] else 0)
        if len(history) == 1 and isinstance(prompt, str) and prompt.startswith("Call read_fixture"):
            fixture_id = re.search(r"fixture_id=([a-f0-9]+)", prompt)[1]
            call = qualify.CallerTool("native-call-" + protocol, "read_fixture", {"fixture_id": fixture_id}, "separate-item" if protocol == "responses" else None)
            return qualify.Reply("", [call], [{"role": "assistant", "content": "completed native tool request"}], False, "tool_calls")
        if isinstance(prompt, str) and prompt.startswith("What was"):
            assert self.process.pid == 101
            value = self.completed[protocol]
        else:
            result = history[-1]
            if protocol == "chat":
                value = result["content"]
                assert result["tool_call_id"] == "native-call-chat"
            elif protocol == "responses":
                value = result["output"]
                assert result["call_id"] == "native-call-responses"
            else:
                value = result["content"][0]["content"]
                assert result["content"][0]["tool_use_id"] == "native-call-messages"
            assert value.startswith("fixture-")
            self.completed[protocol] = value
        return qualify.Reply(value, [], [{"role": "assistant", "content": value}], False, "stop")

    def cancel_stream(self, model):
        return {"caller_wait_ended": True, "upstream_socket_cleanup_observed": True,
                "billing_cessation_claimed": False}


def minimal_repo(tmp_path):
    root = tmp_path / "repo"
    for name in ("src-python", "config", "model-catalogs"):
        (root / name).mkdir(parents=True)
        (root / name / "candidate.txt").write_text("read-only source")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Qualification Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "frozen candidate"], check=True)
    return root


@pytest.mark.parametrize("change", ["staged", "unstaged", "untracked"])
def test_dirty_runtime_fails_before_gateway_start_or_qualification(tmp_path, monkeypatch, change):
    root = minimal_repo(tmp_path)
    path = root / "src-python" / ("untracked.py" if change == "untracked" else "candidate.txt")
    path.write_text("uncommitted runtime change")
    if change == "staged":
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    class UnreachableGateway(FakeGateway):
        def __init__(self, *args, **kwargs):
            raise AssertionError("dirty candidate must not launch a Gateway")
    monkeypatch.setattr(qualify, "PrivateGateway", UnreachableGateway)
    result = qualify.run_qualification(root, "cursor-subscription", "exact-model")
    assert result["candidate_runtime_tracked_dirty"]
    assert not result["candidate_sha_is_exact_runtime"]
    assert not result["ordinary_qualified"] and not result["caller_cancel_qualified"]
    assert result["private_artifacts_removed"]
    assert result["cases"] == [{"case": "gateway.setup", "state": "failed", "code": "candidate-runtime-not-clean"}]


def test_entire_public_harness_keeps_real_results_and_restart_without_qualification_overclaim(tmp_path, monkeypatch):
    monkeypatch.setattr(qualify, "PrivateGateway", FakeGateway)
    result = qualify.run_qualification(minimal_repo(tmp_path), "cursor-subscription", "exact-model")
    assert result["ordinary_qualified"]
    assert result["gateway_restart_observed"] and result["private_artifacts_removed"]
    assert not result["generation_qualified"] and not result["advanced_codemode_v2_qualified"] and not result["dual_platform_qualified"]
    assert len(result["cases"]) == 16
    assert result["windows_gate"]["state"] == "blocked"
    assert "fixture-" not in json.dumps(result)


def test_policy_denial_remains_failed_and_is_not_retried_for_other_protocols(tmp_path, monkeypatch):
    class Denied(FakeGateway):
        denied = True
    monkeypatch.setattr(qualify, "PrivateGateway", Denied)
    result = qualify.run_qualification(minimal_repo(tmp_path), "claude-subscription", "exact-model")
    assert not result["ordinary_qualified"] and result["private_artifacts_removed"]
    assert len(result["cases"]) == 1
    assert result["cases"][0]["code"] == "not-eligible"
    assert result["cases"][0]["state"] == "failed"


def test_runtime_snapshot_is_frozen_before_restart(tmp_path):
    repo = minimal_repo(tmp_path)
    destination = tmp_path / "frozen"
    digest = qualify.freeze_candidate(repo, destination)
    (repo / "src-python/candidate.txt").write_text("later worker edit")
    assert (destination / "src-python/candidate.txt").read_text() == "read-only source"
    assert len(digest) == 64


def test_protocol_delta_skips_previously_qualified_cancellation(tmp_path, monkeypatch):
    class ProtocolOnly(FakeGateway):
        def cancel_stream(self, model):
            raise AssertionError("a protocol delta must not repeat cancellation")
    monkeypatch.setattr(qualify, "PrivateGateway", ProtocolOnly)
    result = qualify.run_qualification(minimal_repo(tmp_path), "cursor-subscription", "exact-model",
                                       protocols=("responses",), include_cancel=False)
    assert result["ordinary_qualified"] and result["private_artifacts_removed"]
    assert [case["case"] for case in result["cases"]] == ["responses.text", "responses.stream",
        "responses.caller-tool", "responses.tool-result", "responses.restart-history-fresh-caller"]
    assert result["protocols"] == ["responses"] and not result["caller_cancel_qualified"]


def test_installed_cli_version_reads_package_path_without_cli_execution(tmp_path, monkeypatch):
    binary = tmp_path / "2026.09.28-64d2043" / "bin" / "cursor-agent"
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(qualify.shutil, "which", lambda name: str(binary))
    assert qualify.installed_cli_version("cursor-subscription") == "2026.09.28-64d2043"


def test_tool_continuation_delta_does_not_repeat_text_stream_or_cancellation(tmp_path, monkeypatch):
    class ToolContinuation(FakeGateway):
        def exchange(self, protocol, payload):
            history = payload["input" if protocol == "responses" else "messages"]
            assert not payload["stream"]
            assert not any(isinstance(item.get("content"), str) and item["content"].startswith("Reply exactly:") for item in history)
            return super().exchange(protocol, payload)
        def cancel_stream(self, model):
            raise AssertionError("this tool continuation delta must not repeat cancellation")
    monkeypatch.setattr(qualify, "PrivateGateway", ToolContinuation)
    result = qualify.run_qualification(minimal_repo(tmp_path), "cursor-subscription", "exact-model",
                                       protocols=("responses",), include_cancel=False, include_text=False)
    assert result["ordinary_qualified"] and result["ordinary_scope"] == "tool-continuation-only"
    assert [case["case"] for case in result["cases"]] == ["responses.caller-tool", "responses.tool-result",
                                                         "responses.restart-history-fresh-caller"]


@pytest.mark.parametrize("scenario,code,windows", [
    ("active", None, False),
    ("natural-finish", "cancel-response-already-ended", False),
    ("natural-finish-during-observation", "cancel-response-already-ended", False),
    ("first-text-after-deadline", "cancel-first-text-timeout", False),
    ("no-text", "cancel-first-text-unobserved", False),
    ("cleanup-unobserved", "cancel-cleanup-unobserved", False),
    ("cleanup-observer-missing", "cancel-cleanup-unobserved", False),
    ("ownership-unobserved", "cancel-ownership-unobserved", False),
    ("inactive-after-text", "cancel-request-inactive", False),
    ("denied", "not-eligible", False),
    pytest.param("active", None, True, id="windows-active"),
    pytest.param("ownership-unobserved", "cancel-ownership-unobserved", True, id="windows-unavailable"),
    pytest.param("identity-mismatch", "cancel-ownership-unobserved", True, id="windows-identity-mismatch"),
    pytest.param("inactive-after-text", "cancel-request-inactive", True, id="windows-inactive"),
    pytest.param("cleanup-unobserved", "cancel-cleanup-unobserved", True, id="windows-cleanup"),
    pytest.param("cleanup-observer-missing", "cancel-cleanup-unobserved", True, id="windows-cleanup-unavailable"),
    pytest.param("natural-finish-during-observation", "cancel-response-already-ended", True, id="windows-terminal"),
    pytest.param("first-text-after-deadline", "cancel-first-text-timeout", True, id="windows-late-text"),
    pytest.param("denied", "not-eligible", True, id="windows-denied"),
])
def test_after_first_text_cancel_observes_real_sse_and_owned_cleanup(monkeypatch, scenario, code, windows):
    """Real local HTTP/SSE; socket ownership is controlled engineering evidence."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    active, disconnected = threading.Event(), threading.Event()
    finish_requested = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(403 if scenario == "denied" else 200)
            self.send_header("Connection", "close")
            self.end_headers()
            if scenario == "denied":
                self.wfile.write(b'{"error":{"code":"not-eligible","message":"private-denial"}}')
                return
            active.set()
            self.wfile.write(b'data: {"choices":[{"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n')
            if scenario == "first-text-after-deadline":
                while not finish_requested.wait(.005):
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":""},"finish_reason":null}]}\n\n')
                    self.wfile.flush()
                threading.Event().wait(.03)
            if scenario != "no-text":
                # A terminal in the same text frame must beat cancellation.
                finish = "stop" if scenario == "natural-finish" else None
                self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": "private-text\u200b"},
                    "finish_reason": finish}]}) + "\n\n").encode())
            if scenario in {"natural-finish", "no-text"}:
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
                self.wfile.flush()
                disconnected.set()
                return
            self.wfile.flush()
            if scenario == "natural-finish-during-observation":
                assert finish_requested.wait(5)
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
                self.wfile.flush()
                disconnected.set()
                return
            self.connection.settimeout(5)
            try:
                self.connection.recv(1)
            finally:
                disconnected.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Gateway(qualify.PrivateGateway):
        def __init__(self):
            self.port = server.server_port
            self.key = "private-key"
            self.timeout = .15 if scenario == "first-text-after-deadline" else .5
            self.process = type("Process", (), {"pid": 123, "poll": lambda self: None})()
            self.windows_tcp = WindowsObservation() if windows else None

    def upstream_count(pid):
        assert pid == 123
        if scenario == "ownership-unobserved":
            return None
        if scenario == "cleanup-observer-missing" and disconnected.is_set():
            return None
        if scenario == "inactive-after-text":
            return 0
        if scenario == "first-text-after-deadline" and active.is_set() and not disconnected.is_set():
            finish_requested.set()
            threading.Event().wait(.07)
            return 1
        if scenario == "natural-finish-during-observation" and active.is_set() and not disconnected.is_set():
            finish_requested.set()
            assert disconnected.wait(2)
            # The socket snapshot can precede terminal delivery. Allow the
            # real reader to observe it while this observation is in flight.
            threading.Event().wait(.05)
            return 1
        if disconnected.is_set() and scenario != "cleanup-unobserved":
            return 0
        return int(active.is_set())

    class WindowsObservation:
        """Controlled adapter seam; no native API or creation proof is claimed."""
        def observe(self):
            if scenario == "identity-mismatch":
                return None, None
            proxy = upstream_count(123)
            return (1 + int(active.is_set() and not disconnected.is_set()), proxy) if proxy is not None else (None, None)

    monkeypatch.setattr(qualify, "_upstream_tls_count", (lambda pid: None) if windows else upstream_count)
    monkeypatch.setattr(qualify, "_socket_count", lambda pid: None if windows else 1 + int(active.is_set() and not disconnected.is_set()))
    try:
        if code:
            with pytest.raises(qualify.QualificationFailure) as error:
                Gateway().cancel_stream_after_first_text("cursor-subscription/exact/high-fast")
            assert error.value.code == code
            evidence = error.value.evidence
        else:
            evidence = Gateway().cancel_stream_after_first_text("cursor-subscription/exact/high-fast")
            assert evidence["first_nonempty_text_observed"]
            assert evidence["request_active_at_disconnect"]
            assert evidence["caller_wait_ended"] and evidence["upstream_socket_cleanup_observed"]
            assert not evidence["terminal_observed_before_cancel"]
            assert not evidence["caller_finished_before_cancel"]
            assert evidence["first_text_sha256"] == qualify.fingerprint("private-text\u200b")
            assert evidence["first_text_elapsed_seconds"] <= evidence["disconnect_elapsed_seconds"]
        assert requests[0]["model"] == "cursor-subscription/exact/high-fast"
        assert requests[0]["stream"] and "max_tokens" not in requests[0]
        if evidence:
            assert evidence["cancel_phase"] == "after-first-nonempty-text"
            assert not evidence["billing_cessation_claimed"]
        if scenario == "first-text-after-deadline":
            assert evidence["first_text_elapsed_seconds"] > evidence["wait_bound_seconds"]
            assert evidence["request_active_at_disconnect"] and evidence["caller_disconnect_observed"]
            assert evidence["caller_wait_ended"] and evidence["upstream_socket_cleanup_observed"]
        assert evidence["caller_wait_ended"]
        if scenario not in {"natural-finish", "natural-finish-during-observation", "no-text", "denied"}:
            assert evidence["caller_disconnect_observed"]
        if windows:
            assert evidence["socket_baseline"] is None and evidence["socket_after_cancel"] is None
            assert evidence["upstream_tls_baseline"] is None and evidence["upstream_tls_after_cancel"] is None
            assert evidence["socket_observation_kind"] == "windows-owned-tcp-endpoints"
            if not code:
                assert evidence["owned_tcp_endpoint_baseline"] == evidence["owned_tcp_endpoint_after_cancel"] == 1
                assert evidence["established_remote443_proxy_endpoint_at_disconnect"] == 1
                assert evidence["windows_tcp_endpoint_cleanup_observed"]
            if scenario in {"ownership-unobserved", "identity-mismatch"}:
                assert evidence["owned_tcp_endpoint_baseline"] is None
                assert not evidence["upstream_socket_cleanup_observed"]
        serialized = json.dumps(evidence)
        assert all(secret not in serialized for secret in ("private-text", "private-key", "private-denial"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_after_first_text_selection_keeps_upstream_wait_report_meaning(tmp_path, monkeypatch):
    class AfterText(FakeGateway):
        def cancel_stream(self, model):
            raise AssertionError("explicit text scenario must not repeat upstream-wait")

        def cancel_stream_after_first_text(self, model):
            assert model == "cursor-subscription/exact-model"
            return {"cancel_phase": "after-first-nonempty-text", "first_nonempty_text_observed": True,
                    "request_active_at_disconnect": True, "terminal_observed_before_cancel": False,
                    "caller_wait_ended": True, "upstream_socket_cleanup_observed": True,
                    "billing_cessation_claimed": False}

    monkeypatch.setattr(qualify, "PrivateGateway", AfterText)
    result = qualify.run_qualification(minimal_repo(tmp_path), "cursor-subscription", "exact-model",
        protocols=(), include_cancel=False, cancel_after_first_text=True)
    assert result["caller_cancel_after_first_text_qualified"]
    assert not result["caller_cancel_qualified"] and not result["ordinary_qualified"]
    assert result["private_artifacts_removed"] and not result["generation_qualified"]
    assert [row["case"] for row in result["cases"]] == ["chat.caller-cancel-after-first-text"]


@pytest.mark.parametrize("flag,key", [
    ("--cancel-only", "caller_cancel_qualified"),
    ("--cancel-after-first-text-only", "caller_cancel_after_first_text_qualified"),
])
def test_cli_selects_one_cancel_phase_and_uses_its_own_result(tmp_path, monkeypatch, flag, key):
    repo = tmp_path / "repo"
    (repo / "src-python").mkdir(parents=True)
    (repo / "src-python/codex_proxy.py").touch()
    calls = []

    def qualification(repo, provider, model, **kwargs):
        calls.append(kwargs)
        return {"ordinary_qualified": False, "caller_cancel_qualified": key == "caller_cancel_qualified",
                "caller_cancel_after_first_text_qualified": key == "caller_cancel_after_first_text_qualified",
                "cases": [], "private_artifacts_removed": True}

    monkeypatch.setattr(qualify, "run_qualification", qualification)
    assert qualify.main(["--repo-root", str(repo), "--provider", "cursor-subscription", "--model", "exact-model",
                         "--output", str(tmp_path / "result.json"), flag]) == 0
    assert calls[0]["protocols"] == ()
    assert calls[0]["include_cancel"] == (flag == "--cancel-only")
    assert calls[0]["cancel_after_first_text"] == (flag == "--cancel-after-first-text-only")


@pytest.fixture
def owned_windows_adapter(tmp_path, monkeypatch):
    """Actual owned Popen, with controlled native calls; not Windows OS proof."""
    import ctypes
    import sys

    process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE)
    if not hasattr(process, "_handle"):
        monkeypatch.setattr(process, "_handle", 12345, raising=False)
    state = {"pid": process.pid, "creation": 42, "wait": 258, "times_ok": True,
             "size": 4, "probe_code": 122, "fetch_code": 0, "calls": [], "rows": {2: [], 23: []}}

    class NativeCall:
        def __init__(self, function):
            self.function = function
        def __call__(self, *args):
            return self.function(*args)

    def get_times(handle, creation, exit_time, kernel, user):
        ctypes.cast(creation, ctypes.POINTER(ctypes.c_uint32))[0] = state["creation"]
        return state["times_ok"]

    def tcp_table(buffer, size, ordered, family, table_class, reserved):
        assert table_class == 5 and reserved == 0
        state["calls"].append((family, buffer is None))
        pointer = ctypes.cast(size, ctypes.POINTER(ctypes.c_uint32))
        rows = state["rows"][family]
        payload = len(rows).to_bytes(4, "little") + b"".join(bytes(row) for row in rows)
        if buffer is None:
            pointer[0] = max(state["size"], len(payload))
            return state["probe_code"]
        if state["fetch_code"] != 0:
            return state["fetch_code"]
        payload = state.get("payload", payload)
        assert len(payload) <= pointer[0]
        ctypes.memmove(buffer, payload, len(payload))
        pointer[0] = len(payload)
        return 0

    kernel = type("Kernel", (), {})()
    kernel.GetProcessId = NativeCall(lambda handle: state["pid"])
    kernel.WaitForSingleObject = NativeCall(lambda handle, timeout: state["wait"])
    kernel.GetProcessTimes = NativeCall(get_times)
    table = NativeCall(tcp_table)
    dll = type("Dll", (), {"GetExtendedTcpTable": table})()
    monkeypatch.setattr(qualify.ctypes, "WinDLL", lambda name, **kwargs: kernel if name == "kernel32" else dll, raising=False)
    try:
        yield qualify.WindowsOwnedTcp(process), state
    finally:
        process.stdin.close()
        process.wait(timeout=3)


def test_windows_owned_tcp_native_layout_network_port_filter_and_transient_rows(owned_windows_adapter):
    import ctypes
    import socket

    observer, state = owned_windows_adapter
    assert observer.creation == 42
    for family, row_type, stride, pid_offset, port_offset in (
        (2, qualify._Tcp4Row, 24, 20, 16), (23, qualify._Tcp6Row, 56, 52, 44)
    ):
        assert ctypes.sizeof(row_type) == stride
        assert row_type.pid.offset == pid_offset and row_type.remote_port.offset == port_offset
        for pid, status, port in ((observer.pid, 5, 443), (observer.pid, 5, 444),
                                  (observer.pid, 2, 443), (observer.pid, 8, 443), (observer.pid + 1, 5, 443)):
            row = row_type()
            row.pid, row.state, row.remote_port = pid, status, socket.htons(port)
            state["rows"][family].append(row)
    assert observer.observe() == (8, 2)
    assert state["calls"] == [(2, True), (2, False), (23, True), (23, False)]
    assert not any("rows" in name or "buffer" in name for name in vars(observer))
    state["rows"] = {2: [], 23: []}
    assert observer.observe() == (0, 0)


@pytest.mark.parametrize("failure", ["pid", "creation", "exited", "wait-failed", "times", "missing-api",
                                     "probe-error", "oversize", "growth", "fetch-error", "short", "truncated"])
def test_windows_owned_tcp_unavailable_never_reuses_zero_or_another_identity(owned_windows_adapter, failure):
    observer, state = owned_windows_adapter
    assert observer.observe() == (0, 0)
    state["calls"].clear()
    if failure == "pid":
        state["pid"] += 1
    elif failure == "creation":
        state["creation"] += 1
    elif failure == "exited":
        state["wait"] = 0
    elif failure == "wait-failed":
        state["wait"] = 0xffffffff
    elif failure == "times":
        state["times_ok"] = False
    elif failure == "missing-api":
        observer.table = None
    elif failure == "probe-error":
        state["probe_code"] = 5
    elif failure == "oversize":
        state["size"] = 1024 * 1024 + 1
    elif failure == "growth":
        state["fetch_code"] = 122
    elif failure == "fetch-error":
        state["fetch_code"] = 87
    elif failure == "short":
        state["payload"] = b"\x00" * 3
    else:
        state["payload"] = b"\x01\x00\x00\x00"
    assert observer.observe() == (None, None)
    if failure == "growth":
        assert state["calls"] == [(2, True)] + [(2, False)] * 3
    if failure == "oversize":
        assert state["calls"] == [(2, True)]


def test_windows_owned_tcp_rejects_unowned_schema_and_identity_change_during_snapshot(owned_windows_adapter):
    observer, state = owned_windows_adapter
    assert qualify.WindowsOwnedTcp(type("Process", (), {"pid": observer.pid, "_handle": observer.handle})()).observe() == (None, None)
    original = observer.table.function
    def changed_after_fetch(*args):
        result = original(*args)
        if args[0] is not None and args[3] == 23:
            state["creation"] += 1
        return result
    observer.table.function = changed_after_fetch
    assert observer.observe() == (None, None)


def test_windows_owned_tcp_binding_failure_stays_unavailable(owned_windows_adapter, monkeypatch):
    observer, state = owned_windows_adapter
    state["creation"] = 0
    assert qualify.WindowsOwnedTcp(observer.process).observe() == (None, None)
    def missing_dll(*args, **kwargs):
        raise OSError("controlled missing DLL")
    monkeypatch.setattr(qualify.ctypes, "WinDLL", missing_dll)
    assert qualify.WindowsOwnedTcp(observer.process).observe() == (None, None)
