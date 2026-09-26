"""Build and run the eight-case Linux real-client CLI E2E matrix."""

# ruff: noqa: E402

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple
from urllib.request import urlopen

try:
    from scripts.e2e_gate_inputs import deepseek_provider_text, qualify_candidate_binary
except ModuleNotFoundError:
    from e2e_gate_inputs import deepseek_provider_text, qualify_candidate_binary

ROOT = Path(__file__).resolve().parents[1]
CLI_CONTRACT_PATH = ROOT / "scripts" / "real_client_cli_contract.v1.json"
DEEPSEEK_CREDENTIAL_SCHEMA = "codexhub.real-client-deepseek.v1"
SENTINEL_PREFIX = "SENTINEL:codexhub-linux-cli-e2e:"
PROMPT_TEMPLATE = (
    "Use exactly one read-only tool call to read ./sentinel.txt. "
    "Then reply with only this exact line and no other text: {sentinel}"
)


def load_deepseek_api_key(path: Path | None) -> str:
    if path is None:
        return ""
    if not path.is_file():
        raise ValueError(f"missing DeepSeek credentials: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid DeepSeek credentials: {error}") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "api_key"}
        or payload.get("schema") != DEEPSEEK_CREDENTIAL_SCHEMA
    ):
        raise ValueError("invalid DeepSeek credential schema")
    key = payload.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("DeepSeek credential missing api_key")
    return key.strip()


class Case(NamedTuple):
    case_id: str
    client: str
    provider: str
    provider_id: str
    diagnostic_provider_id: str
    managed_model: str
    selector: str
    canonical_model: str
    gateway_model: str
    endpoint_binding: str
    protocol: str


def load_cli_contract() -> dict[str, object]:
    try:
        contract = json.loads(CLI_CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid CLI E2E contract: {CLI_CONTRACT_PATH}: {error}") from error
    if not isinstance(contract, dict) or contract.get("schema") != "codexhub.real-client-cli-contract.v1":
        raise RuntimeError("invalid CLI E2E contract schema")
    return contract


CLI_CONTRACT = load_cli_contract()


def _contract_cases() -> tuple[Case, ...]:
    platforms = CLI_CONTRACT["platforms"]
    assert isinstance(platforms, dict)
    linux = platforms["linux"]
    assert isinstance(linux, dict)
    client_names = linux["client_names"]
    provider_names = linux["provider_names"]
    assert isinstance(client_names, dict) and isinstance(provider_names, dict)
    raw_cases = CLI_CONTRACT["cases"]
    assert isinstance(raw_cases, list)
    cases: list[Case] = []
    for raw in raw_cases:
        assert isinstance(raw, dict)
        client_kind = str(raw["client"])
        provider_id = str(raw["provider_id"])
        models = raw["models"]
        ids = raw["case_ids"]
        assert isinstance(models, dict) and isinstance(ids, dict)
        overrides = raw.get("platform_overrides", {}).get("linux", {})
        assert isinstance(overrides, dict)
        gateway_model = str(overrides.get("gateway", models["gateway"]))
        cases.append(
            Case(
                str(ids["linux"]),
                str(client_names[client_kind]),
                str(provider_names[provider_id]),
                provider_id,
                str(raw["diagnostic_provider_id"]),
                str(models["managed"]),
                str(models["selector"]),
                str(models["canonical"]),
                gateway_model,
                str(raw["endpoint_binding"]),
                str(raw["protocol"]),
            )
        )
    return tuple(cases)


CASES = _contract_cases()


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 180,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {
        "check": False,
        "cwd": cwd,
        "env": env,
        "input": input_text,
        "text": True,
        "timeout": timeout,
    }
    # Windows: start keeps the Gateway child on inherited stdout, so
    # capture_output waits forever for that pipe even after timeout.
    if os.name == "nt" and len(command) >= 2 and command[1] == "start":
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    else:
        kwargs["capture_output"] = True
    return subprocess.run(command, **kwargs)


def build_candidate() -> Path:
    env = os.environ.copy()
    env["CODEXHUB_BUILD_FLAVOR"] = "debug"
    env["CARGO_TARGET_DIR"] = str(ROOT / "src-tauri" / "target")
    result = _run(
        ["cargo", "build", "--locked", "--features", "debug-diagnostics"],
        cwd=ROOT / "src-tauri",
        env=env,
        timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cargo build failed: {(result.stderr or '')[-2000:]}")
    binary = ROOT / "src-tauri" / "target" / "debug" / "codexhub"
    if not binary.is_file():
        raise RuntimeError(f"self-built candidate missing: {binary}")
    return binary


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_health(port: int, timeout: int = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def _prepare_runtime(
    work: Path,
    settings_source: Path,
    providers_source: Path,
    auth_source: Path,
    catalog_source: Path | None,
    deepseek_api_key: str,
) -> tuple[dict[str, str], Path, Path, Path, int]:
    runtime = work / "runtime"
    codex_home = work / "codex-home"
    proxy = runtime / "proxy"
    config = proxy / "config"
    config.mkdir(parents=True)
    codex_home.mkdir()
    port = _free_port()
    settings = json.loads(settings_source.read_text(encoding="utf-8"))
    settings.update(
        {
            "auto_start_gateway": False,
            "gateway_bind_address": "127.0.0.1",
            "gateway_client_key": secrets.token_hex(32),
            "gateway_enable_models": True,
            "gateway_enable_responses": True,
            "gateway_enable_chat_completions": True,
            "include_official_models": True,
            "proxy_port": port,
        }
    )
    settings_path = proxy / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    providers_path = config / "providers.toml"
    providers_path.write_text(deepseek_provider_text(providers_source), encoding="utf-8")
    shutil.copy2(auth_source, codex_home / "auth.json")
    env = _isolated_environment()
    env.pop("DEEPSEEK_API_KEY", None)
    env.update(
        {
            "HOME": str(work / "home"),
            "XDG_CONFIG_HOME": str(work / "home" / ".config"),
            "XDG_DATA_HOME": str(work / "home" / ".local" / "share"),
            "CODEX_HOME": str(codex_home),
            "CODEXHUB_CODEX_TARGET_HOME": str(codex_home),
            "CODEXHUB_RUNTIME_HOME": str(runtime),
            "CODEXHUB_ROLLBACK_PROVENANCE_DIR": str(work / "rollback"),
            "CODEXHUB_PYTHON": sys.executable,
            "CODEXHUB_PROXY_PYTHON": sys.executable,
            "CODEXHUB_RESOURCE_ROOT": str(ROOT),
        }
    )
    env["DEEPSEEK_API_KEY"] = deepseek_api_key
    Path(env["HOME"]).mkdir(parents=True)
    catalog = runtime / "model-catalogs" / "codexhub-model-catalog.json"
    if catalog_source and catalog_source.is_file():
        catalog.parent.mkdir(parents=True)
        shutil.copy2(catalog_source, catalog)
    return env, settings_path, providers_path, catalog, port


def _isolated_environment() -> dict[str, str]:
    """Keep runtime necessities but discard inherited client and API credentials."""
    secret_suffixes = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
    host_config_prefixes = (
        "CODEX",
        "PI_",
        "OPENCODE_",
        "OMP_",
        "CLAUDE_",
        "ANTHROPIC_",
        "OPENAI_",
        "DEEPSEEK_",
        "GEMINI_",
        "GOOGLE_",
        "XAI_",
        "GROK_",
        "OPENROUTER_",
    )
    isolated = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(host_config_prefixes)
        and not key.upper().endswith(secret_suffixes)
    }
    return isolated


def _managed(
    binary: Path,
    verb: str,
    case: Case,
    root: Path,
    settings: Path,
    providers: Path,
    catalog: Path,
    env: dict[str, str],
) -> dict[str, object]:
    command = [
        str(binary),
        "managed-client-config",
        verb,
        "--client",
        case.client,
        "--root",
        str(root),
        "--model",
        case.managed_model,
        "--settings-path",
        str(settings),
        "--providers-path",
        str(providers),
        "--python-path",
        sys.executable,
    ]
    if case.provider == "openai":
        command += ["--catalog-path", str(catalog)]
    managed_env = env.copy()
    managed_env.pop("DEEPSEEK_API_KEY", None)
    result = _run(command, env=managed_env)
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    expected_port = json.loads(settings.read_text(encoding="utf-8")).get("proxy_port")
    return {
        "ok": result.returncode == 0
        and managed_payload_matches(payload, case, verb, expected_port),
        "returncode": result.returncode,
    }


def managed_payload_matches(
    payload: object,
    case: Case,
    verb: str,
    expected_proxy_port: object = None,
) -> bool:
    if not isinstance(payload, dict):
        return False
    if case.client == "codex" and verb == "apply":
        return payload.get("mode") == "custom" and payload.get("proxy_port") == expected_proxy_port
    expected_selector = (
        f"custom/{case.managed_model}" if case.client == "codex" else case.selector
    )
    if payload.get("client_id") != case.client:
        return False
    if payload.get("selector") != expected_selector or payload.get("model") != case.managed_model:
        return False
    if payload.get("route_protocol") != case.protocol:
        return False
    return payload.get("applied") is True if verb == "apply" else (
        verb == "preview" or payload.get("ok") is True
    )


def _cli(name: str) -> str:
    if os.name != "nt":
        return name
    found = shutil.which(f"{name}.cmd") or shutil.which(f"{name}.exe") or shutil.which(name)
    if not found:
        raise FileNotFoundError(name)
    return found


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    matches = re.findall(r"(?<![A-Za-z0-9.])v?(\d+)\.(\d+)\.(\d+)(?![A-Za-z0-9.+-])", value)
    if len(matches) != 1:
        return None
    return tuple(int(part) for part in matches[0])


def client_versions(env: dict[str, str] | None = None) -> tuple[dict[str, str], list[str]]:
    linux = CLI_CONTRACT["platforms"]["linux"]
    client_names = linux["client_names"]
    kind_to_binary = {
        "codex_cli": str(client_names["codex_cli"]),
        "opencode": str(client_names["opencode"]),
        "pi": str(client_names["pi"]),
        "omp": str(client_names["omp"]),
    }
    versions: dict[str, str] = {}
    failures: list[str] = []
    for kind, binary in kind_to_binary.items():
        command = _cli(binary)
        result = _run([command, "--version"], env=env, timeout=10)
        detected = _version_tuple((result.stdout or "") + "\n" + (result.stderr or ""))
        minimum = _version_tuple(str(CLI_CONTRACT["minimum_versions"][kind]))
        if detected is None or minimum is None:
            failures.append(f"{kind}: version_unrecognized")
        else:
            versions[kind] = ".".join(str(part) for part in detected)
            if result.returncode != 0 or detected < minimum:
                failures.append(f"{kind}: below_minimum_or_version_failed")
    return versions, failures


def _capture_bounded(stream: object, limit: int) -> tuple[str, int, str, bool]:
    handle = stream
    handle.flush()
    size = handle.seek(0, os.SEEK_END)
    handle.seek(0)
    digest = hashlib.sha256()
    kept = bytearray()
    while chunk := handle.read(64 * 1024):
        digest.update(chunk)
        if len(kept) < limit:
            kept.extend(chunk[: limit - len(kept)])
    return bytes(kept).decode("utf-8", "replace"), size, digest.hexdigest(), size > limit


def _run_client_bounded(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: int,
    input_text: str | None,
    output_limit: int = 1024 * 1024,
) -> dict[str, object]:
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        if input_text is not None and process.stdin is not None:
            try:
                process.stdin.write(input_text.encode("utf-8"))
            except BrokenPipeError:
                pass
            finally:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
        deadline = time.monotonic() + timeout
        timed_out = False
        output_truncated = False
        while process.poll() is None:
            if (
                os.fstat(stdout_file.fileno()).st_size > output_limit
                or os.fstat(stderr_file.fileno()).st_size > output_limit
            ):
                output_truncated = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)
        if output_truncated or timed_out:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
        try:
            returncode: int | None = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            returncode = process.wait()
        finally:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        stdout, stdout_size, stdout_sha256, stdout_truncated = _capture_bounded(
            stdout_file, output_limit
        )
        stderr, stderr_size, stderr_sha256, stderr_truncated = _capture_bounded(
            stderr_file, output_limit
        )
    return {
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_size": stdout_size,
        "stderr_size": stderr_size,
        "stdout_sha256": stdout_sha256,
        "stderr_sha256": stderr_sha256,
        "output_truncated": output_truncated or stdout_truncated or stderr_truncated,
    }


def _copy_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)


def _read_only_sentinel_command(command: object, case_root: Path | None) -> bool:
    if not isinstance(command, str):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if (
        len(parts) == 3
        and Path(parts[0]).name == "bash"
        and parts[1] in {"-c", "-lc"}
    ):
        try:
            parts = shlex.split(parts[2])
        except ValueError:
            return False
    if len(parts) == 3 and parts[1] == "--":
        executable, target = parts[0], parts[2]
    elif len(parts) == 2:
        executable, target = parts
    else:
        return False
    if Path(executable).name != "cat":
        return False
    path = Path(target)
    if case_root is None:
        return not path.is_absolute() and path.name == "sentinel.txt" and ".." not in path.parts
    resolved_root = case_root.resolve()
    resolved_target = path.resolve() if path.is_absolute() else (resolved_root / path).resolve()
    return resolved_target == (resolved_root / "sentinel.txt").resolve()


def parse_client_output(
    client: str,
    output: str,
    sentinel: str,
    case_root: Path | None = None,
) -> dict[str, object]:
    """Normalize versioned client JSONL into the evidence needed by the gate."""
    assistant_texts: list[str] = []
    tool_calls: list[bool] = []
    terminals: list[str] = []
    errors = 0
    malformed = 0
    open_code_text: list[str] = []
    assistant_messages: list[tuple[int, str, str]] = []
    agent_ends: list[int] = []
    last_assistant_line = -1
    tool_inputs: dict[str, object] = {}

    def reads_sentinel(arguments: object) -> bool:
        if not isinstance(arguments, dict):
            return False
        target = arguments.get("filePath", arguments.get("path"))
        if not isinstance(target, str):
            return False
        path = Path(target)
        if case_root is None:
            return path in {Path("sentinel.txt"), Path("./sentinel.txt")}
        return (path if path.is_absolute() else case_root / path).resolve() == (case_root / "sentinel.txt").resolve()

    def flush_open_code_text() -> None:
        if open_code_text:
            assistant_texts.append("".join(open_code_text))
            open_code_text.clear()

    for line_number, line in enumerate(output.splitlines()):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        kind = event.get("type")
        if client == "codex":
            if kind == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict):
                    malformed += 1
                    continue
                item_type = item.get("type")
                if item_type == "command_execution":
                    tool_calls.append(
                        item.get("status") == "completed"
                        and type(item.get("exit_code")) is int
                        and item.get("exit_code") == 0
                        and _read_only_sentinel_command(item.get("command"), case_root)
                    )
                elif item_type == "agent_message":
                    assistant_texts.append(str(item.get("text", "")))
                elif isinstance(item_type, str) and item_type.endswith(("_call", "_tool")):
                    tool_calls.append(False)
            elif kind == "turn.completed":
                terminals.append("completed")
            elif kind in {"error", "turn.failed"}:
                errors += 1
        elif client == "opencode":
            part = event.get("part")
            if not isinstance(part, dict):
                part = {}
            state = part.get("state")
            if not isinstance(state, dict):
                state = {}
            if kind in {"tool_use", "tool"} or part.get("type") == "tool":
                flush_open_code_text()
                name = part.get("tool", event.get("name"))
                input_value = state.get("input", part.get("input", event.get("input", {})))
                has_target = reads_sentinel(input_value)
                tool_calls.append(
                    name in {"read", "read_file"}
                    and has_target
                    and state.get("status") == "completed"
                )
            elif kind == "text":
                text_part = part.get("text")
                if isinstance(text_part, str):
                    open_code_text.append(text_part)
            elif kind == "step_finish":
                reason = part.get("reason")
                if reason == "stop":
                    flush_open_code_text()
                    terminals.append("completed")
                elif reason in {"error", "aborted", "length"}:
                    flush_open_code_text()
                    terminals.append(str(reason))
            elif kind == "error":
                errors += 1
        elif client in {"pi", "omp"}:
            if kind == "tool_execution_start" and isinstance(event.get("toolCallId"), str):
                tool_inputs[event["toolCallId"]] = event.get("args", event.get("input"))
            if kind == "tool_execution_end":
                name = event.get("toolName")
                args = event.get("input", event.get("args", tool_inputs.get(event.get("toolCallId"))))
                has_target = reads_sentinel(args)
                tool_calls.append(name == "read" and not event.get("isError", False) and has_target)
            elif kind == "message_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    content = message.get("content", [])
                    text_value = "".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    ) if isinstance(content, list) else ""
                    reason = str(message.get("stopReason", event.get("stopReason", "")))
                    error_message = str(message.get("errorMessage", event.get("errorMessage", "")))
                    assistant_messages.append((line_number, reason, error_message))
                    last_assistant_line = line_number
                    if text_value:
                        assistant_texts.append(text_value)
            elif kind == "agent_end":
                agent_ends.append(line_number)
            elif kind in {"error", "turn.failed"}:
                errors += 1

    flush_open_code_text()
    if client in {"pi", "omp"}:
        final_message = assistant_messages[-1] if assistant_messages else None
        completed = (
            len(agent_ends) == 1
            and final_message is not None
            and agent_ends[0] > last_assistant_line
            and final_message[1] == "stop"
            and not final_message[2]
        )
        if agent_ends:
            terminals.extend(
                ["completed" if completed else (final_message[1] if final_message else "unclassified")]
                * len(agent_ends)
            )
        if not completed:
            errors += 1

    sentinel_count = sum(text.strip() == sentinel for text in assistant_texts)
    terminal = terminals[-1] if len(terminals) == 1 else "unclassified"
    if terminal not in {"completed", "error", "aborted", "length"}:
        terminal = "unclassified"
    return {
        "tool_call_count": len(tool_calls),
        "read_only_tool_call_count": sum(tool_calls),
        "assistant_output_count": len(assistant_texts),
        "sentinel_chunk_count": sentinel_count,
        "terminal_count": len(terminals),
        "terminal_classification": terminal,
        "error_event_count": errors,
        "malformed_count": malformed,
    }


def assistant_returned_sentinel(client: str, output: str, sentinel: str) -> bool:
    """Require assistant output; an echoed user prompt is not a successful turn."""
    return parse_client_output(client, output, sentinel)["sentinel_chunk_count"] > 0


def _client_launch(
    case: Case,
    managed_root: Path,
    case_root: Path,
    base_env: dict[str, str],
    timeout: int,
) -> dict[str, object]:
    sentinel = SENTINEL_PREFIX + case.case_id
    (case_root / "sentinel.txt").write_text(sentinel + "\n", encoding="utf-8")
    prompt = PROMPT_TEMPLATE.format(sentinel=sentinel)
    env = base_env.copy()
    home = case_root / "home"
    home.mkdir()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    if os.name == "nt":
        roaming = home / "AppData" / "Roaming"
        local = home / "AppData" / "Local"
        roaming.mkdir(parents=True)
        local.mkdir(parents=True)
        env["USERPROFILE"] = str(home)
        env["APPDATA"] = str(roaming)
        env["LOCALAPPDATA"] = str(local)
    if case.client == "codex":
        env["CODEX_HOME"] = str(managed_root / "codex-target")
        command = [
            "codex",
            "exec",
            "--ephemeral",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(case_root),
            "-m",
            case.selector,
            "-s",
            "read-only",
            "-c",
            "features.apps=false",
            "-",
        ]
        input_text = prompt
    elif case.client == "opencode":
        _copy_tree(managed_root / "opencode", Path(env["XDG_CONFIG_HOME"]) / "opencode")
        command = [
            "opencode",
            "run",
            "--format",
            "json",
            "--model",
            case.selector,
            "--dir",
            str(case_root),
            "--title",
            "codexhub-linux-cli-e2e",
            "--pure",
            "--auto",
            prompt,
        ]
        input_text = None
    elif case.client == "pi":
        agent = home / ".pi" / "agent"
        _copy_tree(managed_root / "pi", agent)
        env["PI_CODING_AGENT_DIR"] = str(agent)
        command = [
            "pi",
            "--print",
            "--mode",
            "json",
            "--model",
            case.selector,
            "--no-session",
            "--tools",
            "read",
            "--no-context-files",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            prompt,
        ]
        input_text = None
    else:
        _copy_tree(managed_root / "omp", home / ".omp" / "agent")
        command = [
            "omp",
            "--print",
            "--mode",
            "json",
            "--model",
            case.selector,
            "--no-session",
            "--no-title",
            "--tools",
            "read",
            "--no-extensions",
            "--no-skills",
            "--no-rules",
            "--cwd",
            str(case_root),
            prompt,
        ]
        input_text = None
    try:
        command[0] = _cli(command[0])
        client_env = env.copy()
        client_env.pop("DEEPSEEK_API_KEY", None)
        result = _run_client_bounded(
            command, env=client_env, cwd=case_root, timeout=timeout, input_text=input_text
        )
        parsed = parse_client_output(case.client, str(result["stdout"]), sentinel, case_root)
        timed_out = bool(result["timed_out"])
        truncated = bool(result["output_truncated"])
        return {
            "ok": (
                not timed_out
                and not truncated
                and result["returncode"] == 0
                and parsed["tool_call_count"] == 1
                and parsed["read_only_tool_call_count"] == 1
                and parsed["sentinel_chunk_count"] == 1
                and parsed["terminal_count"] == 1
                and parsed["terminal_classification"] == "completed"
                and parsed["error_event_count"] == 0
                and parsed["malformed_count"] == 0
            ),
            "returncode": result["returncode"],
            "timed_out": timed_out,
            "output_truncated": truncated,
            "stdout_sha256": result["stdout_sha256"],
            "stderr_sha256": result["stderr_sha256"],
            "stdout_size": result["stdout_size"],
            "stderr_size": result["stderr_size"],
            **parsed,
        }
    except (FileNotFoundError, OSError):
        return {
            "ok": False,
            "returncode": 127,
            "timed_out": False,
            "output_truncated": False,
            "launch_error": "client_missing",
        }


def _gateway_event_offset(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def _gateway_delta(path: Path, offset: int) -> tuple[list[dict[str, object]], int]:
    if not path.is_file():
        return [], 0
    if path.stat().st_size < offset:
        return [], 1
    with path.open("rb") as source:
        source.seek(offset)
        content = source.read()
    if not content:
        return [], 0
    lines = content.splitlines()
    if not content.endswith(b"\n") and lines:
        lines.pop()
    events: list[dict[str, object]] = []
    malformed = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed += 1
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            malformed += 1
    return events, malformed


def _gateway_events_for_attempt(
    path: Path,
    offset: int,
    *,
    expected_requests: int,
    timeout: float,
) -> tuple[list[dict[str, object]], int]:
    deadline = time.monotonic() + timeout
    while True:
        events, malformed = _gateway_delta(path, offset)
        starts = sum(event.get("event") == "request_start" for event in events)
        completes = sum(event.get("event") == "request_complete" for event in events)
        errors = sum(event.get("event") == "request_error" for event in events)
        if (starts >= expected_requests and completes >= expected_requests) or errors or time.monotonic() >= deadline:
            return events, malformed
        time.sleep(0.1)


def _model_matches(actual: object, case: Case) -> bool:
    if actual == case.gateway_model:
        return True
    return (
        case.provider_id == "official"
        and case.gateway_model == "gpt-6-luna"
        and actual == "openai/gpt-6-luna"
    )


def gateway_evidence(
    case: Case,
    events: list[dict[str, object]],
    malformed: int,
    expected_requests: int,
) -> dict[str, object]:
    relevant_names = {
        "request_start",
        "request_complete",
        "request_error",
        "upstream_protocol_fallback",
        "upstream_retry",
    }
    relevant = [event for event in events if event.get("event") in relevant_names]
    starts = [event for event in relevant if event.get("event") == "request_start"]
    completes = [event for event in relevant if event.get("event") == "request_complete"]
    errors = [event for event in relevant if event.get("event") == "request_error"]
    fallback_count = sum(event.get("event") == "upstream_protocol_fallback" for event in relevant)
    retry_count = sum(event.get("event") == "upstream_retry" for event in relevant)
    request_ids = {
        event.get("request_id") for event in starts if isinstance(event.get("request_id"), str)
    }
    malformed += sum(
        not isinstance(event.get("request_id"), str)
        or event.get("request_id") not in request_ids
        for event in relevant
    )
    starts_by_id: dict[str, int] = {}
    completes_by_id: dict[str, int] = {}
    for event in starts:
        request_id = event.get("request_id")
        if isinstance(request_id, str):
            starts_by_id[request_id] = starts_by_id.get(request_id, 0) + 1
    for event in completes:
        request_id = event.get("request_id")
        if isinstance(request_id, str):
            completes_by_id[request_id] = completes_by_id.get(request_id, 0) + 1

    metadata_errors = 0
    expected_route_mode = "official" if case.provider_id == "official" else "codexhub"
    for event in relevant:
        kind = event.get("event")
        if kind not in {"request_start", "request_complete", "request_error", "upstream_protocol_fallback"}:
            continue
        if not _model_matches(event.get("model_canonical", event.get("model")), case):
            metadata_errors += 1
        provider_id = event.get("provider_id")
        if provider_id is not None and provider_id != case.diagnostic_provider_id:
            metadata_errors += 1
        if kind in {"request_start", "request_complete"}:
            if event.get("provider_id") != case.diagnostic_provider_id:
                metadata_errors += 1
            if event.get("upstream_format") != case.protocol:
                metadata_errors += 1
            if event.get("inbound_format") != case.protocol:
                metadata_errors += 1
            if event.get("route_mode") != expected_route_mode:
                metadata_errors += 1
            if event.get("is_stream") is not True:
                metadata_errors += 1
        expected_path = re.sub(r"^/v1/providers/[^/]+/", "/v1/providers/{provider}/", case.endpoint_binding)
        if kind == "request_start" and event.get("path") != expected_path:
            metadata_errors += 1

    duplicate_terminal_count = sum(count - 1 for count in completes_by_id.values() if count > 1)
    request_ids_with_terminal = {
        event.get("request_id") for event in completes + errors if isinstance(event.get("request_id"), str)
    }
    unpaired = sum(
        starts_by_id.get(request_id, 0) != 1 or request_id not in request_ids_with_terminal
        for request_id in request_ids
    )
    streaming_count = sum(event.get("is_stream") is True for event in completes)
    http_status = (
        int(completes[-1]["status"])
        if completes and isinstance(completes[-1].get("status"), int)
        else int(errors[-1]["status"])
        if errors and isinstance(errors[-1].get("status"), int)
        else 0
    )
    error_event_count = len(errors) + fallback_count + retry_count + metadata_errors + malformed + unpaired
    return {
        "gateway_request_count": len(starts),
        "gateway_complete_count": len(completes),
        "gateway_error_count": len(errors),
        "request_complete_count": int(bool(completes)),
        "http_status": http_status,
        "streaming_request_count": streaming_count,
        "fallback_count": fallback_count,
        "error_event_count": error_event_count,
        "duplicate_terminal_count": duplicate_terminal_count,
        "reconnect_classification": (
            "unclassified" if len(starts) > expected_requests else "none"
        ),
        "model_matches_expected": bool(completes) and _model_matches(
            completes[-1].get("model_canonical", completes[-1].get("model")), case
        ),
        "request_error_statuses": [
            event.get("status") for event in errors if isinstance(event.get("status"), int)
        ],
        "route_metadata_error_count": metadata_errors,
        "malformed_event_count": malformed,
    }


def _capacity_retryable(live: dict[str, object], gateway: dict[str, object]) -> bool:
    statuses = gateway.get("request_error_statuses", [])
    return (
        live.get("returncode") not in {0, None}
        and not live.get("timed_out")
        and not live.get("output_truncated")
        and live.get("tool_call_count") == 0
        and live.get("assistant_output_count") == 0
        and live.get("terminal_count") == 0
        and live.get("error_event_count") == 0
        and live.get("malformed_count") == 0
        and len(statuses) == 1
        and statuses[0] in {429, 503}
        and gateway.get("gateway_request_count") == 1
        and gateway.get("gateway_complete_count") == 0
        and gateway.get("gateway_error_count") == 1
        and gateway.get("fallback_count") == 0
        and gateway.get("route_metadata_error_count") == 0
    )


def _attempt_passed(case: Case, live: dict[str, object], gateway: dict[str, object]) -> bool:
    return (
        live.get("ok") is True
        and gateway.get("gateway_request_count") == 2
        and gateway.get("gateway_complete_count") == 2
        and gateway.get("gateway_error_count") == 0
        and gateway.get("request_complete_count") == 1
        and gateway.get("http_status") == 200
        and gateway.get("streaming_request_count") == 2
        and gateway.get("fallback_count") == 0
        and gateway.get("error_event_count") == 0
        and gateway.get("duplicate_terminal_count") == 0
        and gateway.get("reconnect_classification") == "none"
        and gateway.get("model_matches_expected") is True
    )


def run_case_attempt(
    case: Case,
    *,
    binary: Path,
    work: Path,
    env: dict[str, str],
    settings: Path,
    providers: Path,
    catalog: Path,
    timeout: int,
    configure: bool = True,
    attempt_number: int = 1,
) -> tuple[dict[str, object], dict[str, object], int]:
    managed_root = work / "managed" / case.case_id
    if configure:
        preview_root = work / "managed-preview" / case.case_id
        preview_root.mkdir(parents=True)
        managed_root.mkdir(parents=True)
        preview = _managed(binary, "preview", case, preview_root, settings, providers, catalog, env)
        apply = (
            _managed(binary, "apply", case, managed_root, settings, providers, catalog, env)
            if preview["ok"]
            else {"ok": False, "returncode": None}
        )
        readback = (
            _managed(binary, "readback", case, managed_root, settings, providers, catalog, env)
            if apply["ok"]
            else {"ok": False, "returncode": None}
        )
    else:
        preview = apply = readback = {"ok": True, "reused": True}
    result: dict[str, object] = {
        "preview": preview,
        "apply": apply,
        "readback": readback,
        "live": {"ok": False, "not_run": True},
        "gateway": {},
    }
    duration_ms = 0
    if preview["ok"] and apply["ok"] and readback["ok"]:
        case_root = work / "cases" / case.case_id / f"attempt-{attempt_number}"
        case_root.mkdir(parents=True, exist_ok=True)
        event_path = Path(env["CODEXHUB_RUNTIME_HOME"]) / "proxy" / "codex-proxy-events.jsonl"
        offset = _gateway_event_offset(event_path)
        started = time.monotonic()
        live = _client_launch(case, managed_root, case_root, env, timeout)
        duration_ms = int((time.monotonic() - started) * 1000)
        raw_events, malformed = _gateway_events_for_attempt(
            event_path,
            offset,
            expected_requests=2,
            timeout=5 if live.get("returncode") == 0 else 2,
        )
        gateway = gateway_evidence(case, raw_events, malformed, expected_requests=2)
        live["ok"] = _attempt_passed(case, live, gateway)
        result["live"] = live
        result["gateway"] = gateway
    return result, {
        "case_id": case.case_id,
        "client": case.client,
        "provider_id": case.provider_id,
        "client_selector": case.selector,
        "canonical_model": case.canonical_model,
        "gateway_model": case.gateway_model,
        "endpoint_binding": case.endpoint_binding,
        "protocol": case.protocol,
    }, duration_ms


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sha", required=True, help="full reviewed source SHA")
    parser.add_argument(
        "--bin", type=Path, help="skip self-build and use this candidate"
    )
    parser.add_argument(
        "--output", type=Path, default=Path("test-results/linux-cli-e2e.json")
    )
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--auth", type=Path, required=True, help="explicit isolated-run Codex auth input")
    parser.add_argument("--providers", type=Path, required=True, help="explicit providers.toml input")
    parser.add_argument("--settings", type=Path, required=True, help="explicit settings.json input")
    parser.add_argument("--catalog", type=Path, help="optional current Official catalog input")
    parser.add_argument(
        "--deepseek-credentials",
        type=Path,
        required=True,
        help="dedicated DeepSeek credential JSON (codexhub.real-client-deepseek.v1)",
    )
    args = parser.parse_args(argv)
    inputs = {
        "auth": args.auth,
        "providers": args.providers,
        "settings": args.settings,
        "catalog": args.catalog,
        "DeepSeek credentials": args.deepseek_credentials,
    }
    missing = [name for name, path in inputs.items() if path is not None and not path.is_file()]
    clients = [name for name in ("codex", "opencode", "pi", "omp") if not shutil.which(name)]
    if missing or clients:
        parser.error(f"missing explicit inputs={missing}; missing clients={clients}")
    try:
        deepseek_api_key = load_deepseek_api_key(args.deepseek_credentials)
    except ValueError as error:
        parser.error(str(error))
    version_home = Path(tempfile.mkdtemp(prefix="codexhub-client-versions-"))
    version_env = _isolated_environment()
    version_env.update(
        {
            "HOME": str(version_home),
            "XDG_CONFIG_HOME": str(version_home / ".config"),
            "XDG_DATA_HOME": str(version_home / ".local" / "share"),
        }
    )
    try:
        versions, version_failures = client_versions(version_env)
    finally:
        shutil.rmtree(version_home, ignore_errors=True)
    if version_failures:
        parser.error(f"client version gate failed: {version_failures}")
    binary = args.bin.resolve() if args.bin else build_candidate()
    try:
        binding = qualify_candidate_binary(binary, ROOT, args.candidate_sha, self_built=args.bin is None)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    report: dict[str, object] = {
        "schema": "codexhub.linux-cli-e2e.v3",
        **binding,
        "client_versions": versions,
        "cases": [],
    }
    failures: list[str] = []
    with tempfile.TemporaryDirectory(
        prefix="codexhub-linux-cli-e2e-",
        ignore_cleanup_errors=True,
    ) as temporary:
        work = Path(temporary)
        env, settings, providers, catalog, port = _prepare_runtime(
            work,
            args.settings,
            args.providers,
            args.auth,
            args.catalog,
            deepseek_api_key,
        )
        refresh_env = env.copy()
        refresh_env.pop("DEEPSEEK_API_KEY", None)
        refresh = None if catalog.is_file() else _run([str(binary), "refresh-models"], env=refresh_env, timeout=180)
        if not catalog.is_file() and (refresh is None or refresh.returncode != 0 or not catalog.is_file()):
            failures.append("candidate: refresh_models_failed")
        else:
            start = _run([str(binary), "start"], env=env, timeout=30)
            if start.returncode != 0 or not _wait_for_health(port):
                failures.append("candidate: gateway_health_failed")
            else:
                try:
                    for case in CASES:
                        result, identity, duration_ms = run_case_attempt(
                            case,
                            binary=binary,
                            work=work,
                            env=env,
                            settings=settings,
                            providers=providers,
                            catalog=catalog,
                            timeout=args.timeout,
                        )
                        live = result["live"]
                        gateway = result["gateway"]
                        retry_classification = "not_needed" if live.get("ok") else "not_eligible"
                        retry_attempt_evidence = None
                        if (
                            not live.get("ok")
                            and not live.get("not_run")
                            and _capacity_retryable(live, gateway)
                        ):
                            retry_classification = (
                                f"capacity_{gateway['request_error_statuses'][0]}_pre_output_retried"
                            )
                            retry_attempt_evidence = {"live": live, "gateway": gateway}
                            retry_result, _identity, retry_duration = run_case_attempt(
                                case,
                                binary=binary,
                                work=work,
                                env=env,
                                settings=settings,
                                providers=providers,
                                catalog=catalog,
                                timeout=args.timeout,
                                configure=False,
                                attempt_number=2,
                            )
                            for field in ("preview", "apply", "readback"):
                                retry_result[field] = result[field]
                            duration_ms += retry_duration
                            result = retry_result
                            live = result["live"]
                            gateway = result["gateway"]
                        outcome = "passed" if live.get("ok") else "failed"
                        artifact_dir_name = f"{args.output.stem}-cases"
                        artifact = f"{artifact_dir_name}/{case.case_id}.json"
                        live_metrics = {
                            "duration_ms": duration_ms,
                            "request_complete_count": gateway.get("request_complete_count", 0),
                            "http_status": gateway.get("http_status", 0),
                            "read_only_tool_call_count": live.get("read_only_tool_call_count", 0),
                            "sentinel_chunk_count": live.get("sentinel_chunk_count", 0),
                            "streaming_request_count": gateway.get("streaming_request_count", 0),
                            "fallback_count": gateway.get("fallback_count", 0),
                            "error_event_count": int(live.get("error_event_count", 0))
                            + int(gateway.get("error_event_count", 0)),
                            "duplicate_terminal_count": max(
                                0,
                                int(live.get("terminal_count", 0)) - 1,
                                int(gateway.get("duplicate_terminal_count", 0)),
                            ),
                            "terminal_classification": (
                                "timeout"
                                if live.get("timed_out")
                                else "nonzero_exit"
                                if live.get("returncode") not in {0, None}
                                else live.get("terminal_classification", "unclassified")
                            ),
                            "reconnect_classification": gateway.get(
                                "reconnect_classification", "unclassified"
                            ),
                            "retry_classification": retry_classification,
                            "gateway_request_count": gateway.get("gateway_request_count", 0),
                            "gateway_complete_count": gateway.get("gateway_complete_count", 0),
                            "gateway_error_count": gateway.get("gateway_error_count", 0),
                            "gateway_request_error_statuses": gateway.get("request_error_statuses", []),
                            "gateway_route_metadata_error_count": gateway.get("route_metadata_error_count", 0),
                            "gateway_malformed_event_count": gateway.get("malformed_event_count", 0),
                            "gateway_model_matches_expected": gateway.get("model_matches_expected", False),
                            "client_returncode": live.get("returncode"),
                            "client_timed_out": live.get("timed_out", False),
                            "client_output_truncated": live.get("output_truncated", False),
                            "client_malformed_event_count": live.get("malformed_count", 0),
                            "stdout_sha256": live.get("stdout_sha256"),
                            "stderr_sha256": live.get("stderr_sha256"),
                            "artifact": artifact,
                        }
                        artifact_root = args.output.parent / artifact_dir_name
                        artifact_root.mkdir(parents=True, exist_ok=True)
                        (artifact_root / f"{case.case_id}.json").write_text(
                            json.dumps(
                                {
                                    **identity,
                                    "outcome": outcome,
                                    **live_metrics,
                                    "retry_attempt": retry_attempt_evidence,
                                },
                                indent=2,
                            )
                            + "\n",
                            encoding="utf-8",
                        )
                        report["cases"].append(
                            {
                                **identity,
                                "outcome": outcome,
                                "preview": result["preview"],
                                "apply": result["apply"],
                                "readback": result["readback"],
                                **live_metrics,
                                "retry_attempt": retry_attempt_evidence,
                            }
                        )
                        if outcome != "passed":
                            failures.append(f"{case.case_id}: gate_failed")
                finally:
                    _run([str(binary), "stop"], env=env, timeout=30)
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
                        "outcome": item["outcome"],
                        "apply": item["apply"]["ok"],
                        "readback": item["readback"]["ok"],
                        "live": item["outcome"] == "passed",
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
