"""Live Grok-shaped provider-path E2E against an isolated candidate Gateway.

Grok is not in ``_infer_client_id``, so these posts omit ``X-Codex-Client-Id``.
Each catalog provider is hit on both ``/responses`` and ``/chat/completions``.
Protocol 400s that this change sanitizes fail closed; missing credentials skip.
"""
# ruff: noqa: E402

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "e2e_chat_completions", ROOT / "scripts" / "e2e_chat_completions.py"
)
assert SPEC and SPEC.loader
CHAT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHAT)

SENTINEL_PREFIX = "SENTINEL:codexhub-grok-e2e:"
SYSTEM = "You are Grok released by xAI."
PROTOCOL_FAILURE_SNIPPETS = (
    "System messages are not allowed",
    "Argument not supported: external_web_access",
    "unsupported_protocol_semantics",
    "Cannot translate unsupported",
    "Cannot honor web_search external_web_access=false",
    "additionalProperties",
    "MissingSessionID",
)

CASES = (
    {
        "case_id": "openai-responses",
        "provider_id": "openai",
        "model": "gpt-5.6-luna",
        "inbound": "responses",
        "endpoint": "/v1/providers/openai/responses",
        "requires": "official",
    },
    {
        "case_id": "openai-chat",
        "provider_id": "openai",
        "model": "gpt-5.6-luna",
        "inbound": "chat_completions",
        "endpoint": "/v1/providers/openai/chat/completions",
        "requires": "official",
    },
    {
        "case_id": "opencode-go-responses",
        "provider_id": "opencode-go",
        "model": "muse-spark-1.3-contributor",
        "inbound": "responses",
        "endpoint": "/v1/providers/opencode-go/responses",
        "requires": "opencode-go",
    },
    {
        "case_id": "opencode-go-chat",
        "provider_id": "opencode-go",
        "model": "muse-spark-1.3-contributor",
        "inbound": "chat_completions",
        "endpoint": "/v1/providers/opencode-go/chat/completions",
        "requires": "opencode-go",
    },
    {
        "case_id": "commandcode-responses",
        "provider_id": "commandcode",
        "model": "deepseek/deepseek-v4.1-flash",
        "inbound": "responses",
        "endpoint": "/v1/providers/commandcode/responses",
        "requires": "commandcode",
    },
    {
        "case_id": "commandcode-chat",
        "provider_id": "commandcode",
        "model": "deepseek/deepseek-v4.1-flash",
        "inbound": "chat_completions",
        "endpoint": "/v1/providers/commandcode/chat/completions",
        "requires": "commandcode",
    },
)


def grok_headers(gateway_key: str) -> dict[str, str]:
    headers = CHAT.chat_headers(gateway_key)
    assert "X-Codex-Client-Id" not in headers
    return headers


def grok_responses_payload(model: str, sentinel: str) -> dict[str, object]:
    return {
        "model": model,
        "input": [
            {"type": "message", "role": "system", "content": SYSTEM},
            {
                "type": "message",
                "role": "user",
                "content": f"Reply with only this exact line and no other text: {sentinel}",
            },
        ],
        "tools": [
            {
                "type": "function",
                "name": "read_file",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            }
        ],
        "stream": True,
        "store": False,
        "prompt_cache_key": "grok-e2e-session-cache",
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": 128,
        "reasoning": {"effort": "xhigh"},
    }


def grok_chat_payload(model: str, sentinel: str) -> dict[str, object]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": f"Reply with only this exact line and no other text: {sentinel}",
            },
        ],
        "tools": [
            {
                "type": "function",
                "name": "read_file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            }
        ],
        "stream": True,
        "max_tokens": 128,
        "prompt_cache_key": "grok-e2e-session-cache",
    }


def protocol_failure(http_status: int, body: bytes) -> str:
    text = body.decode("utf-8", "replace")
    if http_status < 400:
        return ""
    for snippet in PROTOCOL_FAILURE_SNIPPETS:
        if snippet in text:
            return f"HTTP {http_status}: {snippet}"
    if http_status == 400:
        return f"HTTP 400: {text[-400:]}"
    return ""


def _provider_ids(providers_path: Path) -> set[str]:
    import tomllib

    data = tomllib.loads(providers_path.read_text(encoding="utf-8"))
    ids: set[str] = set()
    for provider in data.get("providers", []):
        if isinstance(provider, dict) and provider.get("id"):
            ids.add(str(provider["id"]))
    return ids


def selected_cases(names: list[str] | None) -> list[dict[str, str]]:
    if not names:
        return [dict(case) for case in CASES]
    wanted = set(names)
    found = [dict(case) for case in CASES if case["case_id"] in wanted]
    missing = wanted - {case["case_id"] for case in found}
    if missing:
        raise ValueError(f"unknown grok e2e cases: {sorted(missing)}")
    return found


def _live_case(
    base: str,
    case: dict[str, str],
    gateway_key: str,
    timeout: int,
) -> dict[str, object]:
    sentinel = SENTINEL_PREFIX + case["case_id"]
    payload = (
        grok_responses_payload(case["model"], sentinel)
        if case["inbound"] == "responses"
        else grok_chat_payload(case["model"], sentinel)
    )
    url = base.rstrip("/") + case["endpoint"]
    http_status, body = CHAT._post_chat(url, payload, grok_headers(gateway_key), timeout)
    text = body.decode("utf-8", "replace")
    error = protocol_failure(http_status, body)
    if not error and http_status != 200:
        error = f"HTTP {http_status}: {text[-400:]}"
    result = {
        "case_id": case["case_id"],
        "provider_id": case["provider_id"],
        "model": case["model"],
        "endpoint_binding": case["endpoint"],
        "protocol": case["inbound"],
        "http_status": http_status,
        "ok": not error,
        "error": error,
        "outcome": "passed" if not error else "failed",
        "saw_sentinel": sentinel in body.decode("utf-8", "replace"),
    }
    if error:
        result["body_tail"] = body.decode("utf-8", "replace")[-800:]
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", type=Path, help="skip self-build and use this candidate")
    parser.add_argument("--output", type=Path, default=Path("test-results/grok-provider-e2e.json"))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--auth", type=Path, default=Path.home() / ".codex" / "auth.json")
    parser.add_argument(
        "--providers",
        type=Path,
        default=Path.home() / ".codex" / "proxy" / "config" / "providers.toml",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path.home() / ".codex" / "proxy" / "settings.json",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path.home() / ".codex" / "model-catalogs" / "codexhub-model-catalog.json",
    )
    parser.add_argument(
        "--opencode-go-credentials",
        type=Path,
        help="dedicated OpenCode Go credential JSON for {env:OPENCODE_API_KEY}",
    )
    args = parser.parse_args(argv)
    missing = [
        str(path)
        for path in (args.auth, args.providers, args.settings)
        if not path.is_file()
    ]
    if missing:
        parser.error(f"missing inputs={missing}")
    try:
        cases = selected_cases(args.cases)
    except ValueError as error:
        parser.error(str(error))
    try:
        opencode_go_api_key = CHAT.load_opencode_go_api_key(args.opencode_go_credentials)
    except ValueError as error:
        parser.error(str(error))
    provider_ids = _provider_ids(args.providers)
    runnable: list[dict[str, str]] = []
    skipped: list[str] = []
    for case in cases:
        requires = case["requires"]
        if requires == "official":
            runnable.append(case)
        elif requires == "opencode-go":
            if opencode_go_api_key or "opencode-go" in provider_ids:
                runnable.append(case)
            else:
                skipped.append(case["case_id"])
        elif requires in provider_ids:
            runnable.append(case)
        else:
            skipped.append(case["case_id"])
    binary = args.bin.resolve() if args.bin else CHAT.build_candidate()
    report: dict[str, object] = {
        "schema": "codexhub.grok-provider-e2e.v1",
        "candidate": str(binary),
        "cases": [],
        "skipped": skipped,
    }
    failures: list[str] = []
    work_manager = tempfile.TemporaryDirectory(prefix="codexhub-grok-e2e-")
    work = Path(work_manager.name)
    try:
        env, _settings, catalog, port, gateway_key = CHAT._prepare_runtime(
            work, args.settings, args.providers, args.auth, args.catalog
        )
        if opencode_go_api_key:
            env["OPENCODE_API_KEY"] = opencode_go_api_key
        refresh = None if catalog.is_file() else CHAT._run(
            [str(binary), "refresh-models"], env=env, timeout=180
        )
        if not catalog.is_file():
            failures.append("candidate: refresh-models failed")
            report["bootstrap_tail"] = (
                ((refresh.stdout or "") + "\n" + (refresh.stderr or ""))[-1600:]
                if refresh
                else "Official catalog is missing"
            )
        else:
            starter, bootstrap_tail = CHAT.start_candidate(binary, env, port)
            if starter is None:
                failures.append("candidate: Gateway failed to become healthy")
                report["bootstrap_tail"] = bootstrap_tail
            else:
                try:
                    base = f"http://127.0.0.1:{port}"
                    for case in runnable:
                        result = _live_case(base, case, gateway_key, args.timeout)
                        if not result["ok"]:
                            failures.append(
                                f"{case['case_id']}: {result.get('error') or 'live grok body failed'}"
                            )
                        report["cases"].append(result)
                finally:
                    CHAT._run([str(binary), "stop"], env=env, timeout=60)
                    if starter.poll() is None:
                        starter.terminate()
    finally:
        work_manager.cleanup()
    report["failures"] = failures
    report["ok"] = not failures
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "failures": failures,
                "skipped": skipped,
                "cases": [
                    {
                        "case_id": item["case_id"],
                        "http_status": item.get("http_status"),
                        "endpoint_binding": item.get("endpoint_binding"),
                        "outcome": item.get("outcome"),
                    }
                    for item in report["cases"]
                ],
            },
            indent=2,
        )
    )
    print(f"Report: {args.output}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
