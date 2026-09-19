"""Isolated HTTP replay E2E; the upstream and interrupted client are scripted."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import site
import subprocess
import sys

import pytest


@pytest.mark.parametrize("model", ["volc/glm-5.2", "xai/grok-4.6"])
def test_interrupted_wait_continues_through_gateway(tmp_path, model):
    # Import Gateway only in the child, after isolating every persisted path.
    root = Path(__file__).resolve().parents[1]
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ
    }
    environment.update({
        "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
        "CODEX_HOME": str(tmp_path / "codex"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "APPDATA": str(tmp_path / "appdata"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
        "TMPDIR": str(tmp_path), "TEMP": str(tmp_path), "TMP": str(tmp_path),
        # Keep installed development dependencies when HOME changes.
        "PYTHONPATH": os.pathsep.join((str(root), str(root / "src-python"), site.getusersitepackages())),
        "CODEXHUB_PYTHON": sys.executable,
    })
    result = subprocess.run(
        [sys.executable, "-c",
         "from tests.test_collaboration_abort_e2e import replay_interrupted_wait; "
         "import sys; replay_interrupted_wait(sys.argv[1])", model],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def replay_interrupted_wait(model):
    from python_runtime_contract import require_python_313
    require_python_313(__file__)

    from collaboration_runtime_contract import COLLABORATION_V2, EXPECTED_PARAMETER_SCHEMAS
    from tests.gateway_harness import (
        GATEWAY_CLIENT_KEY, GatewayHarness, parsed_sse_events,
        request_gateway, require_single_terminal,
    )

    tools = [{
        "type": "namespace", "name": "collaboration", "description": "runtime",
        "tools": [
            {"type": "function", "name": name, "description": "runtime",
             "strict": False, "parameters": copy.deepcopy(schema)}
            for name, schema in EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2].items()
        ],
    }]
    interruption = "aborted by user after 104.0s"
    history = [
        {"role": "user", "content": "Wait for the review."},
        {"type": "function_call", "id": "item-wait", "call_id": "call-wait",
         "namespace": "collaboration", "name": "wait_agent",
         "arguments": '{"timeout_ms":120000}'},
        {"type": "function_call_output", "id": "item-result", "call_id": "call-wait",
         "output": interruption},
        {"role": "user", "content": "Continue after interruption."},
    ]
    chat = model.startswith("volc/")
    with GatewayHarness() as harness:
        for turn in range(2):
            marker = f"CONTINUED_{turn}"
            if chat:
                payloads = [{
                    "id": f"chatcmpl-{turn}", "object": "chat.completion.chunk",
                    "model": "glm-5.2",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": marker},
                                 "finish_reason": "stop"}],
                }]
            else:
                payloads = [{
                    "type": "response.completed",
                    "response": {"id": f"resp-{turn}", "object": "response", "status": "completed",
                                 "model": "grok-4.6", "output": [{
                                     "type": "message", "id": f"msg-{turn}", "role": "assistant",
                                     "status": "completed", "content": [{
                                         "type": "output_text", "text": marker, "annotations": [],
                                     }],
                                 }]},
                }]
            chunks = tuple(("data: " + json.dumps(p) + "\n\n").encode() for p in payloads)
            harness.set_sse_response(chunks + ((b"data: [DONE]\n\n",) if chat else ()))
            body = {"model": model, "input": history, "tools": tools,
                    "tool_choice": "auto", "stream": True}
            response = request_gateway(
                harness.host, harness.port, "POST", "/v1/responses",
                body=json.dumps(body).encode(), headers={
                    "Authorization": f"Bearer {GATEWAY_CLIENT_KEY}", "Content-Type": "application/json",
                }, timeout=8,
            )
            assert response.status == 200, response.body
            terminal = json.loads(require_single_terminal(parsed_sse_events(response.body)).data)
            assert terminal["type"] == "response.completed", terminal
            output = terminal["response"]["output"]
            assert any(part.get("text") == marker for item in output for part in item.get("content", []))
            assert len(harness.stub.captures) == turn + 1
            capture = harness.stub.captures[-1]
            assert capture.path.endswith("/chat/completions" if chat else "/responses")
            sent = json.loads(capture.body)
            assert sent["model"] == ("glm-5.2" if chat else "grok-4.6")
            if chat:
                results = [item for item in sent["messages"] if item.get("role") == "tool"]
                assert [(item["tool_call_id"], item["content"]) for item in results] == [("call-wait", interruption)]
            else:
                results = [item for item in sent["input"] if item.get("type") == "function_call_output"]
                assert results == [history[2]]
            # The client replays messages as input items, without output status.
            history.extend({"type": "message", "role": item["role"], "content": item["content"]}
                           for item in output if item["type"] == "message")
            history.append({"role": "user", "content": "Continue again."})

        # A malformed lookalike must still fail before reaching any upstream.
        body["input"][2]["output"] = interruption + "\nextra"
        rejected = request_gateway(
            harness.host, harness.port, "POST", "/v1/responses",
            body=json.dumps(body).encode(), headers={
                "Authorization": f"Bearer {GATEWAY_CLIENT_KEY}", "Content-Type": "application/json",
            }, timeout=8,
        )
        assert rejected.status == 400, rejected.body
        assert b"malformed_collaboration_result" in rejected.body
        assert len(harness.stub.captures) == 2
