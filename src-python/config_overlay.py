from __future__ import annotations

from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import secrets

from atomic_io import atomic_read_or_create_text, atomic_write_text
from model_limits import (
    CURRENT_DIRECT_OFFICIAL_SOURCE,
    DEGRADED_LAST_KNOWN_OFFICIAL_SOURCE,
    FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE,
)
import re
import sys
from urllib.parse import urlsplit


MARKER_BEGIN = "# BEGIN CODEX PROXY SESSION CONFIG"
MARKER_END = "# END CODEX PROXY SESSION CONFIG"
TOP_LEVEL_KEYS = {"model_provider", "model_catalog_json", "openai_base_url"}
PROXY_FEATURE_FLAGS = {
    "responses_websockets": "false",
    "responses_websockets_v2": "false",
}
PROXY_PROVIDER_ID = "custom"
PROXY_PROVIDER_NAME = "Codex Proxy"
UNIFIED_OFFICIAL_PROVIDER_NAME = "OpenAI"
CATALOG_OWNER_MARKER = "# catalog_owner = codexhub"
CATALOG_OWNER_SECRET_FILENAME = ".codexhub-catalog-owner-key"
MANAGED_CATALOG_FILENAMES = {
    "codexhub-model-catalog.json",
    "codex-proxy-official-ollama.json",
}
STALE_PROXY_PROVIDER_SECTIONS = (
    "model_providers.openai",
    "model_providers.custom",
    "model_providers.codex_proxy",
)
NATIVE_AUTO_COMPACT_PERCENT = 90
CONTEXT_GUARD_KEYS = {
    "model_context_window",
    "model_auto_compact_token_limit",
}

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


def _toml_value_without_comment(raw: str) -> str:
    quote: str | None = None
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote == "'":
            if char == "'":
                if index + 1 < len(raw) and raw[index + 1] == "'":
                    index += 2
                    continue
                quote = None
        elif quote == '"':
            if char == "\\":
                index += 2
                continue
            if char == '"':
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "#":
            return raw[:index].rstrip()
        index += 1
    return raw.strip()


def _decode_toml_string(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        if raw[0] == "'":
            return raw[1:-1].replace("''", "'")
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw[1:-1]
    return raw


def top_level_value(text: str, key: str) -> str | None:
    in_top_level = True
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)$")
    for line in text.splitlines():
        if re.match(r"^\s*\[", line):
            in_top_level = False
        if not in_top_level:
            continue
        match = key_pattern.match(line)
        if not match:
            continue
        raw = _toml_value_without_comment(match.group(1))
        return _decode_toml_string(raw)
    return None


def _looks_like_managed_catalog_path(value: str | None) -> bool:
    """Recognize only the standard CodexHub catalog location shape.

    A custom path may use the same basename, so a basename alone is not an
    ownership proof.  The managed catalogs are always emitted below a
    ``model-catalogs`` directory (or as the documented relative path).
    """

    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value.strip().replace("\\", "/").rstrip("/")
    parts = [part for part in normalized.split("/") if part]
    if not parts or parts[-1] not in MANAGED_CATALOG_FILENAMES:
        return False
    # The documented relative handoff is unambiguous.  Absolute paths are
    # owned only when they resolve below the active CODEX_HOME; an unrelated
    # custom directory containing the same basename remains user-owned.
    if len(parts) == 2 and parts[0] == "model-catalogs":
        return True
    try:
        codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        candidate = Path(value).expanduser().resolve()
        return candidate == (codex_home / "model-catalogs" / parts[-1]).resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _catalog_owner_secret_path(catalog_value: str) -> Path:
    return Path(catalog_value).expanduser().resolve().parent / CATALOG_OWNER_SECRET_FILENAME


def _load_catalog_owner_secret(catalog_value: str, *, create: bool) -> bytes | None:
    path = _catalog_owner_secret_path(catalog_value)
    if create:
        raw = atomic_read_or_create_text(
            path,
            lambda: secrets.token_hex(32) + "\n",
            encoding="ascii",
            mode=0o600,
        )
    else:
        try:
            raw = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError):
            return None
    try:
        secret = bytes.fromhex(raw.strip())
    except ValueError:
        return None
    return secret if len(secret) == 32 else None


def _catalog_owner_marker_digest(catalog_value: str, *, create: bool) -> str | None:
    secret = _load_catalog_owner_secret(catalog_value, create=create)
    if secret is None:
        return None
    normalized = str(Path(catalog_value).expanduser().resolve())
    return hmac.new(secret, normalized.encode("utf-8"), "sha256").hexdigest()


def _overlay_marks_managed_catalog(text: str) -> bool:
    marker_pattern = re.compile(
        rf"(?ms)^\s*{re.escape(MARKER_BEGIN)}\s*$.*?^\s*{re.escape(MARKER_END)}\s*$"
    )
    match = marker_pattern.search(text)
    if not match:
        return False
    block = match.group(0)
    catalog_value = top_level_value(block, "model_catalog_json")
    if not catalog_value:
        return False
    marker = re.search(
        rf"(?m)^\s*{re.escape(CATALOG_OWNER_MARKER)}:([0-9a-f]{{64}})\s*$",
        block,
    )
    if marker is None:
        return False
    digest = _catalog_owner_marker_digest(catalog_value, create=False)
    return digest is not None and hmac.compare_digest(marker.group(1), digest)


def is_managed_catalog_path(value: str | None, managed_path: Path | None = None) -> bool:
    """Return whether a config path is owned by CodexHub's catalog layer."""

    if managed_path is not None and isinstance(value, str) and value.strip():
        try:
            candidate = Path(value).expanduser().resolve()
            if candidate == managed_path.expanduser().resolve():
                return True
        except (OSError, RuntimeError, ValueError):
            pass
    return _looks_like_managed_catalog_path(value)


def set_top_level_values(text: str, values: dict[str, str | None]) -> str:
    cleaned = strip_top_level_keys(text, set(values))
    assignments = [f"{key} = {value}" for key, value in values.items() if value is not None]
    if not assignments:
        return cleaned

    prefix = "\n".join(assignments)
    if cleaned.strip():
        return f"{prefix}\n\n{cleaned.lstrip()}"
    return f"{prefix}\n"


def _top_level_positive_int(text: str, key: str) -> int | None:
    raw = top_level_value(text, key)
    if raw is None:
        return None
    try:
        value = int(raw.replace("_", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def _positive_toml_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value.replace("_", ""))
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def context_guard_status(
    config_path: Path,
    state_path: Path | None = None,
) -> dict[str, int | bool | None]:
    text = read_text_preserving_newlines(config_path) if config_path.exists() else ""
    context_window = _top_level_positive_int(text, "model_context_window")
    auto_compact_token_limit = _top_level_positive_int(
        text,
        "model_auto_compact_token_limit",
    )
    state = _read_context_guard_state(state_path) if state_path is not None else None
    entry = (state or {}).get("config", {})
    managed_values = entry.get("managed", {}) if isinstance(entry, dict) else {}
    if not isinstance(managed_values, dict):
        managed_values = {}
    explicit_enabled = entry.get("enabled") if isinstance(entry, dict) else None
    if isinstance(explicit_enabled, bool):
        enabled = explicit_enabled
    else:
        # Version 1 state recorded only the values that it installed.  Treat
        # a missing value as a migrated, enabled guard after the new overlay
        # removes the old global projection; an explicit disable deletes state.
        enabled = bool(managed_values) and all(
            top_level_value(text, key) in {None, managed_values.get(key)}
            for key in CONTEXT_GUARD_KEYS
        )
    current_values = {key: top_level_value(text, key) for key in CONTEXT_GUARD_KEYS}
    global_override_conflict = any(
        value is not None
        and value != managed_values.get(key)
        for key, value in current_values.items()
    )
    return {
        "enabled": enabled,
        "model_context_window": context_window,
        "model_auto_compact_token_limit": auto_compact_token_limit,
        "global_override_conflict": global_override_conflict,
    }


def _context_guard_previous_values(text: str) -> dict[str, str | None]:
    return {key: top_level_value(text, key) for key in CONTEXT_GUARD_KEYS}


def _context_guard_managed_values(text: str) -> dict[str, str | None]:
    """Read legacy guard values only from the CodexHub-owned marker."""

    match = re.search(
        rf"(?ms)^\s*{re.escape(MARKER_BEGIN)}\s*$.*?^\s*{re.escape(MARKER_END)}\s*$",
        text,
    )
    if match is None:
        return {key: None for key in CONTEXT_GUARD_KEYS}
    marker_text = match.group(0)
    return {key: top_level_value(marker_text, key) for key in CONTEXT_GUARD_KEYS}


def _context_guard_default_state_path(backup_path: Path) -> Path:
    """Return the state file colocated with a managed Codex backup."""

    return backup_path.parent / "context-guard-state.json"


def _normalized_context_guard_values(payload: object) -> dict[str, str | None]:
    if not isinstance(payload, dict):
        return {key: None for key in CONTEXT_GUARD_KEYS}
    return {
        key: payload.get(key) if isinstance(payload.get(key), str) else None
        for key in CONTEXT_GUARD_KEYS
    }


def _read_context_guard_state(
    state_path: Path,
) -> dict[str, dict[str, object]] | None:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    entries: dict[str, dict[str, object]] = {}
    for target, values in payload.items():
        if target not in {"config", "backup"} or not isinstance(values, dict):
            continue
        if "previous" in values or "managed" in values:
            entry: dict[str, object] = {
                "previous": _normalized_context_guard_values(values.get("previous")),
                "managed": _normalized_context_guard_values(values.get("managed")),
            }
            if isinstance(values.get("enabled"), bool):
                entry["enabled"] = values["enabled"]
            entries[target] = entry
        else:
            # Older state cannot identify the dynamic value it installed.  Do
            # not remove a potentially user-managed value during disable.
            entries[target] = {
                "previous": _normalized_context_guard_values(values),
                "managed": {key: None for key in CONTEXT_GUARD_KEYS},
            }
    return entries or None


def _migrate_legacy_context_guard_values_for_backups(
    config_path: Path,
    backup_paths: list[Path],
    state_path: Path | None = None,
) -> None:
    """Remove only the old CodexHub-owned global context projection.

    Version 1 wrote the two context keys either inside the managed overlay or
    as top-level values recorded in ``context-guard-state.json``.  The current
    ownership boundary is catalog-scoped, so those values must not survive a
    route switch.  A value is removed only while it still equals the recorded
    managed value; an edit made after the old guard was enabled remains a
    user-owned global override and is surfaced by ``context_guard_status``.
    """

    default_backup_path = backup_paths[0] if backup_paths else config_path
    state_path = state_path or _context_guard_default_state_path(default_backup_path)
    state = _read_context_guard_state(state_path) or {}
    state_changed = False
    targets: list[tuple[str, Path]] = [("config", config_path)]
    targets.extend(("backup", path) for path in backup_paths)
    state_targets_to_clear: set[str] = set()

    for target, path in targets:
        if not path.exists():
            continue
        text = read_text_preserving_newlines(path)
        entry = state.get(target)
        managed = (
            _normalized_context_guard_values(entry.get("managed"))
            if isinstance(entry, dict)
            else {key: None for key in CONTEXT_GUARD_KEYS}
        )
        previous = (
            _normalized_context_guard_values(entry.get("previous"))
            if isinstance(entry, dict)
            else {key: None for key in CONTEXT_GUARD_KEYS}
        )
        marker_managed = _context_guard_managed_values(text)
        updates: dict[str, str | None] = {}
        for key in CONTEXT_GUARD_KEYS:
            known_value = managed.get(key) or marker_managed.get(key)
            if known_value is not None and top_level_value(text, key) == known_value:
                # The old state snapshot is the user's value from before the
                # CodexHub projection. Restore it when it is still untouched;
                # only remove the key when the snapshot was absent.
                updates[key] = previous.get(key)
        if updates:
            atomic_write_text(
                path,
                set_top_level_values(text, updates),
                encoding="utf-8",
            )

        if isinstance(entry, dict) and any(value is not None for value in managed.values()):
            # Defer clearing the shared backup entry until every channel
            # backup has been inspected.  Stable and Beta can both point at
            # this one state file; clearing it after the first backup would
            # hide ownership evidence from the next markerless backup.
            state_targets_to_clear.add(target)

    for target in state_targets_to_clear:
        entry = state.get(target)
        if not isinstance(entry, dict):
            continue
        updated_entry = dict(entry)
        updated_entry["managed"] = {key: None for key in CONTEXT_GUARD_KEYS}
        updated_entry.setdefault("enabled", True)
        state[target] = updated_entry
        state_changed = True

    if state_changed:
        atomic_write_text(
            state_path,
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _migrate_legacy_context_guard_values(
    config_path: Path,
    backup_path: Path,
    state_path: Path | None = None,
) -> None:
    _migrate_legacy_context_guard_values_for_backups(
        config_path,
        [backup_path],
        state_path,
    )


def _preserve_context_guard_overrides(
    current_text: str,
    restored_text: str,
    state_path: Path | None = None,
) -> str:
    """Carry user-owned global context overrides across a backup restore.

    The active overlay can be edited while CodexHub is connected.  The backup
    is intentionally a snapshot from before activation, so blindly restoring
    it would silently discard a later user-owned context override.  Legacy
    CodexHub-managed values have already been removed by migration; values
    still present in the active config are therefore safe to preserve.  When a
    state snapshot proves that a user-owned value existed before the overlay,
    an explicit deletion while connected is carried through the restore too.
    """

    state = _read_context_guard_state(state_path) if state_path is not None else None
    entry = (state or {}).get("config", {})
    previous = (
        _normalized_context_guard_values(entry.get("previous"))
        if isinstance(entry, dict)
        else {key: None for key in CONTEXT_GUARD_KEYS}
    )
    overrides: dict[str, str | None] = {}
    for key in CONTEXT_GUARD_KEYS:
        current_value = top_level_value(current_text, key)
        restored_value = top_level_value(restored_text, key)
        if current_value is not None and restored_value != current_value:
            overrides[key] = current_value
        elif (
            current_value is None
            and previous.get(key) is not None
            and restored_value == previous.get(key)
        ):
            overrides[key] = None
    return set_top_level_values(restored_text, overrides) if overrides else restored_text


def _safe_official_disable_updates(
    text: str,
    previous: dict[str, str | None],
    managed: dict[str, str | None],
    safe_budget: tuple[int, int],
) -> dict[str, str]:
    """Restore only a still-safe Official override when disabling the guard.

    A disabled convenience switch must not revive a larger pre-guard Codex
    runtime value after the current Direct Official authority lowered it.  A
    post-enable user edit is retained only when it is also within the current
    safe budget; otherwise the authoritative safe values remain in place.
    """

    def candidate(key: str) -> str | None:
        current = top_level_value(text, key)
        if managed.get(key) is not None and current == managed.get(key):
            return previous.get(key)
        return current

    context_cap, compact_cap = safe_budget
    requested_context = _positive_toml_int(candidate("model_context_window"))
    context_window = (
        requested_context
        if requested_context is not None and requested_context <= context_cap
        else context_cap
    )
    requested_compact = _positive_toml_int(candidate("model_auto_compact_token_limit"))
    auto_compact_token_limit = (
        requested_compact
        if requested_compact is not None
        and requested_compact <= min(compact_cap, context_window)
        else min(compact_cap, context_window)
    )
    return {
        "model_context_window": str(context_window),
        "model_auto_compact_token_limit": str(auto_compact_token_limit),
    }


def set_context_guard(
    config_path: Path,
    backup_path: Path,
    state_path: Path,
    *,
    enabled: bool,
    catalog_path: Path | None = None,
) -> dict[str, int | bool | None]:
    selected_model = top_level_value(
        read_text_preserving_newlines(config_path) if config_path.exists() else "",
        "model",
    )
    selected_official = _selected_model_is_official(selected_model)
    safe_official_budget = (
        _selected_official_context_budget(catalog_path, selected_model)
        if selected_official and catalog_path is not None
        else None
    )
    if selected_official and safe_official_budget is None:
        raise ValueError("safe current Official context budget is unavailable")
    # Validate the requested Official budget before migrating legacy files.
    # A failed enable/disable must not partially rewrite config or state while
    # the Rust caller leaves the Gateway setting unchanged.
    _migrate_legacy_context_guard_values(config_path, backup_path, state_path)
    target_paths = {"config": config_path}
    if backup_path.exists():
        target_paths["backup"] = backup_path
    if enabled:
        state = _read_context_guard_state(state_path) or {}
        for target, path in target_paths.items():
            text = read_text_preserving_newlines(path) if path.exists() else ""
            entry = state.get(target) if isinstance(state.get(target), dict) else {}
            previous = _normalized_context_guard_values(entry.get("previous"))
            managed = _normalized_context_guard_values(entry.get("managed"))
            if not entry:
                marker_managed = _context_guard_managed_values(text)
                previous_source = text
                if target == "config" and any(marker_managed.values()) and backup_path.exists():
                    previous_source = read_text_preserving_newlines(backup_path)
                previous = _context_guard_previous_values(previous_source)
                # Only values inside the old CodexHub marker are known to be
                # managed.  Unmarked top-level values remain user-owned.
                managed = marker_managed
            removable = {
                key: value
                for key, value in managed.items()
                if value is not None and top_level_value(text, key) == value
            }
            if removable:
                atomic_write_text(
                    path,
                    set_top_level_values(text, {key: None for key in removable}),
                    encoding="utf-8",
                )
            state[target] = {
                "enabled": True,
                "previous": previous,
                "managed": managed,
            }
        atomic_write_text(
            state_path,
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        state_by_target = _read_context_guard_state(state_path) or {}
        for target, path in target_paths.items():
            if not path.exists():
                continue
            entry = state_by_target.get(target, {})
            previous = entry.get("previous", {key: None for key in CONTEXT_GUARD_KEYS})
            managed = entry.get("managed", {key: None for key in CONTEXT_GUARD_KEYS})
            text = read_text_preserving_newlines(path)
            updates = {
                key: previous.get(key)
                for key, managed_value in managed.items()
                if managed_value is not None and top_level_value(text, key) == managed_value
            }
            if updates:
                atomic_write_text(path, set_top_level_values(text, updates), encoding="utf-8")
        state_path.unlink(missing_ok=True)

    return context_guard_status(config_path, state_path)


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
    managed_catalog: bool
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
    managed_catalog = is_managed_catalog_path(top_level_value(text, "model_catalog_json")) or _overlay_marks_managed_catalog(text)
    return UnifiedConfigState(
        provider_id=provider_id,
        custom_section=custom_section,
        exact_unified=custom_section == unified_official_provider_values(),
        managed_gateway=is_managed_gateway_provider(custom_section),
        managed_catalog=managed_catalog,
        stale_catalog=managed_catalog,
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

    keys_to_strip = {"model_provider", "openai_base_url"}
    if state.managed_catalog:
        keys_to_strip.add("model_catalog_json")
    updated = strip_top_level_keys(text, keys_to_strip)
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
    managed_catalog = is_managed_catalog_path(top_level_value(text, "model_catalog_json")) or _overlay_marks_managed_catalog(text)
    custom_section = section_key_values(text, f"model_providers.{PROXY_PROVIDER_ID}")
    if custom_section != unified_official_provider_values() and not is_managed_gateway_provider(custom_section):
        return text
    stripped = strip_section(text, f"model_providers.{PROXY_PROVIDER_ID}")
    keys_to_strip = {"model_provider", "openai_base_url"}
    if managed_catalog:
        keys_to_strip.add("model_catalog_json")
    stripped = strip_top_level_keys(stripped, keys_to_strip)
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


def _positive_catalog_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _selected_official_context_budget(
    catalog_path: Path | None,
    selected_model: str | None,
) -> tuple[int, int] | None:
    """Return the selected Official model's safe Codex configuration cap.

    The generated catalog is the cross-process handoff for the resolver.  A
    context value larger than the conservative fallback is accepted only when
    the catalog records a fresh Direct Official decision.  An explicit
    third-party selection deliberately receives no new global Codex cap.
    """

    if catalog_path is None:
        return None
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        payload = {}

    models = payload.get("models") if isinstance(payload, dict) else None
    official_budgets: dict[str, dict[str, object]] = {}
    if isinstance(models, list):
        for model in models:
            if not isinstance(model, dict):
                continue
            slug = model.get("slug")
            if not isinstance(slug, str):
                continue
            metadata = model.get("codex_proxy_metadata")
            if not isinstance(metadata, dict):
                continue
            if metadata.get("provider") != "openai" or metadata.get("upstream_name") != "official":
                continue
            budget = metadata.get("official_context_budget")
            if isinstance(budget, dict):
                official_budgets[slug] = budget

    normalized_selected_model = selected_model.strip() if isinstance(selected_model, str) else ""
    if normalized_selected_model.startswith("openai/"):
        normalized_selected_model = normalized_selected_model.removeprefix("openai/")

    budget: dict[str, object] | None = None
    if normalized_selected_model:
        budget = official_budgets.get(normalized_selected_model)
        if budget is None:
            return None
    elif official_budgets:
        budget = next(iter(official_budgets.values()))

    if budget is None:
        return None

    source = budget.get("source")
    freshness = budget.get("freshness")
    if source in {
        CURRENT_DIRECT_OFFICIAL_SOURCE,
        FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE,
    }:
        if freshness != "fresh":
            return None
    elif source != DEGRADED_LAST_KNOWN_OFFICIAL_SOURCE:
        return None

    context_window = _positive_catalog_int(budget.get("model_context_window"))
    if context_window is None:
        context_window = _positive_catalog_int(budget.get("context_window"))
    if context_window is None:
        return None

    effective_window = _positive_catalog_int(budget.get("effective_context_window"))
    if effective_window is not None:
        if effective_window > context_window:
            return None
    else:
        effective_percent = _positive_catalog_int(
            budget.get("effective_context_window_percent")
        )
        if effective_percent is None or effective_percent > 100:
            return None
        effective_window = max(1, context_window * effective_percent // 100)
    auto_compact_token_limit = _positive_catalog_int(
        budget.get("model_auto_compact_token_limit")
    )
    if auto_compact_token_limit is None:
        auto_compact_token_limit = context_window * NATIVE_AUTO_COMPACT_PERCENT // 100

    return (
        context_window,
        min(auto_compact_token_limit, effective_window),
    )


def _selected_model_is_official(selected_model: str | None) -> bool:
    if not isinstance(selected_model, str):
        return False
    return selected_model.strip().removeprefix("openai/").startswith("gpt-")


def build_overlay(
    catalog_value: str | None,
    owner: str,
    context_budget: tuple[int, int] | None = None,
    catalog_owned: bool = True,
) -> str:
    # Context limits are model-scoped catalog metadata.  The old global
    # projection is intentionally ignored even when callers still pass the
    # legacy budget argument.
    lines = [
        MARKER_BEGIN,
        f"# owner = {owner}",
        f'model_provider = "{PROXY_PROVIDER_ID}"',
    ]
    if catalog_value is not None and catalog_owned:
        digest = _catalog_owner_marker_digest(catalog_value, create=True)
        if digest is not None:
            lines.append(f"{CATALOG_OWNER_MARKER}:{digest}")
    if catalog_value is not None:
        lines.append(f"model_catalog_json = {toml_literal(catalog_value)}")
    return "\n".join([*lines, MARKER_END, ""])


def overlay_owner(text: str) -> str | None:
    match = re.search(r"(?m)^\s*# owner = (release|beta)\s*$", text)
    return match.group(1) if match else None


def read_text_preserving_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def takeover_metadata_path(backup_path: Path) -> Path:
    return backup_path.with_name(f"{backup_path.name}.takeover.json")


def write_takeover_metadata(backup_path: Path, takeover_owner: str, original_owner: str | None) -> None:
    metadata = {
        "version": 1,
        "takeover_owner": takeover_owner,
        "original_owner": original_owner,
    }
    atomic_write_text(
        takeover_metadata_path(backup_path),
        json.dumps(metadata, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def is_active_takeover_backup(config_text: str, backup_text: str, backup_path: Path) -> bool:
    metadata_path = takeover_metadata_path(backup_path)
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if metadata.get("version") != 1:
        return False
    takeover_owner = metadata.get("takeover_owner")
    original_owner = metadata.get("original_owner")
    if takeover_owner not in {"release", "beta"}:
        return False
    if original_owner not in {None, "release", "beta"}:
        return False
    if original_owner == takeover_owner:
        return False
    return overlay_owner(config_text) == takeover_owner and overlay_owner(backup_text) == original_owner


def build_provider_section(base_url: str, gateway_key: str) -> str:
    return "\n".join(
        [
            f"[model_providers.{PROXY_PROVIDER_ID}]",
            f'name = "{PROXY_PROVIDER_NAME}"',
            f"base_url = {toml_literal(base_url.rstrip('/') + '/v1')}",
            'wire_api = "responses"',
            "requires_openai_auth = true",
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
    catalog_path: Path | None,
    base_url: str,
    owner: str = "release",
    takeover: bool = False,
    gateway_key: str = "codexhub-proxy",
    context_guard_state_path: Path | None = None,
) -> None:
    if owner not in {"release", "beta"}:
        raise ValueError(f"unsupported CodexHub owner: {owner}")
    _migrate_legacy_context_guard_values(
        config_path,
        backup_path,
        context_guard_state_path,
    )
    original = read_text_preserving_newlines(config_path) if config_path.exists() else ""
    custom_section = section_key_values(original, f"model_providers.{PROXY_PROVIDER_ID}")
    if custom_section is not None and not (
        custom_section == unified_official_provider_values() or is_managed_gateway_provider(custom_section)
    ):
        raise ValueError("refusing to overwrite unknown custom provider")
    cleaned = strip_marked_overlay(original)
    active_owner = overlay_owner(original)
    cross_owner_takeover = takeover and active_owner != owner
    if active_owner != owner or not backup_path.exists():
        backup = original if cross_owner_takeover else (cleaned if cleaned != original else original)
        atomic_write_text(backup_path, backup, encoding="utf-8")
        metadata_path = takeover_metadata_path(backup_path)
        if cross_owner_takeover:
            write_takeover_metadata(backup_path, owner, active_owner)
        elif metadata_path.exists():
            metadata_path.unlink()

    for section in STALE_PROXY_PROVIDER_SECTIONS:
        cleaned = strip_section(cleaned, section)
    existing_catalog_value = top_level_value(original, "model_catalog_json")
    catalog_value = (
        existing_catalog_value
        if existing_catalog_value is not None
        and not is_managed_catalog_path(existing_catalog_value, catalog_path)
        else (
            catalog_config_value(config_path, catalog_path)
            if catalog_path is not None
            else existing_catalog_value
        )
    )
    catalog_owned = catalog_value is not None and is_managed_catalog_path(catalog_value, catalog_path)
    cleaned = strip_top_level_keys(cleaned)
    cleaned = set_feature_flags(cleaned, PROXY_FEATURE_FLAGS)
    updated = build_overlay(
        catalog_value,
        owner,
        catalog_owned=catalog_owned,
    ) + cleaned.lstrip()
    updated = insert_provider_section(updated, build_provider_section(base_url, gateway_key))
    atomic_write_text(config_path, updated, encoding="utf-8")


def _preserve_user_catalog_path(current: str, restored: str) -> str:
    current_value = top_level_value(current, "model_catalog_json")
    if not current_value or is_managed_catalog_path(current_value) or _overlay_marks_managed_catalog(current):
        return restored
    if current_value == top_level_value(restored, "model_catalog_json"):
        return restored
    return set_top_level_values(restored, {"model_catalog_json": toml_literal(current_value)})


def _cleanup_after_committed_restore(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        # The config write is already atomic and durable. Cleanup cannot turn
        # that committed route into a failed switch; leave the artifact for a
        # later idempotent cleanup and emit a bounded, path-free warning.
        print(
            f"warning: route committed; backup cleanup deferred ({type(error).__name__})",
            file=sys.stderr,
        )


def restore_overlay(
    config_path: Path,
    backup_path: Path,
    unified_history: bool = False,
    context_guard_state_path: Path | None = None,
) -> str:
    _migrate_legacy_context_guard_values(
        config_path,
        backup_path,
        context_guard_state_path,
    )
    if backup_path.exists():
        restored = read_text_preserving_newlines(backup_path)
        current = read_text_preserving_newlines(config_path) if config_path.exists() else ""
        restored = _preserve_context_guard_overrides(
            current,
            restored,
            context_guard_state_path,
        )
        restored = _preserve_user_catalog_path(current, restored)
        restore_from_backup = True
        if is_active_takeover_backup(current, restored, backup_path):
            restored_owner = overlay_owner(restored)
            if not unified_history or restored_owner is not None:
                atomic_write_text(config_path, restored, encoding="utf-8")
                _cleanup_after_committed_restore(backup_path)
                _cleanup_after_committed_restore(takeover_metadata_path(backup_path))
                return "restored_takeover_backup"
    elif config_path.exists():
        restored = strip_marked_overlay(config_path.read_text(encoding="utf-8"))
        restore_from_backup = False
    else:
        restored = ""
        restore_from_backup = False

    if unified_history:
        restored, status = inject_unified_history_config(restored)
    else:
        restored = strip_unified_history_config(restored)
        status = "disabled"

    if restored or config_path.exists() or unified_history:
        atomic_write_text(config_path, restored, encoding="utf-8")
    if restore_from_backup:
        _cleanup_after_committed_restore(backup_path)
        _cleanup_after_committed_restore(takeover_metadata_path(backup_path))
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply or restore the Codex proxy session config overlay.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--config", required=True, type=Path)
    apply_parser.add_argument("--backup", required=True, type=Path)
    apply_parser.add_argument("--catalog", type=Path)
    apply_parser.add_argument("--base-url", required=True)
    apply_parser.add_argument("--owner", choices=["release", "beta"], default="release")
    apply_parser.add_argument("--takeover", action="store_true")
    apply_parser.add_argument("--gateway-key", default="codexhub-proxy")
    apply_parser.add_argument("--context-guard-state", type=Path)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--config", required=True, type=Path)
    restore_parser.add_argument("--backup", required=True, type=Path)
    restore_parser.add_argument("--unified-history", action="store_true")
    restore_parser.add_argument("--context-guard-state", type=Path)

    inspect_parser = subparsers.add_parser("inspect-unified")
    inspect_parser.add_argument("--config", required=True, type=Path)
    inspect_parser.add_argument("--target", choices=["unified", "separated"], default="unified")

    context_status_parser = subparsers.add_parser("context-guard-status")
    context_status_parser.add_argument("--config", required=True, type=Path)
    context_status_parser.add_argument("--state", type=Path)

    context_set_parser = subparsers.add_parser("context-guard-set")
    context_set_parser.add_argument("--config", required=True, type=Path)
    context_set_parser.add_argument("--backup", required=True, type=Path)
    context_set_parser.add_argument("--state", required=True, type=Path)
    context_set_parser.add_argument("--catalog", required=True, type=Path)
    context_set_parser.add_argument("--enabled", required=True, choices=("true", "false"))

    migrate_context_parser = subparsers.add_parser("migrate-context-guard")
    migrate_context_parser.add_argument("--config", required=True, type=Path)
    migrate_context_parser.add_argument("--backup", required=True, type=Path, action="append")
    migrate_context_parser.add_argument("--context-guard-state", required=True, type=Path)

    args = parser.parse_args(argv)
    if args.command == "apply":
        apply_overlay(
            args.config,
            args.backup,
            args.catalog,
            args.base_url,
            args.owner,
            args.takeover,
            args.gateway_key,
            args.context_guard_state,
        )
    elif args.command == "restore":
        status = restore_overlay(
            args.config,
            args.backup,
            args.unified_history,
            args.context_guard_state,
        )
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
    elif args.command == "context-guard-status":
        print(json.dumps(context_guard_status(args.config, args.state), ensure_ascii=False))
    elif args.command == "context-guard-set":
        print(
            json.dumps(
                set_context_guard(
                    args.config,
                    args.backup,
                    args.state,
                    enabled=args.enabled == "true",
                    catalog_path=args.catalog,
                ),
                ensure_ascii=False,
            )
        )
    elif args.command == "migrate-context-guard":
        _migrate_legacy_context_guard_values_for_backups(
            args.config,
            args.backup,
            args.context_guard_state,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
