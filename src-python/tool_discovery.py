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
        if item.get("execution") != "client" or item.get("status") not in (None, "completed"):
            continue
        discovered = item.get("tools")
        if not isinstance(discovered, list):
            continue
        for declaration in discovered:
            if not isinstance(declaration, dict):
                continue
            kind, name = declaration.get("type"), declaration.get("name")
            if kind not in ("function", "namespace", "custom") or not isinstance(name, str):
                continue
            if kind == "namespace" and not isinstance(declaration.get("tools"), list):
                continue
            declaration = deepcopy(declaration)
            # Discovery has already happened in Codex. Chat APIs cannot carry
            # its defer_loading marker, and the disclosed tool must be callable.
            declaration.pop("defer_loading", None)
            for child in declaration.get("tools", []) if kind == "namespace" else []:
                if isinstance(child, dict):
                    child.pop("defer_loading", None)
            existing = next((t for t in tools if isinstance(t, dict)
                             and t.get("type") == kind and t.get("name") == name), None)
            if kind == "namespace":
                retained.add(name)
                children = declaration.get("tools")
                if existing is not None:
                    current = existing.get("tools")
                    if isinstance(current, list):
                        merged = deepcopy(existing)
                        merged.pop("defer_loading", None)
                        by_name = {c["name"]: c for c in merged["tools"]
                                   if isinstance(c, dict) and isinstance(c.get("name"), str)}
                        for child in children:
                            if not isinstance(child, dict) or not isinstance(child.get("name"), str):
                                continue
                            match = by_name.get(child.get("name"))
                            if match is not None:
                                match.pop("defer_loading", None)
                            else:
                                merged["tools"].append(child)
                                by_name[child.get("name")] = child
                        if merged != existing:
                            tools[tools.index(existing)] = merged
                            changed = True
                    continue
            elif existing is not None and "defer_loading" in existing:
                promoted = dict(existing)
                promoted.pop("defer_loading")
                tools[tools.index(existing)] = promoted
                changed = True
            if existing is None:
                tools.append(declaration)
                changed = True
    return retained, changed
