from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
import re
import tomllib
from typing import Any


class CatalogVisibility(str, Enum):
    """The only visibility states accepted at a catalog boundary."""

    LIST = "list"
    HIDE = "hide"
    UNKNOWN = "unknown"


INTERNAL_MODEL_IDENTIFIERS = frozenset({"codex-auto-review"})
MAX_VISIBILITY_DIAGNOSTIC_COUNT = 100


@dataclass(frozen=True)
class CatalogPolicy:
    denied_models: set[str]
    denied_substrings: set[str]
    display_names: dict[str, str]
    official_models: tuple[str, ...] = field(default_factory=tuple)
    allowed_ollama_cloud_models: tuple[str, ...] = field(default_factory=tuple)
    allowed_provider_models: tuple[str, ...] = field(default_factory=tuple)
    auto_include_ollama_cloud: bool = False


def canonical_model_id(model_id: str) -> str:
    value = model_id.strip()
    if value.endswith(":cloud"):
        value = value[:-6]
    return value


def catalog_visibility(value: Any) -> CatalogVisibility:
    if isinstance(value, CatalogVisibility):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == CatalogVisibility.LIST.value:
            return CatalogVisibility.LIST
        if normalized == CatalogVisibility.HIDE.value:
            return CatalogVisibility.HIDE
    return CatalogVisibility.UNKNOWN


def model_visibility(
    model: Any,
    *,
    missing_is_list: bool = False,
) -> CatalogVisibility:
    """Resolve upstream visibility without trusting unknown values.

    Older hand-authored catalog fixtures omitted visibility entirely.  Callers
    that operate on those trusted legacy records can opt into the historical
    list default; native/upstream ingestion remains fail-closed.
    """

    if not isinstance(model, dict):
        return CatalogVisibility.UNKNOWN
    if model.get("hidden") is True:
        return CatalogVisibility.HIDE
    if "visibility" in model:
        return catalog_visibility(model.get("visibility"))
    hidden = model.get("hidden")
    if isinstance(hidden, bool):
        return CatalogVisibility.HIDE if hidden else CatalogVisibility.LIST
    return CatalogVisibility.LIST if missing_is_list else CatalogVisibility.UNKNOWN


def model_identity_values(model: Any) -> tuple[str, ...]:
    if not isinstance(model, dict):
        return ()
    values: list[str] = []
    for key in ("id", "slug", "model", "name", "upstream_model"):
        value = model.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = canonical_model_id(value).lower()
        if normalized.startswith("openai/"):
            normalized = normalized.removeprefix("openai/")
        values.append(normalized)
    return tuple(values)


def is_internal_model(model: Any) -> bool:
    return any(
        value in INTERNAL_MODEL_IDENTIFIERS
        or any(value.startswith(f"{identifier}/") for identifier in INTERNAL_MODEL_IDENTIFIERS)
        for value in model_identity_values(model)
    )


def is_catalog_model_listable(
    model: Any,
    *,
    missing_is_list: bool = False,
) -> bool:
    return (
        not is_internal_model(model)
        and model_visibility(model, missing_is_list=missing_is_list) is CatalogVisibility.LIST
    )


def catalog_visibility_diagnostics(models: Any) -> dict[str, int]:
    """Return bounded aggregate visibility diagnostics without model identities."""

    counts = {"hidden": 0, "unknown": 0, "internal": 0}
    for model in models if isinstance(models, (list, tuple)) else ():
        if is_internal_model(model):
            key = "internal"
        else:
            visibility = model_visibility(model, missing_is_list=False)
            key = (
                "hidden"
                if visibility is CatalogVisibility.HIDE
                else "unknown"
                if visibility is CatalogVisibility.UNKNOWN
                else ""
            )
        if key:
            counts[key] = min(MAX_VISIBILITY_DIAGNOSTIC_COUNT, counts[key] + 1)
    return counts


def deny_match_model_id(model_id: str) -> str:
    value = canonical_model_id(model_id)
    if "/" in value:
        return value
    base, separator, tag = value.rpartition(":")
    if separator and base and tag:
        return base
    return value


def load_policy(path: Path) -> CatalogPolicy:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    visibility = data.get("visibility", {})
    import maintained_catalog

    denied_models = {canonical_model_id(x) for x in visibility.get("denied_models", [])}
    display_names = dict(maintained_catalog.display_names())
    display_names.update({canonical_model_id(k): str(v) for k, v in data.get("display_names", {}).items()})
    allowed_ollama = tuple(
        dict.fromkeys(
            [
                *(canonical_model_id(str(x)) for x in visibility.get("allowed_ollama_cloud_models", [])),
                *maintained_catalog.allowed_ollama_cloud_model_ids(),
            ]
        )
    )
    allowed_providers = tuple(
        dict.fromkeys(
            [
                *(canonical_model_id(str(x)) for x in visibility.get("allowed_provider_models", [])),
                *maintained_catalog.allowed_provider_slugs(),
            ]
        )
    )
    return CatalogPolicy(
        denied_models=denied_models,
        denied_substrings={str(x).lower() for x in visibility.get("denied_substrings", [])},
        display_names=display_names,
        official_models=tuple(canonical_model_id(str(x)) for x in visibility.get("official_models", [])),
        allowed_ollama_cloud_models=allowed_ollama,
        allowed_provider_models=allowed_providers,
        auto_include_ollama_cloud=bool(visibility.get("auto_include_ollama_cloud", False)),
    )


def should_include_model(model_id: str, policy: CatalogPolicy) -> bool:
    slug = canonical_model_id(model_id)
    if not slug:
        return False
    lowered = slug.lower()
    if slug in policy.denied_models or deny_match_model_id(slug) in policy.denied_models:
        return False
    if any(part in lowered for part in policy.denied_substrings):
        return False
    if slug in policy.official_models:
        return True
    if slug in policy.allowed_ollama_cloud_models:
        return True
    if slug in policy.allowed_provider_models:
        return True
    return policy.auto_include_ollama_cloud


def should_include_external_provider_model(model_id: str, policy: CatalogPolicy) -> bool:
    slug = canonical_model_id(model_id)
    if not slug:
        return False
    lowered = slug.lower()
    deny_candidates = {slug, deny_match_model_id(slug)}
    if "/" in slug:
        _provider, _separator, provider_model_id = slug.partition("/")
        if provider_model_id:
            deny_candidates.add(provider_model_id)
            deny_candidates.add(deny_match_model_id(provider_model_id))
    if any(candidate in policy.denied_models for candidate in deny_candidates):
        return False
    return not any(part in lowered for part in policy.denied_substrings)


def display_name_for(model_id: str, policy: CatalogPolicy) -> str:
    slug = canonical_model_id(model_id)
    if slug in policy.display_names:
        return policy.display_names[slug]
    words = re.split(r"[-_/]+", slug)
    return " ".join(word.upper() if len(word) <= 3 else word.capitalize() for word in words)


def load_catalog_models(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return list(data.get("models", []))
