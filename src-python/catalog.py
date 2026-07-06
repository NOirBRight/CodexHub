from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import tomllib
from typing import Any


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
    return CatalogPolicy(
        denied_models={canonical_model_id(x) for x in visibility.get("denied_models", [])},
        denied_substrings={str(x).lower() for x in visibility.get("denied_substrings", [])},
        display_names={canonical_model_id(k): str(v) for k, v in data.get("display_names", {}).items()},
        official_models=tuple(canonical_model_id(str(x)) for x in visibility.get("official_models", [])),
        allowed_ollama_cloud_models=tuple(
            canonical_model_id(str(x)) for x in visibility.get("allowed_ollama_cloud_models", [])
        ),
        allowed_provider_models=tuple(
            canonical_model_id(str(x)) for x in visibility.get("allowed_provider_models", [])
        ),
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
