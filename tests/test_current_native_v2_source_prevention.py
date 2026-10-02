"""Current actual CLI declarations through the public source-prevention seam."""
import copy
import json
from pathlib import Path

from gateway_compat.collaboration_delivery import ALIAS, CONTEXT_KEY, decode_body, decode_sse_line, make_messages_portable, portable_handler_names
from collaboration_runtime_contract import COLLABORATION_V2, classify_collaboration_tools

FIXTURE = Path(__file__).parent / "fixtures/collaboration/codex-cli-0.159.3-linux-native-v2.json"


def native_declaration():
    return json.loads(FIXTURE.read_text())["tools"]


def test_current_actual_native_schema_is_recognized_and_aliased():
    original = native_declaration()
    assert classify_collaboration_tools(original) == COLLABORATION_V2
    payload = {"tools": copy.deepcopy(original), "input": []}
    assert make_messages_portable(payload)
    assert payload["tools"][0]["name"] == ALIAS
    assert set(portable_handler_names(payload)) == {"spawn_agent", "followup_task", "wait_agent", "send_message", "list_agents", "interrupt_agent"}
    assert original[0]["name"] == "collaboration"
    for child in payload["tools"][0]["tools"]:
        if child["name"] in {"spawn_agent", "send_message", "followup_task"}:
            assert "encrypted" not in child["parameters"]["properties"]["message"]


def test_current_actual_alias_inverse_preserves_ids_and_explicit_plaintext_marker():
    request = {"tools": native_declaration()}
    assert make_messages_portable(request)
    context = {CONTEXT_KEY: portable_handler_names(request)}
    call = {"type": "function_call", "namespace": ALIAS, "name": "spawn_agent", "id": "typed-item", "call_id": "real-call", "arguments": '{"task_name":"reader","message":"controlled literal"}'}
    decoded = json.loads(decode_body(json.dumps({"output": [call]}).encode(), context))["output"][0]
    assert decoded["namespace"] == "collaboration"
    assert decoded["encrypted_function_args"] == []
    assert decoded["id"] == "typed-item" and decoded["call_id"] == "real-call"
    line = b'data: ' + json.dumps({"type": "response.output_item.done", "item": call}).encode() + b'\n'
    decoded_event = json.loads(decode_sse_line(line, context)[5:])
    assert decoded_event["item"] == decoded


def test_actual_native_declaration_is_aliased_at_the_public_official_request_body_seam():
    from gateway_compat.official_passthrough import official_passthrough_request_body
    payload = {"model": "gpt-6-astra", "tools": native_declaration(), "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "controlled offline request"}]}]}
    context = {}
    outgoing = json.loads(official_passthrough_request_body(json.dumps(payload).encode(), payload, {"upstream_model": "gpt-6-astra"}, event_context=context))
    assert outgoing["tools"][0]["name"] == ALIAS
    assert set(context[CONTEXT_KEY]) == set(portable_handler_names(outgoing))
    assert len(context[CONTEXT_KEY]) == 6


def test_actual_cli_compatibility_plan_applies_alias_and_request_scoped_sse_inverse():
    from contextlib import contextmanager
    from types import SimpleNamespace
    from gateway_exchange import ExchangePorts, ExchangeRequest, ParsedInboundRequest, execute_exchange
    from gateway_exchange_ports import DownstreamState
    from gateway_request import request_context_from_headers
    from gateway_transport import bind_route_plan_operational_authentication
    from gateway_compat import compatible_sse_line
    from route_plan import route_plan_for_request
    from route_primitives import MutationPolicy, OperationalAuthentication, RouteProtocol

    headers = {"originator": "codex_exec", "User-Agent": "codex_exec/0.159.3 (Linux Unknown; x86_64) unknown (codex_exec; 0.159.3)"}
    identity = request_context_from_headers(headers)
    assert identity["client_id"] == "unknown"
    upstream = {"name": "official", "base_url": "http://127.0.0.1:1", "upstream_model": "gpt-6-astra", "upstream_format": "responses", "auth": "incoming"}
    plan = route_plan_for_request(upstream, identity, inbound_format="responses", collaboration_protocol=COLLABORATION_V2)
    assert plan.primary_attempt.request_mutation_policy is MutationPolicy.GATEWAY_COMPATIBILITY
    plan = bind_route_plan_operational_authentication(plan, headers, upstream,
        OperationalAuthentication(plan.authentication_strategy, authorization="Bearer offline-fixture"))
    payload = {"model": "gpt-6-astra", "tools": native_declaration(), "input": [], "stream": True, "tool_choice": "auto"}
    body = json.dumps(payload).encode()
    inbound = ParsedInboundRequest(request_id="offline-test", started_at=0, path="/v1/responses", protocol=RouteProtocol.RESPONSES,
        provider_hint=None, headers=headers, request_context=identity, proxy_request_context={}, raw_provider_probe=False,
        content_length=len(body), content_type="application/json", content_encoding=None, content_decoded=False, body=body,
        inbound_payload=payload, request_kind="gateway", model_requested="gpt-6-astra", model="gpt-6-astra", route_reason="model")
    context = {}
    calls = []
    item = {"type": "function_call", "id": "typed-item", "call_id": "real-call", "namespace": ALIAS, "name": "spawn_agent", "arguments": '{"task_name":"reader","message":"controlled literal"}'}
    line = b'data: ' + json.dumps({"type": "response.output_item.done", "item": item}).encode() + b'\n'

    class Transport:
        @contextmanager
        def open(self, opening):
            outgoing = json.loads(opening.request.data)
            assert outgoing["tools"][0]["name"] == ALIAS
            assert len(opening.event_context[CONTEXT_KEY]) == 6
            yield SimpleNamespace(status=200)

    class Downstream:
        def relay(self, response, relay_request):
            calls.append(json.loads(compatible_sse_line(line, "official", relay_request.event_context)[5:])["item"])
            return response.status
        def state(self):
            return DownstreamState(exposed=False, sse_started=False)
        def perform(self, action, **payload):
            return None

    class Control:
        def now(self):
            return 0
        def checkpoint(self):
            pass
        def wait(self, seconds):
            raise AssertionError("unexpected retry")

    class Observer:
        def record(self, event):
            pass

    request = ExchangeRequest(inbound=inbound, route_plan=plan, upstream=upstream, upstream_name="official", prepared_body=body,
        inbound_payload=payload, model_canonical="gpt-6-astra", caller_stream=True, prompt_cache_key=None, caller_request_observability={},
        event_context=context, proxy_request_context={}, usage_capture={}, response_lifecycle_state={}, pre_response_deadline=None)
    result = execute_exchange(request, ExchangePorts(Transport(), Downstream(), Control(), Observer()))
    assert result.status == 200
    assert calls == [dict(item, namespace="collaboration", encrypted_function_args=[])]


def test_official_non_v2_tool_surface_and_sse_remain_unchanged():
    from gateway_compat import compatible_request_body, compatible_sse_line
    payload = {"model": "gpt-6-astra", "tools": [{"type": "function", "name": "plain", "parameters": {"type": "object", "properties": {}}, "strict": False}], "input": [], "stream": True, "store": False}
    context = {}
    outgoing = json.loads(compatible_request_body(json.dumps(payload).encode(), {"name": "official", "upstream_model": "gpt-6-astra"}, event_context=context))
    assert outgoing["tools"] == payload["tools"]
    assert CONTEXT_KEY not in context
    line = b'data: {"type":"response.output_item.done","item":{"type":"function_call","name":"plain","call_id":"plain-call","arguments":"{}"}}\n'
    assert compatible_sse_line(line, "official", context) == line
    alias_body = json.dumps({"output": [{"type": "function_call", "namespace": ALIAS, "name": "spawn_agent", "call_id": "unowned-call", "arguments": "{}"}]}).encode()
    assert decode_body(alias_body, {}) == alias_body
    alias_line = b'data: ' + alias_body + b'\n'
    assert compatible_sse_line(alias_line, "official", {}) == alias_line


def test_completed_plaintext_call_history_preserves_ids_results_and_arguments():
    from gateway_compat import compatible_request_body, compatible_response_body
    call = {"type": "function_call", "namespace": "collaboration", "name": "send_message", "id": "typed-completed-item", "call_id": "caller-completed-call", "arguments": '{"target":"/root/reader","message":"original plaintext"}', "encrypted_function_args": []}
    result = {"type": "function_call_output", "id": "caller-result-item", "call_id": call["call_id"], "output": "actual prior caller result"}
    payload = {"model": "gpt-6-astra", "tools": native_declaration(), "input": [call, result], "tool_choice": "auto", "stream": True}
    context = {}
    prepared = json.loads(compatible_request_body(json.dumps(payload).encode(), {"name": "official"}, event_context=context))
    assert prepared["input"] == [dict(call, namespace=ALIAS), result]
    assert json.loads(compatible_response_body(json.dumps({"output": [prepared["input"][0]]}).encode(), "official", context))["output"] == [call]
    assert payload["input"] == [call, result]
