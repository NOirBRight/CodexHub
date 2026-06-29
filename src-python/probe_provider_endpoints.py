from __future__ import annotations

import argparse
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from catalog import canonical_model_id, load_catalog_models
from catalog_sync import GENERATED_CATALOG_PATH
from codex_proxy import (
    _responses_url,
    choose_upstream,
    compatible_request_body,
    safe_upstream_error_detail,
    upstream_headers,
)


FORBIDDEN_EXTERNAL_TYPES = (
    '"type":"compaction"',
    '"type":"reasoning"',
    '"type":"function_call"',
    '"type":"function_call_output"',
    '"type":"custom_tool_call"',
    '"type":"custom_tool_call_output"',
    '"type":"web_search_call"',
    '"type":"tool_search_call"',
    '"type":"tool_search_output"',
    "gAAAA",
)


def visible_catalog_models(include_official: bool = False) -> list[str]:
    result: list[str] = []
    for model in load_catalog_models(GENERATED_CATALOG_PATH):
        slug = canonical_model_id(str(model.get("slug", "")))
        if not slug:
            continue
        if not include_official and slug.startswith("gpt-"):
            continue
        result.append(slug)
    return result


def probe_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "max_output_tokens": 32,
        "stream": False,
        "input": [
            {"type": "message", "role": "user", "content": "Endpoint probe. Reply with exactly: OK"},
            {
                "type": "compaction",
                "summary": [{"type": "summary_text", "text": "Prior conversation was compacted."}],
            },
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "The previous assistant was checking provider routing."}],
                "encrypted_content": "gAAAA-probe-placeholder",
            },
            {
                "type": "function_call",
                "call_id": "probe_function_call",
                "name": "shell_command",
                "arguments": "{\"command\":\"echo probe\"}",
            },
            {
                "type": "function_call_output",
                "call_id": "probe_function_call",
                "output": "Exit code: 0\nOutput:\nprobe",
            },
            {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "probe_custom_tool_call",
                "name": "apply_patch",
                "input": "*** Begin Patch\n*** Update File: probe.txt\n@@\n+probe\n*** End Patch",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "probe_custom_tool_call",
                "output": "Exit code: 0\nOutput:\nSuccess. Updated probe.txt",
            },
            {"type": "web_search_call", "status": "completed", "action": {"query": "codex proxy probe"}},
            {
                "type": "tool_search_call",
                "status": "completed",
                "call_id": "probe_tool_search",
                "arguments": {"query": "render_chart"},
            },
            {
                "type": "tool_search_output",
                "status": "completed",
                "call_id": "probe_tool_search",
                "tools": [{"name": "render_chart"}],
            },
            {"type": "message", "role": "user", "content": "Now answer OK."},
        ],
    }


def transformed_body(model: str, upstream: dict[str, Any]) -> tuple[bytes, list[str], str | None]:
    body = json.dumps(probe_payload(model), ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    transformed = compatible_request_body(body, upstream, model_id=model)
    text = transformed.decode("utf-8", errors="replace")
    leaked = [item for item in FORBIDDEN_EXTERNAL_TYPES if item in text]
    try:
        transformed_model = json.loads(text).get("model")
    except json.JSONDecodeError:
        transformed_model = None
    return transformed, leaked, transformed_model if isinstance(transformed_model, str) else None


def summarize_response(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body[:200].decode("utf-8", errors="replace")
    if isinstance(payload, dict):
        error = payload.get("error") or payload.get("detail")
        if error:
            return json.dumps(error, ensure_ascii=True)[:300]
        if payload.get("id"):
            return f"id={payload.get('id')}"
    return json.dumps(payload, ensure_ascii=True)[:300]


def probe_model(model: str, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "model": model,
        "ok": False,
        "status": None,
        "duration_ms": None,
        "upstream": None,
        "upstream_host": None,
        "transformed_model": None,
        "leaked_internal_items": [],
        "detail": None,
    }
    try:
        upstream = choose_upstream(model)
        result["upstream"] = upstream.get("name")
        result["upstream_host"] = urlsplit(str(upstream.get("base_url", ""))).netloc
        body, leaked, transformed_model_name = transformed_body(model, upstream)
        result["transformed_model"] = transformed_model_name
        result["leaked_internal_items"] = leaked
        if leaked:
            result["detail"] = "transformed payload still contains internal Codex item types"
            return result

        headers = upstream_headers({"Content-Type": "application/json", "Accept": "application/json"}, upstream)
        request = Request(_responses_url(upstream, "/v1/responses"), data=body, headers=headers, method="POST")
        with urlopen(request, timeout=timeout) as response:
            response_body = response.read(4096)
            result["status"] = response.status
            result["ok"] = 200 <= response.status < 300
            result["detail"] = summarize_response(response_body)
    except HTTPError as exc:
        result["status"] = exc.code
        try:
            response_body = exc.read(4096)
        except OSError:
            response_body = b""
        result["detail"] = summarize_response(response_body) if response_body else safe_upstream_error_detail(exc)
    except (ValueError, URLError, TimeoutError, OSError) as exc:
        result["detail"] = safe_upstream_error_detail(exc)
    finally:
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Codex proxy provider endpoints with normalized history payloads.")
    parser.add_argument("models", nargs="*", help="Model slugs to probe. Defaults to every non-official visible model.")
    parser.add_argument("--include-official", action="store_true", help="Also probe GPT models with incoming auth, if available.")
    parser.add_argument("--timeout", type=int, default=90, help="Per-model timeout in seconds.")
    args = parser.parse_args()

    models = args.models or visible_catalog_models(include_official=args.include_official)
    results = [probe_model(model, args.timeout) for model in models]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item.get("ok") for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
