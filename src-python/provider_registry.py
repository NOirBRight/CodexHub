from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable

from catalog import canonical_model_id


OPENCODE_CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.json"


@dataclass(frozen=True)
class ExternalProviderDefinition:
    alias_prefix: str
    upstream_name: str
    opencode_provider_names: tuple[str, ...]
    display_prefix: str
    description: str
    default_base_url: str
    api_key_env_names: tuple[str, ...]
    priority_base: int
    preferred_models: tuple[str, ...]
    default_models: dict[str, dict[str, Any]]
    probed_max_output_tokens: int | None = None
    probed_max_output_source: str | None = None


@dataclass(frozen=True)
class ExternalProviderModel:
    alias: str
    provider_alias: str
    upstream_name: str
    display_prefix: str
    description: str
    base_url: str
    api_key: str
    upstream_model: str
    priority_base: int
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_modalities: tuple[str, ...] = ("text",)
    context_source: str = "opencode_config"
    max_output_source: str = "opencode_config"


EXTERNAL_PROVIDER_DEFINITIONS: tuple[ExternalProviderDefinition, ...] = (
    ExternalProviderDefinition(
        alias_prefix="volc",
        upstream_name="volcengine",
        opencode_provider_names=("volcengine-plan",),
        display_prefix="Volc",
        description="External Volcano Engine model via Ark Coding Responses API.",
        default_base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
        api_key_env_names=("VOLCENGINE_API_KEY", "ARK_API_KEY"),
        priority_base=200,
        preferred_models=(
            "ark-code-latest",
            "doubao-seed-2.0-code",
            "doubao-seed-2.0-pro",
            "doubao-seed-2.0-lite",
            "glm-5.2",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "minimax-m3",
            "kimi-k2.6",
        ),
        default_models={},
        probed_max_output_tokens=8192,
        probed_max_output_source="live_probe_2026-06-28",
    ),
    ExternalProviderDefinition(
        alias_prefix="minimax-cn",
        upstream_name="minimax_cn",
        opencode_provider_names=("minimax-cn", "minimax", "minimaxcn"),
        display_prefix="MiniMax.cn",
        description="External MiniMax.cn model via OpenAI-compatible API.",
        default_base_url="https://api.minimaxi.com/v1",
        api_key_env_names=("MINIMAX_API_KEY", "MINIMAX_CN_API_KEY"),
        priority_base=300,
        preferred_models=("minimax-m3",),
        default_models={
            "minimax-m3": {
                "name": "MiniMax-M3",
                "id": "MiniMax-M3",
                "tool_call": True,
                "attachment": True,
                "limit": {"context": 1000000, "output": 524288},
                "modalities": {"input": ["text", "image"], "output": ["text"]},
            }
        },
    ),
)


def load_opencode_config(path: Path = OPENCODE_CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _provider_configs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    providers = _as_mapping(config.get("provider"))
    return {str(name): _as_mapping(provider) for name, provider in providers.items()}


def _first_opencode_provider(
    providers: dict[str, dict[str, Any]],
    names: Iterable[str],
) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    for name in names:
        provider = providers.get(name)
        if provider is not None:
            return name, provider
    return None, None


def _resolve_secret(value: Any, env_names: Iterable[str]) -> str | None:
    if isinstance(value, str) and value.strip():
        match = re.fullmatch(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", value.strip())
        if match:
            return os.environ.get(match.group(1))
        return value

    for env_name in env_names:
        secret = os.environ.get(env_name)
        if secret:
            return secret
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _model_limit(model_config: dict[str, Any], key: str) -> int | None:
    limits = _as_mapping(model_config.get("limit"))
    return _positive_int(limits.get(key))


def _model_input_modalities(model_config: dict[str, Any]) -> tuple[str, ...]:
    modalities = _as_mapping(model_config.get("modalities"))
    raw_input = modalities.get("input")
    if not isinstance(raw_input, list):
        return ("text",)
    values = tuple(str(item) for item in raw_input if isinstance(item, str) and item)
    return values or ("text",)


def _model_names_for_definition(
    definition: ExternalProviderDefinition,
    opencode_models: dict[str, Any],
) -> list[str]:
    if not opencode_models:
        return list(definition.default_models)
    if definition.preferred_models:
        models_by_canonical = {_provider_model_key(name): str(name) for name in opencode_models}
        return [
            models_by_canonical[_provider_model_key(name)]
            for name in definition.preferred_models
            if _provider_model_key(name) in models_by_canonical
        ]
    return [str(name) for name in opencode_models]


def _provider_model_key(model_name: str) -> str:
    return canonical_model_id(str(model_name)).lower()


def configured_external_models(
    opencode_path: Path = OPENCODE_CONFIG_PATH,
    definitions: Iterable[ExternalProviderDefinition] = EXTERNAL_PROVIDER_DEFINITIONS,
) -> list[ExternalProviderModel]:
    config = load_opencode_config(opencode_path)
    providers = _provider_configs(config)
    result: list[ExternalProviderModel] = []

    for definition in definitions:
        _, provider_config = _first_opencode_provider(providers, definition.opencode_provider_names)
        provider_config = provider_config or {}
        options = _as_mapping(provider_config.get("options"))
        base_url = str(options.get("baseURL") or options.get("base_url") or definition.default_base_url).rstrip("/")
        api_key = _resolve_secret(options.get("apiKey") or options.get("api_key"), definition.api_key_env_names)
        if not base_url or not api_key:
            continue

        opencode_models = _as_mapping(provider_config.get("models"))
        model_configs = opencode_models or definition.default_models
        for model_name in _model_names_for_definition(definition, opencode_models):
            model_config = _as_mapping(model_configs.get(model_name))
            upstream_model = str(model_config.get("id") or model_config.get("name") or model_name)
            alias = f"{definition.alias_prefix}/{_provider_model_key(model_name)}"
            configured_output = _model_limit(model_config, "output")
            output_source = "opencode_config"
            max_output_tokens = configured_output
            if (
                definition.probed_max_output_tokens
                and (max_output_tokens is None or definition.probed_max_output_tokens > max_output_tokens)
            ):
                max_output_tokens = definition.probed_max_output_tokens
                output_source = definition.probed_max_output_source or "live_probe"
            result.append(
                ExternalProviderModel(
                    alias=alias,
                    provider_alias=definition.alias_prefix,
                    upstream_name=definition.upstream_name,
                    display_prefix=definition.display_prefix,
                    description=definition.description,
                    base_url=base_url,
                    api_key=api_key,
                    upstream_model=upstream_model,
                    priority_base=definition.priority_base,
                    context_window=_model_limit(model_config, "context"),
                    max_output_tokens=max_output_tokens,
                    input_modalities=_model_input_modalities(model_config),
                    max_output_source=output_source,
                )
            )

    return result


def external_model_index(
    opencode_path: Path = OPENCODE_CONFIG_PATH,
    definitions: Iterable[ExternalProviderDefinition] = EXTERNAL_PROVIDER_DEFINITIONS,
) -> dict[str, ExternalProviderModel]:
    return {canonical_model_id(model.alias): model for model in configured_external_models(opencode_path, definitions)}


def resolve_external_model_alias(model_id: str) -> ExternalProviderModel | None:
    return external_model_index().get(canonical_model_id(model_id))
