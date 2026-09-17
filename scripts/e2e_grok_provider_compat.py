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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    {
        "case_id": "seq-fixture-synth-completed",
        "provider_id": "seq-fixture",
        "model": "seq-fixture/seq-synth-complete",
        "inbound": "responses",
        "endpoint": "/v1/responses",
        "requires": "fixture",
        "expect_terminal": "response.completed",
        "expect_min_sequence": 3,
        "allow_status": (200,),
    },
    {
        "case_id": "seq-fixture-synth-failed",
        "provider_id": "seq-fixture",
        "model": "seq-synth-failed",
        "inbound": "responses",
        "endpoint": "/v1/providers/seq-fixture/responses",
        "requires": "fixture",
        "expect_terminal": "response.failed",
        "expect_min_sequence": 2,
        "allow_status": (200, 502),
    },
)


FIXTURE_PROVIDER_ID = "seq-fixture"
FIXTURE_API_KEY = "fixture-seq-key"
SYNTH_COMPLETE_MODEL = "seq-synth-complete"
SYNTH_FAILED_MODEL = "seq-synth-failed"
LAST_UPSTREAM_SEQUENCE = 1
SYNTH_COMPLETE_LAST_SEQUENCE = 2


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


def sse_events(body: bytes) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for raw in body.split(b"\n"):
        if not raw.startswith(b"data:"):
            continue
        payload = raw.split(b":", 1)[1].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def missing_sequence_number(body: bytes) -> str:
    for event in sse_events(body):
        event_type = event.get("type")
        if not isinstance(event_type, str):
            continue
        if not (event_type.startswith("response.") or event_type == "error"):
            continue
        sequence = event.get("sequence_number")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            return f"missing sequence_number on {event_type}"
    return ""


def synthetic_terminal_error(
    body: bytes, *, expect_type: str, expect_min_sequence: int
) -> str:
    missing = missing_sequence_number(body)
    if missing:
        return missing
    matches = [event for event in sse_events(body) if event.get("type") == expect_type]
    if not matches:
        seen = [event.get("type") for event in sse_events(body)]
        return f"missing {expect_type}; saw {seen[-8:]}"
    sequence = matches[-1].get("sequence_number")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < expect_min_sequence:
        return f"{expect_type} sequence_number={sequence!r} want>={expect_min_sequence}"
    return ""


def _fixture_upstream_events(model: str) -> list[dict[str, object]]:
    short = model.rsplit("/", 1)[-1]
    created = {
        "type": "response.created",
        "sequence_number": 0,
        "response": {
            "id": "resp_seq_fixture",
            "object": "response",
            "status": "in_progress",
            "model": short,
            "output": [],
        },
    }
    if short == SYNTH_COMPLETE_MODEL:
        item = {
            "id": "item_seq_1",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_seq_1",
            "name": "read_file",
            "arguments": '{"path":"README.md"}',
        }
        return [
            created,
            {
                "type": "response.output_item.added",
                "sequence_number": 1,
                "output_index": 0,
                "item": item,
            },
            {
                "type": "response.output_item.done",
                "sequence_number": SYNTH_COMPLETE_LAST_SEQUENCE,
                "output_index": 0,
                "item": item,
            },
        ]
    return [
        created,
        {
            "type": "response.in_progress",
            "sequence_number": LAST_UPSTREAM_SEQUENCE,
            "response": {
                "id": "resp_seq_fixture",
                "object": "response",
                "status": "in_progress",
                "model": short,
                "output": [],
            },
        },
    ]


def _sse_bytes(event: dict[str, object]) -> bytes:
    return b"data: " + json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n\n"


class SequenceFixtureServer:
    """Loopback third-party Responses upstream for Gateway-synthesized terminals."""

    def __init__(self) -> None:
        this = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?", 1)[0]
                try:
                    payload = json.loads(body.decode("utf-8-sig") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    payload = {}
                model = payload.get("model") if isinstance(payload, dict) else None
                short = str(model).rsplit("/", 1)[-1] if isinstance(model, str) else ""
                if path not in {"/responses", "/v1/responses"} or short not in {
                    SYNTH_COMPLETE_MODEL,
                    SYNTH_FAILED_MODEL,
                }:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b'{"error":"fixture_unknown_route"}')
                    self.close_connection = True
                    return
                events = _fixture_upstream_events(str(model))
                abort = short == SYNTH_FAILED_MODEL
                payload = b"".join(_sse_bytes(event) for event in events)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                if abort:
                    self.send_header("Content-Length", "1000000")
                else:
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                if abort:
                    self.connection.close()
                self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _append_fixture_provider(providers_path: Path, base_url: str) -> None:
    block = "\n".join(
        [
            "",
            "[[providers]]",
            f'id = "{FIXTURE_PROVIDER_ID}"',
            'name = "Sequence Fixture"',
            f'base_url = "{base_url}"',
            f'api_key = "{FIXTURE_API_KEY}"',
            'upstream_format = "responses"',
            'available_upstream_formats = ["responses"]',
            "enabled = true",
            "",
            "  [[providers.models]]",
            f'  id = "{SYNTH_COMPLETE_MODEL}"',
            '  display_name = "Seq Synth Complete"',
            "  enabled = true",
            "  gateway_exported = true",
            '  visibility = "list"',
            "",
            "  [[providers.models]]",
            f'  id = "{SYNTH_FAILED_MODEL}"',
            '  display_name = "Seq Synth Failed"',
            "  enabled = true",
            "  gateway_exported = true",
            '  visibility = "list"',
            "",
        ]
    )
    providers_path.write_text(providers_path.read_text(encoding="utf-8") + block, encoding="utf-8")


def _disable_retries(settings_path: Path) -> None:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["gateway_auto_retry_enabled"] = False
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


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
    try:
        http_status, body = CHAT._post_chat(url, payload, grok_headers(gateway_key), timeout)
    except TimeoutError:
        return {
            "case_id": case["case_id"],
            "provider_id": case["provider_id"],
            "model": case["model"],
            "endpoint_binding": case["endpoint"],
            "protocol": case["inbound"],
            "http_status": None,
            "ok": False,
            "error": "downstream SSE timed out",
            "outcome": "failed",
            "saw_sentinel": False,
        }
    text = body.decode("utf-8", "replace")
    text = body.decode("utf-8", "replace")
    error = protocol_failure(http_status, body)
    if not error and case["inbound"] == "responses" and case["requires"] != "official":
        expect_terminal = case.get("expect_terminal")
        if isinstance(expect_terminal, str):
            error = synthetic_terminal_error(
                body,
                expect_type=expect_terminal,
                expect_min_sequence=int(case.get("expect_min_sequence") or LAST_UPSTREAM_SEQUENCE + 1),
            )
        else:
            error = missing_sequence_number(body)
    allow_status = case.get("allow_status") or (200,)
    if not error and http_status not in allow_status:
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
    if case.get("requires") == "fixture":
        result["saw_sentinel"] = True
        terminals = [event.get("type") for event in sse_events(body) if event.get("type") in {"response.completed", "response.failed"}]
        result["terminals"] = terminals
        seqs = [event.get("sequence_number") for event in sse_events(body) if event.get("type") == case.get("expect_terminal")]
        result["terminal_sequence"] = seqs[-1] if seqs else None
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
        elif requires == "fixture":
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
    fixture: SequenceFixtureServer | None = None
    try:
        env, settings_path, catalog, port, gateway_key = CHAT._prepare_runtime(
            work, args.settings, args.providers, args.auth, args.catalog
        )
        if any(case["requires"] == "fixture" for case in runnable):
            fixture = SequenceFixtureServer()
            fixture.start()
            _append_fixture_provider(settings_path.parent / "config" / "providers.toml", fixture.base_url)
            _disable_retries(settings_path)
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
        if fixture is not None:
            fixture.close()
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
                        "terminal_sequence": item.get("terminal_sequence"),
                        "terminals": item.get("terminals"),
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
