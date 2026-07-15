from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


CURRENT_DIRECT_OFFICIAL_SOURCE = "current_direct_official"
FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE = "fresh_direct_official_cache_authority"
DEGRADED_LAST_KNOWN_OFFICIAL_SOURCE = "degraded_last_known_official"
NATIVE_AUTO_COMPACT_PERCENT = 90
TRUSTED_FRESH_OFFICIAL_CONTEXT_SOURCES = frozenset(
    {
        CURRENT_DIRECT_OFFICIAL_SOURCE,
        FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE,
    }
)


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


def _context_percent(value: Any) -> int | None:
    value = _positive_int(value)
    return value if value is not None and value <= 100 else None


def _auto_compact_limit(
    *,
    context_window: int,
    effective_context_window: int,
    requested_limit: int | None,
) -> int:
    native_limit = context_window * NATIVE_AUTO_COMPACT_PERCENT // 100
    return min(
        requested_limit if requested_limit is not None else native_limit,
        effective_context_window,
    )


def resolve_official_context_budget(
    *,
    direct_context_window: Any = None,
    direct_max_context_window: Any = None,
    direct_effective_context_window_percent: Any = None,
    direct_auto_compact_token_limit: Any = None,
    direct_freshness: str = "missing",
    direct_source: str = "missing",
    fallback_context_window: Any = None,
    fallback_effective_context_window_percent: Any = None,
    fallback_auto_compact_token_limit: Any = None,
) -> OfficialContextBudget | None:
    """Resolve an Official usable-context budget without trusting stale probes.

    A current Direct Official snapshot or a provenance-checked fresh Direct
    Official cache authority may expand a budget.  A degraded snapshot requires
    a previously resolved safe value and can only hold or tighten it.  Returning
    ``None`` deliberately fails closed when neither source can establish a safe
    Official cap.
    """
    direct_context = _positive_int(direct_context_window)
    direct_max = _positive_int(direct_max_context_window)
    direct_percent = _context_percent(direct_effective_context_window_percent)
    direct_auto_compact = _positive_int(direct_auto_compact_token_limit)
    allowed_freshness = {"fresh", "missing", "stale", "contradictory"}
    freshness = direct_freshness if direct_freshness in allowed_freshness else "missing"

    direct_is_contradictory = (
        direct_context is not None
        and direct_max is not None
        and direct_max < direct_context
    ) or (
        direct_effective_context_window_percent is not None
        and direct_percent is None
    ) or (
        direct_auto_compact_token_limit is not None
        and direct_auto_compact is None
    )
    trusted_fresh_direct = (
        direct_source in TRUSTED_FRESH_OFFICIAL_CONTEXT_SOURCES
        and freshness == "fresh"
        and direct_context is not None
        and direct_percent is not None
        and not direct_is_contradictory
    )

    if trusted_fresh_direct:
        effective_percent = direct_percent
        effective_window = max(1, direct_context * effective_percent // 100)
        auto_compact_limit = _auto_compact_limit(
            context_window=direct_context,
            effective_context_window=effective_window,
            requested_limit=direct_auto_compact,
        )
        return OfficialContextBudget(
            context_window=direct_context,
            max_context_window=direct_context,
            effective_context_window_percent=effective_percent,
            effective_context_window=effective_window,
            model_context_window=direct_context,
            model_auto_compact_token_limit=auto_compact_limit,
            source=direct_source,
            freshness="fresh",
        )

    # A stale, bundled, malformed, or incomplete snapshot is never enough to
    # create a new cap.  It can only tighten a budget already emitted from a
    # previous safe decision.
    fallback_context = _positive_int(fallback_context_window)
    if fallback_context is None:
        return None

    if direct_is_contradictory:
        freshness = "contradictory"
    elif freshness == "fresh":
        freshness = "missing"

    context_candidates = [fallback_context]
    context_candidates.extend(
        value for value in (direct_context, direct_max) if value is not None
    )
    selected_window = min(context_candidates)
    fallback_percent = _context_percent(fallback_effective_context_window_percent)
    if fallback_percent is None:
        return None
    percent_candidates = [fallback_percent]
    percent_candidates.extend(value for value in (direct_percent,) if value is not None)
    effective_percent = min(percent_candidates)
    effective_window = max(1, selected_window * effective_percent // 100)
    fallback_auto_compact = _positive_int(fallback_auto_compact_token_limit)
    auto_candidates = [
        value
        for value in (fallback_auto_compact, direct_auto_compact)
        if value is not None
    ]
    requested_auto_compact = min(auto_candidates) if auto_candidates else None
    auto_compact_limit = _auto_compact_limit(
        context_window=selected_window,
        effective_context_window=effective_window,
        requested_limit=requested_auto_compact,
    )

    return OfficialContextBudget(
        context_window=selected_window,
        max_context_window=selected_window,
        effective_context_window_percent=effective_percent,
        effective_context_window=effective_window,
        model_context_window=selected_window,
        model_auto_compact_token_limit=auto_compact_limit,
        source=DEGRADED_LAST_KNOWN_OFFICIAL_SOURCE,
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
