"""Claude Code discovery aliases for Gateway-exported models.

Claude Code 2.1.278 keeps GET /v1/models entries only when the id matches
``claude|anthropic``. Project a stable alias for every other exported slug so
the picker lists the whole catalog. Canonical ``provider/model`` remains the
routing identity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from catalog import canonical_model_id
from gateway_errors import identity_failure

CLAUDE_DISCOVERY_RE = re.compile(r"claude|anthropic", re.IGNORECASE)
_ALIAS_PREFIX = "claude-codexhub-"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
ROLE_ENV = {
    "main": "ANTHROPIC_MODEL",
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "subagent": "CLAUDE_CODE_SUBAGENT_MODEL",
}


@dataclass(frozen=True)
class RoleBinding:
    role: str
    env_var: str
    canonical: str
    projected: str
    valid: bool
    diagnostic: str | None = None


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


def exported_canonical_ids(catalog: Mapping[str, Any]) -> set[str]:
    return set(projection_map(catalog).values())


def bind_role_mappings(
    catalog: Mapping[str, Any],
    mappings: Mapping[str, str],
    *,
    default_model: str | None = None,
) -> tuple[RoleBinding, ...]:
    exported = exported_canonical_ids(catalog)
    default_slug = canonical_model_id(default_model) if default_model else ""
    bindings: list[RoleBinding] = []
    for role, canonical in mappings.items():
        env_var = ROLE_ENV.get(role)
        if env_var is None:
            raise identity_failure(
                f"unknown Claude Code role: {role}",
                reason="unknown_role",
                model_slug=str(canonical),
            )
        slug = canonical_model_id(str(canonical))
        projected = projected_model_id(slug) if slug else ""
        if role == "main" and default_slug and slug != default_slug:
            raise identity_failure(
                "main role mapping contradicts the default model",
                reason="role_default_conflict",
                model_slug=slug,
            )
        if not slug or slug not in exported:
            bindings.append(
                RoleBinding(
                    role=role,
                    env_var=env_var,
                    canonical=slug,
                    projected=projected,
                    valid=False,
                    diagnostic=f"mapping target is not in the exported catalog: {slug or canonical}",
                )
            )
            continue
        bindings.append(
            RoleBinding(
                role=role,
                env_var=env_var,
                canonical=slug,
                projected=projected,
                valid=True,
            )
        )
    return tuple(bindings)


def client_environment(
    catalog: Mapping[str, Any],
    *,
    default_model: str | None = None,
    mappings: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], tuple[RoleBinding, ...]]:
    bindings = bind_role_mappings(catalog, mappings or {}, default_model=default_model)
    env: dict[str, str] = {}
    if default_model:
        slug = canonical_model_id(default_model)
        if slug not in exported_canonical_ids(catalog):
            raise identity_failure(
                f"default model is not in the exported catalog: {slug}",
                reason="invalid_default_model",
                model_slug=slug,
            )
        env["ANTHROPIC_MODEL"] = projected_model_id(slug)
    for binding in bindings:
        if not binding.valid:
            continue
        env[binding.env_var] = binding.projected
    return env, bindings
