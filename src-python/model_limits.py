from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ResolvedModelLimits:
    provider_id: str
    model_id: str
    effective_context_window: int | None
    max_context_window: int | None
    max_output_tokens: int | None
    effective_source: str | None
    max_source: str | None
    confidence: str
    verified_at: str | None
    probe_cli_version: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResolvedModelLimits":
        return cls(**{field: value.get(field) for field in cls.__dataclass_fields__})


def load_resolved_model_limits(path: Path) -> dict[tuple[str, str], ResolvedModelLimits]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported resolved model limits schema")
    entries = [ResolvedModelLimits.from_dict(item) for item in payload.get("entries", [])]
    return {(item.provider_id, item.model_id): item for item in entries}


def apply_resolved_model_limits(model: dict[str, Any], limits: ResolvedModelLimits | None) -> None:
    if limits is None:
        return
    if limits.effective_context_window is not None:
        model["context_window"] = limits.effective_context_window
    else:
        model.pop("context_window", None)
    if limits.max_context_window is not None:
        model["max_context_window"] = limits.max_context_window
    else:
        model.pop("max_context_window", None)
    if limits.max_output_tokens is not None:
        model.setdefault("max_output_tokens", limits.max_output_tokens)
    model["effective_source"] = limits.effective_source
    model["max_source"] = limits.max_source
    model["confidence"] = limits.confidence
    model["verified_at"] = limits.verified_at
    if limits.probe_cli_version:
        model["probe_cli_version"] = limits.probe_cli_version
