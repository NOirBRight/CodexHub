from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any


ASSIST_MODES = {"strict", "guided", "assisted"}
REPAIR_CODEX_SUBAGENT = "codex_subagent_repair"


def subagent_assist_mode() -> str:
    raw = os.environ.get("CODEXHUB_SUBAGENT_ASSIST_MODE", "assisted")
    value = raw.strip().lower() if isinstance(raw, str) else "assisted"
    return value if value in ASSIST_MODES else "assisted"


def guidance_enabled(context: Mapping[str, Any] | None) -> bool:
    if not _subagent_repair_policy_enabled(context):
        return False
    if _raw_provider_probe(context):
        return False
    return subagent_assist_mode() in {"guided", "assisted"}


def semantic_repair_enabled(context: Mapping[str, Any] | None) -> bool:
    if not _subagent_repair_policy_enabled(context):
        return False
    if _raw_provider_probe(context):
        return False
    return subagent_assist_mode() == "assisted"


def deterministic_required_action(actions: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    deterministic = [action for action in actions if _has_known_tool_and_arguments(action)]
    if len(deterministic) != 1:
        return None
    return deterministic[0]


def _has_known_tool_and_arguments(action: Mapping[str, Any]) -> bool:
    tool_name = action.get("tool_name")
    arguments = action.get("arguments")
    return isinstance(tool_name, str) and bool(tool_name) and isinstance(arguments, Mapping)


def _raw_provider_probe(context: Mapping[str, Any] | None) -> bool:
    return bool(context and context.get("raw_provider_probe"))


def _subagent_repair_policy_enabled(context: Mapping[str, Any] | None) -> bool:
    return bool(context and context.get("repair_policy") == REPAIR_CODEX_SUBAGENT)
