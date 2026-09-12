"""Compatibility reader for the retired subagent-assist configuration.

``CODEXHUB_SUBAGENT_ASSIST_MODE`` is still parsed for callers that display or
migrate the setting.  Gateway execution deliberately ignores every value: the
Codex client owns subagent scheduling and authorization.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

ASSIST_MODES = frozenset({"strict", "guided", "assisted"})
REPAIR_CODEX_SUBAGENT = "codex_subagent_repair"


def subagent_assist_mode() -> str:
    raw = os.environ.get("CODEXHUB_SUBAGENT_ASSIST_MODE", "assisted")
    value = raw.strip().lower() if isinstance(raw, str) else "assisted"
    return value if value in ASSIST_MODES else "assisted"


def guidance_enabled(context: Mapping[str, Any] | None) -> bool:
    """Legacy API retained for compatibility; semantic guidance is retired."""
    _ = context
    return False


def semantic_repair_enabled(context: Mapping[str, Any] | None) -> bool:
    """Legacy API retained for compatibility; semantic repair is retired."""
    _ = context
    return False


def deterministic_required_action(
    actions: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Retired scheduler helper: never select a client action on its behalf."""
    _ = actions
    return None
