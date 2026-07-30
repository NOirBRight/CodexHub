from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path

from atomic_io import atomic_write_text, file_lock_for
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
TAKEOVER_CLEANUP_STATUSES = {
    "already_unified",
    "conflicting_custom_provider",
    "explicit_model_provider",
    "injected",
    "interrupted_takeover_discarded",
    "repaired_unified",
    "replaced_managed_gateway",
    "restored_takeover_backup",
}
UNOWNED_TAKEOVER_CLEANUP_STATUSES = TAKEOVER_CLEANUP_STATUSES - {
    "interrupted_takeover_discarded",
    "restored_takeover_backup",
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
    managed_values = (state or {}).get("config", {}).get("managed", {})
    enabled = bool(managed_values) and all(
        managed_values.get(key) is not None
        and top_level_value(text, key) == managed_values[key]
        for key in CONTEXT_GUARD_KEYS
    )
    return {
        "enabled": enabled,
        "model_context_window": context_window,
        "model_auto_compact_token_limit": auto_compact_token_limit,
    }


def _context_guard_previous_values(text: str) -> dict[str, str | None]:
    return {key: top_level_value(text, key) for key in CONTEXT_GUARD_KEYS}


def _normalized_context_guard_values(payload: object) -> dict[str, str | None]:
    if not isinstance(payload, dict):
        return {key: None for key in CONTEXT_GUARD_KEYS}
    return {
        key: payload.get(key) if isinstance(payload.get(key), str) else None
        for key in CONTEXT_GUARD_KEYS
    }


def _read_context_guard_state(
    state_path: Path,
) -> dict[str, dict[str, dict[str, str | None]]] | None:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    entries: dict[str, dict[str, dict[str, str | None]]] = {}
    for target, values in payload.items():
        if target not in {"config", "backup"} or not isinstance(values, dict):
            continue
        if "previous" in values or "managed" in values:
            entries[target] = {
                "previous": _normalized_context_guard_values(values.get("previous")),
                "managed": _normalized_context_guard_values(values.get("managed")),
            }
        else:
            # Older state cannot identify the dynamic value it installed.  Do
            # not remove a potentially user-managed value during disable.
            entries[target] = {
                "previous": _normalized_context_guard_values(values),
                "managed": {key: None for key in CONTEXT_GUARD_KEYS},
            }
    return entries or None


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
    config_path = config_path.resolve(strict=False)
    backup_path = backup_path.resolve(strict=False)
    state_path = state_path.resolve(strict=False)
    _validate_managed_write_paths(config_path, backup_path, state_path)
    with file_lock_for(overlay_lifecycle_lock_target(config_path)):
        return _set_context_guard_locked(
            config_path,
            backup_path,
            state_path,
            enabled=enabled,
            catalog_path=catalog_path,
        )


def _set_context_guard_locked(
    config_path: Path,
    backup_path: Path,
    state_path: Path,
    *,
    enabled: bool,
    catalog_path: Path | None = None,
) -> dict[str, int | bool | None]:
    _, takeover_state = _prepare_managed_config_write(
        config_path,
        backup_path,
        operation="context guard update",
    )
    if takeover_state == "interrupted":
        raise RuntimeError(
            "refusing context guard update: interrupted takeover recovery is pending"
        )
    target_paths = {"config": config_path}
    if backup_path.exists():
        target_paths["backup"] = backup_path
    if enabled:
        selected_model = top_level_value(
            read_text_preserving_newlines(config_path) if config_path.exists() else "",
            "model",
        )
        budget = (
            _selected_official_context_budget(catalog_path, selected_model)
            if catalog_path is not None
            else None
        )
        if budget is None:
            selected = selected_model.strip() if isinstance(selected_model, str) else ""
            if selected.removeprefix("openai/").startswith("gpt-"):
                raise ValueError("safe current Official context budget is unavailable")
            return context_guard_status(config_path, state_path)

        managed_values = {
            "model_context_window": str(budget[0]),
            "model_auto_compact_token_limit": str(budget[1]),
        }
        state = _read_context_guard_state(state_path) or {}
        for target, path in target_paths.items():
            entry = state.get(target)
            if entry is None:
                entry = {
                    "previous": _context_guard_previous_values(
                        read_text_preserving_newlines(path) if path.exists() else ""
                    ),
                    "managed": {},
                }
                state[target] = entry
            entry["managed"] = dict(managed_values)
            text = read_text_preserving_newlines(path) if path.exists() else ""
            atomic_write_text(path, set_top_level_values(text, managed_values), encoding="utf-8")
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
            selected_model = top_level_value(text, "model")
            selected_official = _selected_model_is_official(selected_model)
            safe_official_budget = (
                _selected_official_context_budget(catalog_path, selected_model)
                if selected_official
                else None
            )
            if selected_official and safe_official_budget is None:
                raise ValueError("safe current Official context budget is unavailable")
            if safe_official_budget is not None:
                updates = _safe_official_disable_updates(
                    text,
                    previous,
                    managed,
                    safe_official_budget,
                )
            else:
                updates = {
                    key: previous.get(key)
                    for key, managed_value in managed.items()
                    if managed_value is not None and top_level_value(text, key) == managed_value
                }
            if updates:
                atomic_write_text(path, set_top_level_values(text, updates), encoding="utf-8")
        state_path.unlink(missing_ok=True)

    completion_read = read_takeover_completion(backup_path)
    if completion_read.state is TakeoverMetadataState.INVALID:
        raise RuntimeError("refusing context guard update: takeover completion is invalid")
    if completion_read.receipt is not None:
        completion_text_path = backup_path if backup_path.exists() else config_path
        if not completion_text_path.exists():
            raise RuntimeError(
                "refusing context guard update: completed config is missing or diverged"
            )
        _rebase_takeover_completion(
            backup_path,
            completion_read.receipt,
            read_text_preserving_newlines(completion_text_path),
        )

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
) -> str:
    lines = [
        MARKER_BEGIN,
        f"# owner = {owner}",
        f'model_provider = "{PROXY_PROVIDER_ID}"',
    ]
    if catalog_value is not None:
        lines.append(f"model_catalog_json = {toml_literal(catalog_value)}")
    if context_budget is not None:
        context_window, auto_compact_token_limit = context_budget
        lines.extend(
            [
                f"model_context_window = {context_window}",
                f"model_auto_compact_token_limit = {auto_compact_token_limit}",
            ]
        )
    return "\n".join([*lines, MARKER_END, ""])


def overlay_owner(text: str) -> str | None:
    match = re.search(r"(?m)^\s*# owner = (release|beta)\s*$", text)
    return match.group(1) if match else None


def read_text_preserving_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def takeover_metadata_path(backup_path: Path) -> Path:
    return backup_path.with_name(f"{backup_path.name}.takeover.json")


def takeover_completion_path(backup_path: Path) -> Path:
    return backup_path.with_name(f"{backup_path.name}.takeover.completed.json")


@dataclass(frozen=True)
class TakeoverMetadata:
    takeover_owner: str
    original_owner: str | None
    cleanup_source_sha256: str | None
    cleanup_recovery_sha256: str | None
    cleanup_final_sha256: str | None
    cleanup_status: str | None


class TakeoverMetadataState(Enum):
    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True)
class TakeoverMetadataRead:
    state: TakeoverMetadataState
    metadata: TakeoverMetadata | None = None


@dataclass(frozen=True)
class TakeoverCompletionReceipt:
    takeover_owner: str
    original_owner: str | None
    final_sha256: str
    same_owner_backup_sha256: str
    status: str


@dataclass(frozen=True)
class TakeoverCompletionRead:
    state: TakeoverMetadataState
    receipt: TakeoverCompletionReceipt | None = None


def takeover_text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate takeover state key: {key}")
        result[key] = value
    return result


def write_takeover_metadata(
    backup_path: Path,
    takeover_owner: str,
    original_owner: str | None,
    *,
    cleanup_source_sha256: str | None = None,
    cleanup_recovery_sha256: str | None = None,
    cleanup_final_sha256: str | None = None,
    cleanup_status: str | None = None,
) -> None:
    cleanup_values = (
        cleanup_source_sha256,
        cleanup_recovery_sha256,
        cleanup_final_sha256,
        cleanup_status,
    )
    if any(value is None for value in cleanup_values) and any(
        value is not None for value in cleanup_values
    ):
        raise ValueError("takeover cleanup digests and status must be written together")
    if cleanup_status is not None and cleanup_status not in TAKEOVER_CLEANUP_STATUSES:
        raise ValueError("unsupported takeover cleanup status")
    metadata = {
        "version": 1,
        "takeover_owner": takeover_owner,
        "original_owner": original_owner,
    }
    if cleanup_source_sha256 is not None:
        metadata["cleanup_source_sha256"] = cleanup_source_sha256
        metadata["cleanup_recovery_sha256"] = cleanup_recovery_sha256
        metadata["cleanup_final_sha256"] = cleanup_final_sha256
        metadata["cleanup_status"] = cleanup_status
    atomic_write_text(
        takeover_metadata_path(backup_path),
        json.dumps(metadata, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_takeover_metadata(backup_path: Path) -> TakeoverMetadataRead:
    metadata_path = takeover_metadata_path(backup_path)
    try:
        metadata = json.loads(
            metadata_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except FileNotFoundError:
        return TakeoverMetadataRead(TakeoverMetadataState.ABSENT)
    except (OSError, ValueError, TypeError, RecursionError):
        return TakeoverMetadataRead(TakeoverMetadataState.INVALID)
    if not isinstance(metadata, dict):
        return TakeoverMetadataRead(TakeoverMetadataState.INVALID)
    base_keys = {"version", "takeover_owner", "original_owner"}
    cleanup_keys = {
        "cleanup_source_sha256",
        "cleanup_recovery_sha256",
        "cleanup_final_sha256",
        "cleanup_status",
    }
    metadata_keys = frozenset(metadata)
    if metadata_keys not in {frozenset(base_keys), frozenset(base_keys | cleanup_keys)}:
        return TakeoverMetadataRead(TakeoverMetadataState.INVALID)
    version = metadata.get("version")
    if type(version) is not int or version != 1:
        return TakeoverMetadataRead(TakeoverMetadataState.INVALID)
    takeover_owner = metadata.get("takeover_owner")
    original_owner = metadata.get("original_owner")
    if not isinstance(takeover_owner, str) or takeover_owner not in {"release", "beta"}:
        return TakeoverMetadataRead(TakeoverMetadataState.INVALID)
    if original_owner is not None and (
        not isinstance(original_owner, str)
        or original_owner not in {"release", "beta"}
    ):
        return TakeoverMetadataRead(TakeoverMetadataState.INVALID)
    if original_owner == takeover_owner:
        return TakeoverMetadataRead(TakeoverMetadataState.INVALID)
    cleanup_source_sha256 = metadata.get("cleanup_source_sha256")
    cleanup_recovery_sha256 = metadata.get("cleanup_recovery_sha256")
    cleanup_final_sha256 = metadata.get("cleanup_final_sha256")
    cleanup_status = metadata.get("cleanup_status")
    cleanup_values = (
        cleanup_source_sha256,
        cleanup_recovery_sha256,
        cleanup_final_sha256,
        cleanup_status,
    )
    has_cleanup_fields = metadata_keys == frozenset(base_keys | cleanup_keys)
    if has_cleanup_fields and any(value is None for value in cleanup_values):
        return TakeoverMetadataRead(TakeoverMetadataState.INVALID)
    if not has_cleanup_fields and any(value is not None for value in cleanup_values):
        return TakeoverMetadataRead(TakeoverMetadataState.INVALID)
    digests = (
        cleanup_source_sha256,
        cleanup_recovery_sha256,
        cleanup_final_sha256,
    )
    if cleanup_source_sha256 is not None and (
        any(
            not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for digest in digests
        )
        or not isinstance(cleanup_status, str)
        or cleanup_status not in TAKEOVER_CLEANUP_STATUSES
    ):
        return TakeoverMetadataRead(TakeoverMetadataState.INVALID)
    return TakeoverMetadataRead(
        TakeoverMetadataState.VALID,
        TakeoverMetadata(
            takeover_owner=takeover_owner,
            original_owner=original_owner,
            cleanup_source_sha256=cleanup_source_sha256,
            cleanup_recovery_sha256=cleanup_recovery_sha256,
            cleanup_final_sha256=cleanup_final_sha256,
            cleanup_status=cleanup_status,
        ),
    )


def write_takeover_completion_receipt(
    backup_path: Path,
    receipt: TakeoverCompletionReceipt,
) -> None:
    payload = {
        "version": 1,
        "takeover_owner": receipt.takeover_owner,
        "original_owner": receipt.original_owner,
        "final_sha256": receipt.final_sha256,
        "same_owner_backup_sha256": receipt.same_owner_backup_sha256,
        "status": receipt.status,
    }
    atomic_write_text(
        takeover_completion_path(backup_path),
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_takeover_completion(backup_path: Path) -> TakeoverCompletionRead:
    try:
        receipt = json.loads(
            takeover_completion_path(backup_path).read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except FileNotFoundError:
        return TakeoverCompletionRead(TakeoverMetadataState.ABSENT)
    except (OSError, ValueError, TypeError, RecursionError):
        return TakeoverCompletionRead(TakeoverMetadataState.INVALID)
    expected_keys = {
        "version",
        "takeover_owner",
        "original_owner",
        "final_sha256",
        "same_owner_backup_sha256",
        "status",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_keys:
        return TakeoverCompletionRead(TakeoverMetadataState.INVALID)
    if type(receipt.get("version")) is not int or receipt["version"] != 1:
        return TakeoverCompletionRead(TakeoverMetadataState.INVALID)
    takeover_owner = receipt.get("takeover_owner")
    original_owner = receipt.get("original_owner")
    final_sha256 = receipt.get("final_sha256")
    same_owner_backup_sha256 = receipt.get("same_owner_backup_sha256")
    status = receipt.get("status")
    if not isinstance(takeover_owner, str) or takeover_owner not in {"release", "beta"}:
        return TakeoverCompletionRead(TakeoverMetadataState.INVALID)
    if original_owner is not None and (
        not isinstance(original_owner, str)
        or original_owner not in {"release", "beta"}
    ):
        return TakeoverCompletionRead(TakeoverMetadataState.INVALID)
    if original_owner == takeover_owner:
        return TakeoverCompletionRead(TakeoverMetadataState.INVALID)
    if (
        not isinstance(final_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", final_sha256) is None
        or not isinstance(same_owner_backup_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", same_owner_backup_sha256) is None
        or not isinstance(status, str)
        or status not in TAKEOVER_CLEANUP_STATUSES
        or (
            status in UNOWNED_TAKEOVER_CLEANUP_STATUSES
            and original_owner is not None
        )
    ):
        return TakeoverCompletionRead(TakeoverMetadataState.INVALID)
    return TakeoverCompletionRead(
        TakeoverMetadataState.VALID,
        TakeoverCompletionReceipt(
            takeover_owner=takeover_owner,
            original_owner=original_owner,
            final_sha256=final_sha256,
            same_owner_backup_sha256=same_owner_backup_sha256,
            status=status,
        ),
    )


def _rebase_takeover_completion(
    backup_path: Path,
    receipt: TakeoverCompletionReceipt,
    text: str,
) -> TakeoverCompletionReceipt:
    if overlay_owner(text) != receipt.original_owner:
        raise RuntimeError(
            "refusing managed config write: completed owner is inconsistent"
        )
    rebased = TakeoverCompletionReceipt(
        takeover_owner=receipt.takeover_owner,
        original_owner=receipt.original_owner,
        final_sha256=takeover_text_sha256(text),
        same_owner_backup_sha256=takeover_text_sha256(
            strip_marked_overlay(text)
        ),
        status=receipt.status,
    )
    if rebased == receipt:
        return receipt
    write_takeover_completion_receipt(backup_path, rebased)
    readback = read_takeover_completion(backup_path)
    if (
        readback.state is not TakeoverMetadataState.VALID
        or readback.receipt != rebased
    ):
        raise RuntimeError(
            "refusing managed config write: completion receipt readback failed"
        )
    return rebased


def _matches_takeover_completion(
    text: str,
    receipt: TakeoverCompletionReceipt,
) -> bool:
    return (
        overlay_owner(text) == receipt.original_owner
        and takeover_text_sha256(text) == receipt.final_sha256
        and takeover_text_sha256(strip_marked_overlay(text))
        == receipt.same_owner_backup_sha256
    )


def _matches_takeover_completion_backup(
    text: str,
    receipt: TakeoverCompletionReceipt,
) -> bool:
    # A later apply can stage either the completed bytes or the exact bytes
    # produced by removing only the completed owner's marked overlay.
    digest = takeover_text_sha256(text)
    return _matches_takeover_completion(text, receipt) or (
        digest == receipt.same_owner_backup_sha256
        and overlay_owner(text) is None
    )


def _retire_takeover_completion(backup_path: Path) -> None:
    takeover_completion_path(backup_path).unlink()


def is_active_takeover_backup(
    config_text: str,
    backup_text: str,
    metadata: TakeoverMetadata | None,
) -> bool:
    return (
        metadata is not None
        and overlay_owner(config_text) == metadata.takeover_owner
        and overlay_owner(backup_text) == metadata.original_owner
    )


def is_interrupted_takeover(
    config_text: str,
    backup_text: str,
    metadata: TakeoverMetadata | None,
) -> bool:
    return (
        metadata is not None
        and overlay_owner(config_text) == metadata.original_owner
        and overlay_owner(backup_text) == metadata.original_owner
    )


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


def overlay_lifecycle_lock_target(config_path: Path) -> Path:
    """Return the shared transaction-lock target for one managed Codex config."""
    canonical_config = config_path.resolve(strict=False)
    return canonical_config.with_name(
        f".{canonical_config.name}.codexhub-overlay-lifecycle"
    )


def _paths_refer_to_same_file(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def _validate_managed_write_paths(
    config_path: Path,
    backup_path: Path,
    state_path: Path | None = None,
) -> None:
    metadata_path = takeover_metadata_path(backup_path)
    completion_path = takeover_completion_path(backup_path)
    managed_paths = [
        ("config", config_path),
        ("backup", backup_path),
        ("metadata", metadata_path),
        ("completion receipt", completion_path),
    ]
    if state_path is not None:
        managed_paths.append(("context guard state", state_path))
    lock_targets = [
        *managed_paths,
        ("lifecycle target", overlay_lifecycle_lock_target(config_path)),
    ]
    named_paths: list[tuple[str, Path]] = list(lock_targets)
    for name, path in lock_targets:
        lock_path = path.with_name(f"{path.name}.lock")
        named_paths.extend(
            [
                (f"{name} lock namespace", lock_path),
                (
                    f"{name} lock guard namespace",
                    lock_path.with_name(f"{lock_path.name}.guard"),
                ),
            ]
        )
    for index, (left_name, left_path) in enumerate(named_paths):
        for right_name, right_path in named_paths[index + 1 :]:
            if _paths_refer_to_same_file(left_path, right_path):
                raise ValueError(
                    "managed paths and lock namespaces must be distinct: "
                    f"{left_name} aliases {right_name}"
                )


def _prepare_managed_config_write(
    config_path: Path,
    backup_path: Path,
    *,
    operation: str,
) -> tuple[TakeoverMetadata | None, str]:
    """Validate or finish takeover recovery before another managed write.

    Callers already hold the lifecycle lock. A terminal cleanup journal is
    itself the next write to perform, so finish it before starting a new
    operation. Active takeover metadata can remain in place because it has no
    byte-bound journal yet. Interrupted takeover state is returned to the
    caller, which may allow only the exact takeover retry.
    """
    metadata_read = read_takeover_metadata(backup_path)
    if metadata_read.state is TakeoverMetadataState.INVALID:
        raise RuntimeError(f"refusing {operation}: takeover metadata is invalid")
    completion_read = read_takeover_completion(backup_path)
    if completion_read.state is TakeoverMetadataState.INVALID:
        raise RuntimeError(f"refusing {operation}: takeover completion is invalid")
    metadata = metadata_read.metadata
    if metadata is None:
        if completion_read.receipt is not None:
            completion = completion_read.receipt
            if backup_path.exists():
                recovery = read_text_preserving_newlines(backup_path)
                if not _matches_takeover_completion_backup(recovery, completion):
                    raise RuntimeError(
                        f"refusing {operation}: completion recovery is inconsistent"
                    )
                _retire_takeover_completion(backup_path)
            elif not config_path.exists():
                raise RuntimeError(
                    f"refusing {operation}: completed config is missing or diverged"
                )
            else:
                _rebase_takeover_completion(
                    backup_path,
                    completion,
                    read_text_preserving_newlines(config_path),
                )
        return None, "absent"
    if metadata.cleanup_source_sha256 is not None:
        _resume_takeover_cleanup(config_path, backup_path, metadata)
        return None, "cleanup_resumed"
    if not backup_path.exists():
        raise RuntimeError(f"refusing {operation}: recovery backup is missing or diverged")

    recovery = read_text_preserving_newlines(backup_path)
    current = read_text_preserving_newlines(config_path) if config_path.exists() else ""
    if is_interrupted_takeover(current, recovery, metadata):
        if not config_path.exists() or current != recovery:
            raise RuntimeError(
                f"refusing {operation}: live config is missing or diverged"
            )
        takeover_state = "interrupted"
    elif is_active_takeover_backup(current, recovery, metadata):
        takeover_state = "active"
    else:
        raise RuntimeError(f"refusing {operation}: takeover state is not recognized")
    if completion_read.receipt is not None:
        completion = completion_read.receipt
        if (
            completion.original_owner != metadata.original_owner
            or not _matches_takeover_completion_backup(recovery, completion)
        ):
            raise RuntimeError(
                f"refusing {operation}: completion recovery is inconsistent"
            )
        _retire_takeover_completion(backup_path)
    return metadata, takeover_state


def apply_overlay(
    config_path: Path,
    backup_path: Path,
    catalog_path: Path | None,
    base_url: str,
    owner: str = "release",
    takeover: bool = False,
    gateway_key: str = "codexhub-proxy",
) -> None:
    # Apply and restore must read, classify, and publish while holding the same
    # lock. Individual atomic-file locks cannot prevent a retry from changing
    # the live config between restore classification and recovery cleanup.
    config_path = config_path.resolve(strict=False)
    backup_path = backup_path.resolve(strict=False)
    _validate_managed_write_paths(config_path, backup_path)
    with file_lock_for(overlay_lifecycle_lock_target(config_path)):
        _apply_overlay_locked(
            config_path,
            backup_path,
            catalog_path,
            base_url,
            owner,
            takeover,
            gateway_key,
        )


def _apply_overlay_locked(
    config_path: Path,
    backup_path: Path,
    catalog_path: Path | None,
    base_url: str,
    owner: str,
    takeover: bool,
    gateway_key: str,
) -> None:
    if owner not in {"release", "beta"}:
        raise ValueError(f"unsupported CodexHub owner: {owner}")
    metadata, takeover_state = _prepare_managed_config_write(
        config_path,
        backup_path,
        operation="overlay apply",
    )
    if takeover_state == "active" and (
        metadata is None or owner != metadata.takeover_owner
    ):
        raise RuntimeError("refusing overlay apply: active takeover belongs to another owner")
    if takeover_state == "interrupted" and (
        metadata is None
        or owner != metadata.takeover_owner
        or not takeover
    ):
        raise RuntimeError(
            "refusing overlay apply: interrupted takeover recovery is pending"
        )
    original = read_text_preserving_newlines(config_path) if config_path.exists() else ""
    custom_section = section_key_values(original, f"model_providers.{PROXY_PROVIDER_ID}")
    if custom_section is not None and not (
        custom_section == unified_official_provider_values() or is_managed_gateway_provider(custom_section)
    ):
        raise ValueError("refusing to overwrite unknown custom provider")
    selected_model = top_level_value(original, "model")
    context_budget = _selected_official_context_budget(catalog_path, selected_model)
    if context_budget is None and _selected_model_is_official(selected_model):
        raise ValueError("safe current Official context budget is unavailable")
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
    cleaned = strip_top_level_keys(cleaned)
    if context_budget is not None:
        cleaned = strip_top_level_keys(cleaned, CONTEXT_GUARD_KEYS)
    cleaned = set_feature_flags(cleaned, PROXY_FEATURE_FLAGS)
    updated = build_overlay(
        catalog_config_value(config_path, catalog_path) if catalog_path is not None else None,
        owner,
        context_budget,
    ) + cleaned.lstrip()
    updated = insert_provider_section(updated, build_provider_section(base_url, gateway_key))
    atomic_write_text(config_path, updated, encoding="utf-8")

    # Retire an older completed generation only after this apply has durable
    # live bytes, a recovery backup, and takeover metadata when ownership moved.
    completion_read = read_takeover_completion(backup_path)
    if completion_read.state is TakeoverMetadataState.INVALID:
        raise RuntimeError("refusing overlay apply: takeover completion is invalid")
    if completion_read.receipt is not None:
        if not backup_path.exists() or not _matches_takeover_completion_backup(
            read_text_preserving_newlines(backup_path),
            completion_read.receipt,
        ):
            raise RuntimeError(
                "refusing overlay apply: completion recovery is inconsistent"
            )
        if cross_owner_takeover:
            new_metadata_read = read_takeover_metadata(backup_path)
            new_metadata = new_metadata_read.metadata
            if (
                new_metadata_read.state is not TakeoverMetadataState.VALID
                or new_metadata is None
                or new_metadata.takeover_owner != owner
                or new_metadata.original_owner != active_owner
                or new_metadata.cleanup_source_sha256 is not None
            ):
                raise RuntimeError(
                    "refusing overlay apply: takeover ownership evidence is inconsistent"
                )
        _retire_takeover_completion(backup_path)


def restore_overlay(config_path: Path, backup_path: Path, unified_history: bool = False) -> str:
    config_path = config_path.resolve(strict=False)
    backup_path = backup_path.resolve(strict=False)
    _validate_managed_write_paths(config_path, backup_path)
    with file_lock_for(overlay_lifecycle_lock_target(config_path)):
        return _restore_overlay_locked(config_path, backup_path, unified_history)


def _takeover_cleanup_final_text(
    recovery_text: str,
    metadata: TakeoverMetadata,
) -> str:
    if metadata.cleanup_status in {
        "interrupted_takeover_discarded",
        "restored_takeover_backup",
    }:
        return recovery_text
    final_text, status = inject_unified_history_config(recovery_text)
    if status != metadata.cleanup_status:
        raise RuntimeError("refusing takeover cleanup: terminal status is inconsistent")
    return final_text


def _resume_takeover_cleanup(
    config_path: Path,
    backup_path: Path,
    metadata: TakeoverMetadata,
) -> str:
    source_sha256 = metadata.cleanup_source_sha256
    recovery_sha256 = metadata.cleanup_recovery_sha256
    final_sha256 = metadata.cleanup_final_sha256
    status = metadata.cleanup_status
    if None in {source_sha256, recovery_sha256, final_sha256, status}:
        raise RuntimeError("refusing takeover cleanup: metadata is incomplete")
    if not config_path.exists():
        raise RuntimeError("refusing takeover cleanup: live config is missing or diverged")

    final_text: str | None = None
    if backup_path.exists():
        recovery_text = read_text_preserving_newlines(backup_path)
        if (
            overlay_owner(recovery_text) != metadata.original_owner
            or takeover_text_sha256(recovery_text) != recovery_sha256
        ):
            raise RuntimeError("refusing takeover cleanup: recovery backup is missing or diverged")
        final_text = _takeover_cleanup_final_text(recovery_text, metadata)
        if takeover_text_sha256(final_text) != final_sha256:
            raise RuntimeError("refusing takeover cleanup: intended final config is inconsistent")

    current = read_text_preserving_newlines(config_path)
    current_sha256 = takeover_text_sha256(current)
    if current_sha256 == final_sha256:
        if overlay_owner(current) != metadata.original_owner:
            raise RuntimeError("refusing takeover cleanup: final config owner is inconsistent")
        completed_text = current
    elif current_sha256 == source_sha256:
        if overlay_owner(current) != metadata.takeover_owner or final_text is None:
            raise RuntimeError("refusing takeover cleanup: live config is missing or diverged")
        atomic_write_text(config_path, final_text, encoding="utf-8")
        published = read_text_preserving_newlines(config_path)
        if (
            overlay_owner(published) != metadata.original_owner
            or takeover_text_sha256(published) != final_sha256
        ):
            raise RuntimeError("refusing takeover cleanup: final config publication diverged")
        completed_text = published
    else:
        raise RuntimeError("refusing takeover cleanup: live config is missing or diverged")

    expected_completion = TakeoverCompletionReceipt(
        takeover_owner=metadata.takeover_owner,
        original_owner=metadata.original_owner,
        final_sha256=final_sha256,
        same_owner_backup_sha256=takeover_text_sha256(
            strip_marked_overlay(completed_text)
        ),
        status=status,
    )
    write_takeover_completion_receipt(backup_path, expected_completion)
    completion_read = read_takeover_completion(backup_path)
    if (
        completion_read.state is not TakeoverMetadataState.VALID
        or completion_read.receipt != expected_completion
    ):
        raise RuntimeError("refusing takeover cleanup: completion receipt readback failed")

    if backup_path.exists():
        backup_path.unlink()
    takeover_metadata_path(backup_path).unlink()
    return status


def _start_takeover_cleanup(
    config_path: Path,
    backup_path: Path,
    metadata: TakeoverMetadata,
    current: str,
    recovery: str,
    final: str,
    status: str,
) -> str:
    write_takeover_metadata(
        backup_path,
        metadata.takeover_owner,
        metadata.original_owner,
        cleanup_source_sha256=takeover_text_sha256(current),
        cleanup_recovery_sha256=takeover_text_sha256(recovery),
        cleanup_final_sha256=takeover_text_sha256(final),
        cleanup_status=status,
    )
    journal_read = read_takeover_metadata(backup_path)
    if journal_read.state is not TakeoverMetadataState.VALID:
        raise RuntimeError("refusing takeover cleanup: journal readback failed")
    journal = journal_read.metadata
    if journal is None:
        raise RuntimeError("refusing takeover cleanup: journal readback failed")
    return _resume_takeover_cleanup(config_path, backup_path, journal)


def _restore_overlay_locked(config_path: Path, backup_path: Path, unified_history: bool) -> str:
    metadata_read = read_takeover_metadata(backup_path)
    if metadata_read.state is TakeoverMetadataState.INVALID:
        raise RuntimeError("refusing takeover restore: takeover metadata is invalid")
    completion_read = read_takeover_completion(backup_path)
    if completion_read.state is TakeoverMetadataState.INVALID:
        raise RuntimeError("refusing takeover restore: takeover completion is invalid")
    metadata = metadata_read.metadata
    if (
        metadata is not None
        and completion_read.receipt is not None
        and completion_read.receipt.original_owner != metadata.original_owner
    ):
        raise RuntimeError(
            "refusing takeover restore: completion ownership is inconsistent"
        )
    if metadata is not None and metadata.cleanup_source_sha256 is not None:
        return _resume_takeover_cleanup(config_path, backup_path, metadata)

    if not backup_path.exists() and metadata is not None:
        raise RuntimeError("refusing takeover restore: recovery backup is missing or diverged")
    if (
        backup_path.exists()
        and metadata is not None
        and completion_read.receipt is not None
        and not _matches_takeover_completion_backup(
            read_text_preserving_newlines(backup_path),
            completion_read.receipt,
        )
    ):
        raise RuntimeError(
            "refusing takeover restore: completion recovery is inconsistent"
        )
    if not backup_path.exists() and metadata is None and completion_read.receipt is not None:
        completion = completion_read.receipt
        if not config_path.exists():
            raise RuntimeError(
                "refusing takeover restore: completed live config is missing or diverged"
            )
        current = read_text_preserving_newlines(config_path)
        if not _matches_takeover_completion(current, completion):
            raise RuntimeError(
                "refusing takeover restore: completed live config is missing or diverged"
            )
        return completion.status
    if backup_path.exists() and metadata is None and completion_read.receipt is not None:
        completion = completion_read.receipt
        staged_backup = read_text_preserving_newlines(backup_path)
        if config_path.exists():
            current = read_text_preserving_newlines(config_path)
            if (
                _matches_takeover_completion(current, completion)
                and _matches_takeover_completion_backup(staged_backup, completion)
            ):
                backup_path.unlink()
                return completion.status
        if not _matches_takeover_completion_backup(staged_backup, completion):
            raise RuntimeError(
                "refusing takeover restore: completion recovery is inconsistent"
            )
        _retire_takeover_completion(backup_path)

    if backup_path.exists():
        restored = read_text_preserving_newlines(backup_path)
        current = read_text_preserving_newlines(config_path) if config_path.exists() else ""
        restore_from_backup = True
        if is_interrupted_takeover(current, restored, metadata):
            if not config_path.exists() or current != restored:
                raise RuntimeError(
                    "refusing interrupted takeover cleanup: live config is missing or diverged"
                )
            if metadata is None:
                raise RuntimeError("refusing interrupted takeover cleanup: metadata is invalid")
            return _start_takeover_cleanup(
                config_path,
                backup_path,
                metadata,
                current,
                restored,
                restored,
                "interrupted_takeover_discarded",
            )
        if is_active_takeover_backup(current, restored, metadata):
            restored_owner = overlay_owner(restored)
            if not unified_history or restored_owner is not None:
                if metadata is None:
                    raise RuntimeError("refusing takeover restore: metadata is invalid")
                return _start_takeover_cleanup(
                    config_path,
                    backup_path,
                    metadata,
                    current,
                    restored,
                    restored,
                    "restored_takeover_backup",
                )
            if metadata is None:
                raise RuntimeError("refusing takeover restore: metadata is invalid")
            final, status = inject_unified_history_config(restored)
            return _start_takeover_cleanup(
                config_path,
                backup_path,
                metadata,
                current,
                restored,
                final,
                status,
            )
        if metadata is not None:
            raise RuntimeError("refusing takeover restore: state is not recognized")
    elif config_path.exists():
        restored = strip_marked_overlay(read_text_preserving_newlines(config_path))
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
        backup_path.unlink()
        metadata_path = takeover_metadata_path(backup_path)
        if metadata_path.exists():
            metadata_path.unlink()
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

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--config", required=True, type=Path)
    restore_parser.add_argument("--backup", required=True, type=Path)
    restore_parser.add_argument("--unified-history", action="store_true")

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
