"""Prompt cache contracts at the planner, conversion and telemetry seams."""

import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

import gateway_compat
import gateway_events
import proxy_telemetry
from gateway_exchange import request_observability_for_attempt
from prompt_cache_policy import PromptCacheKeyPolicy, cache_key_policy_for_endpoint
from protocol_translation import NonForwardable, prepare_exchange
from route_plan import route_plan_for_request
from route_primitives import MutationPolicy
from runtime_tool_compatibility import ProtocolCapabilities, build_tool_compatibility_plan


def _tools():
    return [
        {"type": "namespace", "name": "vendor", "tools": [
            {"type": "function", "name": "run", "parameters": {"type": "object"}},
        ]},
        {"type": "custom", "name": "editor", "format": {"type": "text"}},
        {"type": "tool_search", "execution": "client"},
    ]


def _plan(tools, token="first"):
    return build_tool_compatibility_plan(
        tools, selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(), request_token=token,
    )


@pytest.mark.parametrize("change", ["append", "prepend", "reorder", "schema", "child", "capabilities"])
def test_unrelated_changes_preserve_existing_wire_tools_and_history(change, monkeypatch):
    monkeypatch.setattr(gateway_events, "write_proxy_event", lambda *a, **k: None)
    tools = _tools()
    payload = {"model": "test", "input": "Use the requested tool.", "tools": tools}
    upstream = {"name": "custom", "upstream_format": "responses", "tool_protocol": "chat_tools", "tool_surface_strategy": "eager"}

    def encode(p, u):
        return json.loads(gateway_compat.compatible_request_body(
            json.dumps(p).encode(), u, event_context={}, inject_codex_tools=False,
        ))

    before = encode(payload, upstream)
    next_payload = copy.deepcopy(payload)
    extra = {"type": "function", "name": "extra", "parameters": {"type": "object"}}
    if change == "append":
        next_payload["tools"].append(extra)
    elif change == "prepend":
        next_payload["tools"].insert(0, extra)
    elif change == "reorder":
        next_payload["tools"].reverse()
    elif change == "schema":
        next_payload["tools"][0]["tools"][0].update(description="New description", parameters={"type": "object", "properties": {"x": {"type": "string"}}})
    elif change == "child":
        next_payload["tools"][0]["tools"].insert(0, {"type": "function", "name": "stop", "parameters": {"type": "object"}})
    else:
        upstream = {**upstream, "tool_protocol_capabilities": {"hosted_lifecycles": ["web_search"]}}
    after = encode(next_payload, upstream)
    old_names = {tool["name"] for tool in before["tools"]}
    assert old_names <= {tool["name"] for tool in after["tools"]}
    if change in {"append", "capabilities"}:
        assert after["tools"][:len(before["tools"])] == before["tools"]

    # A previous response is returned to the client in original namespace form.
    # The next request must encode that history with the same upstream name.
    first = _plan(tools)
    second = _plan(next_payload["tools"], token="different-request")
    alias = first.entries[0].aliases[0]
    response = first.decode_payload({"output": [{
        "type": "function_call", "name": alias, "arguments": "{}", "call_id": "call-1", "id": "item-1",
    }]})
    encoded = second.encode_payload({"input": response["output"]})
    assert encoded["input"][0]["name"] == alias


def test_native_collision_changes_only_its_conflicting_alias():
    initial = _plan(_tools())
    aliases = [entry.aliases[0] for entry in initial.entries]
    collided = _plan([*_tools(), {"type": "function", "name": aliases[0]}])
    assert collided.entries[0].aliases[0] != aliases[0]
    assert [entry.aliases[0] for entry in collided.entries[1:3]] == aliases[1:]


def test_short_name_endpoint_retains_all_adapted_tool_families():
    baseline = _plan(_tools())
    constrained = build_tool_compatibility_plan(
        _tools(), selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(max_tool_name_length=32),
    )
    assert [e.aliases for e in baseline.entries] == [e.aliases for e in constrained.entries]
    assert all(len(alias) <= 32 for entry in constrained.entries for alias in entry.aliases)


def test_raw_cache_keys_are_redacted_recursively_but_hashes_and_states_survive():
    clean = proxy_telemetry.sanitize_mapping({
        "prompt_cache_key": "private-cache-value", "nested": [{"prompt_cache_key": "also-private"}],
        "prompt_cache_key_hash": "safe-hash", "upstream_prompt_cache_key_state": "present",
    })
    assert "private" not in json.dumps(clean)
    assert clean["prompt_cache_key_hash"] == "safe-hash"
    assert clean["upstream_prompt_cache_key_state"] == "present"


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
@pytest.mark.parametrize("base_url,expected", [
    ("https://api.openai.com/v1", PromptCacheKeyPolicy.PRESERVE),
    ("https://unknown.test/v1", PromptCacheKeyPolicy.DROP_UNVERIFIED),
    ("https://api.openai.com.evil.test/v1", PromptCacheKeyPolicy.DROP_UNVERIFIED),
    ("http://api.openai.com/v1", PromptCacheKeyPolicy.DROP_UNVERIFIED),
])
def test_route_freezes_endpoint_capability_and_converts_key(protocol, base_url, expected):
    inbound = "chat_completions" if protocol == "responses" else "responses"
    upstream = {"name": "custom", "base_url": base_url, "upstream_format": protocol, "reports_cached_input_tokens": True}
    plan = route_plan_for_request(upstream, {"client_id": "codex-app"}, inbound_format=inbound)
    attempt = plan.primary_attempt
    assert attempt.prompt_cache_key_policy is expected
    upstream["base_url"] = "https://changed.test/v1"
    p = {"model": "test", "prompt_cache_key": "stable-session", "input": "hi"} if inbound == "responses" else {
        "model": "test", "prompt_cache_key": "stable-session", "messages": [{"role": "user", "content": "hi"}],
    }
    converted = attempt.prepare_body(json.dumps(p).encode())
    actual = json.loads(converted.upstream_body)
    if expected is PromptCacheKeyPolicy.PRESERVE:
        assert actual["prompt_cache_key"] == "stable-session"
        assert converted.dropped_cache_controls == ()
    else:
        assert "prompt_cache_key" not in actual
        assert converted.dropped_cache_controls == ("prompt_cache_key",)


@pytest.mark.parametrize("url,protocol", [
    ("https://api.openai.com:444/v1/responses", "responses"),
    ("https://api.openai.com/v1/other", "responses"),
    ("https://api.openai.com/v1/responses", "chat_completions"),
    ("https://user@api.openai.com/v1/responses", "responses"),
    ("https://api.openai.com:bad/v1/responses", "responses"),
])
def test_capability_does_not_match_unverified_endpoint(url, protocol):
    assert cache_key_policy_for_endpoint(url, protocol) is PromptCacheKeyPolicy.DROP_UNVERIFIED


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
@pytest.mark.parametrize("key", ["", None, "stable-session"])
def test_explicit_cache_controls_are_preserved_same_protocol_rejected_on_conversion(protocol, key):
    p = {"model": "test", "prompt_cache_key": key, "prompt_cache_options": {"mode": "explicit", "ttl": "30m"}}
    if protocol == "responses":
        p["input"] = "hi"
    else:
        p["messages"] = [{"role": "user", "content": "hi"}]
    body = json.dumps(p, indent=2).encode()
    assert prepare_exchange(body, inbound_format=protocol, outbound_format=protocol).upstream_body == body
    other = "responses" if protocol == "chat_completions" else "chat_completions"
    with pytest.raises(NonForwardable, match="prompt_cache_options"):
        prepare_exchange(body, inbound_format=protocol, outbound_format=other, prompt_cache_key_policy=PromptCacheKeyPolicy.PRESERVE)


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_content_cache_breakpoint_is_not_silently_removed(protocol):
    content_type = "input_text" if protocol == "responses" else "text"
    item = {"role": "user", "content": [{"type": content_type, "text": "hello", "prompt_cache_breakpoint": {"mode": "explicit"}}]}
    payload = {"model": "test", "input" if protocol == "responses" else "messages": [item]}
    other = "responses" if protocol == "chat_completions" else "chat_completions"
    with pytest.raises(NonForwardable, match="prompt_cache_breakpoint"):
        prepare_exchange(json.dumps(payload).encode(), inbound_format=protocol, outbound_format=other, prompt_cache_key_policy=PromptCacheKeyPolicy.PRESERVE)


@pytest.mark.parametrize("body", [b"[]", b"null", b'"text"'])
@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_non_object_conversion_fails_at_protocol_boundary(body, protocol):
    other = "responses" if protocol == "chat_completions" else "chat_completions"
    with pytest.raises(NonForwardable, match="non-object"):
        prepare_exchange(body, inbound_format=protocol, outbound_format=other)


@pytest.mark.parametrize("value,state", [(None, "null"), ("", "empty"), ("caller-key", "present"), (42, "invalid")])
def test_cache_key_observation_states_and_redaction(tmp_path, value, state):
    fields = proxy_telemetry.enrich_request_observability(body=json.dumps({"prompt_cache_key": value}).encode(), codex_home=tmp_path)
    assert fields["prompt_cache_key_state"] == state
    assert ("prompt_cache_key_hash" in fields) == isinstance(value, str)
    assert "caller-key" not in json.dumps(fields)


@pytest.mark.parametrize("body,state", [(b"{}", "absent"), (b"not json", "unavailable"), (b"[]", "unavailable")])
def test_unavailable_body_is_not_reported_as_absent(tmp_path, body, state):
    fields = proxy_telemetry.enrich_request_observability(body=body, codex_home=tmp_path)
    assert fields["prompt_cache_key_state"] == state


@pytest.mark.parametrize("policy", [MutationPolicy.OFFICIAL_PASSTHROUGH, MutationPolicy.TRANSPARENT])
def test_upstream_observability_uses_actual_attempt_not_caller_key(tmp_path, monkeypatch, policy):
    monkeypatch.setattr(gateway_events, "RUNTIME_CODEX_DIR", tmp_path)
    caller = proxy_telemetry.enrich_request_observability(body=b'{"prompt_cache_key":"caller-key"}', codex_home=tmp_path)
    request = SimpleNamespace(upstream={}, prompt_cache_key="caller-key", caller_request_observability=caller)
    attempt = SimpleNamespace(request_mutation_policy=policy, index=0, upstream_protocol=SimpleNamespace(value="responses"))
    removed = request_observability_for_attempt(request, attempt, b"{}")
    assert removed["caller_prompt_cache_key_state"] == "present"
    assert removed["upstream_prompt_cache_key_state"] == "absent"
    assert "upstream_prompt_cache_key_hash" not in removed
    assert "prompt_cache_key_hash" not in removed
    attempt.index = 1
    changed = request_observability_for_attempt(request, attempt, b'{"prompt_cache_key":"changed-key"}')
    assert changed["upstream_prompt_cache_key_hash"] != changed["caller_prompt_cache_key_hash"]
    assert changed["request_observability_attempt_index"] == 1
    assert "changed-key" not in json.dumps(changed)


@pytest.mark.parametrize("details", ["input_tokens_details", "prompt_tokens_details"])
def test_cache_write_usage_is_recorded_without_double_counting(details):
    usage = gateway_events.normalize_usage_for_event({"input_tokens": 100, "output_tokens": 5, details: {"cached_tokens": 60, "cache_write_tokens": 40}})
    assert usage["usage_cache_write_input_tokens"] == 40
    assert usage["usage_total_tokens"] == 105
    for missing in ({}, {details: {"cache_write_tokens": -1}}, {details: {"cache_write_tokens": True}}):
        assert "usage_cache_write_input_tokens" not in gateway_events.normalize_usage_for_event(missing)


def test_sqlite_migration_preserves_unknown_usage_and_clears_stale_key(tmp_path):
    db = tmp_path / "telemetry.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE gateway_requests (request_id TEXT PRIMARY KEY, prompt_cache_key_hash TEXT)")
        conn.execute("INSERT INTO gateway_requests VALUES ('old', 'old-hash')")
    proxy_telemetry.write_event_to_sqlite(db, {
        "event": "request_complete", "request_id": "old", "prompt_cache_key_state": "absent",
    })
    proxy_telemetry.write_event_to_sqlite(db, {
        "event": "usage_observed", "request_id": "new", "usage_source": "upstream", "usage_cache_write_input_tokens": 0,
    })
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT prompt_cache_key_hash, usage_cache_write_input_tokens FROM gateway_requests WHERE request_id='old'").fetchone() == (None, None)
        assert conn.execute("SELECT usage_cache_write_input_tokens FROM gateway_requests WHERE request_id='new'").fetchone() == (0,)
