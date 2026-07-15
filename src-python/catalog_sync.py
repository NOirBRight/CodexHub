from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from atomic_io import atomic_write_text
from catalog import (
    CatalogPolicy,
    canonical_model_id,
    display_name_for,
    load_catalog_models,
    load_policy,
    should_include_external_provider_model,
    should_include_model,
)
from providers_config import (
    DEFAULT_PROVIDERS_PATH,
    catalog_visible_external_models,
    catalog_visible_ollama_cloud_models,
    load_providers,
    runtime_providers_path,
)
from model_limits import (
    CURRENT_DIRECT_OFFICIAL_SOURCE,
    DEGRADED_LAST_KNOWN_OFFICIAL_SOURCE,
    FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE,
    OfficialContextBudget,
    apply_resolved_model_limits,
    load_resolved_model_limits,
    resolve_official_context_budget,
)


PROXY_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROXY_DIR.parent
def _runtime_codex_dir() -> Path:
    codex_home_env = os.environ.get("CODEX_HOME")
    if codex_home_env:
        return Path(codex_home_env)
    try:
        return Path.home() / ".codex"
    except (RuntimeError, OSError):
        return REPO_ROOT


RUNTIME_CODEX_DIR = _runtime_codex_dir()
BUNDLED_MODEL_CATALOG_DIR = REPO_ROOT / "model-catalogs"
RUNTIME_MODEL_CATALOG_DIR = RUNTIME_CODEX_DIR / "model-catalogs"
CODEX_TARGET_HOME_ENV = "CODEXHUB_CODEX_TARGET_HOME"


def _direct_official_models_cache_path() -> Path:
    """Use the Direct Codex target home without changing runtime state paths."""

    target_home_env = os.environ.get(CODEX_TARGET_HOME_ENV)
    target_home = Path(target_home_env) if target_home_env else RUNTIME_CODEX_DIR
    return target_home / "models_cache.json"


DIRECT_OFFICIAL_MODELS_CACHE_PATH = _direct_official_models_cache_path()

POLICY_PATH = REPO_ROOT / "config" / "catalog_policy.toml"
OFFICIAL_SEED_PATH = BUNDLED_MODEL_CATALOG_DIR / "openai-plus-ollama-cloud.json"
RUNTIME_OFFICIAL_SEED_PATH = RUNTIME_MODEL_CATALOG_DIR / "openai-plus-ollama-cloud.json"
OLLAMA_FALLBACK_PATH = BUNDLED_MODEL_CATALOG_DIR / "ollama-cloud.json"
RUNTIME_OLLAMA_FALLBACK_PATH = RUNTIME_MODEL_CATALOG_DIR / "ollama-cloud.json"
GENERATED_CATALOG_FILENAME = "codexhub-model-catalog.json"
LEGACY_GENERATED_CATALOG_FILENAME = "codex-proxy-official-ollama.json"
GENERATED_CATALOG_PATH = RUNTIME_MODEL_CATALOG_DIR / GENERATED_CATALOG_FILENAME
LEGACY_GENERATED_CATALOG_PATH = RUNTIME_MODEL_CATALOG_DIR / LEGACY_GENERATED_CATALOG_FILENAME
GENERATED_STATE_PATH = RUNTIME_MODEL_CATALOG_DIR / "codex-proxy-state.json"
SETTINGS_PATH = RUNTIME_CODEX_DIR / "proxy" / "settings.json"
RESOLVED_MODEL_LIMITS_PATH = REPO_ROOT / "config" / "resolved_model_limits.json"
RESOLVED_MODEL_LIMITS = load_resolved_model_limits(RESOLVED_MODEL_LIMITS_PATH)
OFFICIAL_CATALOG_METADATA_PATH = REPO_ROOT / "config" / "official_model_catalog_metadata.json"

OLLAMA_MODELS_URL = "https://ollama.com/v1/models"
OLLAMA_SHOW_URL = "https://ollama.com/api/show"
DEFAULT_CLIENT_VERSION = "0.142.0"
OLLAMA_PRIORITY_BASE = 100
DIRECT_OFFICIAL_CONTEXT_MAX_AGE_SECONDS = 12 * 60 * 60
DIRECT_OFFICIAL_CACHE_STATUS_SOURCE = "direct_official_cache"
OFFICIAL_PROXY_PROVIDER_ALIAS = "openai"


OLLAMA_MODEL_LIMIT_OVERRIDES: dict[str, dict[str, Any]] = {
    "minimax-m3": {
        "context_window": 524288,
        "max_output_tokens": 524288,
        "max_output_source": "https://platform.minimax.io/docs/api-reference/text-chat-openai",
    },
    "glm-5.2": {
        "context_window": 1000000,
        "max_output_tokens": 131072,
        "max_output_source": "https://docs.z.ai",
    },
    "kimi-k2.6": {
        "context_window": 262144,
        "max_output_tokens": 32768,
        "max_output_source": "https://ollama.com/library/kimi-k2.6",
    },
    "kimi-k2.7-code": {
        "context_window": 262144,
        "max_output_tokens": 32768,
        "max_output_source": "https://platform.kimi.ai/docs/guide/kimi-k2-7-code-quickstart",
    },
    "gemini-3-flash-preview": {
        "context_window": 1048576,
        "max_output_tokens": 65536,
        "max_output_source": "https://ai.google.dev/gemini-api/docs/models",
    },
    "deepseek-v4-pro": {
        "context_window": 524288,
        "max_output_tokens": 393216,
        "max_output_source": "https://api-docs.deepseek.com/quick_start/pricing",
    },
    "deepseek-v4-flash": {
        "context_window": 1048576,
        "max_output_tokens": 393216,
        "max_output_source": "https://api-docs.deepseek.com/quick_start/pricing",
    },
}


MINIMAL_OFFICIAL_MODEL: dict[str, Any] = {
    "description": "Official OpenAI model.",
    "shell_type": "shell_command",
    "visibility": "list",
    "supported_in_api": True,
    "priority": 10,
    "additional_speed_tiers": [],
    "service_tiers": [],
    "supported_reasoning_levels": [
        {"effort": "low", "description": "Fast responses with lighter reasoning"},
        {
            "effort": "medium",
            "description": "Balances speed and reasoning depth for everyday tasks",
        },
        {"effort": "high", "description": "Greater reasoning depth for complex problems"},
        {
            "effort": "xhigh",
            "description": "Extra high reasoning depth for complex problems",
        },
        {
            "effort": "max",
            "description": "Maximum reasoning depth for the hardest problems",
        },
    ],
    "default_reasoning_level": "medium",
    "base_instructions": "You are Codex, a coding agent. Follow the current session instructions and use tools when needed.",
    "model_messages": {
        "instructions_template": "You are Codex, a coding agent. Follow the current session instructions and use tools when needed.",
        "instructions_variables": {},
        "approvals": None,
    },
    "include_skills_usage_instructions": False,
    "supports_reasoning_summaries": True,
    "default_reasoning_summary": "none",
    "support_verbosity": True,
    "default_verbosity": "low",
    "apply_patch_tool_type": "freeform",
    "web_search_tool_type": "text_and_image",
    "truncation_policy": {"mode": "tokens", "limit": 10000},
    "supports_parallel_tool_calls": True,
    "supports_image_detail_original": True,
    "experimental_supported_tools": [],
    "input_modalities": ["text"],
    "supports_search_tool": True,
    "use_responses_lite": False,
}

OFFICIAL_FAST_SERVICE_TIERS: list[dict[str, str]] = [
    {"id": "priority", "name": "Fast", "description": "1.5x speed, increased usage"}
]
OFFICIAL_GATEWAY_FAST_VARIANT_SLUGS = {"gpt-5.5-fast", "gpt-5.4-fast"}

OFFICIAL_MODEL_DEFAULTS: dict[str, dict[str, Any]] = {
    "gpt-5.5": {
        "additional_speed_tiers": ["fast"],
        "service_tiers": OFFICIAL_FAST_SERVICE_TIERS,
        "default_reasoning_level": "medium",
    },
    "gpt-5.5-fast": {
        "default_reasoning_level": "medium",
    },
    "gpt-5.4": {
        "additional_speed_tiers": ["fast"],
        "service_tiers": OFFICIAL_FAST_SERVICE_TIERS,
        "default_reasoning_level": "medium",
    },
    "gpt-5.4-fast": {
        "default_reasoning_level": "medium",
    },
    "gpt-5.4-mini": {
        "additional_speed_tiers": [],
        "service_tiers": [],
        "default_reasoning_level": "medium",
    },
    "gpt-5.3-codex-spark": {
        "additional_speed_tiers": [],
        "service_tiers": [],
        "default_reasoning_level": "high",
    },
}

DEFAULT_OLLAMA_MODEL: dict[str, Any] = {
    "description": "External Ollama Cloud model via https://ollama.com/v1.",
    "default_reasoning_level": "high",
    "supported_reasoning_levels": [
        {"effort": "low", "description": "Fast responses with lighter reasoning"},
        {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
        {"effort": "high", "description": "Greater reasoning depth for complex problems"},
        {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
        {"effort": "max", "description": "Maximum upstream reasoning depth"},
    ],
    "shell_type": "shell_command",
    "visibility": "list",
    "supported_in_api": True,
    "priority": 20,
    "additional_speed_tiers": [],
    "service_tiers": [],
    "supports_reasoning_summaries": True,
    "default_reasoning_summary": "none",
    "support_verbosity": True,
    "default_verbosity": "low",
    "apply_patch_tool_type": "freeform",
    "web_search_tool_type": "text_and_image",
    "truncation_policy": {"mode": "tokens", "limit": 10000},
    "supports_parallel_tool_calls": True,
    "supports_image_detail_original": True,
    "context_window": 128000,
    "max_context_window": 128000,
    "comp_hash": "2911",
    "effective_context_window_percent": 95,
    "experimental_supported_tools": [],
    "input_modalities": ["text", "image"],
    "supports_search_tool": False,
    "use_responses_lite": False,
    "base_instructions": "You are Codex, a coding agent. Follow the current session instructions and use tools when needed.",
    "instructions_variables": {},
}

REASONING_LEVEL_DESCRIPTIONS = {
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth for everyday tasks",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra high reasoning depth for complex problems",
    "max": "Maximum upstream reasoning depth",
}
THIRD_PARTY_REASONING_LEVEL_ORDER = ("low", "medium", "high", "xhigh", "max")
THIRD_PARTY_REASONING_LEVELS = set(THIRD_PARTY_REASONING_LEVEL_ORDER)
PINNED_OFFICIAL_MODEL_IDS = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
)
PINNED_OFFICIAL_CODE_MODE_MULTI_AGENT_VERSIONS = {
    "gpt-5.6-sol": "v2",
    "gpt-5.6-terra": "v2",
    "gpt-5.6-luna": "v1",
}
PINNED_OFFICIAL_LEGACY_MODEL_IDS = (
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
)
PINNED_OFFICIAL_PLANNER_FIELD_SET = (
    "prefer_websockets",
    "tool_mode",
    "multi_agent_version",
    "use_responses_lite",
)
PINNED_OFFICIAL_MODEL_FIELD_SETS = {
    **{
        slug: PINNED_OFFICIAL_PLANNER_FIELD_SET
        for slug in PINNED_OFFICIAL_CODE_MODE_MULTI_AGENT_VERSIONS
    },
    **{slug: PINNED_OFFICIAL_PLANNER_FIELD_SET for slug in PINNED_OFFICIAL_LEGACY_MODEL_IDS},
    "gpt-5.3-codex-spark": ("use_responses_lite",),
}


def load_pinned_official_catalog_metadata(
    path: Path = OFFICIAL_CATALOG_METADATA_PATH,
) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"official catalog metadata is unreadable: {error}") from error
    if payload.get("schema_version") != 1:
        raise ValueError("official catalog metadata has an unsupported schema")
    models = payload.get("models")
    if not isinstance(models, dict) or set(models) != set(PINNED_OFFICIAL_MODEL_IDS):
        raise ValueError("official catalog metadata has an incomplete model set")

    validated: dict[str, dict[str, Any]] = {}
    for slug in PINNED_OFFICIAL_MODEL_IDS:
        metadata = models.get(slug)
        if not isinstance(metadata, dict):
            raise ValueError(f"official catalog metadata for {slug} is invalid")
        if set(metadata) != set(PINNED_OFFICIAL_MODEL_FIELD_SETS[slug]):
            raise ValueError(f"official catalog metadata for {slug} has an invalid field set")
        if slug in PINNED_OFFICIAL_CODE_MODE_MULTI_AGENT_VERSIONS:
            if metadata["prefer_websockets"] is not True:
                raise ValueError(f"official catalog metadata for {slug} has an invalid websocket flag")
            if metadata["tool_mode"] != "code_mode_only":
                raise ValueError(f"official catalog metadata for {slug} has an invalid tool mode")
            if metadata["multi_agent_version"] != PINNED_OFFICIAL_CODE_MODE_MULTI_AGENT_VERSIONS[slug]:
                raise ValueError(f"official catalog metadata for {slug} has an invalid multi-agent version")
            if metadata["use_responses_lite"] is not True:
                raise ValueError(f"official catalog metadata for {slug} has an invalid Responses Lite flag")
        elif slug in PINNED_OFFICIAL_LEGACY_MODEL_IDS:
            if metadata["prefer_websockets"] is not True:
                raise ValueError(f"official catalog metadata for {slug} has an invalid websocket flag")
            if metadata["tool_mode"] is not None:
                raise ValueError(f"official catalog metadata for {slug} has an invalid tool mode")
            if metadata["multi_agent_version"] is not None:
                raise ValueError(f"official catalog metadata for {slug} has an invalid multi-agent version")
            if metadata["use_responses_lite"] is not False:
                raise ValueError(f"official catalog metadata for {slug} has an invalid Responses Lite flag")
        elif metadata["use_responses_lite"] is not False:
            raise ValueError(f"official catalog metadata for {slug} has an invalid Responses Lite flag")
        validated[slug] = deepcopy(metadata)
    return validated


PINNED_OFFICIAL_CATALOG_METADATA = load_pinned_official_catalog_metadata()


def sanitize_third_party_reasoning_levels(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    sanitized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        raw_effort = item.get("effort") if isinstance(item, dict) else item
        effort = str(raw_effort).strip().lower()
        if effort not in THIRD_PARTY_REASONING_LEVELS or effort in seen:
            continue
        seen.add(effort)
        description = item.get("description") if isinstance(item, dict) else None
        sanitized.append(
            {
                "effort": effort,
                "description": (
                    description
                    if isinstance(description, str) and description.strip()
                    else REASONING_LEVEL_DESCRIPTIONS.get(effort, f"{effort} reasoning effort")
                ),
            }
        )
    return sanitized


def complete_third_party_reasoning_levels(value: Any) -> list[dict[str, str]]:
    configured = {
        item["effort"]: item for item in sanitize_third_party_reasoning_levels(value)
    }
    return [
        configured.get(effort)
        or {"effort": effort, "description": REASONING_LEVEL_DESCRIPTIONS[effort]}
        for effort in THIRD_PARTY_REASONING_LEVEL_ORDER
    ]

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def catalog_cache_dependency_paths() -> tuple[Path, ...]:
    return (
        POLICY_PATH,
        OFFICIAL_SEED_PATH,
        RUNTIME_OFFICIAL_SEED_PATH,
        DIRECT_OFFICIAL_MODELS_CACHE_PATH,
        OLLAMA_FALLBACK_PATH,
        SETTINGS_PATH,
        DEFAULT_PROVIDERS_PATH,
        runtime_providers_path(),
        Path(__file__).resolve(),
        PROXY_DIR / "catalog.py",
        PROXY_DIR / "providers_config.py",
    )


def catalog_cache_is_fresh(max_age_seconds: int, catalog_path: Path = GENERATED_CATALOG_PATH) -> bool:
    if max_age_seconds <= 0 or not catalog_path.exists():
        return False
    catalog_mtime = catalog_path.stat().st_mtime
    for dependency in catalog_cache_dependency_paths():
        if dependency.exists() and dependency.stat().st_mtime > catalog_mtime:
            return False
    age_seconds = datetime.now(timezone.utc).timestamp() - catalog_mtime
    return age_seconds < max_age_seconds


def existing_generated_catalog_path(path: Path = GENERATED_CATALOG_PATH) -> Path:
    if path.exists():
        return path
    if path == GENERATED_CATALOG_PATH and LEGACY_GENERATED_CATALOG_PATH.exists():
        return LEGACY_GENERATED_CATALOG_PATH
    return path


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


@dataclass(frozen=True)
class OfficialSeedSnapshot:
    models: list[dict[str, Any]]
    source: str
    context_freshness: str


def _catalog_fetched_at_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None


def _direct_catalog_context_freshness(payload: dict[str, Any], now_timestamp: float | None) -> str:
    fetched_at = _catalog_fetched_at_timestamp(payload.get("fetched_at"))
    if fetched_at is None:
        return "missing"
    now = now_timestamp if now_timestamp is not None else datetime.now(timezone.utc).timestamp()
    age_seconds = now - fetched_at
    return (
        "fresh"
        if 0 <= age_seconds < DIRECT_OFFICIAL_CONTEXT_MAX_AGE_SECONDS
        else "stale"
    )


def read_client_version(
    seed_path: Path = OFFICIAL_SEED_PATH,
    fallback_path: Path = OLLAMA_FALLBACK_PATH,
    runtime_seed_path: Path = RUNTIME_OFFICIAL_SEED_PATH,
) -> str:
    for path in (seed_path, runtime_seed_path, fallback_path):
        data = load_json_file(path)
        version = data.get("client_version")
        if isinstance(version, str) and version:
            return version
    return DEFAULT_CLIENT_VERSION


def official_seed_catalog_paths(
    path: Path = OFFICIAL_SEED_PATH,
    runtime_path: Path = RUNTIME_OFFICIAL_SEED_PATH,
) -> list[Path]:
    paths = [runtime_path]
    if path not in paths:
        paths.append(path)
    return paths


def load_official_seed_snapshot(
    path: Path = OFFICIAL_SEED_PATH,
    runtime_path: Path = RUNTIME_OFFICIAL_SEED_PATH,
    *,
    now_timestamp: float | None = None,
) -> OfficialSeedSnapshot:
    for candidate in official_seed_catalog_paths(path, runtime_path):
        payload = load_json_file(candidate)
        payload_models = payload.get("models")
        if not isinstance(payload_models, list):
            payload_models = []
        models = [
            deepcopy(model)
            for model in payload_models
            if isinstance(model, dict) and str(model.get("slug", "")).startswith("gpt-")
        ]
        if not models:
            continue
        if candidate == runtime_path:
            freshness = _direct_catalog_context_freshness(payload, now_timestamp)
            return OfficialSeedSnapshot(
                models=models,
                source=(
                    CURRENT_DIRECT_OFFICIAL_SOURCE
                    if freshness == "fresh"
                    else "last_known_direct_official"
                ),
                context_freshness=freshness,
            )
        return OfficialSeedSnapshot(models=models, source="bundled_seed", context_freshness="missing")
    return OfficialSeedSnapshot(models=[], source="missing", context_freshness="missing")


@dataclass(frozen=True)
class DirectOfficialCacheAuthority:
    """Sanitized numeric evidence joined to one current Official model list."""

    context_by_slug: dict[str, dict[str, int]]
    source: str
    freshness: str


def _unavailable_direct_official_cache_authority(
    freshness: str,
) -> DirectOfficialCacheAuthority:
    return DirectOfficialCacheAuthority(
        context_by_slug={},
        source=DIRECT_OFFICIAL_CACHE_STATUS_SOURCE,
        freshness=freshness,
    )


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _direct_official_model_identity(model: Any) -> str | None:
    if not isinstance(model, dict):
        return None
    identities: list[str] = []
    for key in ("slug", "model", "id"):
        if key not in model:
            continue
        value = model.get(key)
        if not _nonempty_string(value):
            return None
        slug = canonical_model_id(value)
        if slug.startswith("openai/gpt-"):
            slug = slug.removeprefix("openai/")
        if not slug.startswith("gpt-"):
            return None
        identities.append(slug)
    if not identities or len(set(identities)) != 1:
        return None
    return identities[0]


def _direct_official_model_index(
    models: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]] | None:
    indexed: dict[str, dict[str, Any]] = {}
    for model in models:
        slug = _direct_official_model_identity(model)
        if slug is None or slug in indexed:
            return None
        indexed[slug] = model
    return indexed


def _positive_context_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _validated_cache_context_values(model: dict[str, Any]) -> dict[str, int] | None:
    raw_context = model.get("context_window")
    raw_max_context = model.get("max_context_window")
    raw_effective_percent = model.get("effective_context_window_percent")
    if all(value is None for value in (raw_context, raw_max_context, raw_effective_percent)):
        return {}

    context_window = _positive_context_int(raw_context)
    max_context_window = _positive_context_int(raw_max_context)
    effective_percent = _positive_context_int(raw_effective_percent)
    if (
        context_window is None
        or max_context_window is None
        or max_context_window < context_window
        or effective_percent is None
        or effective_percent > 100
    ):
        return None

    values = {
        "context_window": context_window,
        "max_context_window": max_context_window,
        "effective_context_window_percent": effective_percent,
    }
    if "auto_compact_token_limit" in model:
        auto_compact_token_limit = _positive_context_int(model.get("auto_compact_token_limit"))
        if auto_compact_token_limit is None:
            return None
        values["auto_compact_token_limit"] = auto_compact_token_limit
    return values


def _load_stable_json_object(path: Path) -> dict[str, Any] | None:
    """Read one stable view of an atomically published cache file.

    Native Codex writes this cache as a replacement.  If its metadata changes
    while it is read, discard the observation rather than accepting a mixed
    write.  Deliberately return no error detail so diagnostics never expose a
    local path or cache contents.
    """

    try:
        if not path.is_file() or path.is_symlink():
            return None
        before = path.stat()
        text = path.read_text(encoding="utf-8-sig")
        after = path.stat()
    except (OSError, UnicodeError):
        return None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def load_fresh_direct_official_cache_authority(
    snapshot: OfficialSeedSnapshot,
    cache_path: Path = DIRECT_OFFICIAL_MODELS_CACHE_PATH,
    *,
    now_timestamp: float | None = None,
) -> DirectOfficialCacheAuthority:
    """Return only numeric Direct-cache evidence proven safe for this list.

    The cache is never a bundled fallback: it is usable solely alongside a
    fresh current app-server model list whose visible model identities agree
    with the cache.  ETag/configuration markers prove provenance internally
    and are intentionally omitted from the returned authority.
    """

    if (
        snapshot.source != CURRENT_DIRECT_OFFICIAL_SOURCE
        or snapshot.context_freshness != "fresh"
    ):
        return _unavailable_direct_official_cache_authority(snapshot.context_freshness)

    payload = _load_stable_json_object(cache_path)
    if payload is None:
        return _unavailable_direct_official_cache_authority("missing")
    freshness = _direct_catalog_context_freshness(payload, now_timestamp)
    if freshness != "fresh":
        return _unavailable_direct_official_cache_authority(freshness)
    if not _nonempty_string(payload.get("etag")) or not _nonempty_string(
        payload.get("client_version")
    ):
        return _unavailable_direct_official_cache_authority("missing")

    cache_models = payload.get("models")
    if not isinstance(cache_models, list):
        return _unavailable_direct_official_cache_authority("missing")
    current_by_slug = _direct_official_model_index(snapshot.models)
    cache_by_slug = _direct_official_model_index(
        [model for model in cache_models if isinstance(model, dict)]
    )
    if current_by_slug is None or cache_by_slug is None or not current_by_slug:
        return _unavailable_direct_official_cache_authority("contradictory")
    if len(cache_models) != len(cache_by_slug) or not set(current_by_slug).issubset(cache_by_slug):
        return _unavailable_direct_official_cache_authority("contradictory")

    context_by_slug: dict[str, dict[str, int]] = {}
    for slug in current_by_slug:
        cached_model = cache_by_slug[slug]
        if not _nonempty_string(cached_model.get("comp_hash")):
            return _unavailable_direct_official_cache_authority("missing")
        values = _validated_cache_context_values(cached_model)
        if values is None:
            return _unavailable_direct_official_cache_authority("contradictory")
        if values:
            context_by_slug[slug] = values

    if not context_by_slug:
        return _unavailable_direct_official_cache_authority("missing")
    return DirectOfficialCacheAuthority(
        context_by_slug=context_by_slug,
        source=FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE,
        freshness="fresh",
    )


def _previous_official_context_budget_is_safe(budget: dict[str, Any]) -> bool:
    source = budget.get("source")
    freshness = budget.get("freshness")
    if source == DEGRADED_LAST_KNOWN_OFFICIAL_SOURCE:
        pass
    elif source in {
        CURRENT_DIRECT_OFFICIAL_SOURCE,
        FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE,
    } and freshness == "fresh":
        pass
    else:
        return False

    context_window = _positive_context_int(
        budget.get("model_context_window", budget.get("context_window"))
    )
    effective_percent = _positive_context_int(budget.get("effective_context_window_percent"))
    effective_window = _positive_context_int(budget.get("effective_context_window"))
    auto_compact_token_limit = _positive_context_int(
        budget.get("model_auto_compact_token_limit")
    )
    return bool(
        context_window is not None
        and effective_percent is not None
        and effective_percent <= 100
        and effective_window is not None
        and effective_window <= context_window
        and auto_compact_token_limit is not None
        and auto_compact_token_limit <= effective_window
    )


def load_previous_official_context_budgets(
    path: Path = GENERATED_CATALOG_PATH,
) -> dict[str, dict[str, Any]]:
    """Read only previously resolved Official budgets that can hold/tighten.

    The generated catalog is atomically published, so this intentionally does
    not trust the raw bundled/runtime model values as a new degraded cap.
    """

    payload = load_json_file(existing_generated_catalog_path(path))
    models = payload.get("models") if isinstance(payload, dict) else None
    budgets: dict[str, dict[str, Any]] = {}
    if not isinstance(models, list):
        return budgets
    for model in models:
        if not isinstance(model, dict):
            continue
        slug = canonical_model_id(str(model.get("slug", "")))
        if slug.startswith("openai/gpt-"):
            slug = slug.removeprefix("openai/")
        metadata = model.get("codex_proxy_metadata")
        if (
            not slug.startswith("gpt-")
            or not isinstance(metadata, dict)
            or metadata.get("provider") != OFFICIAL_PROXY_PROVIDER_ALIAS
            or metadata.get("upstream_name") != "official"
        ):
            continue
        budget = metadata.get("official_context_budget")
        if isinstance(budget, dict) and _previous_official_context_budget_is_safe(budget):
            budgets[slug] = dict(budget)
    return budgets


def _first_present_value(model: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in model:
            return model[key]
    return None


def official_context_signals_from_snapshot(
    snapshot: OfficialSeedSnapshot,
    previous_budgets: dict[str, dict[str, Any]] | None = None,
    *,
    direct_cache_authority: DirectOfficialCacheAuthority | None = None,
) -> dict[str, dict[str, Any]]:
    signals: dict[str, dict[str, Any]] = {}
    for model in snapshot.models:
        raw_slug = canonical_model_id(str(model.get("slug", "")))
        slug = raw_slug.removeprefix("openai/") if raw_slug.startswith("openai/gpt-") else raw_slug
        if not slug:
            continue
        previous = (previous_budgets or {}).get(slug, {})
        context_window = _first_present_value(
            model,
            "context_window",
            "contextWindow",
        )
        max_context_window = _first_present_value(
            model,
            "max_context_window",
            "maxContextWindow",
        )
        effective_context_window_percent = _first_present_value(
            model,
            "effective_context_window_percent",
            "effectiveContextWindowPercent",
        )
        auto_compact_token_limit = _first_present_value(
            model,
            "auto_compact_token_limit",
            "model_auto_compact_token_limit",
            "autoCompactTokenLimit",
            "modelAutoCompactTokenLimit",
        )
        source = snapshot.source
        freshness = snapshot.context_freshness
        if (
            direct_cache_authority is not None
            and snapshot.source == CURRENT_DIRECT_OFFICIAL_SOURCE
            and snapshot.context_freshness == "fresh"
            and all(
                value is None
                for value in (
                    context_window,
                    max_context_window,
                    effective_context_window_percent,
                    auto_compact_token_limit,
                )
            )
        ):
            cache_values = direct_cache_authority.context_by_slug.get(slug)
            if cache_values:
                context_window = cache_values["context_window"]
                max_context_window = cache_values["max_context_window"]
                effective_context_window_percent = cache_values[
                    "effective_context_window_percent"
                ]
                auto_compact_token_limit = cache_values.get("auto_compact_token_limit")
                source = direct_cache_authority.source
                freshness = direct_cache_authority.freshness
            else:
                source = DIRECT_OFFICIAL_CACHE_STATUS_SOURCE
                freshness = (
                    direct_cache_authority.freshness
                    if direct_cache_authority.source == DIRECT_OFFICIAL_CACHE_STATUS_SOURCE
                    else "missing"
                )
        signals[slug] = {
            "context_window": context_window,
            "max_context_window": max_context_window,
            "effective_context_window_percent": effective_context_window_percent,
            "auto_compact_token_limit": auto_compact_token_limit,
            "freshness": freshness,
            "source": source,
            "fallback_context_window": previous.get("model_context_window"),
            "fallback_effective_context_window_percent": previous.get(
                "effective_context_window_percent"
            ),
            "fallback_auto_compact_token_limit": previous.get(
                "model_auto_compact_token_limit"
            ),
        }
    return signals


def load_official_seed_models(
    path: Path = OFFICIAL_SEED_PATH,
    runtime_path: Path = RUNTIME_OFFICIAL_SEED_PATH,
) -> list[dict[str, Any]]:
    return load_official_seed_snapshot(path, runtime_path).models


def fallback_catalog_paths(path: Path = OLLAMA_FALLBACK_PATH) -> list[Path]:
    paths = [path]
    if path == OLLAMA_FALLBACK_PATH and RUNTIME_OLLAMA_FALLBACK_PATH not in paths:
        paths.append(RUNTIME_OLLAMA_FALLBACK_PATH)
    return paths


def load_fallback_catalog_models(path: Path = OLLAMA_FALLBACK_PATH) -> list[dict[str, Any]]:
    for candidate in fallback_catalog_paths(path):
        models = load_catalog_models(candidate)
        if models:
            return models
    return []


def model_ids_from_catalog(path: Path = OLLAMA_FALLBACK_PATH) -> list[str]:
    return [str(model["slug"]) for model in load_fallback_catalog_models(path) if model.get("slug")]


def extract_model_ids(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        raw_models = payload.get("data", payload.get("models", []))
    elif isinstance(payload, list):
        raw_models = payload
    else:
        raw_models = []

    ids: list[str] = []
    for item in raw_models:
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = item.get("id") or item.get("model") or item.get("name") or item.get("slug")
        else:
            value = None
        if value:
            ids.append(str(value))
    return ids


def discover_ollama_http(api_key: str, timeout_seconds: int = 20) -> list[str]:
    request = Request(OLLAMA_MODELS_URL, headers={"Authorization": f"Bearer {api_key}"})
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8-sig"))
    return extract_model_ids(payload)


def show_ollama_model_http(model_id: str, timeout_seconds: int = 20) -> dict[str, Any]:
    payload = json.dumps({"model": f"{canonical_model_id(model_id)}:cloud"}).encode("utf-8")
    request = Request(
        OLLAMA_SHOW_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def extract_context_length(show_payload: dict[str, Any]) -> int | None:
    model_info = show_payload.get("model_info", {})
    if not isinstance(model_info, dict):
        return None
    context_values = [
        value
        for key, value in model_info.items()
        if isinstance(key, str) and key.endswith(".context_length") and isinstance(value, int)
    ]
    if not context_values:
        return None
    return max(context_values)


def extract_capabilities(show_payload: dict[str, Any]) -> list[str]:
    capabilities = show_payload.get("capabilities", [])
    if not isinstance(capabilities, list):
        return []
    return [str(capability) for capability in capabilities]


def discover_ollama_model_metadata(model_ids: Iterable[str]) -> tuple[dict[str, dict[str, Any]], str]:
    metadata: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for model_id in dedupe_canonical_model_ids(model_ids):
        try:
            show_payload = show_ollama_model_http(model_id)
        except Exception as exc:
            failures.append(f"{model_id}:{safe_discovery_error_detail(exc)}")
            continue

        entry: dict[str, Any] = {}
        context_length = extract_context_length(show_payload)
        if context_length:
            entry["context_window"] = context_length
            entry["context_source"] = "ollama_api_show"
        capabilities = extract_capabilities(show_payload)
        if capabilities:
            entry["capabilities"] = capabilities
        if entry:
            metadata[model_id] = entry

    if failures and metadata:
        return metadata, f"partial; failures={';'.join(failures)}"
    if failures:
        return metadata, f"failed; failures={';'.join(failures)}"
    return metadata, f"ok; fetched={len(metadata)}"


def safe_discovery_error_detail(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTPError: {exc.code}"
    if isinstance(exc, json.JSONDecodeError):
        return "JSONDecodeError"
    if isinstance(exc, URLError):
        return "URLError"
    if isinstance(exc, TimeoutError):
        return "TimeoutError"
    if isinstance(exc, OSError):
        return type(exc).__name__
    return type(exc).__name__


def discover_ollama_ids() -> tuple[list[str], str, str, str]:
    api_key = os.environ.get("OLLAMA_API_KEY")
    if not api_key:
        detail = "OLLAMA_API_KEY is not set"
        ids = model_ids_from_catalog(OLLAMA_FALLBACK_PATH)
        if ids:
            return ids, "ollama_cloud_cache", "missing_api_key_cache", detail
        return [], "ollama_cloud_unavailable", "missing_api_key_unavailable", detail

    try:
        ids = discover_ollama_http(api_key)
    except Exception as exc:
        detail = safe_discovery_error_detail(exc)
        ids = model_ids_from_catalog(OLLAMA_FALLBACK_PATH)
        if ids:
            return ids, "ollama_cloud_cache", "http_failed_cache", detail
        return [], "ollama_cloud_unavailable", "http_failed_unavailable", detail

    if ids:
        return ids, "ollama_cloud_http", "ok", f"cloud HTTP returned {len(ids)} models"

    detail = "cloud HTTP returned 0 models"
    ids = model_ids_from_catalog(OLLAMA_FALLBACK_PATH)
    if ids:
        return ids, "ollama_cloud_cache", "http_empty_cache", detail

    return [], "ollama_cloud_unavailable", "http_empty_unavailable", detail


def dedupe_canonical_model_ids(model_ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for model_id in model_ids:
        slug = canonical_model_id(str(model_id))
        if not slug or slug in seen:
            continue
        seen.add(slug)
        result.append(slug)
    return result


def ordered_ollama_candidates(model_ids: Iterable[str], policy: CatalogPolicy) -> list[str]:
    discovered_slugs = set(dedupe_canonical_model_ids(model_ids))
    if not policy.allowed_ollama_cloud_models or policy.auto_include_ollama_cloud:
        return list(dedupe_canonical_model_ids(model_ids))
    return [slug for slug in policy.allowed_ollama_cloud_models if slug in discovered_slugs]


def runtime_ollama_candidates(model_ids: Iterable[str], policy: CatalogPolicy) -> list[str]:
    result: list[str] = []
    for slug in dedupe_canonical_model_ids(model_ids):
        if not should_include_external_provider_model(slug, policy):
            continue
        if not should_include_external_provider_model(f"ollama-cloud/{slug}", policy):
            continue
        result.append(slug)
    return result


def ollama_provider_model_metadata(ollama_models: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for model in ollama_models:
        slug = canonical_model_id(str(model.get("upstream_model") or model.get("alias", "")))
        if not slug:
            continue

        entry: dict[str, Any] = {}
        context_window = model.get("context_window")
        if isinstance(context_window, int) and context_window > 0:
            entry["context_window"] = context_window
            entry["context_source"] = "providers_toml"

        max_output_tokens = model.get("max_output_tokens")
        if isinstance(max_output_tokens, int) and max_output_tokens > 0:
            entry["max_output_tokens"] = max_output_tokens
            entry["max_output_source"] = "providers_toml"

        input_modalities = model.get("input_modalities")
        if isinstance(input_modalities, (list, tuple)) and input_modalities:
            entry["input_modalities"] = [str(value) for value in input_modalities if str(value)]

        if entry:
            metadata[slug] = entry
    return metadata


def fallback_model_index(fallback_models: Iterable[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for model in fallback_models or []:
        slug = model.get("slug")
        if slug:
            index[canonical_model_id(str(slug))] = model
    return index


def build_minimal_official_model(slug: str, policy: CatalogPolicy) -> dict[str, Any]:
    model = deepcopy(MINIMAL_OFFICIAL_MODEL)
    model["slug"] = slug
    model["display_name"] = display_name_for(slug, policy)
    return model


def apply_official_model_defaults(model: dict[str, Any], slug: str) -> None:
    defaults = OFFICIAL_MODEL_DEFAULTS.get(slug)
    if not defaults:
        return
    for key, value in defaults.items():
        model[key] = deepcopy(value)


def apply_pinned_official_catalog_metadata(model: dict[str, Any], slug: str) -> None:
    metadata = PINNED_OFFICIAL_CATALOG_METADATA.get(slug)
    if metadata is not None:
        model.update(deepcopy(metadata))


def normalize_official_responses_lite_opt_in(model: dict[str, Any]) -> None:
    if not isinstance(model.get("use_responses_lite"), bool):
        model["use_responses_lite"] = False


def official_proxy_alias(slug: str) -> str:
    return f"{OFFICIAL_PROXY_PROVIDER_ALIAS}/{slug}"


def is_official_gateway_fast_variant_slug(slug: str) -> bool:
    return canonical_model_id(slug) in OFFICIAL_GATEWAY_FAST_VARIANT_SLUGS


def official_sort_keys(model_id: str) -> tuple[str, str]:
    key = canonical_model_id(model_id)
    prefix = f"{OFFICIAL_PROXY_PROVIDER_ALIAS}/"
    if key.startswith(prefix):
        return key, key[len(prefix):]
    return official_proxy_alias(key), key


def sort_official_slugs(slugs: Iterable[str], sort_order: Iterable[str]) -> list[str]:
    ordered_slugs = list(slugs)
    order_index: dict[str, int] = {}
    for index, model_id in enumerate(sort_order):
        for key in official_sort_keys(str(model_id)):
            if key:
                order_index.setdefault(key, index)

    if not order_index:
        return ordered_slugs

    def sort_key(item: tuple[int, str]) -> tuple[int, int]:
        original_index, slug = item
        alias, upstream = official_sort_keys(slug)
        return order_index.get(alias, order_index.get(upstream, len(order_index) + original_index)), original_index

    return [slug for _, slug in sorted(enumerate(ordered_slugs), key=sort_key)]


def official_short_display_name(slug: str, model: dict[str, Any], policy: CatalogPolicy) -> str:
    raw_name = model.get("display_name")
    if isinstance(raw_name, str) and raw_name.strip():
        display_name = raw_name.strip()
    else:
        display_name = display_name_for(slug, policy)
    if display_name.lower().startswith("openai "):
        display_name = display_name[7:].strip()
    if display_name.lower().startswith("gpt-"):
        display_name = display_name[4:]
    return re.sub(r"[-_]+", " ", display_name).strip()


def _apply_official_context_budget(
    model: dict[str, Any],
    budget: OfficialContextBudget | None,
    *,
    source: str,
    freshness: str,
) -> None:
    proxy_metadata = dict(model.get("codex_proxy_metadata", {}))
    if budget is None:
        for key in (
            "context_window",
            "max_context_window",
            "effective_context_window_percent",
        ):
            model.pop(key, None)
        proxy_metadata["official_context_budget"] = {
            "source": source,
            "freshness": freshness,
        }
    else:
        model["context_window"] = budget.context_window
        model["max_context_window"] = budget.max_context_window
        model["effective_context_window_percent"] = budget.effective_context_window_percent
        proxy_metadata["official_context_budget"] = {
            "source": budget.source,
            "freshness": budget.freshness,
            "context_window": budget.context_window,
            "effective_context_window_percent": budget.effective_context_window_percent,
            "effective_context_window": budget.effective_context_window,
            "model_context_window": budget.model_context_window,
            "model_auto_compact_token_limit": budget.model_auto_compact_token_limit,
        }
    model["codex_proxy_metadata"] = proxy_metadata


def build_official_proxy_model(
    slug: str,
    official_by_slug: dict[str, dict[str, Any]],
    policy: CatalogPolicy,
    official_context_signals: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source_model = official_by_slug.get(slug)
    model = deepcopy(source_model or build_minimal_official_model(slug, policy))
    if source_model is not None:
        for key, value in MINIMAL_OFFICIAL_MODEL.items():
            model.setdefault(key, deepcopy(value))
    model["slug"] = slug
    model["display_name"] = official_short_display_name(slug, model, policy)
    if source_model is None:
        apply_official_model_defaults(model, slug)
    normalize_official_responses_lite_opt_in(model)
    apply_pinned_official_catalog_metadata(model, slug)
    limits = RESOLVED_MODEL_LIMITS.get(("openai", slug))
    if limits is not None and limits.max_output_tokens is not None:
        model.setdefault("max_output_tokens", limits.max_output_tokens)
    raw_context_signal = (official_context_signals or {}).get(slug)
    context_signal = raw_context_signal if isinstance(raw_context_signal, dict) else {}
    budget = resolve_official_context_budget(
        direct_context_window=context_signal.get("context_window"),
        direct_max_context_window=context_signal.get("max_context_window"),
        direct_effective_context_window_percent=context_signal.get(
            "effective_context_window_percent"
        ),
        direct_auto_compact_token_limit=context_signal.get("auto_compact_token_limit"),
        direct_freshness=str(context_signal.get("freshness", "missing")),
        direct_source=str(context_signal.get("source", "missing")),
        fallback_context_window=context_signal.get("fallback_context_window"),
        fallback_effective_context_window_percent=context_signal.get(
            "fallback_effective_context_window_percent"
        ),
        fallback_auto_compact_token_limit=context_signal.get(
            "fallback_auto_compact_token_limit"
        ),
    )
    _apply_official_context_budget(
        model,
        budget,
        source=str(context_signal.get("source", "missing")),
        freshness=str(context_signal.get("freshness", "missing")),
    )
    proxy_metadata = dict(model.get("codex_proxy_metadata", {}))
    proxy_metadata.update(
        {
            "provider": OFFICIAL_PROXY_PROVIDER_ALIAS,
            "upstream_name": "official",
            "upstream_model": slug,
        }
    )
    model["codex_proxy_metadata"] = proxy_metadata
    return model


def official_model_index(official_models: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for model in official_models:
        raw_slug = canonical_model_id(str(model.get("slug", "")))
        if not raw_slug:
            continue
        slug = raw_slug.removeprefix("openai/") if raw_slug.startswith("openai/gpt-") else raw_slug
        existing = index.get(slug)
        if existing is None:
            index[slug] = model
            continue

        existing_slug = canonical_model_id(str(existing.get("slug", "")))
        fresh = model if not raw_slug.startswith("openai/") or existing_slug.startswith("openai/") else existing
        merged = deepcopy(fresh)
        merged["enabled"] = bool(existing.get("enabled", True) or model.get("enabled", True))
        index[slug] = merged
    return index


def build_ollama_model(
    slug: str,
    policy: CatalogPolicy,
    fallback_models_by_slug: dict[str, dict[str, Any]],
    fallback_template: dict[str, Any] | None,
    model_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fallback_model = fallback_models_by_slug.get(slug)
    if fallback_model is not None:
        model = deepcopy(fallback_model)
    elif fallback_template is not None:
        model = deepcopy(fallback_template)
        model["description"] = DEFAULT_OLLAMA_MODEL["description"]
    else:
        model = deepcopy(DEFAULT_OLLAMA_MODEL)

    model["slug"] = slug
    model["display_name"] = display_name_for(slug, policy)
    model.setdefault("description", DEFAULT_OLLAMA_MODEL["description"])
    model.setdefault("visibility", "list")
    model.setdefault("supported_in_api", True)
    apply_ollama_model_limits(model, slug, model_metadata or {})
    return model


def apply_ollama_model_limits(model: dict[str, Any], slug: str, model_metadata: dict[str, dict[str, Any]]) -> None:
    apply_resolved_model_limits(model, RESOLVED_MODEL_LIMITS.get(("ollama-cloud", slug)))
    static_limits = OLLAMA_MODEL_LIMIT_OVERRIDES.get(slug, {})
    context_window = static_limits.get("context_window")
    context_source = "static_official_fallback" if context_window else None

    dynamic_metadata = model_metadata.get(slug, {})
    dynamic_context_window = dynamic_metadata.get("context_window")
    if isinstance(dynamic_context_window, int) and dynamic_context_window > 0:
        context_window = dynamic_context_window
        context_source = str(dynamic_metadata.get("context_source", "ollama_api_show"))

    if isinstance(context_window, int) and context_window > 0:
        model["context_window"] = context_window
        model["max_context_window"] = context_window

    max_output_tokens = static_limits.get("max_output_tokens")
    if isinstance(max_output_tokens, int) and max_output_tokens > 0:
        model["max_output_tokens"] = max_output_tokens

    dynamic_max_output_tokens = dynamic_metadata.get("max_output_tokens")
    if isinstance(dynamic_max_output_tokens, int) and dynamic_max_output_tokens > 0:
        model["max_output_tokens"] = dynamic_max_output_tokens

    input_modalities = dynamic_metadata.get("input_modalities")
    if isinstance(input_modalities, list) and input_modalities:
        model["input_modalities"] = [str(value) for value in input_modalities if str(value)]
    else:
        capabilities = dynamic_metadata.get("capabilities")
        if isinstance(capabilities, list):
            model["input_modalities"] = ["text", "image"] if "vision" in capabilities else ["text"]

    max_output_source = dynamic_metadata.get("max_output_source")
    if isinstance(max_output_source, str) and max_output_source:
        proxy_metadata = dict(model.get("codex_proxy_metadata", {}))
        proxy_metadata["max_output_source"] = max_output_source
        model["codex_proxy_metadata"] = proxy_metadata

    capabilities = dynamic_metadata.get("capabilities")
    if isinstance(capabilities, list) and "input_modalities" not in dynamic_metadata:
        model["input_modalities"] = ["text", "image"] if "vision" in capabilities else ["text"]

    proxy_metadata = dict(model.get("codex_proxy_metadata", {}))
    if context_source:
        proxy_metadata["context_source"] = context_source
    if isinstance(max_output_source, str) and max_output_source:
        proxy_metadata["max_output_source"] = max_output_source
    elif static_limits.get("max_output_source"):
        proxy_metadata["max_output_source"] = static_limits["max_output_source"]
    if proxy_metadata:
        model["codex_proxy_metadata"] = proxy_metadata


def build_external_provider_model(
    external_model: dict[str, Any],
    policy: CatalogPolicy,
    fallback_template: dict[str, Any] | None,
) -> dict[str, Any]:
    if fallback_template is not None:
        model = deepcopy(fallback_template)
    else:
        model = deepcopy(DEFAULT_OLLAMA_MODEL)

    alias = str(external_model["alias"])
    display_prefix = str(external_model.get("display_prefix") or external_model.get("provider_alias") or "provider")

    model["slug"] = alias
    model["display_name"] = display_name_for(alias, policy)
    description = external_model.get("description")
    model["description"] = (
        description
        if isinstance(description, str) and description.strip()
        else f"External {display_prefix} model via providers.toml."
    )
    model.setdefault("visibility", "list")
    model.setdefault("supported_in_api", True)
    model["input_modalities"] = list(external_model.get("input_modalities") or ("text",))

    explicit_reasoning_levels = external_model.get("supported_reasoning_levels")
    has_explicit_reasoning_levels = (
        isinstance(explicit_reasoning_levels, (list, tuple)) and bool(explicit_reasoning_levels)
    )
    reasoning_levels_source = (
        explicit_reasoning_levels
        if has_explicit_reasoning_levels
        else model.get("supported_reasoning_levels")
    )
    sanitized_reasoning_levels = complete_third_party_reasoning_levels(reasoning_levels_source)
    model["supported_reasoning_levels"] = sanitized_reasoning_levels

    configured_default = external_model.get("default_reasoning_level")
    default_source = (
        configured_default
        if isinstance(configured_default, str) and configured_default.strip()
        else model.get("default_reasoning_level")
    )
    normalized_default = str(default_source).strip().lower()
    supported_efforts = [item["effort"] for item in sanitized_reasoning_levels]
    if normalized_default not in supported_efforts:
        normalized_default = "xhigh" if "xhigh" in supported_efforts else supported_efforts[0]
    model["default_reasoning_level"] = normalized_default

    context_window = external_model.get("context_window")
    if isinstance(context_window, int) and context_window > 0:
        model["context_window"] = context_window
        model["max_context_window"] = context_window
    max_output_tokens = external_model.get("max_output_tokens")
    if isinstance(max_output_tokens, int) and max_output_tokens > 0:
        model["max_output_tokens"] = max_output_tokens

    proxy_metadata = dict(model.get("codex_proxy_metadata", {}))
    proxy_metadata.update(
        {
            "provider": external_model["provider_alias"],
            "upstream_name": external_model["upstream_name"],
            "upstream_model": external_model["upstream_model"],
            "upstream_format": external_model.get("upstream_format", "auto"),
            "tool_protocol": external_model.get("tool_protocol", "auto"),
        }
    )
    context_source = external_model.get("context_source")
    if context_source is not None:
        proxy_metadata["context_source"] = context_source
    max_output_source = external_model.get("max_output_source")
    if max_output_source is not None:
        proxy_metadata["max_output_source"] = max_output_source
    model["codex_proxy_metadata"] = proxy_metadata
    apply_resolved_model_limits(
        model,
        RESOLVED_MODEL_LIMITS.get(
            (str(external_model["provider_alias"]), str(external_model["upstream_model"]))
        ),
    )
    return model


def build_codex_catalog(
    official_models: Iterable[dict[str, Any]],
    ollama_ids: Iterable[str],
    policy: CatalogPolicy,
    client_version: str,
    *,
    fallback_models: Iterable[dict[str, Any]] | None = None,
    ollama_model_metadata: dict[str, dict[str, Any]] | None = None,
    external_models: Iterable[dict[str, Any]] | None = None,
    official_model_sort_order: Iterable[str] | None = None,
    disabled_official_model_ids: Iterable[str] | None = None,
    official_context_signals: dict[str, dict[str, Any]] | None = None,
    use_ollama_policy_allowlist: bool = True,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    official_by_slug = official_model_index(official_models)
    disabled_official_slugs = {official_model_disable_key(str(model_id)) for model_id in disabled_official_model_ids or []}
    official_source_slugs = list(official_by_slug.keys()) or list(policy.official_models)
    official_slugs = sort_official_slugs(
        [
            slug
            for slug in official_source_slugs
            if official_model_disable_key(str(slug)) not in disabled_official_slugs
            and not is_official_gateway_fast_variant_slug(str(slug))
        ],
        official_model_sort_order or [],
    )

    for slug in official_slugs:
        if not slug or slug in seen_slugs:
            continue
        model = build_official_proxy_model(slug, official_by_slug, policy, official_context_signals)
        models.append(model)
        seen_slugs.add(slug)

    fallback_list = list(fallback_models or [])
    fallback_by_slug = fallback_model_index(fallback_list)
    fallback_template = fallback_list[0] if fallback_list else None

    ollama_candidates = (
        ordered_ollama_candidates(ollama_ids, policy)
        if use_ollama_policy_allowlist
        else runtime_ollama_candidates(ollama_ids, policy)
    )
    for priority_offset, raw_id in enumerate(ollama_candidates):
        if use_ollama_policy_allowlist and not should_include_model(str(raw_id), policy):
            continue
        slug = canonical_model_id(str(raw_id))
        if not slug or slug in seen_slugs:
            continue
        model = build_ollama_model(slug, policy, fallback_by_slug, fallback_template, ollama_model_metadata)
        model["priority"] = OLLAMA_PRIORITY_BASE + priority_offset
        models.append(model)
        seen_slugs.add(slug)

    for priority_offset, external_model in enumerate(external_models or []):
        slug = canonical_model_id(str(external_model.get("alias", "")))
        if not slug or not should_include_external_provider_model(slug, policy):
            continue
        if slug in seen_slugs:
            continue
        model = build_external_provider_model(external_model, policy, fallback_template)
        priority_base = external_model.get("priority_base")
        model["priority"] = (priority_base if isinstance(priority_base, int) else 0) + priority_offset
        models.append(model)
        seen_slugs.add(slug)

    return {
        "fetched_at": fetched_at or utc_now_iso(),
        "client_version": client_version,
        "models": models,
    }


def diff_model_state(previous: Iterable[str], current: Iterable[str]) -> dict[str, list[str]]:
    previous_slugs = {canonical_model_id(str(slug)) for slug in previous}
    current_slugs = {canonical_model_id(str(slug)) for slug in current}
    return {
        "added": sorted(current_slugs - previous_slugs),
        "removed": sorted(previous_slugs - current_slugs),
    }


def load_previous_visible_models(path: Path = GENERATED_STATE_PATH) -> set[str]:
    data = load_json_file(path)
    models = data.get("visible_models", [])
    if not isinstance(models, list):
        return set()
    return {str(model) for model in models}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def load_cached_state(path: Path = GENERATED_STATE_PATH) -> dict[str, Any]:
    state = load_json_file(path)
    return state if state else {
        "fetched_at": "",
        "client_version": read_client_version(OFFICIAL_SEED_PATH, OLLAMA_FALLBACK_PATH),
        "discovery_source": "cache",
        "discovery_status": "cache_missing",
        "discovery_detail": "cached state is missing",
        "metadata_detail": "",
        "ollama_model_metadata": {},
        "discovered_ollama_models": [],
        "external_provider_models": [],
        "visible_models": [],
        "diff": {"added": [], "removed": []},
    }


def load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_include_official_models() -> bool:
    """Read include_official_models from settings.json (written by Rust backend).

    Defaults to True when settings file is missing or the key is absent,
    matching the Rust Settings::default().
    """
    data = load_settings()
    if not data:
        return True
    value = data.get("include_official_models")
    return value if isinstance(value, bool) else True


def load_official_model_sort_order() -> list[str]:
    data = load_settings()
    value = data.get("official_model_sort_order")
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for model_id in value:
        if not isinstance(model_id, str):
            continue
        normalized = normalize_official_model_id(model_id)
        if normalized and normalized not in output:
            output.append(normalized)
    return output


def load_official_disabled_models() -> list[str]:
    data = load_settings()
    value = data.get("official_disabled_models")
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for model_id in value:
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        normalized = official_model_disable_key(model_id)
        if normalized and normalized not in output:
            output.append(normalized)
    return output


def official_model_disable_key(model_id: str) -> str | None:
    return normalize_official_model_id(model_id)


def known_official_model_ids() -> set[str]:
    known = set(load_policy(POLICY_PATH).official_models)
    for model in load_catalog_models(RUNTIME_OFFICIAL_SEED_PATH):
        slug = canonical_model_id(str(model.get("slug", "")))
        if slug.startswith("openai/gpt-"):
            slug = slug.removeprefix("openai/")
        if slug.startswith("gpt-"):
            known.add(slug)
    return known


def normalize_official_model_id(model_id: str) -> str | None:
    value = canonical_model_id(model_id)
    if value.startswith("openai/gpt-"):
        bare = value.removeprefix("openai/")
        return bare if bare in known_official_model_ids() else None
    return value


def sync_catalog(*, max_age_seconds: int = 0) -> dict[str, Any]:
    if catalog_cache_is_fresh(max_age_seconds):
        state = load_cached_state(GENERATED_STATE_PATH)
        state["cache_status"] = "fresh"
        return state

    policy = load_policy(POLICY_PATH)
    include_official = load_include_official_models()
    official_model_sort_order = load_official_model_sort_order()
    disabled_official_models = load_official_disabled_models()
    official_snapshot = (
        load_official_seed_snapshot(OFFICIAL_SEED_PATH)
        if include_official
        else OfficialSeedSnapshot([], "missing", "missing")
    )
    official_models = official_snapshot.models
    previous_official_context_budgets = load_previous_official_context_budgets()
    direct_cache_authority = (
        load_fresh_direct_official_cache_authority(official_snapshot)
        if include_official
        else None
    )
    official_context_signals = official_context_signals_from_snapshot(
        official_snapshot,
        previous_official_context_budgets,
        direct_cache_authority=direct_cache_authority,
    )
    fallback_models = load_fallback_catalog_models(OLLAMA_FALLBACK_PATH)
    client_version = read_client_version(OFFICIAL_SEED_PATH, OLLAMA_FALLBACK_PATH)
    discovered_ids, discovery_source, discovery_status, discovery_detail = discover_ollama_ids()
    providers = load_providers()
    ollama_runtime_configured, runtime_ollama_models = catalog_visible_ollama_cloud_models(
        providers,
        require_api_key=False,
    )
    ollama_catalog_ids = (
        [str(model["upstream_model"]) for model in runtime_ollama_models]
        if ollama_runtime_configured
        else discovered_ids
    )
    external_models = catalog_visible_external_models(providers, require_api_key=False)
    discovered_slugs = dedupe_canonical_model_ids(discovered_ids)
    visible_ollama_slugs = (
        runtime_ollama_candidates(ollama_catalog_ids, policy)
        if ollama_runtime_configured
        else [
            canonical_model_id(str(slug))
            for slug in ordered_ollama_candidates(discovered_ids, policy)
            if should_include_model(str(slug), policy)
        ]
    )
    ollama_model_metadata, metadata_detail = discover_ollama_model_metadata(visible_ollama_slugs)
    if ollama_runtime_configured:
        ollama_model_metadata.update(ollama_provider_model_metadata(runtime_ollama_models))

    catalog = build_codex_catalog(
        official_models,
        ollama_catalog_ids,
        policy,
        client_version,
        fallback_models=fallback_models,
        ollama_model_metadata=ollama_model_metadata,
        external_models=external_models,
        official_model_sort_order=official_model_sort_order,
        disabled_official_model_ids=disabled_official_models,
        official_context_signals=official_context_signals,
        use_ollama_policy_allowlist=not ollama_runtime_configured,
    )
    visible_slugs = [str(model["slug"]) for model in catalog["models"] if model.get("slug")]
    previous_visible_slugs = load_previous_visible_models(GENERATED_STATE_PATH)
    diff = diff_model_state(previous_visible_slugs, visible_slugs)
    state = {
        "fetched_at": catalog["fetched_at"],
        "client_version": client_version,
        "discovery_source": discovery_source,
        "discovery_status": discovery_status,
        "discovery_detail": discovery_detail,
        "metadata_detail": metadata_detail,
        "ollama_model_metadata": ollama_model_metadata,
        "discovered_ollama_models": discovered_slugs,
        "external_provider_models": [str(model["alias"]) for model in external_models],
        "visible_models": visible_slugs,
        "diff": diff,
    }

    write_json(GENERATED_CATALOG_PATH, catalog)
    write_json(GENERATED_STATE_PATH, state)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Codex proxy model catalog from Ollama discovery.")
    parser.add_argument("--sync", action="store_true", help="discover models and write generated catalog/state files")
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=0,
        help="reuse an existing generated catalog if it is newer than this many seconds",
    )
    args = parser.parse_args(argv)

    if not args.sync:
        parser.print_help()
        return 2

    state = sync_catalog(max_age_seconds=args.max_age_seconds)
    diff = state["diff"]
    print(f"catalog={GENERATED_CATALOG_PATH}")
    print(f"state={GENERATED_STATE_PATH}")
    print(f"discovery_source={state['discovery_source']}")
    print(f"discovery_status={state['discovery_status']}")
    print(f"discovery_detail={state['discovery_detail']}")
    print(f"visible_models={len(state['visible_models'])}")
    if state.get("cache_status"):
        print(f"cache_status={state['cache_status']}")
    print(f"added={','.join(diff['added'])}")
    print(f"removed={','.join(diff['removed'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
