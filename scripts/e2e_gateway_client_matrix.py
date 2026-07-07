from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_PROXY_BASE_URL = "http://127.0.0.1:9099/v1"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_OUTPUT_TOKENS = 256
DEFAULT_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 2.0
RESPONSE_TERMINAL_TYPES = {"response.completed", "response.failed", "response.incomplete", "error"}


@dataclasses.dataclass(frozen=True)
class ClientCase:
    client: str
    provider_id: str
    model_id: str
    display_name: str
    api: str
    base_url: str
    api_key: str
    source_path: str

    @property
    def selector(self) -> str:
        return f"{self.provider_id}/{self.model_id}"

    @property
    def endpoint_kind(self) -> str:
        if self.api in {"openai-responses", "responses"}:
            return "responses"
        if self.api in {"openai-completions", "openai-chat-completions", "chat_completions"}:
            return "chat_completions"
        return self.api or "unknown"


@dataclasses.dataclass
class CaseResult:
    client: str
    provider_id: str
    model_id: str
    api: str
    endpoint: str
    status: str
    duration_ms: int
    http_status: int | None = None
    output_preview: str = ""
    error: str = ""
    frames_seen: int = 0
    terminal_seen: bool = False
    raw_preview: list[str] = dataclasses.field(default_factory=list)
    attempts: int = 1
    retry_errors: list[str] = dataclasses.field(default_factory=list)


def home_path(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def parse_opencode_config(path: Path) -> list[ClientCase]:
    data = read_json(path)
    cases: list[ClientCase] = []
    for provider_id, provider in sorted((data.get("provider") or {}).items()):
        if not provider_id.startswith("codexhub-"):
            continue
        options = provider.get("options") or {}
        api = "openai-responses" if provider.get("npm") == "@ai-sdk/openai" else "openai-completions"
        for model_id, model in sorted((provider.get("models") or {}).items()):
            cases.append(
                ClientCase(
                    client="opencode",
                    provider_id=provider_id,
                    model_id=model_id,
                    display_name=str(model.get("name") or model_id),
                    api=api,
                    base_url=str(options.get("baseURL") or ""),
                    api_key=str(options.get("apiKey") or ""),
                    source_path=str(path),
                )
            )
    return cases


def parse_zcode_v2_config(path: Path) -> list[ClientCase]:
    data = read_json(path)
    cases: list[ClientCase] = []
    for provider_id, provider in sorted((data.get("provider") or {}).items()):
        if not provider_id.startswith("codexhub-"):
            continue
        kind = provider.get("kind")
        api = "openai-responses" if kind == "openai" else "openai-completions"
        api_format = provider.get("apiFormat")
        if api_format in {"openai-responses", "openai-chat-completions"}:
            api = "openai-responses" if api_format == "openai-responses" else "openai-completions"
        options = provider.get("options") or {}
        endpoints = provider.get("endpoints") or {}
        paths = endpoints.get("paths") or {}
        base_url = str(endpoints.get("baseURL") or options.get("baseURL") or "")
        if api == "openai-responses" and paths.get("openai"):
            base_url = combine_url(base_url, str(paths["openai"]), append_endpoint=False)
        elif api == "openai-completions" and paths.get("openai-compatible"):
            base_url = combine_url(base_url, str(paths["openai-compatible"]), append_endpoint=False)
        for model_id, model in sorted((provider.get("models") or {}).items()):
            cases.append(
                ClientCase(
                    client="zcode",
                    provider_id=provider_id,
                    model_id=model_id,
                    display_name=str(model.get("name") or model_id),
                    api=api,
                    base_url=base_url,
                    api_key=str(options.get("apiKey") or provider.get("apiKey") or ""),
                    source_path=str(path),
                )
            )
    return cases


def parse_pi_models(path: Path, *, client: str = "pi") -> list[ClientCase]:
    data = read_json(path)
    cases: list[ClientCase] = []
    for provider_id, provider in sorted((data.get("providers") or {}).items()):
        if not provider_id.startswith("codexhub-"):
            continue
        for model in provider.get("models") or []:
            model_id = str(model.get("id") or "")
            if not model_id:
                continue
            cases.append(
                ClientCase(
                    client=client,
                    provider_id=provider_id,
                    model_id=model_id,
                    display_name=str(model.get("name") or model_id),
                    api=str(provider.get("api") or ""),
                    base_url=str(provider.get("baseUrl") or ""),
                    api_key=str(provider.get("apiKey") or ""),
                    source_path=str(path),
                )
            )
    return cases


def parse_omp_models(path: Path) -> list[ClientCase]:
    cases: list[ClientCase] = []
    provider_id: str | None = None
    provider_api = ""
    provider_base_url = ""
    provider_api_key = ""
    in_models = False
    current_model: dict[str, str] | None = None

    def flush_model() -> None:
        nonlocal current_model
        if provider_id and current_model and current_model.get("id"):
            cases.append(
                ClientCase(
                    client="omp",
                    provider_id=provider_id,
                    model_id=current_model["id"],
                    display_name=current_model.get("name") or current_model["id"],
                    api=provider_api,
                    base_url=provider_base_url,
                    api_key=provider_api_key,
                    source_path=str(path),
                )
            )
        current_model = None

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        if raw_line.startswith("  ") and not raw_line.startswith("    ") and raw_line.rstrip().endswith(":"):
            flush_model()
            candidate = raw_line.strip().rstrip(":")
            provider_id = candidate if candidate.startswith("codexhub-") else None
            provider_api = ""
            provider_base_url = ""
            provider_api_key = ""
            in_models = False
            continue
        if provider_id is None:
            continue
        stripped = raw_line.strip()
        if stripped == "models:":
            in_models = True
            continue
        if stripped.startswith("baseUrl:"):
            provider_base_url = unquote_yaml_scalar(stripped.split(":", 1)[1].strip())
            continue
        if stripped.startswith("api:"):
            provider_api = unquote_yaml_scalar(stripped.split(":", 1)[1].strip())
            continue
        if stripped.startswith("apiKey:"):
            provider_api_key = unquote_yaml_scalar(stripped.split(":", 1)[1].strip())
            continue
        if in_models and stripped.startswith("- id:"):
            flush_model()
            current_model = {"id": unquote_yaml_scalar(stripped.split(":", 1)[1].strip())}
            continue
        if current_model is not None and stripped.startswith("name:"):
            current_model["name"] = unquote_yaml_scalar(stripped.split(":", 1)[1].strip())
    flush_model()
    return cases


def unquote_yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def combine_url(base_url: str, path: str, *, append_endpoint: bool) -> str:
    base_url = base_url.strip().rstrip("/")
    path = path.strip()
    if not base_url:
        return path
    if not path:
        return base_url
    if path.startswith("http://") or path.startswith("https://"):
        return path.rstrip("/")
    if not append_endpoint:
        return f"{base_url}/{path.lstrip('/')}".rstrip("/")
    return f"{base_url}/{path.strip('/')}".rstrip("/")


def endpoint_for_case(case: ClientCase) -> str:
    base_url = case.base_url.rstrip("/")
    if case.endpoint_kind == "responses":
        if base_url.endswith("/responses"):
            return base_url
        return combine_url(base_url, "responses", append_endpoint=True)
    if case.endpoint_kind == "chat_completions":
        if base_url.endswith("/chat/completions"):
            return base_url
        return combine_url(base_url, "chat/completions", append_endpoint=True)
    return base_url


def payload_for_case(case: ClientCase, max_output_tokens: int) -> dict[str, Any]:
    prompt = "Reply with CODEXHUB_E2E_OK."
    if case.endpoint_kind == "responses":
        return {
            "model": case.model_id,
            "input": prompt,
            "stream": True,
            "store": False,
            "max_output_tokens": max_output_tokens,
        }
    return {
        "model": case.model_id,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": max_output_tokens,
    }


def request_headers(case: ClientCase) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Codex-Client-Id": case.client,
        "X-CodexHub-E2E-Selector": case.selector,
        "X-Request-Kind": "main_generation",
    }
    if case.api_key:
        headers["Authorization"] = f"Bearer {case.api_key}"
    return headers


def post_stream(case: ClientCase, timeout_seconds: int, max_output_tokens: int) -> CaseResult:
    endpoint = endpoint_for_case(case)
    started = time.monotonic()
    result = CaseResult(
        client=case.client,
        provider_id=case.provider_id,
        model_id=case.model_id,
        api=case.api,
        endpoint=endpoint,
        status="failed",
        duration_ms=0,
    )
    payload = payload_for_case(case, max_output_tokens)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
        headers=request_headers(case),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            result.http_status = getattr(response, "status", None)
            if result.http_status and result.http_status >= 400:
                result.error = f"HTTP {result.http_status}"
                return result
            parse_stream(response, case, result)
    except urllib.error.HTTPError as exc:
        result.http_status = exc.code
        body = exc.read().decode("utf-8", "replace")
        result.error = compact_error(f"HTTP {exc.code}: {body}")
    except Exception as exc:
        result.error = compact_error(f"{type(exc).__name__}: {exc}")
    finally:
        result.duration_ms = int((time.monotonic() - started) * 1000)
    if result.status != "passed" and not result.error:
        result.error = "stream ended without passing checks"
    return result


def is_transient_failure(result: CaseResult) -> bool:
    if result.status == "passed":
        return False
    error = result.error.lower()
    transient_markers = (
        "system is busy",
        "engineinternalerror:1105",
        "stream reached terminal event without output text",
        "timed out",
        "timeout",
        "connection reset",
        "connection aborted",
    )
    return any(marker in error for marker in transient_markers)


def post_stream_with_retries(
    case: ClientCase,
    timeout_seconds: int,
    max_output_tokens: int,
    attempts: int,
    retry_delay_seconds: float,
) -> CaseResult:
    attempts = max(1, attempts)
    retry_errors: list[str] = []
    for attempt in range(1, attempts + 1):
        result = post_stream(case, timeout_seconds, max_output_tokens)
        result.attempts = attempt
        result.retry_errors = list(retry_errors)
        if result.status == "passed":
            return result
        retry_errors.append(result.error)
        if attempt >= attempts or not is_transient_failure(result):
            result.retry_errors = list(retry_errors[:-1])
            return result
        time.sleep(retry_delay_seconds * attempt)
    return result


def parse_stream(response: Any, case: ClientCase, result: CaseResult) -> None:
    pending_event: str | None = None
    pending_has_data = False
    text_parts: list[str] = []
    error_events: list[str] = []
    frames_seen = 0
    terminal_seen = False
    output_seen = False
    data_seen = False

    for _ in range(1200):
        raw = response.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if len(result.raw_preview) < 18:
            result.raw_preview.append(line)
        if not line:
            if pending_event and not pending_has_data:
                result.error = f"SSE event without data: {pending_event}"
                return
            pending_event = None
            pending_has_data = False
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            if pending_event and not pending_has_data:
                result.error = f"SSE event without data before next event: {pending_event}"
                return
            pending_event = line.split(":", 1)[1].strip()
            pending_has_data = False
            frames_seen += 1
            continue
        if not line.startswith("data:"):
            continue
        data_seen = True
        pending_has_data = True
        raw_data = line.split(":", 1)[1].strip()
        if not raw_data:
            result.error = f"empty SSE data payload for event {pending_event or '<unnamed>'}"
            return
        if raw_data == "[DONE]":
            terminal_seen = True
            break
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            result.error = f"invalid SSE JSON for event {pending_event or '<unnamed>'}: {exc}"
            return
        if isinstance(payload, dict):
            if case.endpoint_kind == "responses":
                event_type = str(payload.get("type") or pending_event or "")
                if event_type.startswith("response.reasoning_summary_text."):
                    continue
                if event_type == "response.output_text.delta":
                    delta = payload.get("delta")
                    if isinstance(delta, str):
                        text_parts.append(delta)
                        output_seen = True
                if event_type in RESPONSE_TERMINAL_TYPES:
                    terminal_seen = True
                    if event_type in {"response.failed", "response.incomplete", "error"}:
                        error_events.append(compact_error(json.dumps(payload, ensure_ascii=False)))
                    break
            else:
                if "error" in payload:
                    error_events.append(compact_error(json.dumps(payload, ensure_ascii=False)))
                    terminal_seen = True
                    break
                choices = payload.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta") or {}
                        message = choice.get("message") or {}
                        content = delta.get("content") if isinstance(delta, dict) else None
                        if content is None and isinstance(message, dict):
                            content = message.get("content")
                        if isinstance(content, str):
                            text_parts.append(content)
                            output_seen = True
                        if choice.get("finish_reason") is not None:
                            terminal_seen = True
                    if terminal_seen:
                        break

    result.frames_seen = frames_seen
    result.terminal_seen = terminal_seen
    result.output_preview = compact_error("".join(text_parts))
    if error_events:
        result.error = error_events[0]
        return
    if not data_seen:
        result.error = "stream produced no data lines"
        return
    if not terminal_seen:
        result.error = "stream ended without terminal event"
        return
    if not output_seen:
        result.error = "stream reached terminal event without output text"
        return
    result.status = "passed"
    result.error = ""


def compact_error(value: str, limit: int = 700) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def load_cases(args: argparse.Namespace) -> list[ClientCase]:
    cases: list[ClientCase] = []
    paths = {
        "opencode": Path(args.opencode_config),
        "zcode": Path(args.zcode_config),
        "pi": Path(args.pi_models),
        "omp": Path(args.omp_models),
    }
    if paths["opencode"].exists():
        cases.extend(parse_opencode_config(paths["opencode"]))
    if paths["zcode"].exists():
        cases.extend(parse_zcode_v2_config(paths["zcode"]))
    if paths["pi"].exists():
        cases.extend(parse_pi_models(paths["pi"], client="pi"))
    if paths["omp"].exists():
        cases.extend(parse_omp_models(paths["omp"]))
    if args.client:
        allowed = set(args.client)
        cases = [case for case in cases if case.client in allowed]
    if args.provider:
        allowed = set(args.provider)
        cases = [case for case in cases if case.provider_id in allowed]
    if args.model:
        wanted = set(args.model)
        cases = [
            case
            for case in cases
            if case.model_id in wanted or case.selector in wanted or f"{case.client}/{case.selector}" in wanted
        ]
    return sorted(cases, key=lambda case: (case.client, case.provider_id, case.model_id))


def summarize_config_coverage(cases: list[ClientCase]) -> dict[str, Any]:
    by_client: dict[str, set[str]] = {}
    for case in cases:
        by_client.setdefault(case.client, set()).add(case.selector)
    all_selectors = sorted({selector for selectors in by_client.values() for selector in selectors})
    missing = {
        client: [selector for selector in all_selectors if selector not in selectors]
        for client, selectors in sorted(by_client.items())
    }
    return {
        "clients": {client: sorted(selectors) for client, selectors in sorted(by_client.items())},
        "missing_by_client": missing,
        "selector_count": len(all_selectors),
        "selectors": all_selectors,
    }


def write_report(output_dir: Path, results: list[CaseResult], cases: list[ClientCase]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"gateway-client-matrix-{timestamp}.json"
    report = {
        "generated_at": timestamp,
        "coverage": summarize_config_coverage(cases),
        "results": [dataclasses.asdict(result) for result in results],
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = output_dir / "gateway-client-matrix-latest.json"
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def print_result(result: CaseResult) -> None:
    label = f"{result.client} {result.provider_id}/{result.model_id}"
    if result.status == "passed":
        retry_suffix = f" attempt={result.attempts}" if result.attempts > 1 else ""
        print(f"PASS {label} {result.api} {result.duration_ms}ms{retry_suffix} {result.output_preview[:80]}")
    else:
        retry_suffix = f" attempts={result.attempts}" if result.attempts > 1 else ""
        print(f"FAIL {label} {result.api} {result.duration_ms}ms{retry_suffix} {result.error}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run CodexHub Gateway client/model E2E matrix.")
    parser.add_argument("--opencode-config", default=str(home_path(".config", "opencode", "opencode.json")))
    parser.add_argument("--zcode-config", default=r"D:\zcode\.zcode\v2\config.json")
    parser.add_argument("--pi-models", default=str(home_path(".pi", "agent", "models.json")))
    parser.add_argument("--omp-models", default=str(home_path(".omp", "agent", "models.yml")))
    parser.add_argument("--client", action="append", choices=["opencode", "zcode", "pi", "omp"])
    parser.add_argument("--provider", action="append")
    parser.add_argument("--model", action="append")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--retry-delay-seconds", type=float, default=DEFAULT_RETRY_DELAY_SECONDS)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--output-dir", default="test-results")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cases = load_cases(args)
    if not cases:
        print("No CodexHub client cases found.", file=sys.stderr)
        return 2
    coverage = summarize_config_coverage(cases)
    print(f"Loaded {len(cases)} cases across {len(coverage['clients'])} clients and {coverage['selector_count']} selectors.")
    for client, missing in coverage["missing_by_client"].items():
        if missing:
            print(f"COVERAGE-GAP {client}: missing {len(missing)} selectors: {', '.join(missing)}")
    if args.dry_run:
        for case in cases:
            print(f"CASE {case.client} {case.provider_id}/{case.model_id} {case.api} {endpoint_for_case(case)}")
        return 0

    results: list[CaseResult] = []
    if args.concurrency <= 1:
        for case in cases:
            result = post_stream_with_retries(
                case,
                args.timeout_seconds,
                args.max_output_tokens,
                args.attempts,
                args.retry_delay_seconds,
            )
            print_result(result)
            results.append(result)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            future_to_case = {
                executor.submit(
                    post_stream_with_retries,
                    case,
                    args.timeout_seconds,
                    args.max_output_tokens,
                    args.attempts,
                    args.retry_delay_seconds,
                ): case
                for case in cases
            }
            for future in concurrent.futures.as_completed(future_to_case):
                result = future.result()
                print_result(result)
                results.append(result)

    report_path = write_report(Path(args.output_dir), results, cases)
    failed = [result for result in results if result.status != "passed"]
    print(f"Report: {report_path}")
    print(f"Summary: {len(results) - len(failed)} passed, {len(failed)} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
