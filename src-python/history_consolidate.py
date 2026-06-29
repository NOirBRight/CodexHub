from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any, Iterable


OPENAI_PROVIDER = "openai"
CUSTOM_PROVIDER = "custom"
PROVIDER_VALUES = {OPENAI_PROVIDER, CUSTOM_PROVIDER}
STATE_DB_FILENAME = "state_5.sqlite"
SESSION_DIR_NAMES = ("sessions", "archived_sessions")
GLOBAL_STATE_THREAD_DICT_KEYS = (
    "thread-workspace-root-hints",
    "thread-projectless-output-directories",
    "queued-follow-ups",
)
GLOBAL_STATE_THREAD_LIST_KEYS = (
    "projectless-thread-ids",
    "pinned-thread-ids",
)
GLOBAL_STATE_LIST_UNION_KEYS = (
    "electron-saved-workspace-roots",
    "project-order",
)
ELECTRON_PERSISTED_THREAD_DICT_KEYS = (
    "heartbeat-thread-permissions-by-id",
)
ELECTRON_PERSISTED_LIST_KEYS = (
    "unread-thread-ids-by-host-v1",
)
REMOTE_SELECTION_KEYS = (
    "selected-remote-host-id",
    "active-remote-project",
    "active-remote-project-id",
    "active-remote-workspace-root",
    "active-remote-host-id",
)
REMOTE_AUTOCONNECT_KEY = "remote-connection-auto-connect-by-host-id"
ATOM_STATE_KEY = "electron-persisted-atom-state"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_config_sqlite_home(codex_dir: Path) -> Path | None:
    config_path = codex_dir / "config.toml"
    if not config_path.exists():
        return None
    text = config_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(?m)^\s*sqlite_home\s*=\s*(['\"])(.*?)\1\s*$", text)
    if not match:
        return None
    return Path(match.group(2)).expanduser()


def sqlite_db_paths(codex_dir: Path) -> list[Path]:
    paths = [codex_dir / STATE_DB_FILENAME]
    sqlite_home = read_config_sqlite_home(codex_dir)
    if sqlite_home is None:
        env_sqlite_home = os.environ.get("CODEX_SQLITE_HOME")
        sqlite_home = Path(env_sqlite_home).expanduser() if env_sqlite_home else None
    if sqlite_home is not None:
        candidate = sqlite_home / STATE_DB_FILENAME
        if candidate not in paths:
            paths.append(candidate)
    return paths


def collect_jsonl_files(codex_dir: Path) -> list[Path]:
    files: list[Path] = []
    for name in SESSION_DIR_NAMES:
        root = codex_dir / name
        if root.exists():
            files.extend(path for path in root.rglob("*.jsonl") if path.is_file())
    return files


def line_ending(line: bytes) -> bytes:
    for ending in (b"\r\n", b"\n", b"\r"):
        if line.endswith(ending):
            return ending
    return b""


def line_body(line: bytes) -> bytes:
    ending = line_ending(line)
    return line[: -len(ending)] if ending else line


def parse_json_line(line: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(line_body(line).decode("utf-8", errors="replace"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def session_meta_provider(line: bytes) -> tuple[str | None, str | None]:
    record = parse_json_line(line)
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None, None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None, None
    session_id = payload.get("id")
    provider = payload.get("model_provider")
    return (
        str(session_id) if session_id else None,
        str(provider) if provider else None,
    )


def equivalent_line_key(line: bytes) -> bytes:
    record = parse_json_line(line)
    if record is None:
        return line_body(line)
    if record.get("type") == "session_meta":
        payload = record.get("payload")
        if isinstance(payload, dict) and "model_provider" in payload:
            record = dict(record)
            payload = dict(payload)
            payload["model_provider"] = "<provider>"
            record["payload"] = payload
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def lines_equivalent(left: bytes, right: bytes) -> bool:
    return left == right or equivalent_line_key(left) == equivalent_line_key(right)


def rewrite_session_meta_provider(line: bytes, target_provider: str) -> bytes:
    record = parse_json_line(line)
    if record is None or record.get("type") != "session_meta":
        return line
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return line
    if payload.get("model_provider") == target_provider:
        return line
    record = dict(record)
    payload = dict(payload)
    payload["model_provider"] = target_provider
    record["payload"] = payload
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + line_ending(line)


def normalize_session_lines(lines: list[bytes], target_provider: str) -> list[bytes]:
    if not lines:
        return lines
    updated = list(lines)
    updated[0] = rewrite_session_meta_provider(updated[0], target_provider)
    return updated


def common_prefix_len(left: list[bytes], right: list[bytes]) -> int:
    count = 0
    for left_line, right_line in zip(left, right):
        if not lines_equivalent(left_line, right_line):
            break
        count += 1
    return count


def merge_session_lines(source_lines: list[bytes], active_lines: list[bytes], target_provider: str) -> tuple[list[bytes], str]:
    source = normalize_session_lines(source_lines, target_provider)
    active = normalize_session_lines(active_lines, target_provider)
    if source == active:
        return active, "unchanged"

    prefix = common_prefix_len(source, active)
    if prefix == len(source):
        return active, "kept-active-extends-source"
    if prefix == len(active):
        return source, "source-extends-active"

    source_keys = {equivalent_line_key(line) for line in source}
    active_extra = [line for line in active[prefix:] if equivalent_line_key(line) not in source_keys]
    if active_extra:
        return source + active_extra, "merged-branch"
    return source, "kept-source"


def relative_to_base(path: Path, base: Path) -> Path:
    try:
        return path.resolve().relative_to(base.resolve())
    except ValueError:
        return Path(path.name)


def backup_file(source: Path, codex_dir: Path, backup_root: Path, category: str) -> Path:
    relative = relative_to_base(source, codex_dir)
    target = backup_root / category / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def backup_sqlite(db_path: Path, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(db_path)
    try:
        try:
            source.execute("PRAGMA wal_checkpoint(FULL)")
        except sqlite3.DatabaseError:
            pass
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        temp_path.replace(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def rewrite_jsonl_file(path: Path, lines: list[bytes], codex_dir: Path, backup_root: Path, category: str) -> None:
    backup_file(path, codex_dir, backup_root, category)
    write_bytes_atomic(path, b"".join(lines))


def normalize_active_jsonl_files(codex_dir: Path, backup_root: Path, target_provider: str) -> int:
    changed = 0
    for path in collect_jsonl_files(codex_dir):
        lines = path.read_bytes().splitlines(keepends=True)
        if not lines:
            continue
        _session_id, provider = session_meta_provider(lines[0])
        if provider not in PROVIDER_VALUES or provider == target_provider:
            continue
        updated = normalize_session_lines(lines, target_provider)
        rewrite_jsonl_file(path, updated, codex_dir, backup_root, "active-before")
        changed += 1
    return changed


def merge_source_jsonl(source_dir: Path, codex_dir: Path, backup_root: Path, target_provider: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for source_path in collect_jsonl_files(source_dir):
        relative = relative_to_base(source_path, source_dir)
        active_path = codex_dir / relative
        source_lines = source_path.read_bytes().splitlines(keepends=True)
        if not source_lines:
            continue
        if not active_path.exists():
            normalized = normalize_session_lines(source_lines, target_provider)
            active_path.parent.mkdir(parents=True, exist_ok=True)
            write_bytes_atomic(active_path, b"".join(normalized))
            counts["copied-source"] += 1
            continue

        active_lines = active_path.read_bytes().splitlines(keepends=True)
        merged, result = merge_session_lines(source_lines, active_lines, target_provider)
        if b"".join(merged) != b"".join(active_lines):
            rewrite_jsonl_file(active_path, merged, codex_dir, backup_root, "active-before")
            counts[result] += 1
        else:
            counts["unchanged"] += 1
    return counts


def table_columns(connection: sqlite3.Connection, table_name: str) -> list[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [str(row[1]) for row in rows]


def quote_sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def merge_state_db(active_db: Path, source_db: Path, backup_root: Path, target_provider: str) -> dict[str, int | str]:
    if not active_db.exists() and not source_db.exists():
        return {"skipped": "missing"}
    if not active_db.exists():
        active_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_db, active_db)

    backup_sqlite(active_db, backup_root / "active-before" / active_db.name)
    active = sqlite3.connect(active_db)
    try:
        active_columns = table_columns(active, "threads")
        if "id" not in active_columns or "model_provider" not in active_columns:
            raise ValueError(f"active threads schema is missing required columns: {active_db}")
        inserted = 0
        if source_db.exists():
            source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
            try:
                source_columns = table_columns(source, "threads")
                if "id" not in source_columns or "model_provider" not in source_columns:
                    raise ValueError(f"source threads schema is missing required columns: {source_db}")

                shared_columns = [column for column in active_columns if column in set(source_columns)]
                if "id" not in shared_columns or "model_provider" not in shared_columns:
                    raise ValueError(
                        f"active/source threads schemas have no usable provider/id intersection: {active_db} <- {source_db}"
                    )

                active_ids = {
                    str(row[0])
                    for row in active.execute("SELECT id FROM threads").fetchall()
                }
                placeholders = ",".join("?" for _ in shared_columns)
                quoted_columns = ",".join(quote_sqlite_identifier(column) for column in shared_columns)
                insert_sql = f"INSERT INTO threads ({quoted_columns}) VALUES ({placeholders})"
                provider_index = shared_columns.index("model_provider")
                id_index = shared_columns.index("id")
                for row in source.execute(f"SELECT {quoted_columns} FROM threads").fetchall():
                    row_values = list(row)
                    thread_id = str(row_values[id_index])
                    if thread_id in active_ids:
                        continue
                    row_values[provider_index] = target_provider
                    active.execute(insert_sql, row_values)
                    active_ids.add(thread_id)
                    inserted += 1
            finally:
                source.close()

        updated_cursor = active.execute(
            "UPDATE threads SET model_provider = ? WHERE model_provider IN (?, ?) AND model_provider <> ?",
            (target_provider, OPENAI_PROVIDER, CUSTOM_PROVIDER, target_provider),
        )
        updated = updated_cursor.rowcount if updated_cursor.rowcount is not None else 0
        active.commit()
        return {"inserted": inserted, "provider_updated": updated}
    finally:
        active.close()


def merge_state_dbs(codex_dir: Path, source_dir: Path, backup_root: Path, target_provider: str) -> list[dict[str, Any]]:
    source_paths = sqlite_db_paths(source_dir)
    active_paths = sqlite_db_paths(codex_dir)
    results: list[dict[str, Any]] = []
    for index, active_db in enumerate(active_paths):
        source_db = source_paths[index] if index < len(source_paths) else source_dir / STATE_DB_FILENAME
        result = merge_state_db(active_db, source_db, backup_root, target_provider)
        result["path"] = str(active_db)
        results.append(result)
    return results


def list_union(destination: list[Any], source: list[Any]) -> list[Any]:
    result = list(destination)
    seen = {json.dumps(value, sort_keys=True, ensure_ascii=False, default=str) for value in result}
    for value in source:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            result.append(value)
            seen.add(key)
    return result


def merge_dict_missing(destination: dict[str, Any], source: dict[str, Any]) -> int:
    added = 0
    for key, value in source.items():
        if key not in destination:
            destination[key] = value
            added += 1
    return added


def remove_remote_selection_values(container: dict[str, Any]) -> int:
    changed = 0
    for key in REMOTE_SELECTION_KEYS:
        if key in container:
            container.pop(key, None)
            changed += 1
    value = container.get(REMOTE_AUTOCONNECT_KEY)
    if isinstance(value, dict) and value:
        container[REMOTE_AUTOCONNECT_KEY] = {}
        changed += 1
    return changed


def sanitize_global_state_remote_selection(state: dict[str, Any]) -> int:
    changed = remove_remote_selection_values(state)
    atom_state = state.get(ATOM_STATE_KEY)
    if isinstance(atom_state, dict):
        changed += remove_remote_selection_values(atom_state)
    return changed


def merge_global_state(codex_dir: Path, source_dir: Path, backup_root: Path) -> dict[str, int | str]:
    source_path = source_dir / ".codex-global-state.json"
    active_path = codex_dir / ".codex-global-state.json"
    if not source_path.exists():
        return {"skipped": "source-missing"}
    if not active_path.exists():
        active_path.parent.mkdir(parents=True, exist_ok=True)
        source = json.loads(source_path.read_text(encoding="utf-8-sig"))
        removed_remote_keys = sanitize_global_state_remote_selection(source) if isinstance(source, dict) else 0
        active_path.write_text(json.dumps(source, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        return {"copied": 1, "removed_remote_keys": removed_remote_keys}

    source = json.loads(source_path.read_text(encoding="utf-8-sig"))
    active = json.loads(active_path.read_text(encoding="utf-8-sig"))
    changed = 0
    changed += sanitize_global_state_remote_selection(active)
    if isinstance(source, dict):
        sanitize_global_state_remote_selection(source)

    for key in GLOBAL_STATE_THREAD_DICT_KEYS:
        if isinstance(source.get(key), dict):
            if not isinstance(active.get(key), dict):
                active[key] = {}
                changed += 1
            changed += merge_dict_missing(active[key], source[key])

    for key in GLOBAL_STATE_THREAD_LIST_KEYS + GLOBAL_STATE_LIST_UNION_KEYS:
        if isinstance(source.get(key), list):
            old = active.get(key) if isinstance(active.get(key), list) else []
            new = list_union(old, source[key])
            if new != old:
                active[key] = new
                changed += 1

    source_epa = source.get("electron-persisted-atom-state")
    active_epa = active.get("electron-persisted-atom-state")
    if isinstance(source_epa, dict):
        if not isinstance(active_epa, dict):
            active_epa = {}
            active["electron-persisted-atom-state"] = active_epa
            changed += 1
        for key in ELECTRON_PERSISTED_THREAD_DICT_KEYS:
            if isinstance(source_epa.get(key), dict):
                if not isinstance(active_epa.get(key), dict):
                    active_epa[key] = {}
                    changed += 1
                changed += merge_dict_missing(active_epa[key], source_epa[key])
        for key in ELECTRON_PERSISTED_LIST_KEYS:
            if isinstance(source_epa.get(key), dict):
                if not isinstance(active_epa.get(key), dict):
                    active_epa[key] = {}
                    changed += 1
                for host, values in source_epa[key].items():
                    if not isinstance(values, list):
                        continue
                    old_values = active_epa[key].get(host) if isinstance(active_epa[key].get(host), list) else []
                    new_values = list_union(old_values, values)
                    if new_values != old_values:
                        active_epa[key][host] = new_values
                        changed += 1

    if changed:
        backup_file(active_path, codex_dir, backup_root, "active-before")
        active_path.write_text(json.dumps(active, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return {"changed_fields": changed}


def provider_counts_from_jsonl(codex_dir: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    for path in collect_jsonl_files(codex_dir):
        try:
            first = path.open("rb").readline()
        except OSError:
            continue
        _session_id, provider = session_meta_provider(first)
        counts[provider or "<missing>"] += 1
    return counts


def provider_counts_from_state(codex_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for db_path in sqlite_db_paths(codex_dir):
        if not db_path.exists():
            results.append({"path": str(db_path), "skipped": "missing"})
            continue
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT COALESCE(model_provider, '<null>'), COUNT(*) FROM threads GROUP BY 1 ORDER BY 1"
            ).fetchall()
            results.append({"path": str(db_path), "counts": dict((str(k), int(v)) for k, v in rows)})
        finally:
            connection.close()
    return results


def status(codex_dir: Path) -> dict[str, Any]:
    return {
        "codex_dir": str(codex_dir),
        "jsonl": dict(provider_counts_from_jsonl(codex_dir)),
        "state": provider_counts_from_state(codex_dir),
    }


def official_main(codex_dir: Path, source_dir: Path, backup_root: Path, target_provider: str) -> dict[str, Any]:
    if target_provider not in PROVIDER_VALUES:
        raise ValueError(f"unsupported target provider: {target_provider}")
    if not source_dir.exists():
        raise FileNotFoundError(source_dir)

    backup_root.mkdir(parents=True, exist_ok=True)
    meta = {
        "created_at": utc_now_iso(),
        "codex_dir": str(codex_dir),
        "source_dir": str(source_dir),
        "target_provider": target_provider,
    }
    (backup_root / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    jsonl_counts = merge_source_jsonl(source_dir, codex_dir, backup_root, target_provider)
    normalized = normalize_active_jsonl_files(codex_dir, backup_root, target_provider)
    state_results = merge_state_dbs(codex_dir, source_dir, backup_root, target_provider)
    global_state = merge_global_state(codex_dir, source_dir, backup_root)

    return {
        "jsonl": dict(sorted(jsonl_counts.items())),
        "normalized_jsonl": normalized,
        "state": state_results,
        "global_state": global_state,
        "backup_root": str(backup_root),
    }


def print_result(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consolidate Codex single-history provider buckets.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--codex-dir", required=True, type=Path)

    official_parser = subparsers.add_parser("official-main")
    official_parser.add_argument("--codex-dir", required=True, type=Path)
    official_parser.add_argument("--source-dir", required=True, type=Path)
    official_parser.add_argument("--backup-root", required=True, type=Path)
    official_parser.add_argument("--target-provider", required=True, choices=sorted(PROVIDER_VALUES))

    args = parser.parse_args(argv)
    if args.command == "status":
        print_result(status(args.codex_dir))
        return 0
    if args.command == "official-main":
        print_result(official_main(args.codex_dir, args.source_dir, args.backup_root, args.target_provider))
        return 0
    raise ValueError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
