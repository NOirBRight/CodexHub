from __future__ import annotations

import argparse
import json
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


UPSTREAM_FORMAT_AUTO = "auto"
UPSTREAM_FORMAT_RESPONSES = "responses"
UPSTREAM_FORMAT_CHAT = "chat_completions"
UPSTREAM_FORMAT_ANTHROPIC = "anthropic_messages"
PROBE_REQUEST_TIMEOUT_SECONDS = 6


def endpoint_url(base_url: str, path: str) -> str:
    base = base_url.strip().rstrip("/")
    if not base:
        raise ValueError("provider base_url is required")
    if not path.startswith("/"):
        path = "/" + path
    if base_has_version_suffix(base):
        return base + path
    return base + "/v1" + path


def base_has_version_suffix(base_url: str) -> bool:
    path = urlsplit(base_url).path.rstrip("/")
    if not path:
        return False
    return bool(re.fullmatch(r"v\d+(?:\.\d+)?", path.rsplit("/", 1)[-1].lower()))


def headers(api_key: str, *, json_body: bool = False) -> dict[str, str]:
    result = {"Accept": "application/json"}
    if json_body:
        result["Content-Type"] = "application/json"
    stripped = api_key.strip()
    if stripped:
        result["Authorization"] = f"Bearer {stripped}"
    return result


def model_ids_from_payload(payload: Any) -> list[str]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        raw_items = payload.get("data", payload.get("models", []))
        items = raw_items if isinstance(raw_items, list) else []
    else:
        items = []

    ids: list[str] = []
    for item in items:
        model_id = None
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict):
            for key in ("id", "model", "name", "slug"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    model_id = value
                    break
        if model_id and model_id.strip() not in ids:
            ids.append(model_id.strip())
    return ids


def weather_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "City and region"},
        },
        "required": ["location"],
        "additionalProperties": False,
    }


def responses_text_payload(model: str, *, stream: bool = False) -> dict[str, Any]:
    return {
        "model": model,
        "input": "Endpoint probe. Reply with exactly: OK",
        "max_output_tokens": 32,
        "stream": stream,
    }


def responses_tool_payload(model: str, *, stream: bool) -> dict[str, Any]:
    return {
        "model": model,
        "input": "Use get_weather for Paris.",
        "max_output_tokens": 64,
        "stream": stream,
        "tools": [
            {
                "type": "function",
                "name": "get_weather",
                "description": "Return fake weather for a city.",
                "parameters": weather_parameters(),
            }
        ],
        "tool_choice": {"type": "function", "name": "get_weather"},
    }


def chat_text_payload(model: str, *, stream: bool = False) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Endpoint probe. Reply with exactly: OK"}],
        "max_tokens": 32,
        "stream": stream,
    }


def chat_tool_payload(model: str, *, stream: bool) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Use get_weather for Paris."}],
        "max_tokens": 64,
        "stream": stream,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Return fake weather for a city.",
                    "parameters": weather_parameters(),
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
    }


def anthropic_text_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Endpoint probe. Reply with exactly: OK"}],
        "max_tokens": 32,
    }


def request_json(
    base_url: str,
    api_key: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int,
) -> tuple[bool, int | None, Any, str | None]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=True).encode("utf-8")
    request = Request(
        endpoint_url(base_url, path),
        data=body,
        headers=headers(api_key, json_body=payload is not None),
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            parsed = json.loads(raw.decode("utf-8-sig")) if raw else {}
            return 200 <= response.status < 300, response.status, parsed, None
    except HTTPError as exc:
        detail = safe_error_detail(exc)
        try:
            raw = exc.read(4096)
            parsed = json.loads(raw.decode("utf-8-sig")) if raw else None
            if parsed is not None:
                detail = compact_json(parsed)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        return False, exc.code, None, detail
    except (OSError, TimeoutError, URLError, ValueError) as exc:
        return False, None, None, safe_error_detail(exc)


def request_sse_events(
    base_url: str,
    api_key: str,
    path: str,
    payload: dict[str, Any],
    timeout: int,
) -> tuple[bool, int | None, list[dict[str, Any]], str | None]:
    request = Request(
        endpoint_url(base_url, path),
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers=headers(api_key, json_body=True),
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            events = parse_sse_response(response)
            return 200 <= response.status < 300, response.status, events, None
    except HTTPError as exc:
        return False, exc.code, [], safe_error_detail(exc)
    except (OSError, TimeoutError, URLError, ValueError) as exc:
        return False, None, [], safe_error_detail(exc)


def parse_sse_response(response: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw_line in response:
        line = raw_line.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            break
        try:
            payload = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from iter_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_dicts(item)


def responses_tool_ok(payload: Any) -> bool:
    for item in iter_dicts(payload):
        if item.get("type") != "function_call":
            continue
        name = item.get("name")
        call_id = item.get("call_id")
        if name == "get_weather" and isinstance(call_id, str) and call_id:
            return True
    return False


def responses_stream_tool_ok(events: list[dict[str, Any]]) -> bool:
    done_call_id = None
    completed_call_id = None
    for event in events:
        if event.get("type") == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") == "get_weather":
                call_id = item.get("call_id")
                if isinstance(call_id, str) and call_id:
                    done_call_id = call_id
        if event.get("type") == "response.completed":
            response = event.get("response")
            for item in iter_dicts(response):
                if item.get("type") == "function_call" and item.get("name") == "get_weather":
                    call_id = item.get("call_id")
                    if isinstance(call_id, str) and call_id:
                        completed_call_id = call_id
    return bool(done_call_id and completed_call_id and done_call_id == completed_call_id)


def chat_tool_ok(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            call_id = tool_call.get("id")
            function = tool_call.get("function")
            if (
                isinstance(call_id, str)
                and call_id
                and isinstance(function, dict)
                and function.get("name") == "get_weather"
            ):
                return True
    return False


def chat_stream_tool_ok(events: list[dict[str, Any]]) -> bool:
    first_call_id = None
    name_seen = False
    id_conflict = False
    for chunk in events:
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            tool_calls = delta.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                call_id = tool_call.get("id")
                if isinstance(call_id, str) and call_id:
                    if first_call_id is None:
                        first_call_id = call_id
                    elif call_id != first_call_id:
                        id_conflict = True
                function = tool_call.get("function")
                if isinstance(function, dict) and function.get("name") == "get_weather":
                    name_seen = True
    return bool(first_call_id and name_seen and not id_conflict)


def anthropic_text_ok(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    content = payload.get("content")
    if isinstance(content, list):
        return True
    return isinstance(payload.get("id"), str) and payload.get("type") == "message"


def recommended_format(result: dict[str, Any]) -> str:
    if result.get("responses_text_ok"):
        return UPSTREAM_FORMAT_RESPONSES
    if result.get("chat_text_ok"):
        return UPSTREAM_FORMAT_CHAT
    if result.get("anthropic_text_ok"):
        return UPSTREAM_FORMAT_ANTHROPIC
    return UPSTREAM_FORMAT_AUTO


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))[:300]


def safe_error_detail(exc: BaseException) -> str:
    detail = str(exc)
    api_key = os.environ.get("CODEXHUB_PROBE_API_KEY", "")
    if api_key:
        detail = detail.replace(api_key, "<redacted>")
    return detail


def probe(base_url: str, api_key: str, requested_model: str | None, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    notes: list[str] = []
    request_timeout = max(1, min(timeout, PROBE_REQUEST_TIMEOUT_SECONDS))
    result: dict[str, Any] = {
        "base_url": base_url,
        "model": requested_model.strip() if requested_model and requested_model.strip() else None,
        "models_ok": False,
        "responses_text_ok": False,
        "responses_tool_ok": False,
        "responses_tool_stream_ok": False,
        "chat_text_ok": False,
        "chat_tool_ok": False,
        "chat_tool_stream_ok": False,
        "anthropic_text_ok": False,
        "recommended_format": UPSTREAM_FORMAT_AUTO,
        "notes": notes,
    }

    ok, status, payload, detail = request_json(base_url, api_key, "/models", timeout=request_timeout)
    result["models_ok"] = ok
    if ok:
        model_ids = model_ids_from_payload(payload)
        if not result["model"] and model_ids:
            result["model"] = model_ids[0]
        notes.append(f"/v1/models: OK ({len(model_ids)} models)")
    else:
        notes.append(f"/v1/models: failed ({status or 'no status'}): {detail or 'unknown error'}")

    model = result["model"]
    if not isinstance(model, str) or not model.strip():
        notes.append("No model is available for POST probes.")
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        return result

    checks = [
        ("responses_text_ok", "/responses", responses_text_payload(model), lambda value: isinstance(value, dict)),
        ("chat_text_ok", "/chat/completions", chat_text_payload(model), lambda value: isinstance(value, dict)),
        ("anthropic_text_ok", "/messages", anthropic_text_payload(model), anthropic_text_ok),
    ]
    for key, path, body, validator in checks:
        ok, status, payload, detail = request_json(
            base_url,
            api_key,
            path,
            method="POST",
            payload=body,
            timeout=request_timeout,
        )
        result[key] = bool(ok and validator(payload))
        label = key.removesuffix("_ok").replace("_", " ")
        if result[key]:
            notes.append(f"{label}: OK")
        else:
            notes.append(f"{label}: failed ({status or 'no status'}): {detail or 'invalid response shape'}")

    result["recommended_format"] = recommended_format(result)
    if result["recommended_format"] == UPSTREAM_FORMAT_RESPONSES:
        notes.append("Recommended: Responses")
    elif result["recommended_format"] == UPSTREAM_FORMAT_CHAT:
        notes.append("Recommended: Chat Completions")
    elif result["recommended_format"] == UPSTREAM_FORMAT_ANTHROPIC:
        notes.append("Recommended: Anthropic Messages detected; Gateway conversion is planned")
    else:
        notes.append("Warning: no supported endpoint responded to the lightweight probe.")

    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe provider upstream response/chat format support.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()

    api_key = os.environ.get("CODEXHUB_PROBE_API_KEY", "")
    result = probe(args.base_url, api_key, args.model, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
