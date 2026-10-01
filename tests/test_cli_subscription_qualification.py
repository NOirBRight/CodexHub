"""Qualification observes real terminal/identity facts and exposes only hashes."""
from __future__ import annotations

import io
import json
from pathlib import Path
import re

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
    return root


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
