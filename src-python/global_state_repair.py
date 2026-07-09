from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
from typing import Any

from atomic_io import atomic_write_text


REMOTE_SELECTION_KEYS = (
    "selected-remote-host-id",
    "active-remote-project",
    "active-remote-project-id",
    "active-remote-workspace-root",
    "active-remote-host-id",
)
REMOTE_AUTOCONNECT_KEY = "remote-connection-auto-connect-by-host-id"
ATOM_STATE_KEY = "electron-persisted-atom-state"


def default_state_path() -> Path:
    return Path.home() / ".codex" / ".codex-global-state.json"


def remove_remote_selection_values(container: dict[str, Any]) -> dict[str, Any]:
    removed: dict[str, Any] = {}
    for key in REMOTE_SELECTION_KEYS:
        if key in container:
            removed[key] = container.pop(key)

    value = container.get(REMOTE_AUTOCONNECT_KEY)
    if isinstance(value, dict) and value:
        removed[REMOTE_AUTOCONNECT_KEY] = dict(value)
        container[REMOTE_AUTOCONNECT_KEY] = {}
    return removed


def repair_global_state(state_path: Path, backup_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"path": str(state_path), "changed": False, "skipped": "missing"}

    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    if not isinstance(state, dict):
        return {"path": str(state_path), "changed": False, "skipped": "not_json_object"}

    removed: dict[str, Any] = {}
    top_level_removed = remove_remote_selection_values(state)
    if top_level_removed:
        removed["top_level"] = top_level_removed

    atom_state = state.get(ATOM_STATE_KEY)
    if isinstance(atom_state, dict):
        atom_removed = remove_remote_selection_values(atom_state)
        if atom_removed:
            removed[ATOM_STATE_KEY] = atom_removed

    if not removed:
        return {"path": str(state_path), "changed": False, "removed": {}}

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(state_path, backup_path)
    atomic_write_text(
        state_path,
        json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return {
        "path": str(state_path),
        "changed": True,
        "backup": str(backup_path),
        "removed_keys": {
            scope: sorted(values.keys()) for scope, values in removed.items() if isinstance(values, dict)
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair Codex Desktop global state for local proxy launches.")
    parser.add_argument("command", choices=("repair",))
    parser.add_argument("--state", type=Path, default=default_state_path())
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args(argv)

    result = repair_global_state(args.state, args.backup)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
