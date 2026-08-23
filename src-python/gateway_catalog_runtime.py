"""Catalog-backed model routing and request-time catalog projection.

The module owns published-catalog validation, exact provider resolution, model
limits, Official context guards, fast-variant projection, and modality lookup.
Callers inject live readers so a facade can preserve request-time patches while
tests exercise the same typed seam directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import contextvars
import sys
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import tomllib
from types import MappingProxyType
from typing import Any, Protocol, TypedDict

import gateway_settings

from catalog import (
    CatalogPolicy,
    CatalogVisibility,
    canonical_model_id,
    deny_match_model_id,
    is_internal_model,
    load_catalog_models,
    load_policy,
    model_visibility,
    should_include_external_provider_model,
    should_include_model,
)
from catalog_sync import (
    CONTEXT_WINDOW_OUTPUT_FALLBACK_SOURCE,
    GENERATED_CATALOG_PATH,
    LEGACY_GENERATED_CATALOG_PATH,
    POLICY_PATH,
    existing_generated_catalog_path,
    known_official_model_ids,
    official_short_display_name,
)
from gateway_errors import (
    ModelIdentityResolutionError,
    _catalog_failure,
    _identity_failure,
)
from model_limits import (
    CURRENT_DIRECT_OFFICIAL_SOURCE,
    DEGRADED_LAST_KNOWN_OFFICIAL_SOURCE,
)
from providers_config import resolve_external_model_alias, resolve_ollama_cloud_model
from subscription_credential import provider_auth_mode, register_builtin_adapters

register_builtin_adapters()


CatalogDocument = dict[str, Any]
CatalogModel = dict[str, Any]


class CatalogUpstream(TypedDict, total=False):
    """Ephemeral resolved model facts copied into an immutable RoutePlan."""

    name: str
    provider_id: str
    model_id: str
    base_url: str
    auth: str
    api_key: str
    upstream_model: str
    upstream_format: str
    service_tier: str
    reports_cached_input_tokens: bool
    supports_developer_role: bool
    supported_reasoning_levels: tuple[str, ...]
    input_modalities: tuple[str, ...]
    tool_protocol: str
    tool_surface_strategy: str
    native_responses_tool_codec: str
    tool_protocol_capabilities: Mapping[str, Any]
    tool_exposure_mode: str
    tool_capability_state: str
    supports_search_tool: bool
    proven_tool_subset: tuple[str, ...]
    capability_manifest_version: str
    capability_manifest_hash: str
    capability_manifest_state: str
    capability_binding: Mapping[str, Any]


UpstreamFacts = CatalogUpstream
CatalogPathReader = Callable[[Path], Path]
CatalogModelsReader = Callable[[Path], list[dict[str, Any]]]
PolicyReader = Callable[[Path], CatalogPolicy]
RoutingConfigReader = Callable[[], Mapping[str, Any]]
ExternalModelReader = Callable[[str], dict[str, Any] | None]
class OllamaModelReader(Protocol):
    def __call__(self, model_id: str, *, refresh: bool = False) -> tuple[bool, dict[str, Any] | None]: ...
CatalogBySlugReader = Callable[[], dict[str, dict[str, Any]]]
OllamaRuntimeReader = Callable[[str, Any], UpstreamFacts | None]
TextReader = Callable[[Path, str], str]


def _read_text(path: Path, encoding: str) -> str:
    return path.read_text(encoding=encoding)


@dataclass(frozen=True)
class CatalogFacts:
    """Immutable catalog paths, provider identities, and routing constants."""

    generated_catalog_path: Path = GENERATED_CATALOG_PATH
    legacy_generated_catalog_path: Path = LEGACY_GENERATED_CATALOG_PATH
    policy_path: Path = POLICY_PATH
    official_base_url: str = "https://api.openai.com/v1"
    ollama_cloud_base_url: str = "https://ollama.com/v1"
    default_official_prefixes: tuple[str, ...] = ("gpt-",)
    official_alias_prefix: str = "openai/"
    ollama_cloud_alias_prefix: str = "ollama-cloud/"
    official_fast_variant_service_tier: str = "priority"
    official_fast_variant_base_models: Mapping[str, str] = field(
        default_factory=lambda: {
            "gpt-5.5-fast": "gpt-5.5",
            "gpt-5.4-fast": "gpt-5.4",
        }
    )
    official_fast_variant_display_names: Mapping[str, str] = field(
        default_factory=lambda: {
            "gpt-5.5-fast": "5.5 Fast",
            "gpt-5.4-fast": "5.4 Fast",
        }
    )
    upstream_max_output_token_caps: Mapping[str, int] = field(
        default_factory=lambda: {"minimax-m3": 131072}
    )
    official_refresh_state_filename: str = "official-refresh-state.json"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "official_fast_variant_base_models",
            MappingProxyType(dict(self.official_fast_variant_base_models)),
        )
        object.__setattr__(
            self,
            "official_fast_variant_display_names",
            MappingProxyType(dict(self.official_fast_variant_display_names)),
        )
        object.__setattr__(
            self,
            "upstream_max_output_token_caps",
            MappingProxyType(dict(self.upstream_max_output_token_caps)),
        )


@dataclass(frozen=True, slots=True)
class CatalogRuntime:
    """Typed seam for catalog reads, validation, routing, and presentation."""

    facts: CatalogFacts = field(default_factory=CatalogFacts)
    catalog_path_reader: CatalogPathReader = existing_generated_catalog_path
    catalog_models_reader: CatalogModelsReader = load_catalog_models
    text_reader: TextReader = _read_text
    policy_reader: PolicyReader = load_policy
    routing_config_reader: RoutingConfigReader = lambda: {}
    external_model_reader: ExternalModelReader = resolve_external_model_alias
    ollama_model_reader: OllamaModelReader = resolve_ollama_cloud_model
    vision_proxy_enabled_reader: Callable[[], bool] = lambda: False
    known_official_ids_reader: Callable[[], set[str]] = known_official_model_ids
    official_display_name_reader: Callable[[str, dict[str, Any], CatalogPolicy], str] = official_short_display_name
    catalog_by_slug_reader: CatalogBySlugReader | None = None
    generated_catalog_by_slug_reader: Callable[[Path], dict[str, dict[str, Any]]] | None = None
    published_model_reader: Callable[[str], dict[str, Any] | None] | None = None
    generated_official_reader: Callable[[str, Any], str | None] | None = None
    official_alias_reader: Callable[[str, Any], str | None] | None = None
    official_fast_variant_reader: Callable[[str, Any], str | None] | None = None
    ollama_runtime_reader: OllamaRuntimeReader | None = None
    ollama_alias_reader: OllamaRuntimeReader | None = None
    should_include_model_reader: Callable[[str, CatalogPolicy], bool] = should_include_model
    should_include_external_model_reader: Callable[[str, CatalogPolicy], bool] = should_include_external_provider_model
    model_visibility_reader: Callable[..., CatalogVisibility] = model_visibility
    internal_model_reader: Callable[[Any], bool] = is_internal_model
    official_base_url_reader: Callable[[], str] | None = None
    ollama_base_url_reader: Callable[[], str] | None = None
    official_fast_projection_reader: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    context_guard_reader: Callable[..., dict[str, Any]] | None = None
    vision_projection_reader: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    canonical_models_reader: Callable[[list[Any], CatalogPolicy], list[Any]] | None = None
    modalities_reader: Callable[[Any], bool] | None = None
    input_modalities_reader: Callable[[str | None, Mapping[str, Any] | None], Any] | None = None
    published_budget_reader: Callable[[Path], dict[str, Mapping[str, Any]]] | None = None

    def _resolved_catalog_path(self, path: Path) -> Path:
        return self.catalog_path_reader(path)

    def official_prefixes(self) -> tuple[str, ...]:
        prefixes = self.routing_config_reader().get(
            "official_prefixes", self.facts.default_official_prefixes
        )
        if not isinstance(prefixes, list):
            return self.facts.default_official_prefixes
        values = tuple(str(prefix) for prefix in prefixes if str(prefix))
        return values or self.facts.default_official_prefixes

    def official_base_url(self) -> str:
        if self.official_base_url_reader is not None:
            return self.official_base_url_reader()
        value = self.routing_config_reader().get(
            "official_upstream_base_url", self.facts.official_base_url
        )
        return str(value).rstrip("/") if value else self.facts.official_base_url

    def ollama_cloud_base_url(self) -> str:
        if self.ollama_base_url_reader is not None:
            return self.ollama_base_url_reader()
        value = self.routing_config_reader().get(
            "ollama_cloud_base_url", self.facts.ollama_cloud_base_url
        )
        return str(value).rstrip("/") if value else self.facts.ollama_cloud_base_url

    def catalog_identity_slug(self, slug: str) -> str:
        """Return the single exact route identity used by the catalog."""

        value = canonical_model_id(slug)
        if value.startswith("openai/gpt-"):
            return value.removeprefix("openai/")
        return value

    def generated_catalog_by_slug(
        self, path: Path | None = None
    ) -> dict[str, dict[str, Any]]:
        selected_path = path or self.facts.generated_catalog_path
        resolved_path = self._resolved_catalog_path(selected_path)
        try:
            if resolved_path.exists():
                document = json.loads(self.text_reader(resolved_path, "utf-8-sig"))
                if not isinstance(document, Mapping) or not isinstance(
                    document.get("models"), list
                ):
                    raise ValueError("catalog root must contain a models list")
            catalog_models = self.catalog_models_reader(resolved_path)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ModelIdentityResolutionError(
                "published catalog is malformed and cannot authorize routing",
                classification="catalog_inconsistency",
                reason="malformed_catalog",
            ) from exc

        if not isinstance(catalog_models, list):
            raise ModelIdentityResolutionError(
                "published catalog models must be a list",
                classification="catalog_inconsistency",
                reason="malformed_catalog",
            )
        models: dict[str, dict[str, Any]] = {}
        for model in catalog_models:
            if not isinstance(model, Mapping):
                raise ModelIdentityResolutionError(
                    "published catalog contains a non-object model row",
                    classification="catalog_inconsistency",
                    reason="malformed_model_row",
                )
            raw_slug_value = model.get("slug")
            if not isinstance(raw_slug_value, str) or not raw_slug_value.strip():
                raise ModelIdentityResolutionError(
                    "published catalog contains a model without a slug",
                    classification="catalog_inconsistency",
                    reason="missing_catalog_slug",
                )
            slug = self.catalog_identity_slug(canonical_model_id(raw_slug_value))
            if slug in models:
                raise ModelIdentityResolutionError(
                    "published catalog contains duplicate canonical model identity",
                    classification="catalog_inconsistency",
                    reason="duplicate_canonical_slug",
                    model_slug=slug,
                )
            models[slug] = dict(model)
        return models

    def generated_catalog_slugs(self, path: Path | None = None) -> set[str]:
        reader = self.generated_catalog_by_slug_reader or self.generated_catalog_by_slug
        return set(reader(path))

    def _catalog_by_slug(self) -> dict[str, dict[str, Any]]:
        if self.catalog_by_slug_reader is not None:
            return self.catalog_by_slug_reader()
        return self.generated_catalog_by_slug()

    def published_catalog_model(self, slug: str) -> dict[str, Any] | None:
        resolved_path = self._resolved_catalog_path(self.facts.generated_catalog_path)
        if resolved_path == self.facts.legacy_generated_catalog_path:
            raise ModelIdentityResolutionError(
                "current generated catalog is missing; legacy catalog cannot authorize routing",
                classification="catalog_inconsistency",
                reason="stale_legacy_catalog",
            )
        catalog = (
            self.catalog_by_slug_reader()
            if self.catalog_by_slug_reader is not None
            else self.generated_catalog_by_slug(resolved_path)
        )
        identity_slug = self.catalog_identity_slug(slug)
        model = catalog.get(identity_slug)
        if model is None and identity_slug.startswith("gpt-"):
            model = catalog.get(f"openai/{identity_slug}")
        return model

    def _published_model(self, slug: str) -> dict[str, Any] | None:
        if self.published_model_reader is not None:
            return self.published_model_reader(slug)
        return self.published_catalog_model(slug)

    def _generated_official(self, slug: str, policy: Any) -> str | None:
        if self.generated_official_reader is not None:
            return self.generated_official_reader(slug, policy)
        return self.generated_official_upstream_model(slug, policy)

    def _official_alias(self, slug: str, policy: Any) -> str | None:
        if self.official_alias_reader is not None:
            return self.official_alias_reader(slug, policy)
        return self.official_alias_upstream_model(slug, policy)

    def _official_fast_variant(self, slug: str, policy: Any) -> str | None:
        if self.official_fast_variant_reader is not None:
            return self.official_fast_variant_reader(slug, policy)
        return self.official_fast_variant_upstream_model(slug, policy)

    def _ollama_alias(self, slug: str, policy: Any) -> UpstreamFacts | None:
        if self.ollama_alias_reader is not None:
            return self.ollama_alias_reader(slug, policy)
        return self.ollama_cloud_alias_upstream_model(slug, policy)

    def is_internal_route_identity(self, value: Any) -> bool:
        values: list[Any]
        if isinstance(value, Mapping):
            if self.internal_model_reader(value):
                return True
            values = [
                value.get(key)
                for key in (
                    "id", "slug", "model", "name", "alias", "matched_alias",
                    "model_id", "upstream_model",
                )
            ]
        else:
            values = [value]
        for raw_value in values:
            if not isinstance(raw_value, str):
                continue
            identity = canonical_model_id(raw_value).strip().lower()
            if identity and any(
                part.strip() == "codex-auto-review" for part in identity.split("/")
            ):
                return True
        return False

    def validate_published_model_for_provider(
        self,
        model: Mapping[str, Any],
        *,
        provider_id: str,
        model_slug: str,
        expected_upstream_model: str | None = None,
    ) -> None:
        if self.is_internal_route_identity(model):
            raise _identity_failure(
                f"model identity is internal and cannot be routed: {model_slug}",
                reason="internal_model",
                provider_id=provider_id,
                model_slug=model_slug,
            )
        if self.model_visibility_reader(model, missing_is_list=True) is not CatalogVisibility.LIST:
            raise _catalog_failure(
                "published catalog model is not explicitly listable",
                reason="unsupported_visibility",
                provider_id=provider_id,
                model_slug=model_slug,
            )
        if "supported_in_api" in model and not isinstance(
            model["supported_in_api"], bool
        ):
            raise _catalog_failure(
                "published catalog model supported_in_api is malformed",
                reason="malformed_supported_in_api",
                provider_id=provider_id,
                model_slug=model_slug,
            )
        if model.get("supported_in_api") is False:
            raise _identity_failure(
                f"model identity is not supported in the API: {model_slug}",
                reason="unsupported_model",
                provider_id=provider_id,
                model_slug=model_slug,
            )
        raw_slug = model.get("slug")
        if isinstance(raw_slug, str) and canonical_model_id(raw_slug) != canonical_model_id(
            model_slug
        ):
            raise _catalog_failure(
                "published catalog row slug contradicts the requested model slug",
                reason="configured_model_mismatch",
                provider_id=provider_id,
                model_slug=model_slug,
            )
        metadata = model.get("codex_proxy_metadata")
        if "codex_proxy_metadata" in model and not isinstance(metadata, Mapping):
            raise _catalog_failure(
                "published catalog model metadata is malformed",
                reason="malformed_metadata",
                provider_id=provider_id,
                model_slug=model_slug,
            )
        if isinstance(metadata, Mapping):
            catalog_provider = canonical_model_id(str(metadata.get("provider") or ""))
            allowed_providers = {provider_id, provider_id.replace("-", "_")}
            if not catalog_provider or catalog_provider not in allowed_providers:
                raise _catalog_failure(
                    f"model identity belongs to another provider: {model_slug}",
                    reason="provider_mismatch",
                    provider_id=provider_id,
                    model_slug=model_slug,
                )
            expected_upstream = canonical_model_id(
                expected_upstream_model or model_slug
            )
            catalog_upstream = metadata.get("upstream_model")
            if not isinstance(catalog_upstream, str) or canonical_model_id(
                catalog_upstream
            ) != expected_upstream:
                raise _catalog_failure(
                    "published catalog row upstream_model contradicts the requested model slug",
                    reason="upstream_model_mismatch",
                    provider_id=provider_id,
                    model_slug=model_slug,
                )
            catalog_upstream_name = canonical_model_id(
                str(metadata.get("upstream_name") or "")
            )
            if catalog_upstream_name not in allowed_providers:
                raise _catalog_failure(
                    "published catalog row upstream_name contradicts the provider identity",
                    reason="upstream_name_mismatch",
                    provider_id=provider_id,
                    model_slug=model_slug,
                )

    @staticmethod
    def provider_catalog_failure(
        message: str, *, provider_id: str, model_slug: str
    ) -> ModelIdentityResolutionError:
        return _catalog_failure(
            message,
            reason="provider_model_index_inconsistency",
            provider_id=provider_id,
            model_slug=model_slug,
        )

    def resolve_external_model(self, slug: str) -> dict[str, Any] | None:
        try:
            return self.external_model_reader(slug)
        except ModelIdentityResolutionError:
            raise
        except ValueError as exc:
            raise self.provider_catalog_failure(
                "external provider model index is inconsistent",
                provider_id=slug.partition("/")[0] or "external",
                model_slug=slug,
            ) from exc

    def resolve_ollama_cloud_model(
        self, model_id: str, *, require_api_key: bool
    ) -> tuple[bool, dict[str, Any] | None]:
        try:
            return self.ollama_model_reader(
                model_id, require_api_key=require_api_key
            )
        except ModelIdentityResolutionError:
            raise
        except ValueError as exc:
            raise self.provider_catalog_failure(
                "Ollama Cloud model index is inconsistent",
                provider_id="ollama-cloud",
                model_slug=canonical_model_id(model_id),
            ) from exc

    def catalog_output_limit(self, model_id: str) -> tuple[int | None, bool]:
        slug = canonical_model_id(model_id)
        model = self._catalog_by_slug().get(self.catalog_identity_slug(slug))
        cap = self.facts.upstream_max_output_token_caps.get(slug)
        valid_cap = cap if isinstance(cap, int) and cap > 0 else None
        if not model:
            return valid_cap, False
        value = model.get("max_output_tokens")
        catalog_value = value if isinstance(value, int) and value > 0 else None
        if valid_cap is not None:
            return (
                min(catalog_value, valid_cap)
                if catalog_value is not None
                else valid_cap
            ), False
        metadata = model.get("codex_proxy_metadata")
        context_fallback = (
            isinstance(metadata, Mapping)
            and metadata.get("max_output_source")
            == CONTEXT_WINDOW_OUTPUT_FALLBACK_SOURCE
        )
        return catalog_value, context_fallback

    def catalog_max_output_tokens(self, model_id: str) -> int | None:
        return self.catalog_output_limit(model_id)[0]

    @staticmethod
    def policy_denies_model(model_id: Any, policy: Any) -> bool:
        slug = canonical_model_id(str(model_id))
        if not slug:
            return False
        if slug in policy.denied_models or deny_match_model_id(slug) in policy.denied_models:
            return True
        lowered = slug.lower()
        return any(part in lowered for part in policy.denied_substrings)

    def policy_denies_any_model(self, model_ids: tuple[Any, ...], policy: Any) -> bool:
        return any(
            model_id is not None and self.policy_denies_model(model_id, policy)
            for model_id in model_ids
        )

    def generated_official_upstream_model(
        self, slug: str, policy: Any
    ) -> str | None:
        prefix = self.facts.official_alias_prefix
        upstream_model = slug[len(prefix):] if slug.startswith(prefix) else slug
        if not upstream_model.startswith(self.official_prefixes()):
            return None
        alias = f"{prefix}{upstream_model}"
        model = self._published_model(upstream_model)
        if not model:
            return None
        if self.is_internal_route_identity(model):
            raise _identity_failure(
                f"model identity is internal and cannot be routed: {slug}",
                reason="internal_model",
                provider_id="openai",
                model_slug=upstream_model,
            )
        if self.model_visibility_reader(model, missing_is_list=True) is not CatalogVisibility.LIST:
            raise _catalog_failure(
                "published catalog model is not explicitly listable",
                reason="unsupported_visibility",
                provider_id="openai",
                model_slug=upstream_model,
            )
        if "supported_in_api" in model and not isinstance(
            model["supported_in_api"], bool
        ):
            raise _catalog_failure(
                "official catalog model supported_in_api is malformed",
                reason="malformed_supported_in_api",
                provider_id="openai",
                model_slug=upstream_model,
            )
        if model.get("supported_in_api") is False:
            raise _identity_failure(
                f"model identity is not supported in the API: {slug}",
                reason="unsupported_model",
                provider_id="openai",
                model_slug=upstream_model,
            )
        metadata = model.get("codex_proxy_metadata")
        if "codex_proxy_metadata" in model and not isinstance(metadata, Mapping):
            raise _catalog_failure(
                "official catalog model metadata is malformed",
                reason="malformed_metadata",
                provider_id="openai",
                model_slug=upstream_model,
            )
        if not isinstance(metadata, Mapping):
            if upstream_model == "gpt-5.5" and self.should_include_model_reader(
                upstream_model, policy
            ):
                return upstream_model
            raise _catalog_failure(
                "official catalog row is missing its upstream identity binding",
                reason="missing_catalog_metadata",
                provider_id="openai",
                model_slug=upstream_model,
            )
        catalog_upstream = canonical_model_id(
            str(metadata.get("upstream_model", ""))
        )
        if (
            metadata.get("provider") != "openai"
            or metadata.get("upstream_name") != "official"
            or catalog_upstream != upstream_model
            or not catalog_upstream.startswith(self.official_prefixes())
        ):
            raise _catalog_failure(
                "official catalog row has contradictory upstream identity metadata",
                reason="upstream_model_mismatch",
                provider_id="openai",
                model_slug=upstream_model,
            )
        if self.policy_denies_any_model((slug, alias, catalog_upstream), policy):
            raise _identity_failure(
                f"model is not allowed: {slug}",
                reason="denied_model",
                provider_id="openai",
                model_slug=upstream_model,
            )
        return catalog_upstream

    def official_alias_upstream_model(self, slug: str, policy: Any) -> str | None:
        prefix = self.facts.official_alias_prefix
        if not slug.startswith(prefix):
            return None
        upstream_model = slug[len(prefix):]
        if self.policy_denies_any_model((slug, upstream_model), policy):
            raise _identity_failure(
                f"model is not allowed: {slug}",
                reason="denied_model",
                provider_id="openai",
                model_slug=upstream_model,
            )
        if not upstream_model.startswith(self.official_prefixes()):
            return None
        return self._generated_official(slug, policy)

    def official_fast_variant_upstream_model(
        self, slug: str, policy: Any
    ) -> str | None:
        prefix = self.facts.official_alias_prefix
        fast_model = slug[len(prefix):] if slug.startswith(prefix) else slug
        upstream_model = self.facts.official_fast_variant_base_models.get(fast_model)
        if upstream_model is None:
            return None
        upstream_alias = f"{prefix}{upstream_model}"
        if self.policy_denies_any_model(
            (slug, fast_model, upstream_model, upstream_alias), policy
        ):
            raise _identity_failure(
                f"model is not allowed: {slug}",
                reason="denied_model",
                provider_id="openai",
                model_slug=fast_model,
            )
        if not upstream_model.startswith(self.official_prefixes()):
            return None
        return self._generated_official(upstream_model, policy)

    @staticmethod
    def route_capability_metadata(source: Mapping[str, Any]) -> dict[str, Any]:
        keys = (
            "tool_protocol_capabilities", "tool_exposure_mode",
            "tool_capability_state", "supports_search_tool", "proven_tool_subset",
            "capability_manifest_version", "capability_manifest_hash",
            "capability_manifest_state", "capability_binding",
        )
        return {key: source[key] for key in keys if key in source}

    def ollama_cloud_runtime_upstream(
        self, model_id: str, policy: Any
    ) -> UpstreamFacts | None:
        configured, runtime_model = self.resolve_ollama_cloud_model(
            model_id, require_api_key=False
        )
        if not configured:
            return None
        slug = canonical_model_id(model_id)
        if runtime_model is None:
            raise _identity_failure(
                f"model is not allowed: {slug}", reason="unsupported_model",
                provider_id="ollama-cloud", model_slug=slug,
            )
        if self.is_internal_route_identity(slug) or self.is_internal_route_identity(
            runtime_model
        ):
            raise _identity_failure(
                f"model identity is internal and cannot be routed: {slug}",
                reason="internal_model", provider_id="ollama-cloud", model_slug=slug,
            )
        if runtime_model.get("matched_alias"):
            raise _identity_failure(
                "model aliases are presentation-only and cannot authorize routing",
                reason="model_alias_not_routable", provider_id="ollama-cloud",
                model_slug=slug,
            )
        policy_alias = runtime_model.get(
            "alias", f"{self.facts.ollama_cloud_alias_prefix}{slug}"
        )
        upstream_model = runtime_model.get("upstream_model")
        if not isinstance(upstream_model, str) or not upstream_model.strip():
            raise _catalog_failure(
                "Ollama Cloud configuration is missing upstream_model",
                reason="missing_upstream_model", provider_id="ollama-cloud",
                model_slug=slug,
            )
        upstream_model = upstream_model.strip()
        configured_id = canonical_model_id(
            str(runtime_model.get("model_id") or slug)
        )
        if configured_id != slug and configured_id != f"{self.facts.ollama_cloud_alias_prefix}{slug}":
            raise _catalog_failure(
                "Ollama Cloud configuration model identity does not match the request",
                reason="configured_model_mismatch", provider_id="ollama-cloud",
                model_slug=slug,
            )
        if self.policy_denies_any_model((slug, policy_alias, upstream_model), policy):
            raise _identity_failure(
                f"model is not allowed: {slug}", reason="denied_model",
                provider_id="ollama-cloud", model_slug=slug,
            )
        api_key = runtime_model.get("api_key")
        upstream: UpstreamFacts = {
            "name": "ollama_cloud", "provider_id": "ollama-cloud",
            "model_id": slug,
            "base_url": runtime_model.get("base_url") or self.ollama_cloud_base_url(),
            "auth": "api_key" if api_key else "ollama_api_key",
            "upstream_model": upstream_model,
            "upstream_format": runtime_model.get("upstream_format", "responses"),
            "tool_protocol": runtime_model.get("tool_protocol", "auto"),
            "tool_surface_strategy": runtime_model.get("tool_surface_strategy", "eager"),
            "native_responses_tool_codec": runtime_model.get(
                "native_responses_tool_codec", "none"
            ),
            "reports_cached_input_tokens": False,
            "input_modalities": tuple(
                runtime_model.get("input_modalities") or ("text",)
            ),
            **self.route_capability_metadata(runtime_model),
        }
        if api_key:
            upstream["api_key"] = api_key
        return upstream

    def _runtime_ollama(self, model_id: str, policy: Any) -> UpstreamFacts | None:
        if self.ollama_runtime_reader is not None:
            return self.ollama_runtime_reader(model_id, policy)
        return self.ollama_cloud_runtime_upstream(model_id, policy)

    def ollama_cloud_alias_upstream_model(
        self, slug: str, policy: Any
    ) -> UpstreamFacts | None:
        prefix = self.facts.ollama_cloud_alias_prefix
        if not slug.startswith(prefix):
            return None
        upstream_model = slug[len(prefix):]
        if not upstream_model:
            return None
        if self.policy_denies_any_model((slug, upstream_model), policy):
            raise _identity_failure(
                f"model is not allowed: {slug}", reason="denied_model",
                provider_id="ollama-cloud", model_slug=slug,
            )
        runtime_upstream = self._runtime_ollama(slug, policy)
        if runtime_upstream is not None:
            return runtime_upstream
        if not (
            self.should_include_model_reader(slug, policy)
            or self.should_include_model_reader(upstream_model, policy)
        ):
            raise _identity_failure(
                f"model is not allowed: {slug}", reason="unsupported_model",
                provider_id="ollama-cloud", model_slug=slug,
            )
        model = self._published_model(slug)
        if model is None:
            raise _identity_failure(
                f"model is not in the generated cloud catalog: {upstream_model}",
                reason="missing_catalog_model", provider_id="ollama-cloud",
                model_slug=slug,
            )
        self.validate_published_model_for_provider(
            model, provider_id="ollama-cloud", model_slug=slug,
            expected_upstream_model=upstream_model,
        )
        return {
            "name": "ollama_cloud", "provider_id": "ollama-cloud",
            "model_id": slug, "base_url": self.ollama_cloud_base_url(),
            "auth": "ollama_api_key", "upstream_model": upstream_model,
            "reports_cached_input_tokens": False,
        }

    def _official_upstream(self, slug: str, upstream_model: str) -> UpstreamFacts:
        return {
            "name": "official", "provider_id": "openai", "model_id": slug,
            "base_url": self.official_base_url(), "auth": "codex_auth",
            "upstream_model": upstream_model, "reports_cached_input_tokens": True,
        }

    def choose_upstream(self, model_id: str) -> UpstreamFacts:
        slug = canonical_model_id(str(model_id))
        if not slug:
            raise ValueError("model is required")
        policy = self.policy_reader(self.facts.policy_path)
        official_fast = self._official_fast_variant(slug, policy)
        if official_fast is not None:
            upstream = self._official_upstream(slug, official_fast)
            upstream["service_tier"] = self.facts.official_fast_variant_service_tier
            return upstream
        official_alias = self._official_alias(slug, policy)
        if official_alias is not None:
            return self._official_upstream(slug, official_alias)
        discovered_official = self._generated_official(slug, policy)
        if discovered_official is not None:
            return self._official_upstream(slug, discovered_official)
        ollama_alias = self._ollama_alias(slug, policy)
        if ollama_alias is not None:
            return ollama_alias
        if slug.startswith(self.official_prefixes()):
            raise _identity_failure(
                f"model is not in the generated Official catalog: {slug}",
                reason="missing_catalog_model", provider_id="openai", model_slug=slug,
            )
        external_model = self.resolve_external_model(slug)
        if external_model is not None:
            return self._external_upstream(slug, external_model, policy)
        if "/" in slug:
            raise _identity_failure(
                f"external provider model is not configured: {slug}",
                reason="unsupported_model", provider_id=slug.partition("/")[0],
                model_slug=slug,
            )
        runtime_ollama = self._runtime_ollama(slug, policy)
        if runtime_ollama is not None:
            return runtime_ollama
        if not self.should_include_model_reader(slug, policy):
            raise _identity_failure(
                f"model is not allowed: {slug}", reason="unsupported_model",
                provider_id="ollama-cloud", model_slug=slug,
            )
        model = self._published_model(slug)
        if model is not None:
            self.validate_published_model_for_provider(
                model, provider_id="ollama-cloud", model_slug=slug,
                expected_upstream_model=slug,
            )
            return {
                "name": "ollama_cloud", "provider_id": "ollama-cloud",
                "model_id": slug, "base_url": self.ollama_cloud_base_url(),
                "auth": "ollama_api_key", "reports_cached_input_tokens": False,
                "upstream_model": slug,
            }
        raise _identity_failure(
            f"model is not in the generated cloud catalog: {slug}",
            reason="missing_catalog_model", provider_id="ollama-cloud",
            model_slug=slug,
        )

    def _external_upstream(
        self, slug: str, external_model: Mapping[str, Any], policy: Any
    ) -> UpstreamFacts:
        provider_hint = str(external_model.get("provider_alias") or "") or None
        if self.is_internal_route_identity(slug) or self.is_internal_route_identity(
            external_model
        ):
            raise _identity_failure(
                f"model identity is internal and cannot be routed: {slug}",
                reason="internal_model", provider_id=provider_hint, model_slug=slug,
            )
        policy_alias = external_model.get("alias", slug)
        if self.policy_denies_any_model(
            (slug, policy_alias, external_model.get("matched_alias")), policy
        ):
            raise _identity_failure(
                f"model is not allowed: {slug}", reason="denied_model",
                provider_id=provider_hint, model_slug=slug,
            )
        if not self.should_include_external_model_reader(policy_alias, policy):
            raise _identity_failure(
                f"model is not allowed: {slug}", reason="unsupported_model",
                provider_id=provider_hint, model_slug=slug,
            )
        provider_id = canonical_model_id(provider_hint or "")
        configured_model = canonical_model_id(
            str(external_model.get("alias") or "")
        )
        upstream_model_value = external_model.get("upstream_model")
        if (
            not provider_id or not configured_model
            or not isinstance(upstream_model_value, str)
            or not upstream_model_value.strip()
        ):
            raise _catalog_failure(
                "external provider configuration is missing exact model identity",
                reason="missing_upstream_model", provider_id=provider_id or None,
                model_slug=slug,
            )
        if configured_model != slug:
            raise _identity_failure(
                "model aliases are presentation-only and cannot authorize routing",
                reason="model_alias_not_routable", provider_id=provider_id,
                model_slug=slug,
            )
        if canonical_model_id(upstream_model_value).lower() == "codex-auto-review":
            raise _identity_failure(
                f"model identity is internal and cannot be routed: {slug}",
                reason="internal_model", provider_id=provider_id, model_slug=slug,
            )
        auth_mode = provider_auth_mode(provider_id) or "api_key"
        return {
            "name": external_model["upstream_name"], "provider_id": provider_id,
            "model_id": slug, "base_url": external_model["base_url"],
            "auth": auth_mode, "api_key": external_model["api_key"],
            "upstream_model": external_model["upstream_model"],
            "upstream_format": external_model.get("upstream_format", "responses"),
            "tool_protocol": external_model.get("tool_protocol", "auto"),
            "tool_surface_strategy": external_model.get("tool_surface_strategy", "eager"),
            "native_responses_tool_codec": external_model.get(
                "native_responses_tool_codec", "none"
            ),
            "reports_cached_input_tokens": bool(
                external_model.get("reports_cached_input_tokens")
            ),
            "supports_developer_role": bool(
                external_model.get("supports_developer_role", True)
            ),
            "supported_reasoning_levels": tuple(
                external_model.get("supported_reasoning_levels") or ()
            ),
            "input_modalities": tuple(
                external_model.get("input_modalities") or ("text",)
            ),
            **self.route_capability_metadata(external_model),
        }

    def current_catalog_data(self) -> CatalogDocument:
        catalog_path = self._resolved_catalog_path(self.facts.generated_catalog_path)
        if not catalog_path.exists():
            return {"models": []}
        published_budgets = (self.published_budget_reader or self.published_official_context_budgets)(catalog_path)
        catalog = json.loads(self.text_reader(catalog_path, "utf-8-sig"))
        fast_projection = self.official_fast_projection_reader or self.catalog_with_official_fast_variants
        context_guard = self.context_guard_reader or self.catalog_with_openai_context_guard
        vision_projection = self.vision_projection_reader or self.catalog_with_vision_proxy_capabilities
        return vision_projection(
            context_guard(
                fast_projection(catalog),
                published_budgets,
                require_published_snapshot=True,
            )
        )

    @staticmethod
    def openai_model_list(catalog: Mapping[str, Any]) -> dict[str, Any]:
        models = catalog.get("models")
        if not isinstance(models, list):
            models = []
        data: list[dict[str, str]] = []
        for model in models:
            if not isinstance(model, Mapping):
                continue
            model_id = model.get("slug")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            metadata = model.get("codex_proxy_metadata")
            owner = metadata.get("provider") if isinstance(metadata, Mapping) else None
            data.append({
                "id": model_id, "object": "model",
                "owned_by": owner if isinstance(owner, str) and owner.strip() else "codexhub",
            })
        return {"object": "list", "data": data}

    def published_official_context_budgets(
        self, catalog_path: Path
    ) -> dict[str, Mapping[str, Any]]:
        state_path = catalog_path.parent / self.facts.official_refresh_state_filename
        try:
            payload = json.loads(self.text_reader(state_path, "utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, Mapping) or payload.get("publication_ready") is not True:
            return {}
        budgets = payload.get("published_context_budgets")
        if not isinstance(budgets, Mapping):
            return {}
        return {
            str(model_id): budget for model_id, budget in budgets.items()
            if isinstance(model_id, str) and isinstance(budget, Mapping)
        }

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value > 0
            else None
        )

    def catalog_with_openai_context_guard(
        self,
        catalog: dict[str, Any],
        published_budgets: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        require_published_snapshot: bool = False,
    ) -> dict[str, Any]:
        models = catalog.get("models")
        if not isinstance(models, list):
            return catalog

        def guarded_model(model: Any) -> Any:
            if not isinstance(model, Mapping):
                return model

            def without_context_budget() -> dict[str, Any]:
                updated = dict(model)
                updated.pop("context_window", None)
                updated.pop("max_context_window", None)
                updated.pop("effective_context_window_percent", None)
                return updated

            metadata = model.get("codex_proxy_metadata")
            if not isinstance(metadata, Mapping):
                return model
            if metadata.get("provider") != "openai" or metadata.get("upstream_name") != "official":
                return model
            budget = metadata.get("official_context_budget")
            if not isinstance(budget, Mapping):
                return without_context_budget()
            source = budget.get("source")
            freshness = budget.get("freshness")
            trusted = (
                source == CURRENT_DIRECT_OFFICIAL_SOURCE and freshness == "fresh"
            ) or source == DEGRADED_LAST_KNOWN_OFFICIAL_SOURCE
            if not trusted:
                return without_context_budget()
            guard_window = self._positive_int(budget.get("model_context_window"))
            if guard_window is None:
                guard_window = self._positive_int(budget.get("context_window"))
            effective_percent = self._positive_int(
                budget.get("effective_context_window_percent")
            )
            effective_window = self._positive_int(budget.get("effective_context_window"))
            auto_compact_limit = self._positive_int(
                budget.get("model_auto_compact_token_limit")
            )
            if (
                guard_window is None or effective_percent is None
                or effective_percent > 100 or effective_window is None
                or effective_window > guard_window or auto_compact_limit is None
                or auto_compact_limit > effective_window
            ):
                return without_context_budget()
            if require_published_snapshot:
                if not isinstance(published_budgets, Mapping):
                    return without_context_budget()
                model_id = str(model.get("slug", "")).removeprefix("openai/")
                upstream_model = metadata.get("upstream_model")
                expected = published_budgets.get(model_id)
                if expected is None and isinstance(upstream_model, str):
                    expected = published_budgets.get(
                        upstream_model.removeprefix("openai/")
                    )
                if not isinstance(expected, Mapping) or any(
                    expected.get(key) != budget.get(key)
                    for key in (
                        "model_context_window", "effective_context_window_percent",
                        "effective_context_window", "model_auto_compact_token_limit",
                    )
                ):
                    return without_context_budget()
            reported = [
                value for value in (
                    self._positive_int(model.get("context_window")),
                    self._positive_int(model.get("max_context_window")),
                ) if value is not None
            ]
            guarded_window = min(guard_window, *reported) if reported else guard_window
            return {
                **model, "context_window": guarded_window,
                "max_context_window": guarded_window,
                "effective_context_window_percent": effective_percent,
            }

        updated = dict(catalog)
        updated["models"] = [guarded_model(model) for model in models]
        return updated

    def catalog_with_vision_proxy_capabilities(
        self, catalog: dict[str, Any]
    ) -> dict[str, Any]:
        if not self.vision_proxy_enabled_reader():
            return catalog
        models = catalog.get("models")
        if not isinstance(models, list):
            return catalog
        updated = dict(catalog)
        updated["models"] = [
            {
                **model,
                "input_modalities": list(dict.fromkeys([
                    *(model.get("input_modalities") or ["text"]), "image"
                ])),
            } if isinstance(model, Mapping) else model
            for model in models
        ]
        return updated

    def canonical_catalog_models(
        self, models: list[Any], policy: CatalogPolicy
    ) -> list[Any]:
        known_official_ids = self.known_official_ids_reader()
        for model in models:
            if isinstance(model, Mapping):
                slug = canonical_model_id(str(model.get("slug", "")))
                if slug.startswith("gpt-"):
                    known_official_ids.add(slug)
        output: list[Any] = []
        positions: dict[str, int] = {}
        bare_sources: dict[str, bool] = {}
        prefix = self.facts.official_alias_prefix
        for model in models:
            if not isinstance(model, Mapping):
                output.append(model)
                continue
            raw_slug = canonical_model_id(str(model.get("slug", "")))
            is_legacy_alias = raw_slug.startswith(prefix + "gpt-")
            if is_legacy_alias:
                canonical_slug = raw_slug[len(prefix):]
                if canonical_slug not in known_official_ids:
                    continue
            elif raw_slug.startswith("gpt-"):
                canonical_slug = raw_slug
            else:
                output.append(model)
                continue
            candidate = deepcopy(dict(model))
            candidate["slug"] = canonical_slug
            candidate["display_name"] = self.official_display_name_reader(
                canonical_slug, candidate, policy
            )
            position = positions.get(canonical_slug)
            if position is None:
                positions[canonical_slug] = len(output)
                bare_sources[canonical_slug] = not is_legacy_alias
                output.append(candidate)
                continue
            existing = output[position]
            existing_is_bare = bare_sources.get(canonical_slug, False)
            fresh = (
                candidate if not is_legacy_alias or not existing_is_bare
                else deepcopy(dict(existing))
            )
            if "enabled" in existing or "enabled" in candidate:
                fresh["enabled"] = bool(
                    existing.get("enabled", True) or candidate.get("enabled", True)
                )
            output[position] = fresh
            bare_sources[canonical_slug] = existing_is_bare or not is_legacy_alias
        return output

    def catalog_with_official_fast_variants(
        self, catalog: dict[str, Any]
    ) -> dict[str, Any]:
        models = catalog.get("models")
        if not isinstance(models, list):
            return catalog
        policy = self.policy_reader(self.facts.policy_path)
        models = (self.canonical_models_reader or self.canonical_catalog_models)(models, policy)
        catalog["models"] = models
        by_slug = {
            canonical_model_id(str(model.get("slug", ""))): model
            for model in models if isinstance(model, Mapping)
        }
        for fast_model, upstream_model in self.facts.official_fast_variant_base_models.items():
            legacy_base_slug = f"{self.facts.official_alias_prefix}{upstream_model}"
            base_model = by_slug.get(upstream_model) or by_slug.get(legacy_base_slug)
            if not isinstance(base_model, Mapping) or fast_model in by_slug:
                continue
            fast_entry = deepcopy(dict(base_model))
            fast_entry["slug"] = fast_model
            fast_entry["display_name"] = self.facts.official_fast_variant_display_names.get(
                fast_model, f"{base_model.get('display_name', upstream_model)} Fast"
            )
            metadata = dict(fast_entry.get("codex_proxy_metadata", {}))
            metadata.update({
                "provider": "openai", "upstream_model": upstream_model,
                "service_tier": self.facts.official_fast_variant_service_tier,
            })
            fast_entry["codex_proxy_metadata"] = metadata
            models.append(fast_entry)
            by_slug[fast_model] = fast_entry
        return catalog

    @staticmethod
    def modalities_include_image(value: Any) -> bool:
        if not isinstance(value, (list, tuple, set)):
            return False
        return any(str(item).lower() == "image" for item in value)

    def catalog_input_modalities(
        self, model_id: str | None, upstream: Mapping[str, Any] | None = None
    ) -> Any:
        if self.input_modalities_reader is not None:
            return self.input_modalities_reader(model_id, upstream)
        candidates: list[str] = []
        for value in (
            model_id, upstream.get("upstream_model") if upstream else None
        ):
            if not isinstance(value, str) or not value.strip():
                continue
            slug = canonical_model_id(value)
            if not slug:
                continue
            candidates.append(slug)
            if slug.startswith(self.facts.official_alias_prefix):
                candidates.append(slug[len(self.facts.official_alias_prefix):])
            else:
                candidates.append(f"{self.facts.official_alias_prefix}{slug}")
        catalog = self._catalog_by_slug()
        for candidate in dict.fromkeys(candidates):
            model = catalog.get(self.catalog_identity_slug(candidate))
            if isinstance(model, Mapping) and "input_modalities" in model:
                return model.get("input_modalities")
        return None

    def model_supports_image(
        self, model_id: str | None, upstream: Mapping[str, Any] | None = None
    ) -> bool:
        image_reader = self.modalities_reader or self.modalities_include_image
        if upstream and image_reader(upstream.get("input_modalities")):
            return True
        return image_reader(self.catalog_input_modalities(model_id, upstream))


def load_routing_config(path: Path = POLICY_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    routing = data.get("routing", {})
    return routing if isinstance(routing, dict) else {}


def generated_catalog_by_slug(
    path: Path = GENERATED_CATALOG_PATH,
) -> dict[str, dict[str, Any]]:
    return default_catalog_runtime().generated_catalog_by_slug(path)


def generated_catalog_slugs(path: Path = GENERATED_CATALOG_PATH) -> set[str]:
    return default_catalog_runtime().generated_catalog_slugs(path)


def published_catalog_model(slug: str) -> dict[str, Any] | None:
    return default_catalog_runtime().published_catalog_model(slug)


def ollama_cloud_runtime_upstream(model_id: str, policy: Any) -> dict[str, Any] | None:
    return default_catalog_runtime().ollama_cloud_runtime_upstream(model_id, policy)


def ollama_cloud_alias_upstream_model(slug: str, policy: Any) -> dict[str, Any] | None:
    return default_catalog_runtime().ollama_cloud_alias_upstream_model(slug, policy)


def choose_upstream(model_id: str) -> dict[str, Any]:
    return default_catalog_runtime().choose_upstream(model_id)


def official_upstream() -> dict[str, Any]:
    runtime = default_catalog_runtime()
    return {
        "name": "official",
        "provider_id": "openai",
        "base_url": runtime.official_base_url(),
        "auth": "codex_auth",
        "reports_cached_input_tokens": True,
    }


def current_catalog_data() -> CatalogDocument:
    return default_catalog_runtime().current_catalog_data()


def catalog_max_output_tokens(model_id: str) -> int | None:
    return default_catalog_runtime().catalog_max_output_tokens(model_id)


def official_prefixes() -> tuple[str, ...]:
    return default_catalog_runtime().official_prefixes()


def official_base_url() -> str:
    return default_catalog_runtime().official_base_url()


def ollama_cloud_base_url() -> str:
    return default_catalog_runtime().ollama_cloud_base_url()


def catalog_identity_slug(slug: str) -> str:
    return default_catalog_runtime().catalog_identity_slug(slug)


def is_internal_route_identity(value: Any) -> bool:
    return default_catalog_runtime().is_internal_route_identity(value)


def validate_published_model_for_provider(
    model: Mapping[str, Any],
    *,
    provider_id: str,
    model_slug: str,
    expected_upstream_model: str | None = None,
) -> None:
    default_catalog_runtime().validate_published_model_for_provider(
        model,
        provider_id=provider_id,
        model_slug=model_slug,
        expected_upstream_model=expected_upstream_model,
    )


def provider_catalog_failure(
    message: str,
    *,
    provider_id: str,
    model_slug: str,
) -> ModelIdentityResolutionError:
    return CatalogRuntime.provider_catalog_failure(
        message,
        provider_id=provider_id,
        model_slug=model_slug,
    )


def resolve_external_model(slug: str) -> dict[str, Any] | None:
    return default_catalog_runtime().resolve_external_model(slug)


def resolve_ollama_cloud_model_checked(
    model_id: str,
    *,
    require_api_key: bool,
) -> tuple[bool, dict[str, Any] | None]:
    return default_catalog_runtime().resolve_ollama_cloud_model(
        model_id,
        require_api_key=require_api_key,
    )


def catalog_output_limit(model_id: str) -> tuple[int | None, bool]:
    return default_catalog_runtime().catalog_output_limit(model_id)


def policy_denies_model(model_id: Any, policy: Any) -> bool:
    return CatalogRuntime.policy_denies_model(model_id, policy)


def policy_denies_any_model(model_ids: tuple[Any, ...], policy: Any) -> bool:
    return default_catalog_runtime().policy_denies_any_model(model_ids, policy)


def generated_official_catalog_upstream_model(slug: str, policy: Any) -> str | None:
    return default_catalog_runtime().generated_official_upstream_model(slug, policy)


def official_alias_upstream_model(slug: str, policy: Any) -> str | None:
    return default_catalog_runtime().official_alias_upstream_model(slug, policy)


def official_fast_variant_upstream_model(slug: str, policy: Any) -> str | None:
    return default_catalog_runtime().official_fast_variant_upstream_model(slug, policy)


def route_capability_metadata(source: Mapping[str, Any]) -> dict[str, Any]:
    return CatalogRuntime.route_capability_metadata(source)


def openai_model_list(catalog: Mapping[str, Any]) -> dict[str, Any]:
    return CatalogRuntime.openai_model_list(catalog)


def published_official_context_budgets(catalog_path: Path) -> dict[str, Mapping[str, Any]]:
    return default_catalog_runtime().published_official_context_budgets(catalog_path)


def catalog_with_openai_context_guard(
    catalog: dict[str, Any],
    published_budgets: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    require_published_snapshot: bool = False,
) -> dict[str, Any]:
    return default_catalog_runtime().catalog_with_openai_context_guard(
        catalog,
        published_budgets,
        require_published_snapshot=require_published_snapshot,
    )


def catalog_with_vision_proxy_capabilities(catalog: dict[str, Any]) -> dict[str, Any]:
    return default_catalog_runtime().catalog_with_vision_proxy_capabilities(catalog)


def catalog_with_official_fast_variants(catalog: dict[str, Any]) -> dict[str, Any]:
    return default_catalog_runtime().catalog_with_official_fast_variants(catalog)


def canonical_catalog_models(
    models: list[Any],
    policy: CatalogPolicy,
) -> list[Any]:
    return default_catalog_runtime().canonical_catalog_models(models, policy)


def modalities_include_image(value: Any) -> bool:
    return CatalogRuntime.modalities_include_image(value)


def catalog_input_modalities(
    model_id: str | None,
    upstream: Mapping[str, Any] | None = None,
) -> Any:
    return default_catalog_runtime().catalog_input_modalities(model_id, upstream)


def model_supports_image(
    model_id: str | None,
    upstream: Mapping[str, Any] | None = None,
) -> bool:
    return default_catalog_runtime().model_supports_image(model_id, upstream)


# Originals captured so request-time wiring can detect monkeypatched module
# functions without freezing them into the seam.
_OWNED_GENERATED_CATALOG_BY_SLUG = generated_catalog_by_slug
_OWNED_GENERATED_CATALOG_SLUGS = generated_catalog_slugs
_OWNED_PUBLISHED_CATALOG_MODEL = published_catalog_model
_OWNED_CHOOSE_UPSTREAM = choose_upstream
_OWNED_OFFICIAL_UPSTREAM = official_upstream
_OWNED_CURRENT_CATALOG_DATA = current_catalog_data
_OWNED_CATALOG_MAX_OUTPUT_TOKENS = catalog_max_output_tokens
_OWNED_OLLAMA_CLOUD_RUNTIME = ollama_cloud_runtime_upstream
_OWNED_OLLAMA_CLOUD_ALIAS = ollama_cloud_alias_upstream_model
_OWNED_LOAD_POLICY = load_policy
_OWNED_LOAD_CATALOG_MODELS = load_catalog_models
_OWNED_CATALOG_PATH_READER = existing_generated_catalog_path
_OWNED_EXTERNAL_MODEL_READER = resolve_external_model_alias
_OWNED_OLLAMA_MODEL_READER = resolve_ollama_cloud_model
_OWNED_SHOULD_INCLUDE_MODEL = should_include_model
_CATALOG_ORIGINAL_OFFICIAL_BASE_URL = official_base_url
_CATALOG_ORIGINAL_OLLAMA_BASE_URL = ollama_cloud_base_url
_CATALOG_ORIGINAL_FAST_VARIANTS = catalog_with_official_fast_variants
_CATALOG_ORIGINAL_CONTEXT_GUARD = catalog_with_openai_context_guard
_CATALOG_ORIGINAL_VISION_PROJECTION = catalog_with_vision_proxy_capabilities
_CATALOG_ORIGINAL_CANONICAL_MODELS = canonical_catalog_models
_CATALOG_ORIGINAL_MODALITIES = modalities_include_image
_CATALOG_ORIGINAL_INPUT_MODALITIES = catalog_input_modalities
_CATALOG_ORIGINAL_PUBLISHED_BUDGETS = published_official_context_budgets
_CATALOG_HOOK_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "catalog_hook_depth", default=0
)


def _patched(current: Any, original: Any) -> Any | None:
    """Return the monkeypatched module function, or None when unpatched."""
    return None if current is original else current


def _catalog_override(candidate: Callable[..., Any], original: Callable[..., Any]) -> Callable[..., Any] | None:
    if candidate is original or _CATALOG_HOOK_DEPTH.get() > 0:
        return None

    def invoke(*args: Any, **kwargs: Any) -> Any:
        token = _CATALOG_HOOK_DEPTH.set(_CATALOG_HOOK_DEPTH.get() + 1)
        try:
            return candidate(*args, **kwargs)
        finally:
            _CATALOG_HOOK_DEPTH.reset(token)

    return invoke


def default_catalog_runtime() -> CatalogRuntime:
    """Build a request-time catalog seam so module-level monkeypatches stay live."""

    patched_by_slug = _patched(generated_catalog_by_slug, _OWNED_GENERATED_CATALOG_BY_SLUG)
    patched_published = _patched(published_catalog_model, _OWNED_PUBLISHED_CATALOG_MODEL)
    def _live_ollama_model_reader(
        model_id: str, *, require_api_key: bool = True
    ) -> tuple[bool, dict[str, Any] | None]:
        module = sys.modules[__name__]
        return module.resolve_ollama_cloud_model(model_id, require_api_key=require_api_key)

    return CatalogRuntime(
        catalog_path_reader=lambda path: existing_generated_catalog_path(path),
        catalog_models_reader=lambda path: load_catalog_models(path),
        policy_reader=lambda path: load_policy(path),
        routing_config_reader=lambda: load_routing_config(),
        external_model_reader=lambda slug: resolve_external_model_alias(slug),
        ollama_model_reader=_live_ollama_model_reader,
        vision_proxy_enabled_reader=gateway_settings.gateway_image_proxy_enabled,
        official_base_url_reader=_catalog_override(official_base_url, _CATALOG_ORIGINAL_OFFICIAL_BASE_URL),
        ollama_base_url_reader=_catalog_override(ollama_cloud_base_url, _CATALOG_ORIGINAL_OLLAMA_BASE_URL),
        official_fast_projection_reader=_catalog_override(catalog_with_official_fast_variants, _CATALOG_ORIGINAL_FAST_VARIANTS),
        context_guard_reader=_catalog_override(catalog_with_openai_context_guard, _CATALOG_ORIGINAL_CONTEXT_GUARD),
        vision_projection_reader=_catalog_override(catalog_with_vision_proxy_capabilities, _CATALOG_ORIGINAL_VISION_PROJECTION),
        canonical_models_reader=_catalog_override(canonical_catalog_models, _CATALOG_ORIGINAL_CANONICAL_MODELS),
        modalities_reader=_catalog_override(modalities_include_image, _CATALOG_ORIGINAL_MODALITIES),
        input_modalities_reader=_catalog_override(catalog_input_modalities, _CATALOG_ORIGINAL_INPUT_MODALITIES),
        generated_catalog_by_slug_reader=patched_by_slug,
        published_budget_reader=_catalog_override(published_official_context_budgets, _CATALOG_ORIGINAL_PUBLISHED_BUDGETS),
        known_official_ids_reader=known_official_model_ids,
        official_display_name_reader=official_short_display_name,
        catalog_by_slug_reader=(lambda: patched_by_slug()) if patched_by_slug is not None else None,
        published_model_reader=patched_published,
        generated_official_reader=generated_official_catalog_upstream_model,
        official_alias_reader=official_alias_upstream_model,
        official_fast_variant_reader=official_fast_variant_upstream_model,
        ollama_runtime_reader=_patched(ollama_cloud_runtime_upstream, _OWNED_OLLAMA_CLOUD_RUNTIME),
        ollama_alias_reader=_patched(ollama_cloud_alias_upstream_model, _OWNED_OLLAMA_CLOUD_ALIAS),
        should_include_model_reader=lambda slug, policy: should_include_model(slug, policy),
        should_include_external_model_reader=should_include_external_provider_model,
        model_visibility_reader=model_visibility,
        internal_model_reader=is_internal_model,
    )


__all__ = [
    "CatalogFacts",
    "CatalogRuntime",
    "CatalogUpstream",
    "POLICY_PATH",
    "GENERATED_CATALOG_PATH",
    "canonical_catalog_models",
    "catalog_identity_slug",
    "catalog_input_modalities",
    "catalog_max_output_tokens",
    "catalog_output_limit",
    "catalog_with_official_fast_variants",
    "catalog_with_openai_context_guard",
    "catalog_with_vision_proxy_capabilities",
    "choose_upstream",
    "current_catalog_data",
    "default_catalog_runtime",
    "existing_generated_catalog_path",
    "generated_catalog_by_slug",
    "generated_catalog_slugs",
    "generated_official_catalog_upstream_model",
    "is_internal_route_identity",
    "load_catalog_models",
    "load_policy",
    "load_routing_config",
    "modalities_include_image",
    "model_supports_image",
    "official_alias_upstream_model",
    "official_base_url",
    "official_fast_variant_upstream_model",
    "official_prefixes",
    "official_upstream",
    "ollama_cloud_alias_upstream_model",
    "ollama_cloud_base_url",
    "ollama_cloud_runtime_upstream",
    "openai_model_list",
    "policy_denies_any_model",
    "policy_denies_model",
    "provider_catalog_failure",
    "published_catalog_model",
    "published_official_context_budgets",
    "resolve_external_model",
    "resolve_external_model_alias",
    "resolve_ollama_cloud_model",
    "resolve_ollama_cloud_model_checked",
    "route_capability_metadata",
    "should_include_model",
    "validate_published_model_for_provider",
]
