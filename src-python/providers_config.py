from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import tomllib
from typing import Any, Iterable


DEFAULT_PROVIDERS_PATH = Path(__file__).resolve().parents[1] / "config" / "providers.toml"
ENV_PLACEHOLDER_RE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class ModelConfig:
    id: str
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    sort_order: int = 0
    enabled: bool = True


@dataclass
class ProviderConfig:
    id: str
    name: str
    base_url: str
    api_key: str
    display_prefix: str | None = None
    sort_order: int = 0
    enabled: bool = True
    models: list[ModelConfig] = field(default_factory=list)

    def resolved_api_key(self) -> str | None:
        if not self.api_key:
            return None
        match = ENV_PLACEHOLDER_RE.fullmatch(self.api_key)
        if match:
            return os.environ.get(match.group(1))
        return self.api_key


def load_providers(path: Path = DEFAULT_PROVIDERS_PATH) -> list[ProviderConfig]:
    if not path.exists():
        return []

    data = tomllib.loads(path.read_text(encoding="utf-8"))
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
                display_name=_optional_string_field(raw_model.get("display_name")),
                context_window=_optional_int_field(raw_model.get("context_window")),
                max_output_tokens=_optional_int_field(raw_model.get("max_output_tokens")),
                sort_order=_int_field(raw_model.get("sort_order"), 0),
                enabled=_bool_field(raw_model.get("enabled"), True),
            )
            indexed_models.append((model_index, model))

        provider = ProviderConfig(
            id=_string_field(raw_provider.get("id")),
            name=_string_field(raw_provider.get("name")),
            base_url=_string_field(raw_provider.get("base_url")),
            api_key=_string_field(raw_provider.get("api_key")),
            display_prefix=_optional_string_field(raw_provider.get("display_prefix")),
            sort_order=_int_field(raw_provider.get("sort_order"), 0),
            enabled=_bool_field(raw_provider.get("enabled"), True),
            models=_sort_by_order(indexed_models),
        )
        indexed_providers.append((provider_index, provider))

    return _sort_by_order(indexed_providers)


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
            if model.display_name is not None:
                chunks.append(_toml_string_line("display_name", model.display_name, indent="  "))
            if model.context_window is not None:
                chunks.append(_toml_int_line("context_window", model.context_window, indent="  "))
            if model.max_output_tokens is not None:
                chunks.append(_toml_int_line("max_output_tokens", model.max_output_tokens, indent="  "))
            chunks.extend(
                [
                    _toml_int_line("sort_order", model.sort_order, indent="  "),
                    _toml_bool_line("enabled", model.enabled, indent="  "),
                ]
            )

    path.write_text("\n".join(chunks).rstrip() + "\n", encoding="utf-8")


def _sort_by_order[T](indexed_items: Iterable[tuple[int, T]]) -> list[T]:
    return [item for _, item in sorted(indexed_items, key=lambda indexed: (_item_sort_order(indexed[1]), indexed[0]))]


def _item_sort_order(value: Any) -> int:
    return value.sort_order if isinstance(value.sort_order, int) else 0


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
