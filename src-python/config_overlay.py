from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path

from atomic_io import atomic_write_text
import re
import sys
from urllib.parse import urlsplit


MARKER_BEGIN = "# BEGIN CODEX PROXY SESSION CONFIG"
MARKER_END = "# END CODEX PROXY SESSION CONFIG"
TOP_LEVEL_KEYS = {"model", "model_provider", "model_catalog_json", "openai_base_url"}
PROXY_FEATURE_FLAGS = {
    "responses_websockets": "false",
    "responses_websockets_v2": "false",
}
PROXY_PROVIDER_ID = "custom"
PROXY_PROVIDER_NAME = "Codex Proxy"
UNIFIED_OFFICIAL_PROVIDER_NAME = "OpenAI"
STALE_PROXY_PROVIDER_SECTIONS = (
    "model_providers.openai",
    "model_providers.custom",
    "model_providers.codex_proxy",
)


def toml_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def toml_basic_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def strip_marked_overlay(text: str) -> str:
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(MARKER_BEGIN)}\s*$.*?^\s*{re.escape(MARKER_END)}\s*$\r?\n?"
    )
    return pattern.sub("", text)


def strip_top_level_keys(text: str, keys: set[str] = TOP_LEVEL_KEYS) -> str:
    result: list[str] = []
    in_top_level = True
    key_pattern = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")

    for line in text.splitlines(keepends=True):
        if re.match(r"^\s*\[", line):
            in_top_level = False
        match = key_pattern.match(line)
        if in_top_level and match and match.group(1) in keys:
            continue
        result.append(line)

    return "".join(result)


def strip_section(text: str, section_name: str) -> str:
    header_pattern = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
    result: list[str] = []
    skipping = False

    for line in text.splitlines(keepends=True):
        match = header_pattern.match(line)
        if match:
            skipping = match.group(1).strip() == section_name
            if skipping:
                continue
        if not skipping:
            result.append(line)

    return "".join(result)


def top_level_value(text: str, key: str) -> str | None:
    in_top_level = True
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*(?:#.*)?$")
    for line in text.splitlines():
        if re.match(r"^\s*\[", line):
            in_top_level = False
        if not in_top_level:
            continue
        match = key_pattern.match(line)
        if not match:
            continue
        raw = match.group(1).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            return raw[1:-1]
        return raw
    return None


def section_key_values(text: str, section_name: str) -> dict[str, str] | None:
    header_pattern = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
    key_pattern = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*(.+?)\s*(?:#.*)?$")
    in_section = False
    values: dict[str, str] = {}

    for line in text.splitlines():
        header = header_pattern.match(line)
        if header:
            if in_section:
                break
            in_section = header.group(1).strip() == section_name
            continue
        if not in_section:
            continue
        match = key_pattern.match(line)
        if not match:
            continue
        raw = match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            raw = raw[1:-1]
        values[match.group(1)] = raw

    return values if in_section or values else None


def unified_official_provider_values() -> dict[str, str]:
    return {
        "name": UNIFIED_OFFICIAL_PROVIDER_NAME,
        "requires_openai_auth": "true",
        "supports_websockets": "true",
        "wire_api": "responses",
    }


@dataclass(frozen=True)
class UnifiedConfigState:
    provider_id: str | None
    custom_section: dict[str, str] | None
    exact_unified: bool
    managed_gateway: bool
    stale_catalog: bool


def is_managed_gateway_provider(values: dict[str, str] | None) -> bool:
    if not values or values.get("name") != PROXY_PROVIDER_NAME:
        return False
    if values.get("wire_api") != "responses":
        return False
    if values.get("supports_websockets") != "false":
        return False
    legacy_auth = values.get("requires_openai_auth") == "true" and "experimental_bearer_token" not in values
    keyed_auth = "experimental_bearer_token" in values and values.get("requires_openai_auth") in {"true", "false"}
    if not (legacy_auth or keyed_auth):
        return False
    parsed = urlsplit(values.get("base_url", ""))
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and parsed.port is not None
        and parsed.path.rstrip("/") == "/v1"
    )


def unified_config_state(text: str) -> UnifiedConfigState:
    provider_id = top_level_value(text, "model_provider")
    custom_section = section_key_values(text, f"model_providers.{PROXY_PROVIDER_ID}")
    return UnifiedConfigState(
        provider_id=provider_id,
        custom_section=custom_section,
        exact_unified=custom_section == unified_official_provider_values(),
        managed_gateway=is_managed_gateway_provider(custom_section),
        stale_catalog=top_level_value(text, "model_catalog_json") is not None,
    )


def build_unified_official_provider_section() -> str:
    return "\n".join(
        [
            f"[model_providers.{PROXY_PROVIDER_ID}]",
            f'name = "{UNIFIED_OFFICIAL_PROVIDER_NAME}"',
            "requires_openai_auth = true",
            "supports_websockets = true",
            'wire_api = "responses"',
            "",
        ]
    )


def inspect_unified_history_config(text: str, unified_history: bool = True) -> str:
    state = unified_config_state(text)
    if state.provider_id == PROXY_PROVIDER_ID and state.managed_gateway:
        return "gateway_active"
    if state.provider_id not in {None, "openai", PROXY_PROVIDER_ID}:
        return "conflict"
    if state.custom_section is not None and not (state.exact_unified or state.managed_gateway):
        return "conflict"
    if unified_history:
        if state.provider_id == PROXY_PROVIDER_ID and state.exact_unified and not state.stale_catalog:
            return "clean"
    elif state.provider_id in {None, "openai"} and state.custom_section is None and not state.stale_catalog:
        return "clean"
    return "needs_repair"


def inject_unified_history_config(text: str) -> tuple[str, str]:
    state = unified_config_state(text)
    if state.provider_id is not None:
        if state.provider_id == PROXY_PROVIDER_ID and state.exact_unified and not state.stale_catalog:
            return text, "already_unified"
        if state.provider_id not in {"openai", PROXY_PROVIDER_ID}:
            return text, "explicit_model_provider"
        if state.provider_id == PROXY_PROVIDER_ID and not (state.exact_unified or state.managed_gateway):
            return text, "explicit_model_provider"

    if state.custom_section is not None and not (state.exact_unified or state.managed_gateway):
        return text, "conflicting_custom_provider"

    updated = strip_top_level_keys(text, {"model_provider", "model_catalog_json", "openai_base_url"})
    if state.custom_section is not None:
        updated = strip_section(updated, f"model_providers.{PROXY_PROVIDER_ID}")
    updated = insert_provider_section(updated, build_unified_official_provider_section())

    prefix = f'model_provider = "{PROXY_PROVIDER_ID}"\n'
    if updated.strip():
        updated = prefix + "\n" + updated.lstrip()
    else:
        updated = prefix
    if state.managed_gateway:
        return updated, "replaced_managed_gateway"
    if state.exact_unified:
        return updated, "repaired_unified"
    return updated, "injected"


def strip_unified_history_config(text: str) -> str:
    custom_section = section_key_values(text, f"model_providers.{PROXY_PROVIDER_ID}")
    if custom_section != unified_official_provider_values() and not is_managed_gateway_provider(custom_section):
        return text
    stripped = strip_section(text, f"model_providers.{PROXY_PROVIDER_ID}")
    stripped = strip_top_level_keys(stripped, {"model_provider", "model_catalog_json", "openai_base_url"})
    return stripped.lstrip() if text.startswith("model_provider") else stripped


def set_feature_flags(text: str, flags: dict[str, str]) -> str:
    lines = text.splitlines()
    result: list[str] = []
    in_features = False
    features_seen = False
    flags_written = False
    key_pattern = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")

    for line in lines:
        section_match = re.match(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$", line)
        if section_match:
            if in_features and not flags_written:
                result.extend(f"{key} = {value}" for key, value in flags.items())
                flags_written = True
            in_features = section_match.group(1).strip() == "features"
            features_seen = features_seen or in_features
            result.append(line)
            if in_features and not flags_written:
                result.extend(f"{key} = {value}" for key, value in flags.items())
                flags_written = True
            continue

        key_match = key_pattern.match(line)
        if in_features and key_match and key_match.group(1) in flags:
            continue
        result.append(line)

    if features_seen:
        if in_features and not flags_written:
            result.extend(f"{key} = {value}" for key, value in flags.items())
        return "\n".join(result).rstrip() + "\n"

    suffix = ["", "[features]"]
    suffix.extend(f"{key} = {value}" for key, value in flags.items())
    return "\n".join(result + suffix).rstrip() + "\n"


def catalog_config_value(_config_path: Path, catalog_path: Path) -> str:
    return str(catalog_path.resolve())


def build_overlay(catalog_value: str, owner: str) -> str:
    return "\n".join(
        [
            MARKER_BEGIN,
            f"# owner = {owner}",
            'model = "gpt-5.5"',
            f'model_provider = "{PROXY_PROVIDER_ID}"',
            f"model_catalog_json = {toml_literal(catalog_value)}",
            MARKER_END,
            "",
        ]
    )


def overlay_owner(text: str) -> str | None:
    match = re.search(r"(?m)^\s*# owner = (release|beta)\s*$", text)
    return match.group(1) if match else None


def read_text_preserving_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def build_provider_section(base_url: str, gateway_key: str) -> str:
    return "\n".join(
        [
            f"[model_providers.{PROXY_PROVIDER_ID}]",
            f'name = "{PROXY_PROVIDER_NAME}"',
            f"base_url = {toml_literal(base_url.rstrip('/') + '/v1')}",
            'wire_api = "responses"',
            "requires_openai_auth = false",
            f"experimental_bearer_token = {toml_basic_string(gateway_key)}",
            "supports_websockets = false",
            "",
        ]
    )


def insert_provider_section(text: str, provider_section: str) -> str:
    match = re.search(r"(?m)^\s*\[", text)
    if match:
        return text[: match.start()] + provider_section + text[match.start() :]
    if text.strip():
        return text.rstrip() + "\n\n" + provider_section
    return provider_section


def apply_overlay(
    config_path: Path,
    backup_path: Path,
    catalog_path: Path,
    base_url: str,
    owner: str = "release",
    takeover: bool = False,
    gateway_key: str = "codexhub-proxy",
) -> None:
    if owner not in {"release", "beta"}:
        raise ValueError(f"unsupported CodexHub owner: {owner}")
    original = read_text_preserving_newlines(config_path) if config_path.exists() else ""
    custom_section = section_key_values(original, f"model_providers.{PROXY_PROVIDER_ID}")
    if custom_section is not None and not (
        custom_section == unified_official_provider_values() or is_managed_gateway_provider(custom_section)
    ):
        raise ValueError("refusing to overwrite unknown custom provider")
    cleaned = strip_marked_overlay(original)
    active_owner = overlay_owner(original)
    if active_owner != owner or not backup_path.exists():
        backup = original if takeover else (cleaned if cleaned != original else original)
        atomic_write_text(backup_path, backup, encoding="utf-8")

    for section in STALE_PROXY_PROVIDER_SECTIONS:
        cleaned = strip_section(cleaned, section)
    cleaned = strip_top_level_keys(cleaned)
    cleaned = set_feature_flags(cleaned, PROXY_FEATURE_FLAGS)
    updated = build_overlay(catalog_config_value(config_path, catalog_path), owner) + cleaned.lstrip()
    updated = insert_provider_section(updated, build_provider_section(base_url, gateway_key))
    atomic_write_text(config_path, updated, encoding="utf-8")


def restore_overlay(config_path: Path, backup_path: Path, unified_history: bool = False) -> str:
    if backup_path.exists():
        restored = read_text_preserving_newlines(backup_path)
        atomic_write_text(config_path, restored, encoding="utf-8")
        backup_path.unlink()
        return "restored_backup"
    elif config_path.exists():
        restored = strip_marked_overlay(config_path.read_text(encoding="utf-8"))
    else:
        restored = ""

    if unified_history:
        restored, status = inject_unified_history_config(restored)
    else:
        restored = strip_unified_history_config(restored)
        status = "disabled"

    if restored or config_path.exists() or unified_history:
        atomic_write_text(config_path, restored, encoding="utf-8")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply or restore the Codex proxy session config overlay.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--config", required=True, type=Path)
    apply_parser.add_argument("--backup", required=True, type=Path)
    apply_parser.add_argument("--catalog", required=True, type=Path)
    apply_parser.add_argument("--base-url", required=True)
    apply_parser.add_argument("--owner", choices=["release", "beta"], default="release")
    apply_parser.add_argument("--takeover", action="store_true")
    apply_parser.add_argument("--gateway-key", default="codexhub-proxy")

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--config", required=True, type=Path)
    restore_parser.add_argument("--backup", required=True, type=Path)
    restore_parser.add_argument("--unified-history", action="store_true")

    inspect_parser = subparsers.add_parser("inspect-unified")
    inspect_parser.add_argument("--config", required=True, type=Path)
    inspect_parser.add_argument("--target", choices=["unified", "separated"], default="unified")

    args = parser.parse_args(argv)
    if args.command == "apply":
        apply_overlay(args.config, args.backup, args.catalog, args.base_url, args.owner, args.takeover, args.gateway_key)
    elif args.command == "restore":
        status = restore_overlay(args.config, args.backup, args.unified_history)
        if args.unified_history:
            print(f"unified_history={status}")
    elif args.command == "inspect-unified":
        text = args.config.read_text(encoding="utf-8") if args.config.exists() else ""
        print(
            json.dumps(
                {"status": inspect_unified_history_config(text, args.target == "unified")},
                ensure_ascii=True,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
