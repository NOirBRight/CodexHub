from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


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


def has_exact_unified_official_provider(text: str) -> bool:
    return section_key_values(text, f"model_providers.{PROXY_PROVIDER_ID}") == unified_official_provider_values()


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


def inject_unified_history_config(text: str) -> tuple[str, str]:
    provider_id = top_level_value(text, "model_provider")
    if provider_id is not None:
        if provider_id == PROXY_PROVIDER_ID and has_exact_unified_official_provider(text):
            return text, "already_unified"
        if provider_id != "openai":
            return text, "explicit_model_provider"
        text = strip_top_level_keys(text, {"model_provider"})

    custom_section = section_key_values(text, f"model_providers.{PROXY_PROVIDER_ID}")
    if custom_section is not None and custom_section != unified_official_provider_values():
        return text, "conflicting_custom_provider"

    updated = text
    if custom_section is None:
        updated = insert_provider_section(updated, build_unified_official_provider_section())

    prefix = f'model_provider = "{PROXY_PROVIDER_ID}"\n'
    if updated.strip():
        updated = prefix + "\n" + updated.lstrip()
    else:
        updated = prefix
    return updated, "injected"


def strip_unified_history_config(text: str) -> str:
    if top_level_value(text, "model_provider") != PROXY_PROVIDER_ID:
        return text
    if not has_exact_unified_official_provider(text):
        return text
    stripped = strip_section(text, f"model_providers.{PROXY_PROVIDER_ID}")
    stripped = strip_top_level_keys(stripped, {"model_provider"})
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


def catalog_config_value(config_path: Path, catalog_path: Path) -> str:
    try:
        return catalog_path.resolve().relative_to(config_path.parent.resolve()).as_posix()
    except ValueError:
        return str(catalog_path)


def build_overlay(catalog_value: str) -> str:
    return "\n".join(
        [
            MARKER_BEGIN,
            'model = "openai/gpt-5.5"',
            f'model_provider = "{PROXY_PROVIDER_ID}"',
            f"model_catalog_json = {toml_literal(catalog_value)}",
            MARKER_END,
            "",
        ]
    )


def build_provider_section(base_url: str) -> str:
    return "\n".join(
        [
            f"[model_providers.{PROXY_PROVIDER_ID}]",
            f'name = "{PROXY_PROVIDER_NAME}"',
            f"base_url = {toml_literal(base_url.rstrip('/') + '/v1')}",
            'wire_api = "responses"',
            "requires_openai_auth = true",
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


def apply_overlay(config_path: Path, backup_path: Path, catalog_path: Path, base_url: str) -> None:
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    cleaned = strip_marked_overlay(original)
    backup_path.write_text(cleaned if cleaned != original else original, encoding="utf-8")

    for section in STALE_PROXY_PROVIDER_SECTIONS:
        cleaned = strip_section(cleaned, section)
    cleaned = strip_top_level_keys(cleaned)
    cleaned = set_feature_flags(cleaned, PROXY_FEATURE_FLAGS)
    updated = build_overlay(catalog_config_value(config_path, catalog_path)) + cleaned.lstrip()
    updated = insert_provider_section(updated, build_provider_section(base_url))
    config_path.write_text(updated, encoding="utf-8")


def restore_overlay(config_path: Path, backup_path: Path, unified_history: bool = False) -> str:
    if backup_path.exists():
        restored = backup_path.read_text(encoding="utf-8")
        backup_path.unlink()
    elif config_path.exists():
        restored = config_path.read_text(encoding="utf-8")
    else:
        restored = ""

    if unified_history:
        restored, status = inject_unified_history_config(restored)
    else:
        restored = strip_unified_history_config(restored)
        status = "disabled"

    if restored or config_path.exists() or unified_history:
        config_path.write_text(restored, encoding="utf-8")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply or restore the Codex proxy session config overlay.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--config", required=True, type=Path)
    apply_parser.add_argument("--backup", required=True, type=Path)
    apply_parser.add_argument("--catalog", required=True, type=Path)
    apply_parser.add_argument("--base-url", required=True)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--config", required=True, type=Path)
    restore_parser.add_argument("--backup", required=True, type=Path)
    restore_parser.add_argument("--unified-history", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "apply":
        apply_overlay(args.config, args.backup, args.catalog, args.base_url)
    elif args.command == "restore":
        status = restore_overlay(args.config, args.backup, args.unified_history)
        if args.unified_history:
            print(f"unified_history={status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
