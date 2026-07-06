from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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

OLLAMA_MODELS_URL = "https://ollama.com/v1/models"
OLLAMA_SHOW_URL = "https://ollama.com/api/show"
DEFAULT_CLIENT_VERSION = "0.142.0"
OLLAMA_PRIORITY_BASE = 100
DEFAULT_CACHE_MAX_AGE_SECONDS = 86400
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
    "visibility": "list",
    "supported_in_api": True,
}

OFFICIAL_FAST_SERVICE_TIERS: list[dict[str, str]] = [
    {"id": "priority", "name": "Fast", "description": "1.5x speed, increased usage"}
]
OFFICIAL_GATEWAY_FAST_VARIANT_SLUGS = {"gpt-5.5-fast", "gpt-5.4-fast"}

OFFICIAL_MODEL_DEFAULTS: dict[str, dict[str, Any]] = {
    "gpt-5.5": {
        "context_window": 258400,
        "max_context_window": 258400,
        "additional_speed_tiers": ["fast"],
        "service_tiers": OFFICIAL_FAST_SERVICE_TIERS,
        "default_reasoning_level": "medium",
    },
    "gpt-5.5-fast": {
        "context_window": 258400,
        "max_context_window": 258400,
        "default_reasoning_level": "medium",
    },
    "gpt-5.4": {
        "context_window": 272000,
        "max_context_window": 272000,
        "additional_speed_tiers": ["fast"],
        "service_tiers": OFFICIAL_FAST_SERVICE_TIERS,
        "default_reasoning_level": "medium",
    },
    "gpt-5.4-fast": {
        "context_window": 272000,
        "max_context_window": 272000,
        "default_reasoning_level": "medium",
    },
    "gpt-5.4-mini": {
        "context_window": 272000,
        "max_context_window": 272000,
        "default_reasoning_level": "medium",
    },
    "gpt-5.3-codex-spark": {
        "context_window": 128000,
        "max_context_window": 128000,
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

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def catalog_cache_dependency_paths() -> tuple[Path, ...]:
    return (
        POLICY_PATH,
        OFFICIAL_SEED_PATH,
        RUNTIME_OFFICIAL_SEED_PATH,
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
    paths = [path]
    if runtime_path not in paths:
        paths.append(runtime_path)
    return paths


def load_official_seed_models(
    path: Path = OFFICIAL_SEED_PATH,
    runtime_path: Path = RUNTIME_OFFICIAL_SEED_PATH,
) -> list[dict[str, Any]]:
    for candidate in official_seed_catalog_paths(path, runtime_path):
        models = [
            deepcopy(model)
            for model in load_catalog_models(candidate)
            if str(model.get("slug", "")).startswith("gpt-")
        ]
        if models:
            return models
    return []


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
        model.setdefault(key, deepcopy(value))


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


def build_official_proxy_model(slug: str, official_by_slug: dict[str, dict[str, Any]], policy: CatalogPolicy) -> dict[str, Any]:
    model = deepcopy(official_by_slug.get(slug) or build_minimal_official_model(slug, policy))
    alias = official_proxy_alias(slug)
    model["slug"] = alias
    model["display_name"] = f"OpenAI {display_name_for(slug, policy)}"
    model.setdefault("description", MINIMAL_OFFICIAL_MODEL["description"])
    model.setdefault("visibility", "list")
    model.setdefault("supported_in_api", True)
    apply_official_model_defaults(model, slug)
    for key, value in DEFAULT_OLLAMA_MODEL.items():
        model.setdefault(key, deepcopy(value))
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
        slug = canonical_model_id(str(model.get("slug", "")))
        if slug:
            index[slug] = model
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

    reasoning_levels = external_model.get("supported_reasoning_levels")
    if isinstance(reasoning_levels, (list, tuple)) and reasoning_levels:
        model["supported_reasoning_levels"] = [
            {
                "effort": str(level),
                "description": REASONING_LEVEL_DESCRIPTIONS.get(str(level), f"{level} reasoning effort"),
            }
            for level in reasoning_levels
            if str(level).strip()
        ]
    default_reasoning_level = external_model.get("default_reasoning_level")
    if isinstance(default_reasoning_level, str) and default_reasoning_level.strip():
        model["default_reasoning_level"] = default_reasoning_level.strip()

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
    use_ollama_policy_allowlist: bool = True,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    official_by_slug = official_model_index(official_models)
    disabled_official_slugs = {official_model_disable_key(str(model_id)) for model_id in disabled_official_model_ids or []}
    official_slugs = sort_official_slugs(
        [
            slug
            for slug in (list(policy.official_models) or list(official_by_slug.keys()))
            if official_model_disable_key(str(slug)) not in disabled_official_slugs
            and not is_official_gateway_fast_variant_slug(str(slug))
        ],
        official_model_sort_order or [],
    )

    for slug in official_slugs:
        alias = official_proxy_alias(slug)
        if not slug or alias in seen_slugs:
            continue
        model = build_official_proxy_model(slug, official_by_slug, policy)
        models.append(model)
        seen_slugs.add(alias)

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
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


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
    return [model_id for model_id in value if isinstance(model_id, str) and model_id.strip()]


def load_official_disabled_models() -> list[str]:
    data = load_settings()
    value = data.get("official_disabled_models")
    if not isinstance(value, list):
        return []
    return [official_model_disable_key(model_id) for model_id in value if isinstance(model_id, str) and model_id.strip()]


def official_model_disable_key(model_id: str) -> str:
    value = canonical_model_id(model_id)
    return value.removeprefix("openai/")


def sync_catalog(*, max_age_seconds: int = 0) -> dict[str, Any]:
    if catalog_cache_is_fresh(max_age_seconds):
        state = load_cached_state(GENERATED_STATE_PATH)
        state["cache_status"] = "fresh"
        return state

    policy = load_policy(POLICY_PATH)
    include_official = load_include_official_models()
    official_model_sort_order = load_official_model_sort_order()
    disabled_official_models = load_official_disabled_models()
    official_models = load_official_seed_models(OFFICIAL_SEED_PATH) if include_official else []
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
