import json
import os
from pathlib import Path
import sys


CLIENTS = {"codex", "opencode", "zcode", "pi", "omp"}
VERBS = {"preview", "apply", "readback"}


def fail(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def parse_args(argv: list[str]) -> tuple[str, dict[str, str]]:
    if len(argv) < 2 or argv[0] != "managed-client-config" or argv[1] not in VERBS:
        fail("unsupported managed-client-config invocation")
    values = argv[2:]
    if len(values) % 2:
        fail("managed-client-config arguments must be flag/value pairs")
    parsed = dict(zip(values[::2], values[1::2], strict=True))
    allowed = {
        "--client",
        "--root",
        "--model",
        "--settings-path",
        "--providers-path",
        "--catalog-path",
        "--python-path",
        "--backup-subdir",
    }
    if not set(parsed) <= allowed or parsed.get("--client") not in CLIENTS:
        fail("unsupported managed-client-config arguments")
    for required in ("--root", "--model", "--settings-path", "--providers-path"):
        if not parsed.get(required):
            fail(f"missing {required}")
    for input_flag in ("--settings-path", "--providers-path"):
        input_path = Path(parsed[input_flag])
        if not input_path.is_absolute() or not input_path.is_file():
            fail(f"invalid {input_flag}")
    return argv[1], parsed


def selection(client: str, model: str) -> tuple[str, str]:
    if client == "codex":
        return f"custom/{model}", "responses"
    provider, model_id = model.split("/", 1)
    selector = f"codexhub-{provider}/{model_id}"
    protocol = "responses" if provider == "openai" else "chat_completions"
    return selector, protocol


def targets(client: str) -> list[str]:
    return {
        "codex": ["codex-target/config.toml"],
        "opencode": ["opencode/opencode.json"],
        "pi": ["pi/settings.json", "pi/models.json"],
        "omp": ["omp/config.yml", "omp/models.yml"],
        "zcode": [
            "zcode/codexhub.json",
            "zcode/config.json",
            "zcode/bots-model-cache.v2.json",
        ],
    }[client]


def write_zcode(root: Path) -> None:
    gateway = "http://127.0.0.1:19190"
    specs = {
        "codexhub-openai": ("openai", "gpt-5.6-luna", "openai", "openai-responses", "responses"),
        "codexhub-volc": ("volc", "glm-5.2", "openai-compatible", "openai-chat-completions", "chat/completions"),
    }
    catalog_providers = []
    cache_providers = []
    config_providers = {}
    for provider_id, (route, model_id, kind, api_format, path) in specs.items():
        model = {
            "id": model_id,
            "name": model_id,
            "kinds": [kind],
            "defaultKind": kind,
            "modalities": {"input": ["text"], "output": ["text"]},
            "maxOutputTokens": 32768,
        }
        common = {
            "id": provider_id,
            "name": provider_id,
            "enabled": True,
            "source": "custom",
            "apiFormat": api_format,
            "apiKeyRequired": True,
            "apiKey": "fixture-gateway-private-key",
            "defaultKind": kind,
            "models": [model],
            "createdAt": 1,
            "updatedAt": 1,
        }
        catalog_providers.append(
            common | {"endpoints": {"baseURL": gateway, "paths": {kind: f"/v1/providers/{route}/{path}"}}}
        )
        provider_url = f"{gateway}/v1/providers/{route}"
        cache_providers.append(
            common | {"endpoints": {"baseURL": provider_url, "paths": {kind: f"/{path}"}}}
        )
        config_providers[provider_id] = {
            "name": provider_id,
            "kind": kind,
            "enabled": True,
            "source": "custom",
            "apiFormat": api_format,
            "endpoints": {"baseURL": provider_url, "paths": {kind: f"/{path}"}},
            "options": {
                "baseURL": provider_url,
                "apiKey": "fixture-gateway-private-key",
                "apiKeyRequired": True,
            },
            "models": {
                model_id: {
                    "name": model_id,
                    "limit": {"output": 32768},
                    "modalities": {"input": ["text"], "output": ["text"]},
                }
            },
        }
    (root / "zcode").mkdir(parents=True, exist_ok=True)
    (root / "zcode" / "codexhub.json").write_text(
        json.dumps({"schemaVersion": "zcode.model-providers.v2", "providers": catalog_providers}),
        encoding="utf-8",
    )
    (root / "zcode" / "bots-model-cache.v2.json").write_text(
        json.dumps({"schemaVersion": "zcode.model-providers.v2", "providers": cache_providers}),
        encoding="utf-8",
    )
    (root / "zcode" / "config.json").write_text(
        json.dumps({"provider": config_providers}), encoding="utf-8"
    )


def write_targets(root: Path, client: str, model: str) -> None:
    if client == "zcode":
        write_zcode(root)
        return
    for relative in targets(client):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"fixture managed config for {client} {model} http://127.0.0.1:19190\n",
            encoding="utf-8",
        )


def main() -> None:
    verb, args = parse_args(sys.argv[1:])
    client = args["--client"]
    model = args["--model"]
    root = Path(args["--root"])
    if not root.is_absolute():
        fail("root must be absolute")
    mode = os.environ.get("CODEXHUB_E2E_MATERIALIZER_MODE", "ok")
    if mode == "failure":
        fail("materializer failed with fixture-gateway-private-key", 9)
    root.mkdir(parents=True, exist_ok=True)
    selector, protocol = selection(client, model)
    if verb == "apply":
        write_targets(root, client, model)
        (root / ".fake-managed-state.json").write_text(
            json.dumps({"client": client, "model": model}), encoding="utf-8"
        )
    elif verb == "readback":
        state_path = root / ".fake-managed-state.json"
        if not state_path.is_file():
            fail("readback state missing", 8)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state != {"client": client, "model": model}:
            fail("readback state mismatch", 8)
    if mode == "contradiction-zcode" and client == "zcode" and verb == "readback":
        selector = "codexhub-volc/contradiction"
    log_path = os.environ.get("CODEXHUB_E2E_MATERIALIZER_LOG")
    if log_path:
        with Path(log_path).open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "verb": verb,
                        "client": client,
                        "model": model,
                        "root_role": root.name,
                        "flags": sorted(args),
                    }
                )
                + "\n"
            )
    base = {
        "client_id": client,
        "selector": selector,
        "model": model,
        "route_protocol": protocol,
    }
    if verb == "preview":
        if client == "codex":
            result = base | {"target_names": targets(client), "overlay_args_relative": ["apply"]}
        else:
            result = base | {"target_names": targets(client), "next_redacted": "[fixture]"}
    elif verb == "apply":
        if client == "codex":
            result = {
                "mode": "custom",
                "proxy_running": False,
                "proxy_port": 19190,
                "proxy_build": None,
                "message": "fixture",
                "gateway_lifecycle": "unavailable",
            }
            if mode == "present-optionals":
                result.update(
                    {
                        "history_sync_status": None,
                        "history_sync_message": "isolated fixture",
                    }
                )
            elif mode == "missing-required":
                result.pop("message")
            elif mode == "unknown-key":
                result["future_field"] = "not approved"
        else:
            result = base | {
                "applied": True,
                "target_names": targets(client),
                "backup_dir_relative": "backups",
            }
    else:
        result = base | {"ok": True}
    if mode == "unsafe-output":
        result["api_key"] = "fixture-gateway-private-key"
    print(json.dumps(result))


if __name__ == "__main__":
    main()
