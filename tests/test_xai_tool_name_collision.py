"""The selected xAI hosted search must not shadow a client image function."""

import copy
import json
from http.server import BaseHTTPRequestHandler

import pytest

import gateway_compat
from gateway_compat.official_passthrough import request_tool_plan
from tests.gateway_harness import GatewayHarness, GATEWAY_CLIENT_KEY, request_gateway


def prepare(payload, provider="xai"):
    context = {}
    upstream = {"name": provider, "provider_id": provider, "upstream_format": "responses",
                "tool_protocol": "responses_structured", "tool_surface_strategy": "eager"}
    body = gateway_compat.compatible_request_body(
        json.dumps(payload).encode(), upstream, event_context=context, inject_codex_tools=False,
    )
    return json.loads(body), request_tool_plan(context)


def payload():
    return {"model": "grok-4.6", "input": [], "tools": [
        {"type": "function", "name": "view_image", "description": "Inspect a local image",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"type": "web_search"},
    ], "tool_choice": {"type": "function", "name": "view_image"}}


def test_xai_image_alias_roundtrip_and_history():
    original = payload()
    wire, plan = prepare(original)
    alias = wire["tools"][0]["name"]
    assert alias != "view_image"
    assert wire["tools"][0] == {**original["tools"][0], "name": alias}
    assert wire["tools"][1] == original["tools"][1]
    assert plan.encode_payload(original)["tool_choice"]["name"] == alias
    call = {"type": "function_call", "id": "item_image", "call_id": "call_image",
            "name": alias, "arguments": '{"path":"/tmp/probe.png"}', "status": "completed"}
    decoded = plan.decode_payload({"output": [call]})
    client_call = decoded["output"][0]
    assert client_call == {**call, "name": "view_image"}
    followup = copy.deepcopy(original)
    result = {"type": "function_call_output", "call_id": "call_image", "output": "image inspected"}
    followup["input"] = [client_call, result]
    replay, _ = prepare(followup)
    assert replay["input"] == [call, result]
    assert original == payload()


@pytest.mark.parametrize("provider,search", [("other", True), ("xai", False)])
def test_nonconflicting_function_is_unchanged(provider, search):
    original = payload()
    if not search:
        original["tools"].pop()
    wire, plan = prepare(original, provider)
    assert wire["tools"] == original["tools"]
    assert plan.encode_payload(original)["tool_choice"] == original["tool_choice"]


def test_http_stream_restores_name_and_replays_call_result():
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            image = next(tool for tool in body["tools"] if tool.get("description") == "Inspect a local image")
            alias = image["name"]
            call = {"type": "function_call", "id": "item_image", "call_id": "call_image",
                    "name": alias, "arguments": '{"path":"/tmp/probe.png"}', "status": "completed"}
            events = [
                {"type": "response.created", "response": {"id": "resp_image", "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**call, "arguments": "", "status": "in_progress"}},
                {"type": "response.function_call_arguments.delta", "item_id": "item_image", "output_index": 0, "delta": call["arguments"]},
                {"type": "response.function_call_arguments.done", "item_id": "item_image", "output_index": 0, "arguments": call["arguments"]},
                {"type": "response.output_item.done", "output_index": 0, "item": call},
                {"type": "response.completed", "response": {"id": "resp_image", "status": "completed", "output": [call]}},
            ]
            if len(requests) == 2:
                events = [{"type": "response.completed", "response": {"id": "resp_done", "status": "completed", "output": []}}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()

    with GatewayHarness() as harness:
        harness.stub.server.RequestHandlerClass = Provider
        body = payload()
        body.update(model="xai/grok-4.6", stream=True, tool_choice="auto")
        def post(value):
            return request_gateway(harness.host, harness.port, "POST", "/v1/responses",
                body=json.dumps(value).encode(), headers={"Authorization": "Bearer " + GATEWAY_CLIENT_KEY,
                "Content-Type": "application/json", "Connection": "close"}, timeout=8)
        response = post(body)
        assert response.status == 200
        events = [json.loads(line[5:]) for line in response.body.splitlines() if line.startswith(b"data: {")]
        assert not any(e.get("type") == "error" for e in events)
        terminal = next(e for e in events if e.get("type") == "response.completed")
        call = terminal["response"]["output"][0]
        assert call["name"] == "view_image"
        assert requests[0]["tools"][0]["name"] != "view_image"
        for event in events:
            if isinstance(event.get("item"), dict):
                assert event["item"]["name"] == "view_image"
        result = {"type": "function_call_output", "call_id": "call_image", "output": "inspected"}
        body["input"] = [call, result]
        assert post(body).status == 200
        assert requests[1]["input"] == [{**call, "name": requests[0]["tools"][0]["name"]}, result]


def test_alias_collision_uses_another_name_without_changing_native_tool():
    original = payload()
    wire, _ = prepare(original)
    occupied = wire["tools"][0]["name"]
    native = {"type": "function", "name": occupied, "parameters": {"type": "object"}}
    original["tools"].append(native)
    wire, _ = prepare(original)
    assert wire["tools"][0]["name"] not in {"view_image", occupied}
    assert wire["tools"][-1] == native
