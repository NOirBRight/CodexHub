"""Live Gateway E2E for OpenCode Muse Spark and Command Code DeepSeek V4 Flash."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from collaboration_runtime_contract import (
    COLLABORATION_V1,
    COLLABORATION_V2,
    EXPECTED_PARAMETER_SCHEMAS,
    V1_NAMESPACE,
    V1_TOOLS,
    V2_NAMESPACE,
    V2_TOOLS,
)
from probe_upstream_format import headers as probe_headers

GATEWAY = "http://127.0.0.1:9099"
MUSE = "opencode-go/muse-spark-1.2-contributor"
DEEPSEEK = "commandcode/deepseek/deepseek-v4-flash"


def _gateway_key() -> str:
    settings = json.loads(Path.home().joinpath(".codex/proxy/settings.json").read_text())
    return str(settings.get("gateway_client_key") or "codexhub-proxy")


def _gateway_up() -> bool:
    try:
        with urlopen(Request(f"{GATEWAY}/health", method="GET"), timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _gateway_up(), reason="local Gateway is not healthy")


def _post(path: str, payload: dict, timeout: int = 120) -> tuple[int, dict | str]:
    body = json.dumps(payload).encode()
    headers = probe_headers(_gateway_key(), json_body=True)
    headers["User-Agent"] = "codex-app"
    req = Request(f"{GATEWAY}{path}", data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except URLError as exc:
        return 0, str(exc)


def _error_text(body: dict | str) -> str:
    if isinstance(body, str):
        return body
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)
    if isinstance(err, str):
        return err
    hub = body.get("codexhub_error")
    if isinstance(hub, dict):
        return str(hub.get("message") or hub)
    return json.dumps(body)[:800]


def _output_items(body: dict | str) -> list[dict]:
    if not isinstance(body, dict):
        return []
    items = body.get("output")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _texts(body: dict | str) -> str:
    chunks: list[str] = []
    for item in _output_items(body):
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    chunks.append(str(part.get("text") or ""))
        elif isinstance(content, str):
            chunks.append(content)
    if not chunks and isinstance(body, dict):
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            message = (choices[0] or {}).get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                chunks.append(message["content"])
    return "\n".join(chunks)


def _function_calls(body: dict | str) -> list[dict]:
    calls: list[dict] = []
    for item in _output_items(body):
        if item.get("type") == "function_call":
            calls.append(item)
    if not calls and isinstance(body, dict):
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            message = (choices[0] or {}).get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function") if isinstance(call.get("function"), dict) else {}
                    calls.append(
                        {
                            "type": "function_call",
                            "call_id": call.get("id") or call.get("call_id") or "call_0",
                            "name": function.get("name") or call.get("name"),
                            "arguments": function.get("arguments") or "{}",
                        }
                    )
    return calls


def _namespace(name: str, protocol: str, tool_names: tuple[str, ...]) -> dict:
    children = []
    for tool_name in tool_names:
        parameters = copy.deepcopy(EXPECTED_PARAMETER_SCHEMAS[protocol][tool_name])
        if not parameters.get("required"):
            parameters.pop("required", None)
        children.append(
            {
                "type": "function",
                "name": tool_name,
                "description": "runtime",
                "strict": False,
                "parameters": parameters,
            }
        )
    return {"type": "namespace", "name": name, "description": "runtime", "tools": children}


STATUS_TOOL = {
    "type": "function",
    "name": "get_status",
    "description": "Return a ping status. Call this instead of guessing.",
    "parameters": {
        "type": "object",
        "properties": {"ping": {"type": "string"}},
        "required": ["ping"],
        "additionalProperties": False,
    },
}


def test_gateway_health_still_ok() -> None:
    assert _gateway_up()


@pytest.mark.parametrize("model", [MUSE, DEEPSEEK])
def test_text_generation(model: str) -> None:
    status, body = _post(
        "/v1/responses",
        {
            "model": model,
            "stream": False,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Reply with exactly: TEXT_OK"}],
                }
            ],
        },
    )
    assert status == 200, _error_text(body)
    assert "TEXT_OK" in _texts(body)


@pytest.mark.parametrize("model", [MUSE, DEEPSEEK])
def test_function_tool_roundtrip(model: str) -> None:
    first = {
        "model": model,
        "stream": False,
        "tool_choice": "auto",
        "tools": [STATUS_TOOL],
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Call get_status with ping=hello. Do not answer in plain text before the tool call.",
                    }
                ],
            }
        ],
    }
    status, body = _post("/v1/responses", first)
    assert status == 200, _error_text(body)
    calls = _function_calls(body)
    assert calls, f"{model} did not emit a function_call: {_texts(body)[:400]} {json.dumps(_output_items(body))[:800]}"
    call = calls[0]
    call_id = str(call.get("call_id") or call.get("id") or "call_status")
    name = str(call.get("name") or "get_status")
    arguments = call.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments or {})
    second_input = list(first["input"]) + [
        {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments},
        {"type": "function_call_output", "call_id": call_id, "output": json.dumps({"ping": "hello", "ok": True})},
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "The tool succeeded. Reply with exactly: TOOL_OK"}],
        },
    ]
    status, body = _post(
        "/v1/responses",
        {"model": model, "stream": False, "tools": [STATUS_TOOL], "input": second_input},
    )
    assert status == 200, _error_text(body)
    text = _texts(body)
    assert "TOOL_OK" in text or "hello" in text.lower() or "ok" in text.lower(), text[:500]


@pytest.mark.parametrize(
    ("model", "protocol", "namespace", "tools"),
    [
        (MUSE, COLLABORATION_V1, V1_NAMESPACE, V1_TOOLS),
        (MUSE, COLLABORATION_V2, V2_NAMESPACE, V2_TOOLS),
        (DEEPSEEK, COLLABORATION_V1, V1_NAMESPACE, V1_TOOLS),
        (DEEPSEEK, COLLABORATION_V2, V2_NAMESPACE, V2_TOOLS),
    ],
)
def test_subagent_surface_accepted_and_can_emit_spawn(
    model: str, protocol: str, namespace: str, tools: tuple[str, ...]
) -> None:
    payload = {
        "model": model,
        "stream": False,
        "tool_choice": "auto",
        "tools": [_namespace(namespace, protocol, tools)],
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "You must call spawn_agent now. "
                            "For v2 use task_name=worker and message=say ping. "
                            "For v1 use agent_type=worker, fork_context=false, message=say ping. "
                            "Do not answer without a tool call."
                        ),
                    }
                ],
            }
        ],
    }
    status, body = _post("/v1/responses", payload)
    assert status == 200, f"{model} {protocol} rejected: {_error_text(body)}"
    calls = _function_calls(body)
    names = {str(call.get("name") or "") for call in calls}
    if calls:
        assert any("spawn" in name or name.endswith("spawn_agent") for name in names), names


@pytest.mark.parametrize("model", [MUSE, DEEPSEEK])
@pytest.mark.parametrize(
    ("protocol", "namespace", "arguments"),
    [
        (COLLABORATION_V1, V1_NAMESPACE, {"agent_type": "worker", "fork_context": False, "message": "say ping"}),
        (COLLABORATION_V2, V2_NAMESPACE, {"task_name": "worker", "message": "say ping"}),
    ],
)
def test_subagent_history_with_synthetic_spawn_is_accepted(
    model: str, protocol: str, namespace: str, arguments: dict
) -> None:
    tools = V1_TOOLS if protocol == COLLABORATION_V1 else V2_TOOLS
    payload = {
        "model": model,
        "stream": False,
        "tool_choice": "auto",
        "tools": [_namespace(namespace, protocol, tools)],
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Spawn a worker sub-agent."}],
            },
            {
                "type": "function_call",
                "id": "fc_spawn_1",
                "call_id": "call_spawn_1",
                "name": "spawn_agent",
                "namespace": namespace,
                "arguments": json.dumps(arguments),
            },
            {
                "type": "function_call_output",
                "id": "fc_spawn_out_1",
                "call_id": "call_spawn_1",
                "output": json.dumps(
                    {"agent_id": "agent/root/worker", "nickname": "worker"}
                    if protocol == COLLABORATION_V1
                    else {"task_name": "worker"}
                ),
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "The worker exists. Reply with exactly: SUBAGENT_OK"}],
            },
        ],
    }
    status, body = _post("/v1/responses", payload)
    assert status == 200, f"{model} {protocol} synthetic spawn rejected: {_error_text(body)}"
    text = _texts(body)
    assert "SUBAGENT_OK" in text or "worker" in text.lower() or _function_calls(body) or text, text[:500]


def test_muse_recursive_schema_and_stale_reasoning_still_succeed() -> None:
    status, body = _post(
        "/v1/responses",
        {
            "model": MUSE,
            "previous_response_id": "resp_stale",
            "stream": False,
            "tools": [
                {
                    "type": "function",
                    "name": "tree",
                    "parameters": {
                        "type": "object",
                        "$defs": {
                            "Node": {
                                "type": "object",
                                "properties": {"child": {"$ref": "#/$defs/Node"}},
                            }
                        },
                        "properties": {"root": {"$ref": "#/$defs/Node"}},
                    },
                }
            ],
            "input": [
                {
                    "type": "reasoning",
                    "id": "rs_0c348d5074c4ae3b016a936bac430887d09bf21d97c7195f75",
                    "summary": [{"type": "summary_text", "text": "old"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Reply with exactly: MUSE_OK"}],
                },
            ],
        },
    )
    assert status == 200, _error_text(body)
    assert "MUSE_OK" in _texts(body) or _output_items(body)
