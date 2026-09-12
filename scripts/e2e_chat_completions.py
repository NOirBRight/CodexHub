"""Live inbound Chat Completions E2E against an isolated candidate Gateway."""
# ruff: noqa: E402

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
CHAT_CONTRACT_PATH = ROOT / "scripts" / "real_client_chat_contract.v1.json"
SENTINEL_PREFIX = "SENTINEL:codexhub-chat-e2e:"
PROMPT_TEMPLATE = "Reply with only this exact line and no other text: {sentinel}"
OPENCODE_GO_CREDENTIAL_SCHEMA = "codexhub.real-client-opencode-go.v1"


def load_opencode_go_api_key(path: Path | None) -> str:
    if path is None:
        return ""
    if not path.is_file():
        raise ValueError(f"missing OpenCode Go credentials: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid OpenCode Go credentials: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema") != OPENCODE_GO_CREDENTIAL_SCHEMA:
        raise ValueError("invalid OpenCode Go credential schema")
    key = payload.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("OpenCode Go credential missing api_key")
    return key.strip()


class Case(NamedTuple):
    case_id: str
    provider: str
    model: str
    endpoint_binding: str
    prompt_cache_key: str
    max_tokens: int = 256


def load_chat_contract() -> dict[str, object]:
    try:
        contract = json.loads(CHAT_CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid Chat E2E contract: {CHAT_CONTRACT_PATH}: {error}") from error
    if not isinstance(contract, dict) or contract.get("schema") != "codexhub.real-client-chat-contract.v1":
        raise RuntimeError("invalid Chat E2E contract schema")
    return contract


CHAT_CONTRACT = load_chat_contract()


def _contract_cases() -> tuple[Case, ...]:
    raw_cases = CHAT_CONTRACT["cases"]
    assert isinstance(raw_cases, list)
    cases: list[Case] = []
    for raw in raw_cases:
        assert isinstance(raw, dict)
        ids = raw["case_ids"]
        assert isinstance(ids, dict)
        cases.append(
            Case(
                str(ids["linux"]),
                str(raw["provider_id"]),
                str(raw["model"]),
                str(raw["endpoint_binding"]),
                str(raw.get("prompt_cache_key") or ""),
                int(raw.get("max_tokens") or 256),
            )
        )
    return tuple(cases)


CASES = _contract_cases()


def selected_cases(case_ids: list[str] | None) -> tuple[Case, ...]:
    if not case_ids:
        return CASES
    wanted = list(dict.fromkeys(case_ids))
    by_id = {case.case_id: case for case in CASES}
    missing = [case_id for case_id in wanted if case_id not in by_id]
    if missing:
        raise ValueError(f"unknown chat e2e cases: {missing}")
    return tuple(by_id[case_id] for case_id in wanted)


def chat_payload(case: Case, sentinel: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": case.model,
        "messages": [{"role": "user", "content": PROMPT_TEMPLATE.format(sentinel=sentinel)}],
        "stream": True,
        "max_tokens": case.max_tokens,
    }
    if case.prompt_cache_key:
        payload["prompt_cache_key"] = case.prompt_cache_key
    return payload


def _src_python() -> None:
    src = str(ROOT / "src-python")
    if src not in sys.path:
        sys.path.insert(0, src)


def v2_chat_tools() -> list[dict[str, object]]:
    _src_python()
    from collaboration_runtime_contract import COLLABORATION_V2, EXPECTED_PARAMETER_SCHEMAS

    tools: list[dict[str, object]] = []
    for name, schema in EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2].items():
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": schema,
                    "strict": False,
                },
            }
        )
    return tools


def official_function_payload(model: str) -> dict[str, object]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Call get_time now. Do not write other text."}],
        "stream": True,
        "max_tokens": 256,
        "prompt_cache_key": "codexhub-chat-e2e-function",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_time",
                    "description": "Return the current time.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "get_time"}},
    }


def official_v2_payload(model: str) -> dict[str, object]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Call list_agents now with empty arguments. Do not write other text.",
            }
        ],
        "stream": True,
        "max_tokens": 256,
        "prompt_cache_key": "codexhub-chat-e2e-v2",
        "tools": v2_chat_tools(),
        "tool_choice": "auto",
    }


def official_web_search_payload(model: str) -> dict[str, object]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Search the web for Codex CLI and reply with one short sentence.",
            }
        ],
        "stream": True,
        "max_tokens": 256,
        "prompt_cache_key": "codexhub-chat-e2e-web-search",
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
    }


def image_gen_namespace_tool() -> dict[str, object]:
    return {
        "type": "namespace",
        "name": "image_gen",
        "description": "Generate images from text prompts.",
        "tools": [
            {
                "type": "function",
                "name": "imagegen",
                "description": "Generate an image from a text prompt.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "The image to generate.",
                        }
                    },
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
            }
        ],
    }


def muse_web_search_payload(model: str) -> dict[str, object]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Search the web for Codex CLI and reply with one short sentence.",
            }
        ],
        "stream": True,
        "max_tokens": 1024,
        "prompt_cache_key": "codexhub-chat-e2e-muse-web-search",
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
    }


def muse_image_gen_payload(model: str) -> dict[str, object]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Call the image_gen.imagegen tool now with prompt "
                    "'a red cube on a white table'. Do not write other text."
                ),
            }
        ],
        "stream": True,
        "max_tokens": 1024,
        "prompt_cache_key": "codexhub-chat-e2e-muse-image-gen",
        "tools": [image_gen_namespace_tool()],
        "tool_choice": "auto",
    }


def muse_responses_web_search_payload(model: str) -> dict[str, object]:
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": "Search the web for Codex CLI and reply with one short sentence.",
            }
        ],
        "stream": True,
        "max_output_tokens": 1024,
        "prompt_cache_key": "codexhub-chat-e2e-muse-responses-web-search",
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
    }


def muse_responses_image_gen_payload(model: str) -> dict[str, object]:
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": (
                    "Call the image_gen.imagegen tool now with prompt "
                    "'a red cube on a white table'. Do not write other text."
                ),
            }
        ],
        "stream": True,
        "max_output_tokens": 1024,
        "prompt_cache_key": "codexhub-chat-e2e-muse-responses-image-gen",
        "tools": [image_gen_namespace_tool()],
        "tool_choice": "auto",
    }


def collect_tool_call_names(body: bytes) -> list[str]:
    names: list[str] = []
    for payload in _iter_json_objects(body):
        choices = payload.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            for container in (choice.get("delta"), choice.get("message")):
                if not isinstance(container, dict):
                    continue
                calls = container.get("tool_calls")
                if not isinstance(calls, list):
                    continue
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function")
                    name = function.get("name") if isinstance(function, dict) else None
                    if isinstance(name, str) and name:
                        names.append(name)
    return names


def collect_responses_tool_names(body: bytes) -> list[str]:
    names: list[str] = []
    for payload in _iter_json_objects(body):
        event_type = payload.get("type")
        item = payload.get("item") if isinstance(payload.get("item"), dict) else None
        if event_type in {"response.output_item.added", "response.output_item.done"} and item:
            name = item.get("name")
            item_type = item.get("type")
            if isinstance(name, str) and name:
                names.append(name)
            elif item_type == "image_generation_call":
                names.append("image_generation")
            elif item_type == "web_search_call":
                names.append("web_search")
        output = payload.get("output")
        if isinstance(output, list):
            for entry in output:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                entry_type = entry.get("type")
                if isinstance(name, str) and name:
                    names.append(name)
                elif entry_type == "image_generation_call":
                    names.append("image_generation")
                elif entry_type == "web_search_call":
                    names.append("web_search")
    return list(dict.fromkeys(names))


def is_image_gen_tool_name(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in {"imagegen", "image_gen", "image_generation"}
        or lowered.startswith("image_gen.")
        or "imagegen" in lowered
    )


def conversion_shape(payload: dict[str, object]) -> dict[str, object]:
    tools_out: list[dict[str, object]] = []
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "namespace":
                children = tool.get("tools")
                tools_out.append(
                    {
                        "type": "namespace",
                        "name": tool.get("name"),
                        "children": [
                            child.get("name")
                            for child in children
                            if isinstance(child, dict)
                        ]
                        if isinstance(children, list)
                        else [],
                    }
                )
            else:
                tools_out.append({"type": tool.get("type"), "name": tool.get("name")})
    input_items = payload.get("input")
    return {
        "keys": sorted(payload),
        "stream": payload.get("stream"),
        "has_prompt_cache_key": "prompt_cache_key" in payload,
        "tool_choice": payload.get("tool_choice"),
        "tools": tools_out,
        "input_types": [
            item.get("type")
            for item in input_items
            if isinstance(item, dict)
        ]
        if isinstance(input_items, list)
        else [],
    }


def dump_conversion_shapes() -> dict[str, object]:
    _src_python()
    from gateway_compat import compatible_request_body
    from prompt_cache_policy import PromptCacheKeyPolicy
    from protocol_translation import prepare_exchange

    official = next(case for case in CASES if case.provider == "official")
    muse = next(case for case in CASES if case.provider == "opencode-go")

    def _prepared(payload: dict[str, object], *, policy: PromptCacheKeyPolicy, official_compat: bool) -> dict[str, object]:
        exchange = prepare_exchange(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
            inbound_format="chat_completions",
            outbound_format="responses",
            prompt_cache_key_policy=policy,
        )
        body = exchange.upstream_body
        if official_compat:
            body = compatible_request_body(
                body,
                {"name": "official", "upstream_format": "responses"},
                event_context={},
                inject_codex_tools=False,
            )
        converted = json.loads(body)
        assert isinstance(converted, dict)
        shape = conversion_shape(converted)
        shape["dropped_cache_controls"] = list(exchange.dropped_cache_controls)
        return shape

    return {
        "official_sentinel": _prepared(
            chat_payload(official, SENTINEL_PREFIX + official.case_id),
            policy=PromptCacheKeyPolicy.PRESERVE,
            official_compat=True,
        ),
        "muse_sentinel": _prepared(
            chat_payload(muse, SENTINEL_PREFIX + muse.case_id),
            policy=PromptCacheKeyPolicy.DROP_UNVERIFIED,
            official_compat=False,
        ),
        "official_function": _prepared(
            official_function_payload(official.model),
            policy=PromptCacheKeyPolicy.PRESERVE,
            official_compat=True,
        ),
        "official_v2": _prepared(
            official_v2_payload(official.model),
            policy=PromptCacheKeyPolicy.PRESERVE,
            official_compat=True,
        ),
        "official_web_search": _prepared(
            official_web_search_payload(official.model),
            policy=PromptCacheKeyPolicy.PRESERVE,
            official_compat=True,
        ),
    }


def chat_headers(gateway_key: str) -> dict[str, str]:
    # Do not set X-Codex-Client-Id. Any non-unknown, non-codex-app identity
    # selects the official transparent-metered profile instead of
    # official_gateway_compat, which is the Chat→official #509 path.
    _src_python()
    from route_primitives import UPSTREAM_USER_AGENT

    return {
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {gateway_key}",
        "Content-Type": "application/json",
        "User-Agent": UPSTREAM_USER_AGENT,
        "X-Request-Kind": "main_generation",
    }


def chat_url(base: str, case: Case) -> str:
    return base.rstrip("/") + case.endpoint_binding


def _contains_encrypted_content(value: object) -> bool:
    if isinstance(value, dict):
        if "encrypted_content" in value:
            return True
        return any(_contains_encrypted_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_encrypted_content(item) for item in value)
    return False


def _text_field(container: object, key: str) -> str:
    if not isinstance(container, dict):
        return ""
    value = container.get(key)
    return value if isinstance(value, str) else ""


def _iter_json_objects(body: bytes) -> list[dict[str, object]]:
    objects: list[dict[str, object]] = []
    text = body.decode("utf-8", "replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        raw_data = ""
        if line.startswith("data:"):
            raw_data = line[5:].strip()
            if not raw_data or raw_data == "[DONE]":
                continue
        elif line.startswith("{") or line.startswith("["):
            raw_data = line
        else:
            continue
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            objects.append(payload)
    if not objects and text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            objects.append(payload)
    return objects


def _chat_error_message(payload: dict[str, object]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return "Chat error object without message"
    if isinstance(error, str) and error.strip():
        return error.strip()
    return ""


def evaluate_chat_body(http_status: int, body: bytes, sentinel: str) -> dict[str, object]:
    text = body.decode("utf-8", "replace")
    contents: list[str] = []
    reasoning: list[str] = []
    objects: list[str] = []
    errors: list[str] = []
    saw_encrypted = False
    saw_responses = False
    saw_choice = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("data:"):
            raw_data = line[5:].strip()
            if not raw_data or raw_data == "[DONE]":
                continue
            try:
                payload = json.loads(raw_data)
            except json.JSONDecodeError:
                continue
        elif line.startswith("{") or line.startswith("["):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
        else:
            continue
        if not isinstance(payload, dict):
            continue
        if _contains_encrypted_content(payload):
            saw_encrypted = True
        event_type = payload.get("type")
        if isinstance(event_type, str) and event_type.startswith("response."):
            saw_responses = True
        object_name = payload.get("object")
        if isinstance(object_name, str):
            objects.append(object_name)
        error_message = _chat_error_message(payload)
        if error_message:
            errors.append(error_message)
        choices = payload.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            saw_choice = True
            delta = choice.get("delta")
            message = choice.get("message")
            contents.append(_text_field(delta, "content"))
            contents.append(_text_field(message, "content"))
            reasoning.append(_text_field(delta, "reasoning_content"))
            reasoning.append(_text_field(message, "reasoning_content"))
    if not saw_choice and text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            if _contains_encrypted_content(payload):
                saw_encrypted = True
            event_type = payload.get("type")
            if isinstance(event_type, str) and event_type.startswith("response."):
                saw_responses = True
            object_name = payload.get("object")
            if isinstance(object_name, str):
                objects.append(object_name)
            error_message = _chat_error_message(payload)
            if error_message:
                errors.append(error_message)
            choices = payload.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    saw_choice = True
                    message = choice.get("message")
                    contents.append(_text_field(message, "content"))
                    reasoning.append(_text_field(message, "reasoning_content"))
    content = "".join(contents)
    reasoning_content = "".join(reasoning)
    saw_sentinel = sentinel in content
    error = ""
    if http_status != 200:
        error = f"HTTP {http_status}"
        if errors:
            error = f"{error}: {errors[0]}"
    elif errors:
        error = errors[0]
    elif saw_encrypted:
        error = "official encrypted_content leaked onto Chat wire"
    elif saw_responses:
        error = "Responses events returned on Chat endpoint"
    elif not saw_choice:
        error = "Chat response had no choices"
    elif not saw_sentinel:
        error = "sentinel missing from Chat content"
    return {
        "ok": not error,
        "http_status": http_status,
        "object": objects[0] if objects else "",
        "content": content,
        "reasoning_content": reasoning_content,
        "saw_encrypted_content": saw_encrypted,
        "saw_responses_event": saw_responses,
        "saw_sentinel": saw_sentinel,
        "saw_reasoning_content": bool(reasoning_content),
        "error": error,
    }


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def debug_candidate_paths() -> tuple[Path, ...]:
    debug = ROOT / "src-tauri" / "target" / "debug"
    if os.name == "nt":
        return (debug / "codexhub.exe", debug / "codexhub")
    return (debug / "codexhub",)


def build_candidate() -> Path:
    env = os.environ.copy()
    env["CODEXHUB_BUILD_FLAVOR"] = "debug"
    result = _run(
        ["cargo", "build", "--locked", "--features", "debug-diagnostics"],
        cwd=ROOT / "src-tauri",
        env=env,
        timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cargo build failed: {(result.stderr or '')[-2000:]}")
    for binary in debug_candidate_paths():
        if binary.is_file():
            return binary
    raise RuntimeError(f"self-built candidate missing: {debug_candidate_paths()[0]}")


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def local_opener():
    # Loopback health/chat must not inherit HTTP_PROXY. On Windows, FlClash
    # will accept 127.0.0.1 CONNECT and never reach the isolated Gateway.
    return build_opener(ProxyHandler({}))


def _wait_for_health(port: int, timeout: int = 30) -> bool:
    opener = local_opener()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def start_candidate(binary: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[str] | None, str]:
    # Windows `codexhub start` can stay attached after the Gateway is healthy.
    # Wait on /health, not on the CLI process exiting.
    starter = subprocess.Popen(
        [str(binary), "start"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if _wait_for_health(port, timeout=60):
        return starter, ""
    tail = ""
    try:
        stdout, stderr = starter.communicate(timeout=8)
        tail = ((stdout or "") + "\n" + (stderr or ""))[-1600:]
    except subprocess.TimeoutExpired:
        starter.kill()
        stdout, stderr = starter.communicate()
        tail = ((stdout or "") + "\n" + (stderr or ""))[-1600:]
    return None, tail or "Gateway failed to become healthy"


def _prepare_runtime(
    work: Path,
    settings_source: Path,
    providers_source: Path,
    auth_source: Path,
    catalog_source: Path | None,
) -> tuple[dict[str, str], Path, Path, int, str]:
    runtime = work / "runtime"
    codex_home = work / "codex-home"
    proxy = runtime / "proxy"
    config = proxy / "config"
    config.mkdir(parents=True)
    codex_home.mkdir()
    port = _free_port()
    settings = json.loads(settings_source.read_text(encoding="utf-8"))
    gateway_key = secrets.token_hex(32)
    settings.update(
        {
            "auto_start_gateway": False,
            "gateway_bind_address": "127.0.0.1",
            "gateway_client_key": gateway_key,
            "gateway_enable_models": True,
            "gateway_enable_responses": True,
            "gateway_enable_chat_completions": True,
            "include_official_models": True,
            "proxy_port": port,
        }
    )
    settings_path = proxy / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(providers_source, config / "providers.toml")
    shutil.copy2(auth_source, codex_home / "auth.json")
    home = work / "home"
    appdata = home / "appdata"
    temp = home / "temp"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(appdata / "roaming"),
            "LOCALAPPDATA": str(appdata / "local"),
            "TEMP": str(temp),
            "TMP": str(temp),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "CODEX_HOME": str(codex_home),
            "CODEXHUB_CODEX_TARGET_HOME": str(codex_home),
            "CODEXHUB_RUNTIME_HOME": str(runtime),
            "CODEXHUB_ROLLBACK_PROVENANCE_DIR": str(work / "rollback"),
            "CODEXHUB_PYTHON": sys.executable,
            "CODEXHUB_PROXY_PYTHON": sys.executable,
            "CODEXHUB_E2E_PYTHON": sys.executable,
            "CODEXHUB_RESOURCE_ROOT": str(ROOT),
        }
    )
    home.mkdir(parents=True)
    (appdata / "roaming").mkdir(parents=True)
    (appdata / "local").mkdir(parents=True)
    temp.mkdir(parents=True)
    catalog = runtime / "model-catalogs" / "codexhub-model-catalog.json"
    if catalog_source and catalog_source.is_file():
        catalog.parent.mkdir(parents=True)
        shutil.copy2(catalog_source, catalog)
    return env, settings_path, catalog, port, gateway_key


def _post_chat(url: str, payload: dict[str, object], headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with local_opener().open(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), response.read()
    except HTTPError as error:
        return int(error.code), error.read()
    except URLError as error:
        raise RuntimeError(f"chat request failed: {error}") from error


def evaluate_capability_body(
    kind: str,
    http_status: int,
    body: bytes,
    *,
    protocol: str = "chat_completions",
) -> dict[str, object]:
    _src_python()
    from collaboration_runtime_contract import V2_TOOLS

    responses_kind = kind.startswith("muse-responses-")
    if responses_kind or protocol == "responses":
        errors = [_chat_error_message(payload) for payload in _iter_json_objects(body)]
        errors = [item for item in errors if item]
        tool_names = collect_responses_tool_names(body)
        parsed: dict[str, object] = {
            "http_status": http_status,
            "content": "",
            "error": errors[0] if errors else "",
            "saw_encrypted_content": any(
                _contains_encrypted_content(payload) for payload in _iter_json_objects(body)
            ),
            "saw_responses_event": True,
        }
        base_error = ""
        if http_status != 200:
            base_error = f"HTTP {http_status}"
            if errors:
                base_error = f"{base_error}: {errors[0]}"
        elif errors:
            base_error = errors[0]
    else:
        parsed = evaluate_chat_body(http_status, body, sentinel="")
        tool_names = collect_tool_call_names(body)
        base_error = str(parsed.get("error") or "")
    parsed["tool_names"] = tool_names
    parsed["saw_tool_call"] = bool(tool_names)
    body_text = body.decode("utf-8", "replace")
    error = ""
    if "encrypted_native_tool_unavailable" in body_text or "encrypted_native_tool_unavailable" in base_error:
        error = "official encrypted native tool unavailable on Chat"
    elif kind in {"muse-web-search", "muse-responses-web-search"}:
        parsed["hosted_search"] = "web_search" in tool_names
        content = str(parsed.get("content") or "")
        if http_status != 200:
            error = base_error or f"HTTP {http_status}"
        elif "web_search" not in tool_names and not content.strip():
            error = "web_search produced neither a tool_call nor content"
        elif "web_search" not in tool_names:
            error = "hosted web_search tool_call missing"
    elif http_status != 200 or (base_error and kind not in {"muse-image-gen", "muse-responses-image-gen"}):
        error = base_error or f"HTTP {http_status}"
    elif kind == "official-function":
        if "get_time" not in tool_names:
            error = "forced function tool_call missing"
        elif any(name.startswith("__codexhub_ns_") for name in tool_names):
            error = "function tool leaked a namespace alias"
    elif kind == "official-v2":
        if any(name.startswith("__codexhub_ns_") or name.startswith("collaboration.") for name in tool_names):
            error = "V2 tool leaked a namespace alias"
        elif tool_names and any(name not in V2_TOOLS for name in tool_names):
            error = f"unexpected V2 tool names: {tool_names}"
    elif kind == "official-web-search":
        content = str(parsed.get("content") or "")
        if "web_search" not in tool_names and not content.strip():
            error = "web_search produced neither a tool_call nor content"
    elif kind in {"muse-image-gen", "muse-responses-image-gen"}:
        if http_status != 200:
            error = base_error or f"HTTP {http_status}"
        elif not any(is_image_gen_tool_name(name) for name in tool_names):
            error = f"image_gen tool_call missing: {tool_names or base_error}"
    else:
        error = f"unknown capability {kind}"
    parsed["ok"] = not error
    parsed["error"] = error
    parsed["saw_v2_tool"] = any(name in V2_TOOLS for name in tool_names)
    return parsed


def _live_capability(
    base: str,
    kind: str,
    payload: dict[str, object],
    gateway_key: str,
    timeout: int,
    *,
    endpoint: str = "/v1/chat/completions",
    provider_id: str = "official",
    protocol: str = "chat_completions",
) -> dict[str, object]:
    url = base.rstrip("/") + endpoint
    http_status, body = _post_chat(url, payload, chat_headers(gateway_key), timeout)
    result = evaluate_capability_body(kind, http_status, body, protocol=protocol)
    result.update(
        {
            "case_id": kind,
            "provider_id": provider_id,
            "model": payload.get("model"),
            "endpoint_binding": endpoint,
            "protocol": protocol,
            "outcome": "passed" if result["ok"] else "failed",
        }
    )
    if not result["ok"]:
        result["body_tail"] = body.decode("utf-8", "replace")[-800:]
    return result


def _live_case(base: str, case: Case, gateway_key: str, timeout: int) -> dict[str, object]:
    sentinel = SENTINEL_PREFIX + case.case_id
    url = chat_url(base, case)
    http_status, body = _post_chat(url, chat_payload(case, sentinel), chat_headers(gateway_key), timeout)
    result = evaluate_chat_body(http_status, body, sentinel)
    result.update(
        {
            "case_id": case.case_id,
            "provider_id": case.provider,
            "model": case.model,
            "endpoint_binding": case.endpoint_binding,
            "protocol": "chat_completions",
            "outcome": "passed" if result["ok"] else "failed",
        }
    )
    if not result["ok"]:
        result["body_tail"] = body.decode("utf-8", "replace")[-800:]
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", type=Path, help="skip self-build and use this candidate")
    parser.add_argument("--output", type=Path, default=Path("test-results/chat-completions-e2e.json"))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument(
        "--capabilities",
        action="store_true",
        help="also probe official Chat function/V2/web_search and Muse search/image_gen",
    )
    parser.add_argument(
        "--dump-conversion",
        action="store_true",
        help="print Chat→Responses conversion shapes and exit",
    )
    parser.add_argument(
        "--keep-runtime",
        type=Path,
        help="use this empty directory instead of a throwaway runtime",
    )
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
        help="reuse a current Official catalog without restarting a running Codex Desktop",
    )
    parser.add_argument(
        "--opencode-go-credentials",
        type=Path,
        help="dedicated OpenCode Go credential JSON for {env:OPENCODE_API_KEY} providers.toml",
    )
    args = parser.parse_args(argv)
    if args.dump_conversion:
        shapes = dump_conversion_shapes()
        print(json.dumps(shapes, indent=2))
        return 0
    if args.capabilities and not args.cases:
        args.cases = ["chat-official"]
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
        opencode_go_api_key = load_opencode_go_api_key(args.opencode_go_credentials)
    except ValueError as error:
        parser.error(str(error))
    if any(case.provider == "opencode-go" for case in cases) and not opencode_go_api_key:
        parser.error("muse Chat case requires --opencode-go-credentials")
    binary = args.bin.resolve() if args.bin else build_candidate()
    report: dict[str, object] = {
        "schema": "codexhub.chat-completions-e2e.v1",
        "candidate": str(binary),
        "cases": [],
    }
    failures: list[str] = []
    keep_runtime = args.keep_runtime.resolve() if args.keep_runtime else None
    if keep_runtime is not None:
        keep_runtime.mkdir(parents=True, exist_ok=True)
        if any(keep_runtime.iterdir()):
            parser.error(f"keep-runtime must be empty: {keep_runtime}")
        work_manager = None
        work = keep_runtime
    else:
        work_manager = tempfile.TemporaryDirectory(prefix="codexhub-chat-e2e-")
        work = Path(work_manager.name)
    try:
        env, _settings, catalog, port, gateway_key = _prepare_runtime(
            work, args.settings, args.providers, args.auth, args.catalog
        )
        if opencode_go_api_key:
            env["OPENCODE_API_KEY"] = opencode_go_api_key
        refresh = None if catalog.is_file() else _run([str(binary), "refresh-models"], env=env, timeout=180)
        if not catalog.is_file():
            failures.append("candidate: refresh-models failed")
            report["bootstrap_tail"] = (
                ((refresh.stdout or "") + "\n" + (refresh.stderr or ""))[-1600:]
                if refresh
                else "Official catalog is missing"
            )
        else:
            starter, bootstrap_tail = start_candidate(binary, env, port)
            if starter is None:
                failures.append("candidate: Gateway failed to become healthy")
                report["bootstrap_tail"] = bootstrap_tail
            else:
                try:
                    base = f"http://127.0.0.1:{port}"
                    for case in cases:
                        result = _live_case(base, case, gateway_key, args.timeout)
                        if not result["ok"]:
                            failures.append(f"{case.case_id}: {result.get('error') or 'live chat failed'}")
                        report["cases"].append(result)
                    if args.capabilities:
                        official = next(case for case in CASES if case.provider == "official")
                        probes = (
                            ("official-function", official_function_payload(official.model), {}),
                            ("official-v2", official_v2_payload(official.model), {}),
                            ("official-web-search", official_web_search_payload(official.model), {}),
                        )
                        muse = next((case for case in cases if case.provider == "opencode-go"), None)
                        if muse is not None:
                            probes = probes + (
                                (
                                    "muse-web-search",
                                    muse_web_search_payload(muse.model),
                                    {
                                        "endpoint": muse.endpoint_binding,
                                        "provider_id": muse.provider,
                                    },
                                ),
                                (
                                    "muse-image-gen",
                                    muse_image_gen_payload(muse.model),
                                    {
                                        "endpoint": muse.endpoint_binding,
                                        "provider_id": muse.provider,
                                    },
                                ),
                                (
                                    "muse-responses-web-search",
                                    muse_responses_web_search_payload(muse.model),
                                    {
                                        "endpoint": "/v1/providers/opencode-go/responses",
                                        "provider_id": muse.provider,
                                        "protocol": "responses",
                                    },
                                ),
                                (
                                    "muse-responses-image-gen",
                                    muse_responses_image_gen_payload(muse.model),
                                    {
                                        "endpoint": "/v1/providers/opencode-go/responses",
                                        "provider_id": muse.provider,
                                        "protocol": "responses",
                                    },
                                ),
                            )
                        for kind, payload, extra in probes:
                            result = _live_capability(
                                base,
                                kind,
                                payload,
                                gateway_key,
                                args.timeout,
                                **extra,
                            )
                            if not result["ok"]:
                                failures.append(f"{kind}: {result.get('error') or 'capability probe failed'}")
                            report["cases"].append(result)
                finally:
                    _run([str(binary), "stop"], env=env, timeout=60)
                    if starter.poll() is None:
                        starter.terminate()
    finally:
        if work_manager is not None:
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
                "cases": [
                    {
                        "case_id": item["case_id"],
                        "http_status": item.get("http_status"),
                        "saw_sentinel": item.get("saw_sentinel"),
                        "tool_names": item.get("tool_names"),
                        "saw_v2_tool": item.get("saw_v2_tool"),
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
