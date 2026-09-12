from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.request import ProxyHandler

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "e2e_chat_completions", ROOT / "scripts" / "e2e_chat_completions.py"
)
assert SPEC and SPEC.loader
E2E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E2E)
CONTRACT = json.loads(E2E.CHAT_CONTRACT_PATH.read_text(encoding="utf-8"))
RUNNER = ROOT / "scripts" / "Run-ChatCompletionsE2E.ps1"
CLI_RUNNER = ROOT / "scripts" / "Run-RealClientE2E.ps1"


def test_chat_contract_is_a_sibling_not_a_cli_row() -> None:
    assert CONTRACT["schema"] == "codexhub.real-client-chat-contract.v1"
    assert {case["provider_id"] for case in CONTRACT["cases"]} == {
        "official",
        "opencode-go",
    }
    assert {case["protocol"] for case in CONTRACT["cases"]} == {"chat_completions"}
    assert {case["case_ids"]["linux"] for case in CONTRACT["cases"]} == {
        "chat-official",
        "chat-opencode-go",
    }
    assert {case["case_ids"]["windows"] for case in CONTRACT["cases"]} == {
        "chat-official",
        "chat-opencode-go",
    }
    official, muse = CONTRACT["cases"]
    assert official["endpoint_binding"] == "/v1/chat/completions"
    assert muse["endpoint_binding"] == "/v1/providers/opencode-go/chat/completions"
    assert official["prompt_cache_key"]
    assert muse["prompt_cache_key"]


def test_chat_payload_and_headers_stay_on_official_compat_path() -> None:
    case = E2E.CASES[0]
    sentinel = E2E.SENTINEL_PREFIX + case.case_id
    payload = E2E.chat_payload(case, sentinel)
    headers = E2E.chat_headers("gateway-key")
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["stream"] is True
    assert payload["max_tokens"] == 256
    assert payload["prompt_cache_key"] == "codexhub-chat-e2e-official"
    muse = next(case for case in E2E.CASES if case.provider == "opencode-go")
    assert E2E.chat_payload(muse, "x")["max_tokens"] == 1024
    assert sentinel in str(payload["messages"])
    assert "X-Codex-Client-Id" not in headers
    assert headers["Authorization"] == "Bearer gateway-key"
    assert E2E.chat_url("http://127.0.0.1:9", case) == "http://127.0.0.1:9/v1/chat/completions"


def test_local_opener_disables_inherited_http_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    opener = E2E.local_opener()
    assert not any(getattr(handler, "proxies", None) for handler in opener.handlers)
    assert not any(
        isinstance(handler, ProxyHandler) and hasattr(handler, "http_open")
        for handler in opener.handlers
    )


def test_opencode_go_credentials_fail_closed(tmp_path: Path) -> None:
    assert E2E.load_opencode_go_api_key(None) == ""
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="missing OpenCode Go credentials"):
        E2E.load_opencode_go_api_key(missing)
    bad_schema = tmp_path / "bad.json"
    bad_schema.write_text('{"schema":"other","api_key":"x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid OpenCode Go credential schema"):
        E2E.load_opencode_go_api_key(bad_schema)
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps(
            {
                "schema": "codexhub.real-client-opencode-go.v1",
                "api_key": "  live-key  ",
            }
        ),
        encoding="utf-8",
    )
    assert E2E.load_opencode_go_api_key(good) == "live-key"


def test_evaluate_chat_body_accepts_choices_and_rejects_responses_or_ciphertext() -> None:
    sentinel = "SENTINEL:codexhub-chat-e2e:chat-official"
    ok = E2E.evaluate_chat_body(
        200,
        b'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"'
        + sentinel.encode()
        + b'"}}]}\n',
        sentinel,
    )
    assert ok["ok"] is True
    assert ok["saw_sentinel"] is True
    assert ok["saw_responses_event"] is False

    leaked = E2E.evaluate_chat_body(
        200,
        b'data: {"choices":[{"delta":{"content":"'
        + sentinel.encode()
        + b'","encrypted_content":"secret"}}]}\n',
        sentinel,
    )
    assert leaked["ok"] is False
    assert "encrypted_content" in leaked["error"]

    responses = E2E.evaluate_chat_body(
        200,
        b'data: {"type":"response.created","response":{"id":"resp"}}\n',
        sentinel,
    )
    assert responses["ok"] is False
    assert "Responses events" in responses["error"]

    failed = E2E.evaluate_chat_body(
        403,
        b'{"error":{"message":"Upstream stream ended before response.completed."}}',
        sentinel,
    )
    assert failed["ok"] is False
    assert failed["http_status"] == 403
    assert "Upstream stream ended" in failed["error"]


def test_conversion_shapes_expand_v2_and_keep_muse_plain() -> None:
    shapes = E2E.dump_conversion_shapes()
    official = shapes["official_sentinel"]
    assert official["has_prompt_cache_key"] is True
    assert official["dropped_cache_controls"] == []
    assert official["tools"] == []

    muse = shapes["muse_sentinel"]
    assert muse["has_prompt_cache_key"] is False
    assert muse["dropped_cache_controls"] == ["prompt_cache_key"]
    assert set(muse["keys"]) >= {"model", "input", "stream", "max_output_tokens"}

    function = shapes["official_function"]
    assert function["tools"] == [{"type": "function", "name": "get_time"}]
    assert function["tool_choice"] == {"type": "function", "name": "get_time"}

    v2 = shapes["official_v2"]
    assert v2["tools"] == [
        {
            "type": "namespace",
            "name": "collaboration",
            "children": [
                "followup_task",
                "interrupt_agent",
                "list_agents",
                "send_message",
                "spawn_agent",
                "wait_agent",
            ],
        }
    ]
    assert v2["tool_choice"] == "auto"

    search = shapes["official_web_search"]
    assert search["tools"] == [{"type": "web_search", "name": None}]


def test_capability_evaluator_requires_forced_function_and_accepts_v2_text() -> None:
    function = E2E.evaluate_capability_body(
        "official-function",
        200,
        b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"get_time"}}]}}]}\n',
    )
    assert function["ok"] is True
    assert function["tool_names"] == ["get_time"]

    leaked = E2E.evaluate_capability_body(
        "official-v2",
        200,
        b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"__codexhub_ns_list_agents"}}]}}]}\n',
    )
    assert leaked["ok"] is False
    assert "namespace alias" in leaked["error"]

    accepted = E2E.evaluate_capability_body(
        "official-v2",
        200,
        b'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"ok"}}]}\n',
    )
    assert accepted["ok"] is True
    assert accepted["saw_v2_tool"] is False

    hosted = E2E.evaluate_capability_body(
        "official-web-search",
        200,
        b'data: {"error":{"message":"Tool compatibility failed at response: encrypted_native_tool_unavailable.","code":"tool_compatibility_boundary"}}\n',
    )
    assert hosted["ok"] is False
    assert "encrypted native tool" in hosted["error"]

    searched = E2E.evaluate_capability_body(
        "official-web-search",
        200,
        b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"web_search","arguments":"{\\"action\\":{\\"query\\":\\"Codex CLI\\"}}"}}]}}]}\n'
        + b'data: {"choices":[{"delta":{"content":"Codex CLI is the terminal client."}}]}\n',
    )
    assert searched["ok"] is True
    assert "web_search" in searched["tool_names"]

    muse_search_memory = E2E.evaluate_capability_body(
        "muse-web-search",
        200,
        b'data: {"choices":[{"delta":{"content":"Codex CLI is a terminal client."}}]}\n',
    )
    assert muse_search_memory["ok"] is False
    assert muse_search_memory["hosted_search"] is False
    assert "hosted web_search" in muse_search_memory["error"]

    muse_search = E2E.evaluate_capability_body(
        "muse-web-search",
        200,
        b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"web_search","arguments":"{\\"action\\":{\\"query\\":\\"Codex CLI\\"}}"}}]}}]}\n'
        + b'data: {"choices":[{"delta":{"content":"Codex CLI is a terminal client."}}]}\n',
    )
    assert muse_search["ok"] is True
    assert muse_search["hosted_search"] is True
    assert "web_search" in muse_search["tool_names"]

    muse_image = E2E.evaluate_capability_body(
        "muse-image-gen",
        200,
        b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"imagegen"}}]}}]}\n',
    )
    assert muse_image["ok"] is True
    assert muse_image["tool_names"] == ["imagegen"]

    muse_responses_image = E2E.evaluate_capability_body(
        "muse-responses-image-gen",
        200,
        b'data: {"type":"response.output_item.done","item":{"type":"function_call","name":"image_gen.imagegen"}}\n',
        protocol="responses",
    )
    assert muse_responses_image["ok"] is True
    assert "image_gen.imagegen" in muse_responses_image["tool_names"]


def test_windows_wrapper_stays_off_the_cli_summary() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    cli = CLI_RUNNER.read_text(encoding="utf-8")
    assert "codexhub-python.cmd" in source
    assert "e2e_chat_completions.py" in source
    assert "OpenCodeGoCredentials" in source
    assert "summary.json" not in source
    assert "gateway_enable_chat_completions = $false" in cli
    assert "/chat/completions" not in cli
