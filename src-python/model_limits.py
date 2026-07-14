from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


OFFICIAL_CONTEXT_FALLBACK_WINDOW = 272_000
OFFICIAL_EFFECTIVE_CONTEXT_WINDOW_PERCENT = 95
OFFICIAL_AUTO_COMPACT_TOKEN_LIMIT = 240_000
CURRENT_DIRECT_OFFICIAL_SOURCE = "current_direct_official"


@dataclass(frozen=True)
class OfficialContextBudget:
    """One safe, sanitized context decision for an Official catalog model."""

    context_window: int
    max_context_window: int
    effective_context_window_percent: int
    effective_context_window: int
    model_context_window: int
    model_auto_compact_token_limit: int
    source: str
    freshness: str


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def resolve_official_context_budget(
    *,
    direct_context_window: Any = None,
    direct_max_context_window: Any = None,
    direct_freshness: str = "missing",
    direct_source: str = "missing",
    fallback_context_window: Any = None,
) -> OfficialContextBudget:
    """Resolve an Official usable-context budget without trusting stale probes.

    Only a fresh Direct Official catalog value can raise the conservative
    fallback.  The selected value is deliberately projected into every
    context-facing field so the catalog and Codex auto-compaction setting do
    not disagree about the usable upper bound.
    """

    fallback_values = [OFFICIAL_CONTEXT_FALLBACK_WINDOW]
    fallback_context = _positive_int(fallback_context_window)
    if fallback_context is not None:
        fallback_values.append(fallback_context)
    conservative_window = min(fallback_values)

    direct_context = _positive_int(direct_context_window)
    direct_max = _positive_int(direct_max_context_window)
    allowed_freshness = {"fresh", "missing", "stale", "contradictory"}
    freshness = direct_freshness if direct_freshness in allowed_freshness else "missing"
    source = "conservative_fallback"
    selected_window = conservative_window

    trusted_current_direct = (
        direct_source == CURRENT_DIRECT_OFFICIAL_SOURCE
        and freshness == "fresh"
    )
    values_that_can_only_tighten = [
        value
        for value in (direct_context, direct_max)
        if value is not None
    ]

    if trusted_current_direct and direct_context is not None:
        if direct_max is not None and direct_max < direct_context:
            # A reported maximum below the usable value is internally
            # contradictory.  Preserve the lower bound rather than emitting
            # a larger catalog/runtime setting.
            selected_window = min(conservative_window, direct_max)
            freshness = "contradictory"
        else:
            selected_window = direct_context
            source = CURRENT_DIRECT_OFFICIAL_SOURCE
    else:
        # Untrusted, stale, or incomplete data can never expand the budget,
        # but an independently smaller value remains a safe cap.
        if values_that_can_only_tighten:
            selected_window = min(conservative_window, *values_that_can_only_tighten)
        if direct_context is not None and direct_max is not None and direct_max < direct_context:
            freshness = "contradictory"
        elif freshness == "fresh":
            freshness = "missing"

    effective_window = max(
        1,
        selected_window * OFFICIAL_EFFECTIVE_CONTEXT_WINDOW_PERCENT // 100,
    )
    auto_compact_limit = min(OFFICIAL_AUTO_COMPACT_TOKEN_LIMIT, effective_window)

    return OfficialContextBudget(
        context_window=selected_window,
        max_context_window=selected_window,
        effective_context_window_percent=OFFICIAL_EFFECTIVE_CONTEXT_WINDOW_PERCENT,
        effective_context_window=effective_window,
        model_context_window=selected_window,
        model_auto_compact_token_limit=auto_compact_limit,
        source=source,
        freshness=freshness,
    )


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
