"""Expose client-discovered declarations on plain-function provider wires."""

from copy import deepcopy
from typing import Any


def promote_client_search_results(payload: dict[str, Any]) -> tuple[set[str], bool]:
    """Merge completed client search results; return namespaces to retain.

    Codex keeps discoveries in tool_search_output rather than repeating them
    in the top-level tools list. The client owns search and execution; this
    boundary only makes its returned declarations representable upstream.
    """
    tools = payload.get("tools")
    history = payload.get("input")
    if not isinstance(tools, list) or not isinstance(history, list):
        return set(), False
    if not any(isinstance(t, dict) and t.get("type") == "tool_search"
               and t.get("execution") == "client" for t in tools):
        return set(), False
    retained: set[str] = set()
    changed = False
    for item in history:
        if not isinstance(item, dict) or item.get("type") != "tool_search_output":
            continue
        if item.get("execution") != "client" or item.get("status") not in {None, "completed"}:
            continue
        discovered = item.get("tools")
        if not isinstance(discovered, list):
            continue
        for declaration in discovered:
            if not isinstance(declaration, dict):
                continue
            declaration = deepcopy(declaration)
            # Discovery has already happened in Codex. Chat APIs cannot carry
            # its defer_loading marker, and the disclosed tool must be callable.
            declaration.pop("defer_loading", None)
            for child in declaration.get("tools", []):
                if isinstance(child, dict):
                    child.pop("defer_loading", None)
            kind, name = declaration.get("type"), declaration.get("name")
            if kind not in {"function", "namespace", "custom"} or not isinstance(name, str):
                continue
            existing = next((t for t in tools if isinstance(t, dict)
                             and t.get("type") == kind and t.get("name") == name), None)
            if kind == "namespace":
                retained.add(name)
                children = declaration.get("tools")
                if not isinstance(children, list):
                    continue
                if existing is not None:
                    current = existing.get("tools")
                    if isinstance(current, list):
                        names = {c.get("name") for c in current if isinstance(c, dict)}
                        additions = [c for c in children if isinstance(c, dict) and c.get("name") not in names]
                        current.extend(additions)
                        changed |= bool(additions)
                    continue
            if existing is None:
                tools.append(declaration)
                changed = True
    return retained, changed
