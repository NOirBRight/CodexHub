"""Claude Code discovery aliases for Gateway-exported models.

Claude Code 2.1.278 keeps GET /v1/models entries only when the id matches
``claude|anthropic``. Project a stable alias for every other exported slug so
the picker lists the whole catalog. Canonical ``provider/model`` remains the
routing identity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from catalog import canonical_model_id
from gateway_errors import identity_failure

CLAUDE_DISCOVERY_RE = re.compile(r"claude|anthropic", re.IGNORECASE)
_ALIAS_PREFIX = "claude-codexhub-"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def projected_model_id(canonical: str) -> str:
    slug = canonical_model_id(canonical)
    if not slug:
        raise identity_failure("model is required", reason="unsupported_model", model_slug=slug)
    if CLAUDE_DISCOVERY_RE.search(slug):
        return slug
    return _ALIAS_PREFIX + _UNSAFE.sub("-", slug).strip("-")


def projection_map(catalog: Mapping[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    models = catalog.get("models")
    if not isinstance(models, list):
        return mapping
    for model in models:
        if not isinstance(model, Mapping):
            continue
        canonical = model.get("slug")
        if not isinstance(canonical, str) or not canonical.strip():
            continue
        slug = canonical_model_id(canonical)
        projected = projected_model_id(slug)
        existing = mapping.get(projected)
        if existing is not None and existing != slug:
            raise identity_failure(
                f"Claude discovery alias collision: {projected}",
                reason="alias_collision",
                model_slug=slug,
            )
        mapping[projected] = slug
    return mapping


def resolve_projected_model_id(model_id: str, catalog: Mapping[str, Any]) -> str:
    slug = canonical_model_id(str(model_id))
    if not slug:
        raise identity_failure("model is required", reason="unsupported_model", model_slug=slug)
    mapping = projection_map(catalog)
    return mapping.get(slug, slug)


def claude_model_list(catalog: Mapping[str, Any]) -> dict[str, Any]:
    models = catalog.get("models")
    if not isinstance(models, list):
        models = []
    mapping = projection_map(catalog)
    reverse = {canonical: projected for projected, canonical in mapping.items()}
    data: list[dict[str, Any]] = []
    for model in models:
        if not isinstance(model, Mapping):
            continue
        canonical = model.get("slug")
        if not isinstance(canonical, str) or not canonical.strip():
            continue
        slug = canonical_model_id(canonical)
        metadata = model.get("codex_proxy_metadata")
        owner = metadata.get("provider") if isinstance(metadata, Mapping) else None
        data.append(
            {
                "id": reverse.get(slug, projected_model_id(slug)),
                "object": "model",
                "created": 0,
                "owned_by": owner if isinstance(owner, str) and owner.strip() else "codexhub",
            }
        )
    return {"object": "list", "data": data}
