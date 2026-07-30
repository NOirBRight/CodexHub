from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib
from typing import Any, Iterable
from urllib.request import Request, urlopen

from atomic_io import atomic_write_text, provider_catalog_transaction_guard
from catalog import canonical_model_id


DEFAULT_PROVIDERS_PATH = Path(__file__).resolve().parents[1] / "config" / "providers.toml"


def runtime_providers_path() -> Path:
    """Return the active providers.toml path.

    Prefer the runtime copy at CODEX_HOME/proxy/config/providers.toml (written by
    the Tauri/React UI), falling back to the bundled repo config when the runtime
    copy does not exist yet. This keeps catalog_sync, proxy routing, and the Rust
    backend reading the same source of truth.
    """
    codex_home_env = os.environ.get("CODEX_HOME")
    if codex_home_env:
        codex_home = Path(codex_home_env)
    else:
        try:
            codex_home = Path.home() / ".codex"
        except (RuntimeError, OSError):
            return DEFAULT_PROVIDERS_PATH
    runtime_path = codex_home / "proxy" / "config" / "providers.toml"
    if runtime_path.exists():
        return runtime_path
    return DEFAULT_PROVIDERS_PATH

OFFICIAL_OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
ENV_PLACEHOLDER_RE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
EXTERNAL_PROVIDER_UPSTREAM_NAMES = {
    "ollama-cloud": "ollama_cloud",
    "minimax-cn": "minimax_cn",
    "volc": "volcengine",
}
EXTERNAL_PROVIDER_EXCLUDED_IDS = {"ollama-cloud"}
UPSTREAM_FORMATS = {"auto", "responses", "chat_completions", "anthropic_messages"}
TOOL_PROTOCOLS = {"auto", "responses_structured", "chat_tools", "text_compat", "none"}
TOOL_SURFACE_STRATEGIES = {"eager", "deferred_core"}
NATIVE_RESPONSES_TOOL_CODECS = {"none", "strict_apply_patch"}
CAPABILITY_PROFILE_SCHEMA_VERSION = 1
CAPABILITY_PROFILE_PROTOCOLS = {"responses", "chat_completions"}
QUALIFICATION_STATES = {
    "supported",
    "unsupported",
    "unqualified",
    "temporarily_unavailable",
    "degraded",
}


@dataclass
class CapabilityProfile:
    schema_version: int
    upstream_protocol: str
    tool_profile: str
    collaboration_backend: str
    collaboration_version: str
    capability_manifest_version: str
    capability_manifest_hash: str
    qualification_state: str


@dataclass
class ModelConfig:
    id: str
    upstream_model: str | None = None
    aliases: tuple[str, ...] = ()
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_modalities: tuple[str, ...] = ("text",)
    supported_reasoning_levels: tuple[str, ...] = ()
    default_reasoning_level: str | None = None
    tool_surface_strategy: str | None = None
    native_responses_tool_codec: str | None = None
    capability_profiles: tuple[CapabilityProfile, ...] = ()
    _capability_profiles_explicit: bool = field(
        default=False, init=False, repr=False, compare=False
    )
    _bundled_tool_surface_strategy: str | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _bundled_native_responses_tool_codec: str | None = field(
        default=None, init=False, repr=False, compare=False
    )
    sort_order: int = 0
    enabled: bool = True
    codex_enabled: bool = True
    gateway_exported: bool = True


@dataclass
class ProviderConfig:
    id: str
    name: str
    base_url: str
    api_key: str
    upstream_format: str = "auto"
    available_upstream_formats: tuple[str, ...] = ()
    tool_protocol: str = "auto"
    tool_surface_strategy: str = "eager"
    native_responses_tool_codec: str = "none"
    _tool_surface_strategy_explicit: bool = field(
        default=False, init=False, repr=False, compare=False
    )
    _bundled_tool_surface_strategy: str | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _native_responses_tool_codec_explicit: bool = field(
        default=False, init=False, repr=False, compare=False
    )
    _bundled_native_responses_tool_codec: str | None = field(
        default=None, init=False, repr=False, compare=False
    )
    reports_cached_input_tokens: bool = False
    supports_developer_role: bool = True
    display_prefix: str | None = None
    sort_order: int = 0
    enabled: bool = True
    models: list[ModelConfig] = field(default_factory=list)

    def resolved_api_key(self) -> str | None:
        configured_api_key = self.api_key.strip()
        if not configured_api_key:
            return None
        match = ENV_PLACEHOLDER_RE.fullmatch(configured_api_key)
        if match:
            env_api_key = os.environ.get(match.group(1))
            if env_api_key is None:
                return None
            resolved_api_key = env_api_key.strip()
            return resolved_api_key or None
        return configured_api_key


def discover_official_models(api_key: str, timeout_seconds: int = 20) -> list[dict[str, Any]]:
    headers = {"Accept": "application/json"}
    stripped_api_key = api_key.strip()
    if stripped_api_key:
        headers["Authorization"] = f"Bearer {stripped_api_key}"

    request = Request(OFFICIAL_OPENAI_MODELS_URL, headers=headers)
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))

    models_by_id: dict[str, dict[str, Any]] = {}
    for raw_model in _provider_models_payload_items(payload):
        model_id = _official_model_id(raw_model)
        if not model_id.startswith("gpt-") or model_id in models_by_id:
            continue
        models_by_id[model_id] = {
            "id": model_id,
            "context_window": _discovered_numeric_limit(
                raw_model,
                ("context_window", "max_context_window", "context_length"),
                "context",
            ),
            "max_output_tokens": _discovered_numeric_limit(
                raw_model,
                ("max_output_tokens", "output_tokens"),
                "output",
            ),
        }

    return [models_by_id[model_id] for model_id in sorted(models_by_id)]


def discover_provider_models(base_url: str, api_key: str, timeout_seconds: int = 20) -> list[dict[str, Any]]:
    headers = {"Accept": "application/json"}
    stripped_api_key = api_key.strip()
    if stripped_api_key:
        headers["Authorization"] = f"Bearer {stripped_api_key}"

    request = Request(base_url.rstrip("/") + "/models", headers=headers)
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))

    raw_models = _provider_models_payload_items(payload)
    models: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_model in raw_models:
        model_id = _discovered_model_id(raw_model)
        if not model_id or model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        models.append(
            {
                "id": model_id,
                "context_window": _discovered_numeric_limit(
                    raw_model,
                    ("context_window", "max_context_window", "context_length"),
                    "context",
                ),
                "max_output_tokens": _discovered_numeric_limit(
                    raw_model,
                    ("max_output_tokens", "output_tokens"),
                    "output",
                ),
            }
        )
    return models


def build_external_model_index(
    providers: Iterable[ProviderConfig],
    *,
    require_api_key: bool = True,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for provider in providers:
        provider_id = canonical_model_id(provider.id)
        if (
            not provider.enabled
            or not provider_id
            or provider_id in EXTERNAL_PROVIDER_EXCLUDED_IDS
        ):
            continue

        base_url = provider.base_url.strip()
        if not base_url:
            continue

        api_key = provider.resolved_api_key()
        if require_api_key and not api_key:
            continue

        for model in provider.models:
            if not model.enabled or not model.gateway_exported:
                continue

            model_id = canonical_model_id(model.id)
            if not model_id:
                continue

            alias = canonical_model_id(f"{provider_id}/{model_id}")
            tool_surface_strategy = _resolved_tool_surface_strategy(provider, model)
            native_responses_tool_codec = _resolved_native_responses_tool_codec(provider, model)
            capability_binding = select_capability_binding(
                provider_id,
                model_id,
                provider.upstream_format,
                model.capability_profiles,
            )
            entry = {
                "alias": alias,
                "provider_alias": provider_id,
                "upstream_name": EXTERNAL_PROVIDER_UPSTREAM_NAMES.get(provider_id, provider_id),
                "display_prefix": provider.display_prefix or provider.name,
                "base_url": base_url,
                "api_key": api_key,
                "upstream_format": provider.upstream_format,
                "tool_protocol": provider.tool_protocol,
                "tool_surface_strategy": tool_surface_strategy,
                "native_responses_tool_codec": native_responses_tool_codec,
                "capability_binding": capability_binding,
                "reports_cached_input_tokens": provider.reports_cached_input_tokens,
                "supports_developer_role": provider.supports_developer_role,
                "upstream_model": _upstream_model_name(model),
                "context_window": model.context_window,
                "max_output_tokens": model.max_output_tokens,
                "input_modalities": model.input_modalities or ("text",),
                "supported_reasoning_levels": model.supported_reasoning_levels,
                "default_reasoning_level": model.default_reasoning_level,
                "context_source": "providers_toml",
                "max_output_source": "providers_toml",
                "priority_base": _provider_priority_base(provider),
            }
            result[alias] = entry
            for model_alias in model.aliases:
                alias_id = canonical_model_id(model_alias)
                if not alias_id:
                    continue
                qualified_alias = alias_id if "/" in alias_id else canonical_model_id(f"{provider_id}/{alias_id}")
                if qualified_alias and qualified_alias not in result:
                    alias_entry = dict(entry)
                    alias_entry["matched_alias"] = qualified_alias
                    result[qualified_alias] = alias_entry
    return result


def catalog_visible_external_models(
    providers: Iterable[ProviderConfig],
    *,
    require_api_key: bool = True,
) -> list[dict[str, Any]]:
    return [
        entry
        for entry in build_external_model_index(providers, require_api_key=require_api_key).values()
        if "matched_alias" not in entry
    ]


def build_ollama_cloud_model_index(
    providers: Iterable[ProviderConfig],
    *,
    require_api_key: bool = True,
) -> tuple[bool, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, Any]] = {}
    configured = False
    for provider in providers:
        provider_id = canonical_model_id(provider.id)
        if provider_id != "ollama-cloud":
            continue
        configured = True
        if not provider.enabled:
            continue

        base_url = provider.base_url.strip()
        if not base_url:
            continue

        api_key = provider.resolved_api_key()
        if require_api_key and not api_key:
            continue

        for model in provider.models:
            if not model.enabled or not model.gateway_exported:
                continue

            model_id = canonical_model_id(model.id)
            if not model_id:
                continue

            qualified_alias = canonical_model_id(f"{provider_id}/{model_id}")
            tool_surface_strategy = _resolved_tool_surface_strategy(provider, model)
            native_responses_tool_codec = _resolved_native_responses_tool_codec(provider, model)
            capability_binding = select_capability_binding(
                provider_id,
                model_id,
                provider.upstream_format,
                model.capability_profiles,
            )
            entry = {
                "alias": qualified_alias,
                "provider_alias": provider_id,
                "upstream_name": EXTERNAL_PROVIDER_UPSTREAM_NAMES[provider_id],
                "display_prefix": provider.display_prefix or provider.name,
                "base_url": base_url,
                "api_key": api_key,
                "upstream_format": provider.upstream_format,
                "tool_protocol": provider.tool_protocol,
                "tool_surface_strategy": tool_surface_strategy,
                "native_responses_tool_codec": native_responses_tool_codec,
                "capability_binding": capability_binding,
                "upstream_model": _upstream_model_name(model),
                "context_window": model.context_window,
                "max_output_tokens": model.max_output_tokens,
                "input_modalities": model.input_modalities or ("text",),
                "supported_reasoning_levels": model.supported_reasoning_levels,
                "default_reasoning_level": model.default_reasoning_level,
                "context_source": "providers_toml",
                "max_output_source": "providers_toml",
                "priority_base": _provider_priority_base(provider),
            }
            result[model_id] = entry
            result[qualified_alias] = entry
            for model_alias in model.aliases:
                alias_id = canonical_model_id(model_alias)
                if not alias_id:
                    continue
                result.setdefault(alias_id, entry)
                result.setdefault(canonical_model_id(f"{provider_id}/{alias_id}"), entry)
    return configured, result


def catalog_visible_ollama_cloud_models(
    providers: Iterable[ProviderConfig],
    *,
    require_api_key: bool = True,
) -> tuple[bool, list[dict[str, Any]]]:
    configured, index = build_ollama_cloud_model_index(providers, require_api_key=require_api_key)
    seen_aliases: set[str] = set()
    models: list[dict[str, Any]] = []
    for entry in index.values():
        alias = str(entry.get("alias", ""))
        if not alias or alias in seen_aliases:
            continue
        seen_aliases.add(alias)
        models.append(entry)
    return configured, models


def resolve_ollama_cloud_model(
    model_id: str,
    providers_path: Path | None = None,
    *,
    require_api_key: bool = True,
) -> tuple[bool, dict[str, Any] | None]:
    if providers_path is None:
        providers_path = runtime_providers_path()
    configured, index = build_ollama_cloud_model_index(
        load_providers(providers_path),
        require_api_key=require_api_key,
    )
    return configured, index.get(canonical_model_id(model_id))


def resolve_external_model_alias(
    model_id: str,
    providers_path: Path | None = None,
) -> dict[str, Any] | None:
    if providers_path is None:
        providers_path = runtime_providers_path()
    return build_external_model_index(load_providers(providers_path)).get(canonical_model_id(model_id))


def load_providers(path: Path | None = None) -> list[ProviderConfig]:
    if path is None:
        path = runtime_providers_path()
    if not path.exists():
        return []

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    providers = _providers_from_data(data)
    if path.resolve() != DEFAULT_PROVIDERS_PATH.resolve():
        _apply_bundled_tool_surface_strategy_defaults(providers)
        _apply_bundled_native_responses_tool_codec_defaults(providers)
        _apply_bundled_capability_profile_defaults(providers)
    return providers


def _providers_from_data(data: dict[str, Any]) -> list[ProviderConfig]:
    raw_providers = data.get("providers", [])
    if not isinstance(raw_providers, list):
        raise ValueError("providers must be an array of tables")

    indexed_providers: list[tuple[int, ProviderConfig]] = []
    for provider_index, raw_provider in enumerate(raw_providers):
        if not isinstance(raw_provider, dict):
            raise ValueError("providers must be an array of tables")

        raw_models = raw_provider.get("models", [])
        if not isinstance(raw_models, list):
            raise ValueError("provider models must be an array of tables")

        indexed_models: list[tuple[int, ModelConfig]] = []
        for model_index, raw_model in enumerate(raw_models):
            if not isinstance(raw_model, dict):
                raise ValueError("provider models must be an array of tables")
            model = ModelConfig(
                id=_string_field(raw_model.get("id")),
                upstream_model=_optional_string_field(raw_model.get("upstream_model")),
                aliases=_string_tuple_field(raw_model.get("aliases"), ()),
                display_name=_optional_string_field(raw_model.get("display_name")),
                context_window=_optional_int_field(raw_model.get("context_window")),
                max_output_tokens=_optional_int_field(raw_model.get("max_output_tokens")),
                input_modalities=_string_tuple_field(raw_model.get("input_modalities"), ("text",)),
                supported_reasoning_levels=_string_tuple_field(raw_model.get("supported_reasoning_levels"), ()),
                default_reasoning_level=_optional_string_field(raw_model.get("default_reasoning_level")),
                tool_surface_strategy=_tool_surface_strategy_field(
                    raw_model.get("tool_surface_strategy"), default=None
                ),
                native_responses_tool_codec=_native_responses_tool_codec_field(
                    raw_model.get("native_responses_tool_codec"), default=None
                ),
                capability_profiles=_capability_profiles_field(
                    raw_model.get("capability_profiles")
                ),
                sort_order=_int_field(raw_model.get("sort_order"), 0),
                enabled=_bool_field(raw_model.get("enabled"), True),
                codex_enabled=_bool_field(raw_model.get("codex_enabled"), True),
                gateway_exported=_bool_field(raw_model.get("gateway_exported"), True),
            )
            model._capability_profiles_explicit = "capability_profiles" in raw_model
            indexed_models.append((model_index, model))

        provider_tool_surface_strategy = _tool_surface_strategy_field(
            raw_provider.get("tool_surface_strategy"), default="eager"
        )
        assert provider_tool_surface_strategy is not None
        provider_native_responses_tool_codec = _native_responses_tool_codec_field(
            raw_provider.get("native_responses_tool_codec"), default="none"
        )
        assert provider_native_responses_tool_codec is not None
        provider = ProviderConfig(
            id=_string_field(raw_provider.get("id")),
            name=_string_field(raw_provider.get("name")),
            base_url=_string_field(raw_provider.get("base_url")),
            api_key=_string_field(raw_provider.get("api_key")),
            upstream_format=_upstream_format_field(raw_provider.get("upstream_format")),
            available_upstream_formats=_upstream_formats_field(raw_provider.get("available_upstream_formats")),
            tool_protocol=_tool_protocol_field(raw_provider.get("tool_protocol")),
            tool_surface_strategy=provider_tool_surface_strategy,
            native_responses_tool_codec=provider_native_responses_tool_codec,
            reports_cached_input_tokens=_bool_field(raw_provider.get("reports_cached_input_tokens"), False),
            supports_developer_role=_bool_field(raw_provider.get("supports_developer_role"), True),
            display_prefix=_optional_string_field(raw_provider.get("display_prefix")),
            sort_order=_int_field(raw_provider.get("sort_order"), 0),
            enabled=_bool_field(raw_provider.get("enabled"), True),
            models=_sort_by_order(indexed_models),
        )
        provider._tool_surface_strategy_explicit = "tool_surface_strategy" in raw_provider
        provider._native_responses_tool_codec_explicit = (
            "native_responses_tool_codec" in raw_provider
        )
        indexed_providers.append((provider_index, provider))

    return _sort_by_order(indexed_providers)


def _apply_bundled_tool_surface_strategy_defaults(runtime_providers: Iterable[ProviderConfig]) -> None:
    runtime_providers = list(runtime_providers)
    if not _providers_require_bundled_tool_surface_defaults(runtime_providers):
        return

    try:
        bundled_data = tomllib.loads(DEFAULT_PROVIDERS_PATH.read_text(encoding="utf-8"))
        bundled_providers = _providers_from_data(bundled_data)
    except (OSError, ValueError) as exc:
        raise ValueError(
            "bundled providers configuration is required to resolve omitted tool_surface_strategy"
        ) from exc

    bundled_by_id = _provider_config_index_by_id(bundled_providers)
    for runtime_provider in runtime_providers:
        bundled_provider = bundled_by_id.get(_canonical_config_identifier(runtime_provider.id))
        if bundled_provider is None:
            continue

        if not _provider_tool_surface_strategy_is_explicit(runtime_provider):
            runtime_provider._bundled_tool_surface_strategy = _bundled_provider_tool_surface_strategy(
                bundled_provider
            )

        bundled_models_by_id = _model_config_index_by_identifier(bundled_provider.models)
        for runtime_model in runtime_provider.models:
            if runtime_model.tool_surface_strategy is not None:
                continue
            bundled_model = _matching_model_config(runtime_model, bundled_models_by_id)
            if bundled_model is not None:
                runtime_model._bundled_tool_surface_strategy = bundled_model.tool_surface_strategy


def _providers_require_bundled_tool_surface_defaults(providers: Iterable[ProviderConfig]) -> bool:
    return any(model.tool_surface_strategy is None for provider in providers for model in provider.models)


def _apply_bundled_native_responses_tool_codec_defaults(
    runtime_providers: Iterable[ProviderConfig],
) -> None:
    runtime_providers = list(runtime_providers)
    if not any(
        model.native_responses_tool_codec is None
        for provider in runtime_providers
        for model in provider.models
    ):
        return

    try:
        bundled_data = tomllib.loads(DEFAULT_PROVIDERS_PATH.read_text(encoding="utf-8"))
        bundled_providers = _providers_from_data(bundled_data)
    except (OSError, ValueError):
        # No codec is the conservative compatibility default when bundled
        # selection authority cannot be read.
        return

    bundled_by_id = _provider_config_index_by_id(bundled_providers)
    for runtime_provider in runtime_providers:
        bundled_provider = bundled_by_id.get(_canonical_config_identifier(runtime_provider.id))
        if bundled_provider is None:
            continue

        if not _provider_native_responses_tool_codec_is_explicit(runtime_provider):
            runtime_provider._bundled_native_responses_tool_codec = (
                _bundled_provider_native_responses_tool_codec(bundled_provider)
            )

        bundled_models_by_id = _model_config_index_by_identifier(bundled_provider.models)
        for runtime_model in runtime_provider.models:
            if runtime_model.native_responses_tool_codec is not None:
                continue
            bundled_model = _matching_model_config(runtime_model, bundled_models_by_id)
            if bundled_model is not None:
                runtime_model._bundled_native_responses_tool_codec = (
                    bundled_model.native_responses_tool_codec
                )


def _apply_bundled_capability_profile_defaults(
    runtime_providers: Iterable[ProviderConfig],
) -> None:
    runtime_providers = list(runtime_providers)
    if not any(
        not model._capability_profiles_explicit
        for provider in runtime_providers
        for model in provider.models
    ):
        return

    try:
        bundled_data = tomllib.loads(DEFAULT_PROVIDERS_PATH.read_text(encoding="utf-8"))
        bundled_providers = _providers_from_data(bundled_data)
    except (OSError, ValueError):
        # Missing reviewed metadata is unqualified by construction.
        return

    bundled_by_id = _provider_config_index_by_id(bundled_providers)
    for runtime_provider in runtime_providers:
        bundled_provider = bundled_by_id.get(_canonical_config_identifier(runtime_provider.id))
        if bundled_provider is None:
            continue
        bundled_models_by_id = _model_config_index_by_identifier(bundled_provider.models)
        for runtime_model in runtime_provider.models:
            if runtime_model._capability_profiles_explicit:
                continue
            bundled_model = _matching_model_config(runtime_model, bundled_models_by_id)
            if bundled_model is not None:
                runtime_model.capability_profiles = bundled_model.capability_profiles


def _provider_native_responses_tool_codec_is_explicit(provider: ProviderConfig) -> bool:
    codec = _native_responses_tool_codec_field(
        provider.native_responses_tool_codec,
        default="none",
    )
    assert codec is not None
    return provider._native_responses_tool_codec_explicit or codec != "none"


def _bundled_provider_native_responses_tool_codec(provider: ProviderConfig) -> str | None:
    if not _provider_native_responses_tool_codec_is_explicit(provider):
        return None
    return _native_responses_tool_codec_field(
        provider.native_responses_tool_codec,
        default=None,
    )


def _provider_tool_surface_strategy_is_explicit(provider: ProviderConfig) -> bool:
    strategy = _tool_surface_strategy_field(provider.tool_surface_strategy, default="eager")
    assert strategy is not None
    return provider._tool_surface_strategy_explicit or strategy != "eager"


def _bundled_provider_tool_surface_strategy(provider: ProviderConfig) -> str | None:
    if not _provider_tool_surface_strategy_is_explicit(provider):
        return None
    return _tool_surface_strategy_field(provider.tool_surface_strategy, default=None)


def _canonical_config_identifier(value: Any) -> str:
    return canonical_model_id(_string_field(value))


def _canonical_config_identifiers(values: Iterable[Any]) -> tuple[str, ...]:
    identifiers: list[str] = []
    for value in values:
        identifier = _canonical_config_identifier(value)
        if identifier and identifier not in identifiers:
            identifiers.append(identifier)
    return tuple(identifiers)


def _provider_config_index_by_id(providers: Iterable[ProviderConfig]) -> dict[str, ProviderConfig]:
    result: dict[str, ProviderConfig] = {}
    for provider in providers:
        for identifier in _canonical_config_identifiers((provider.id,)):
            result.setdefault(identifier, provider)
    return result


def _model_config_index_by_identifier(models: Iterable[ModelConfig]) -> dict[str, ModelConfig]:
    result: dict[str, ModelConfig] = {}
    for model in models:
        for identifier in _canonical_config_identifiers((model.id, *model.aliases)):
            result.setdefault(identifier, model)
    return result


def _matching_model_config(model: ModelConfig, index: dict[str, ModelConfig]) -> ModelConfig | None:
    for identifier in _canonical_config_identifiers((model.id, *model.aliases)):
        bundled_model = index.get(identifier)
        if bundled_model is not None:
            return bundled_model
    return None


def save_providers(providers: Iterable[ProviderConfig], path: Path = DEFAULT_PROVIDERS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []

    indexed_providers = list(enumerate(providers))
    for provider_index, provider in enumerate(_sort_by_order(indexed_providers)):
        if provider_index:
            chunks.append("")
        chunks.extend(
            [
                "[[providers]]",
                _toml_string_line("id", provider.id),
                _toml_string_line("name", provider.name),
                _toml_string_line("base_url", provider.base_url),
                _toml_string_line("api_key", provider.api_key),
            ]
        )
        if provider.display_prefix is not None:
            chunks.append(_toml_string_line("display_prefix", provider.display_prefix))
        if provider.upstream_format:
            chunks.append(_toml_string_line("upstream_format", provider.upstream_format))
        if provider.available_upstream_formats:
            chunks.append(_toml_string_list_line("available_upstream_formats", provider.available_upstream_formats))
        if provider.tool_protocol and provider.tool_protocol != "auto":
            chunks.append(_toml_string_line("tool_protocol", provider.tool_protocol))
        provider_tool_surface_strategy = _tool_surface_strategy_field(
            provider.tool_surface_strategy, default="eager"
        )
        if provider._tool_surface_strategy_explicit or provider_tool_surface_strategy != "eager":
            chunks.append(_toml_string_line("tool_surface_strategy", provider_tool_surface_strategy))
        provider_native_responses_tool_codec = _native_responses_tool_codec_field(
            provider.native_responses_tool_codec, default="none"
        )
        if provider_native_responses_tool_codec != "none":
            chunks.append(
                _toml_string_line("native_responses_tool_codec", provider_native_responses_tool_codec)
            )
        if provider.reports_cached_input_tokens:
            chunks.append(_toml_bool_line("reports_cached_input_tokens", provider.reports_cached_input_tokens))
        if not provider.supports_developer_role:
            chunks.append(_toml_bool_line("supports_developer_role", provider.supports_developer_role))
        chunks.extend(
            [
                _toml_int_line("sort_order", provider.sort_order),
                _toml_bool_line("enabled", provider.enabled),
            ]
        )

        indexed_models = list(enumerate(provider.models))
        for model in _sort_by_order(indexed_models):
            chunks.append("")
            chunks.extend(
                [
                    "  [[providers.models]]",
                    _toml_string_line("id", model.id, indent="  "),
                ]
            )
            if model.upstream_model is not None:
                chunks.append(_toml_string_line("upstream_model", model.upstream_model, indent="  "))
            if model.aliases:
                chunks.append(_toml_string_list_line("aliases", model.aliases, indent="  "))
            if model.display_name is not None:
                chunks.append(_toml_string_line("display_name", model.display_name, indent="  "))
            if model.context_window is not None:
                chunks.append(_toml_int_line("context_window", model.context_window, indent="  "))
            if model.max_output_tokens is not None:
                chunks.append(_toml_int_line("max_output_tokens", model.max_output_tokens, indent="  "))
            if model.input_modalities and model.input_modalities != ("text",):
                chunks.append(_toml_string_list_line("input_modalities", model.input_modalities, indent="  "))
            if model.supported_reasoning_levels:
                chunks.append(_toml_string_list_line("supported_reasoning_levels", model.supported_reasoning_levels, indent="  "))
            if model.default_reasoning_level is not None:
                chunks.append(_toml_string_line("default_reasoning_level", model.default_reasoning_level, indent="  "))
            model_tool_surface_strategy = _tool_surface_strategy_field(
                model.tool_surface_strategy, default=None
            )
            if model_tool_surface_strategy is not None:
                chunks.append(
                    _toml_string_line("tool_surface_strategy", model_tool_surface_strategy, indent="  ")
                )
            model_native_responses_tool_codec = _native_responses_tool_codec_field(
                model.native_responses_tool_codec, default=None
            )
            if model_native_responses_tool_codec is not None:
                chunks.append(
                    _toml_string_line(
                        "native_responses_tool_codec",
                        model_native_responses_tool_codec,
                        indent="  ",
                    )
                )
            if model._capability_profiles_explicit and not model.capability_profiles:
                chunks.append("  capability_profiles = []")
            chunks.extend(
                [
                    _toml_int_line("sort_order", model.sort_order, indent="  "),
                    _toml_bool_line("enabled", model.enabled, indent="  "),
                    _toml_bool_line("codex_enabled", model.codex_enabled, indent="  "),
                    _toml_bool_line("gateway_exported", model.gateway_exported, indent="  "),
                ]
            )
            for profile in model.capability_profiles:
                chunks.extend(
                    [
                        "",
                        "    [[providers.models.capability_profiles]]",
                        _toml_int_line("schema_version", profile.schema_version, indent="    "),
                        _toml_string_line(
                            "upstream_protocol",
                            profile.upstream_protocol,
                            indent="    ",
                        ),
                        _toml_string_line("tool_profile", profile.tool_profile, indent="    "),
                        _toml_string_line(
                            "collaboration_backend",
                            profile.collaboration_backend,
                            indent="    ",
                        ),
                        _toml_string_line(
                            "collaboration_version",
                            profile.collaboration_version,
                            indent="    ",
                        ),
                        _toml_string_line(
                            "capability_manifest_version",
                            profile.capability_manifest_version,
                            indent="    ",
                        ),
                        _toml_string_line(
                            "capability_manifest_hash",
                            profile.capability_manifest_hash,
                            indent="    ",
                        ),
                        _toml_string_line(
                            "qualification_state",
                            profile.qualification_state,
                            indent="    ",
                        ),
                    ]
                )

    text = "\n".join(chunks).rstrip() + "\n"
    with provider_catalog_transaction_guard(_provider_catalog_runtime_root(path)):
        if _is_runtime_provider_destination(path):
            raise PermissionError(
                "runtime provider configuration must be saved through the Rust provider/catalog transaction"
            )
        atomic_write_text(path, text, encoding="utf-8")


def _provider_catalog_runtime_root(path: Path) -> Path:
    resolved = path.resolve(strict=False)
    if resolved.parent.name == "config" and resolved.parent.parent.name == "proxy":
        return resolved.parent.parent.parent
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).resolve(strict=False)
    return resolved.parent


def _is_runtime_provider_destination(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    return resolved.parent.name == "config" and resolved.parent.parent.name == "proxy"


def _sort_by_order[T](indexed_items: Iterable[tuple[int, T]]) -> list[T]:
    return [item for _, item in sorted(indexed_items, key=lambda indexed: (_item_sort_order(indexed[1]), indexed[0]))]


def _item_sort_order(value: Any) -> int:
    return value.sort_order if isinstance(value.sort_order, int) else 0


def _provider_models_payload_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
        models = payload.get("models")
        if isinstance(models, list):
            return models
    return []


def _discovered_model_id(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("id", "model", "name", "slug"):
        model_id = _string_field(value.get(key)).strip()
        if model_id:
            return model_id
    return ""


def _official_model_id(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("id", "model", "name", "slug"):
        raw_model_id = value.get(key)
        if isinstance(raw_model_id, str):
            model_id = raw_model_id.strip()
            if model_id:
                return model_id
    return ""


def _discovered_numeric_limit(value: Any, keys: tuple[str, ...], nested_limit_key: str) -> int | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        parsed = _optional_int_field(value.get(key))
        if parsed is not None:
            return parsed
    limit = value.get("limit")
    if isinstance(limit, dict):
        return _optional_int_field(limit.get(nested_limit_key))
    return None


def _provider_priority_base(provider: ProviderConfig) -> int:
    return provider.sort_order * 100 if isinstance(provider.sort_order, int) else 0


def _upstream_model_name(model: ModelConfig) -> str:
    upstream_model = (model.upstream_model or "").strip()
    if upstream_model:
        return upstream_model
    return model.id.strip()


def _string_field(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return default


def _optional_string_field(value: Any) -> str | None:
    if value is None:
        return None
    return _string_field(value)


def _string_tuple_field(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else default
    if not isinstance(value, list):
        return default
    items = tuple(_string_field(item).strip() for item in value)
    items = tuple(item for item in items if item)
    return items or default


def _upstream_format_field(value: Any) -> str:
    upstream_format = _string_field(value, "auto").strip().lower()
    return upstream_format if upstream_format in UPSTREAM_FORMATS else "auto"


def _upstream_formats_field(value: Any) -> tuple[str, ...]:
    formats = _string_tuple_field(value, ())
    result: list[str] = []
    for item in formats:
        upstream_format = _upstream_format_field(item)
        if upstream_format != "auto" and upstream_format not in result:
            result.append(upstream_format)
    return tuple(result)


def _capability_profiles_field(value: Any) -> tuple[CapabilityProfile, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        return ()
    profiles: list[CapabilityProfile] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        profiles.append(
            CapabilityProfile(
                schema_version=_int_field(item.get("schema_version"), 0),
                upstream_protocol=_string_field(item.get("upstream_protocol"))
                .strip()
                .lower(),
                tool_profile=_string_field(item.get("tool_profile")).strip(),
                collaboration_backend=_string_field(
                    item.get("collaboration_backend")
                ).strip(),
                collaboration_version=_string_field(
                    item.get("collaboration_version")
                ).strip(),
                capability_manifest_version=_string_field(
                    item.get("capability_manifest_version")
                ).strip(),
                capability_manifest_hash=_string_field(
                    item.get("capability_manifest_hash")
                )
                .strip()
                .lower(),
                qualification_state=_string_field(item.get("qualification_state"))
                .strip()
                .lower(),
            )
        )
    return tuple(profiles)


def capability_profile_manifest_hash(
    provider_id: str,
    model_id: str,
    profile: CapabilityProfile,
) -> str:
    manifest = {
        "schema_version": profile.schema_version,
        "provider_id": canonical_model_id(provider_id),
        "model_id": canonical_model_id(model_id),
        "upstream_protocol": profile.upstream_protocol,
        "tool_profile": profile.tool_profile,
        "collaboration_backend": profile.collaboration_backend,
        "collaboration_version": profile.collaboration_version,
        "capability_manifest_version": profile.capability_manifest_version,
        "qualification_state": profile.qualification_state,
    }
    canonical = json.dumps(
        manifest,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def select_capability_binding(
    provider_id: str,
    model_id: str,
    upstream_protocol: str,
    profiles: Iterable[CapabilityProfile],
) -> dict[str, Any]:
    provider_id = canonical_model_id(provider_id)
    model_id = canonical_model_id(model_id)
    upstream_protocol = _upstream_format_field(upstream_protocol)
    base = {
        "schema_version": CAPABILITY_PROFILE_SCHEMA_VERSION,
        "provider": provider_id,
        "model": model_id,
        "upstream_protocol": upstream_protocol,
        "qualification_state": "unqualified",
        "advanced_capabilities_enabled": False,
    }
    if upstream_protocol not in CAPABILITY_PROFILE_PROTOCOLS:
        return {**base, "rejection_reason": "unsupported_upstream_protocol_scope"}

    matching = [
        profile
        for profile in profiles
        if profile.upstream_protocol == upstream_protocol
    ]
    if not matching:
        return {**base, "rejection_reason": "missing_route_profile"}
    if len(matching) != 1:
        return {**base, "rejection_reason": "conflicting_route_profiles"}

    profile = matching[0]
    required_values = (
        profile.tool_profile,
        profile.collaboration_backend,
        profile.collaboration_version,
        profile.capability_manifest_version,
        profile.capability_manifest_hash,
    )
    if (
        profile.schema_version != CAPABILITY_PROFILE_SCHEMA_VERSION
        or not all(value.strip() for value in required_values)
        or profile.qualification_state not in QUALIFICATION_STATES
        or (profile.collaboration_backend == "none")
        != (profile.collaboration_version == "none")
    ):
        return {**base, "rejection_reason": "invalid_route_profile"}

    expected_hash = capability_profile_manifest_hash(provider_id, model_id, profile)
    if profile.capability_manifest_hash != expected_hash:
        return {**base, "rejection_reason": "stale_capability_manifest_hash"}

    binding = {
        **base,
        "tool_profile": profile.tool_profile,
        "collaboration_backend": profile.collaboration_backend,
        "collaboration_version": profile.collaboration_version,
        "capability_manifest_version": profile.capability_manifest_version,
        "capability_manifest_hash": profile.capability_manifest_hash,
        "qualification_state": profile.qualification_state,
        "advanced_capabilities_enabled": profile.qualification_state == "supported",
    }
    if profile.qualification_state != "supported":
        binding["rejection_reason"] = (
            f"qualification_state_{profile.qualification_state}"
        )
    return binding


def _tool_protocol_field(value: Any) -> str:
    tool_protocol = _string_field(value, "auto").strip().lower()
    return tool_protocol if tool_protocol in TOOL_PROTOCOLS else "auto"


def _tool_surface_strategy_field(value: Any, *, default: str | None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("tool_surface_strategy must be eager or deferred_core")
    strategy = value.strip().lower()
    if strategy not in TOOL_SURFACE_STRATEGIES:
        raise ValueError("tool_surface_strategy must be eager or deferred_core")
    return strategy


def _resolved_tool_surface_strategy(provider: ProviderConfig, model: ModelConfig) -> str:
    model_strategy = _tool_surface_strategy_field(model.tool_surface_strategy, default=None)
    if model_strategy is not None:
        return model_strategy
    bundled_model_strategy = _tool_surface_strategy_field(
        model._bundled_tool_surface_strategy, default=None
    )
    if bundled_model_strategy is not None:
        return bundled_model_strategy
    provider_strategy = _tool_surface_strategy_field(provider.tool_surface_strategy, default="eager")
    assert provider_strategy is not None
    if _provider_tool_surface_strategy_is_explicit(provider):
        return provider_strategy
    bundled_provider_strategy = _tool_surface_strategy_field(
        provider._bundled_tool_surface_strategy, default=None
    )
    if bundled_provider_strategy is not None:
        return bundled_provider_strategy
    return provider_strategy


def _native_responses_tool_codec_field(value: Any, *, default: str | None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("native_responses_tool_codec must be none or strict_apply_patch")
    codec = value.strip().lower()
    if codec not in NATIVE_RESPONSES_TOOL_CODECS:
        raise ValueError("native_responses_tool_codec must be none or strict_apply_patch")
    return codec


def _resolved_native_responses_tool_codec(provider: ProviderConfig, model: ModelConfig) -> str:
    model_codec = _native_responses_tool_codec_field(
        model.native_responses_tool_codec,
        default=None,
    )
    if model_codec is not None:
        return model_codec
    provider_codec = _native_responses_tool_codec_field(
        provider.native_responses_tool_codec,
        default="none",
    )
    assert provider_codec is not None
    if _provider_native_responses_tool_codec_is_explicit(provider):
        return provider_codec
    bundled_model_codec = _native_responses_tool_codec_field(
        model._bundled_native_responses_tool_codec,
        default=None,
    )
    if bundled_model_codec is not None:
        return bundled_model_codec
    bundled_provider_codec = _native_responses_tool_codec_field(
        provider._bundled_native_responses_tool_codec,
        default=None,
    )
    if bundled_provider_codec is not None:
        return bundled_provider_codec
    return provider_codec


def _int_field(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"[+-]?\d+", stripped):
            return int(stripped)
    return default


def _optional_int_field(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"[+-]?\d+", stripped):
            return int(stripped)
    return None


def _bool_field(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1"}:
            return True
        if lowered in {"false", "0"}:
            return False
    return default


def _toml_string_line(key: str, value: str, indent: str = "") -> str:
    return f"{indent}{key} = {json.dumps(value, ensure_ascii=False)}"


def _toml_int_line(key: str, value: int, indent: str = "") -> str:
    return f"{indent}{key} = {value}"


def _toml_bool_line(key: str, value: bool, indent: str = "") -> str:
    return f"{indent}{key} = {'true' if value else 'false'}"


def _toml_string_list_line(key: str, values: Iterable[str], indent: str = "") -> str:
    encoded = ", ".join(json.dumps(value, ensure_ascii=False) for value in values)
    return f"{indent}{key} = [{encoded}]"
