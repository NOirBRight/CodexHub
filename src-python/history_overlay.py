from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any, Iterable

from atomic_io import atomic_write_text


SOURCE_PROVIDER = "openai"
TARGET_PROVIDER = "custom"
STATE_DB_FILENAME = "state_5.sqlite"
SESSION_DIR_NAMES = ("sessions", "archived_sessions")
SQLITE_ID_CHUNK = 500
OFFICIAL_ENCRYPTED_CONTENT_PREFIX = "gAAAA"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def read_config_sqlite_home(codex_dir: Path) -> Path | None:
    config_path = codex_dir / "config.toml"
    if not config_path.exists():
        return None
    text = config_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(?m)^\s*sqlite_home\s*=\s*(['\"])(.*?)\1\s*$", text)
    if not match:
        return None
    return Path(match.group(2)).expanduser()


def live_config_routes_custom(codex_dir: Path) -> bool:
    config_path = codex_dir / "config.toml"
    if not config_path.exists():
        return False
    text = config_path.read_text(encoding="utf-8", errors="replace")
    in_top_level = True
    for line in text.splitlines():
        if re.match(r"^\s*\[", line):
            in_top_level = False
        if not in_top_level:
            continue
        match = re.match(r"^\s*model_provider\s*=\s*(['\"])(.*?)\1\s*(?:#.*)?$", line)
        if match:
            return match.group(2).strip() == TARGET_PROVIDER
    return False


def sqlite_db_paths(codex_dir: Path) -> list[Path]:
    paths: list[Path] = [codex_dir / STATE_DB_FILENAME]
    sqlite_home = read_config_sqlite_home(codex_dir)
    if sqlite_home is None:
        env_sqlite_home = os.environ.get("CODEX_SQLITE_HOME")
        sqlite_home = Path(env_sqlite_home).expanduser() if env_sqlite_home else None
    if sqlite_home is not None:
        candidate = sqlite_home / STATE_DB_FILENAME
        if candidate not in paths:
            paths.append(candidate)
    return paths


def backup_file(source: Path, codex_dir: Path, backup_root: Path, category: str) -> Path:
    try:
        relative = source.resolve().relative_to(codex_dir.resolve())
    except ValueError:
        relative = Path(source.name)
    target = backup_root / category / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def backup_sqlite(db_path: Path, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(db_path)
    try:
        source.execute("PRAGMA wal_checkpoint(FULL)")
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def sqlite_backup_path(db_path: Path, codex_dir: Path, backup_root: Path) -> Path:
    try:
        relative = db_path.resolve().relative_to(codex_dir.resolve())
        return backup_root / "state" / relative
    except (OSError, ValueError):
        try:
            key = str(db_path.resolve())
        except OSError:
            key = str(db_path)
        digest = hashlib.sha256(key.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
        return backup_root / "state-external" / digest / db_path.name


def chunks(values: list[str], size: int = SQLITE_ID_CHUNK) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate_state_db(db_path: Path, codex_dir: Path, backup_root: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"path": str(db_path), "thread_ids": [], "target_thread_ids": [], "skipped": "missing"}

    backup_sqlite(db_path, sqlite_backup_path(db_path, codex_dir, backup_root))
    connection = sqlite3.connect(db_path)
    try:
        target_rows = connection.execute(
            "SELECT id FROM threads WHERE model_provider = ?",
            (TARGET_PROVIDER,),
        ).fetchall()
        target_thread_ids = [str(row[0]) for row in target_rows]
        rows = connection.execute(
            "SELECT id FROM threads WHERE model_provider = ?",
            (SOURCE_PROVIDER,),
        ).fetchall()
        thread_ids = [str(row[0]) for row in rows]
        if thread_ids:
            connection.execute(
                "UPDATE threads SET model_provider = ? WHERE model_provider = ?",
                (TARGET_PROVIDER, SOURCE_PROVIDER),
            )
            connection.commit()
        return {"path": str(db_path), "thread_ids": thread_ids, "target_thread_ids": target_thread_ids}
    finally:
        connection.close()


def restore_state_db(entry: dict[str, Any]) -> int:
    db_path = Path(str(entry.get("path", "")))
    thread_ids = [str(value) for value in entry.get("thread_ids", []) if value]
    if not db_path.exists() or not thread_ids:
        return 0

    connection = sqlite3.connect(db_path)
    try:
        restored = 0
        for group in chunks(thread_ids):
            placeholders = ",".join("?" for _ in group)
            params = [SOURCE_PROVIDER, TARGET_PROVIDER, *group]
            cursor = connection.execute(
                f"UPDATE threads SET model_provider = ? WHERE model_provider = ? AND id IN ({placeholders})",
                params,
            )
            restored += cursor.rowcount if cursor.rowcount is not None else 0
        connection.commit()
        return restored
    finally:
        connection.close()


def collect_jsonl_files(codex_dir: Path, modified_after: float | None = None) -> list[Path]:
    files: list[Path] = []
    for name in SESSION_DIR_NAMES:
        root = codex_dir / name
        if root.exists():
            for path in root.rglob("*.jsonl"):
                if modified_after is not None:
                    try:
                        if path.stat().st_mtime < modified_after:
                            continue
                    except OSError:
                        continue
                files.append(path)
    return files


def collect_state_session_files(codex_dir: Path) -> list[Path]:
    files_by_key: dict[str, Path] = {}
    for db_path in sqlite_db_paths(codex_dir):
        if not db_path.exists():
            continue
        connection = sqlite3.connect(db_path)
        try:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(threads)").fetchall()}
            if "rollout_path" not in columns or "model_provider" not in columns:
                continue
            rows = connection.execute(
                """
                SELECT rollout_path FROM threads
                WHERE model_provider IN (?, ?)
                  AND rollout_path IS NOT NULL
                  AND rollout_path != ''
                """,
                (SOURCE_PROVIDER, TARGET_PROVIDER),
            ).fetchall()
        finally:
            connection.close()

        for row in rows:
            path = Path(str(row[0]))
            if not path.is_absolute():
                path = codex_dir / path
            if not path.exists():
                continue
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            files_by_key.setdefault(key, path)
    return list(files_by_key.values())


def collect_normalization_session_files(codex_dir: Path) -> tuple[list[Path], str]:
    state_files = collect_state_session_files(codex_dir)
    filesystem_files = collect_jsonl_files(codex_dir)

    files_by_key: dict[str, Path] = {}
    state_keys: set[str] = set()
    for path in state_files:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        state_keys.add(key)
        files_by_key.setdefault(key, path)

    filesystem_added = False
    for path in filesystem_files:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key not in state_keys:
            filesystem_added = True
        files_by_key.setdefault(key, path)

    if state_files and filesystem_added:
        return list(files_by_key.values()), "state_rollout_path+filesystem_scan"
    if state_files:
        return list(files_by_key.values()), "state_rollout_path"
    return list(files_by_key.values()), "filesystem_scan"


def parse_session_meta(line: str) -> tuple[str | None, str | None, dict[str, Any] | None]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None, None, None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None, None, None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None, None, None
    session_id = payload.get("id")
    provider = payload.get("model_provider")
    return (
        str(session_id) if session_id else None,
        str(provider) if provider else None,
        record,
    )


def relative_to_codex_dir(path: Path, codex_dir: Path) -> str:
    try:
        return path.resolve().relative_to(codex_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


def collect_session_provider_entries(codex_dir: Path, provider: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for path in collect_jsonl_files(codex_dir):
        first_line = read_first_line(path)
        if not first_line:
            continue
        session_id, current_provider, record = parse_session_meta(first_line)
        if record is None or current_provider != provider:
            continue
        entries.append({"path": relative_to_codex_dir(path, codex_dir), "session_id": session_id or ""})
    return entries


def split_line_ending(line: str) -> tuple[str, str]:
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return line[: -len(ending)], ending
    return line, ""


def looks_like_official_encrypted_content(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(OFFICIAL_ENCRYPTED_CONTENT_PREFIX)


def sanitize_invalid_reasoning_encrypted_content(value: Any) -> bool:
    changed = False

    if isinstance(value, list):
        for item in value:
            if sanitize_invalid_reasoning_encrypted_content(item):
                changed = True
        return changed

    if not isinstance(value, dict):
        return False

    if value.get("type") == "reasoning" and "encrypted_content" in value:
        if not looks_like_official_encrypted_content(value.get("encrypted_content")):
            value.pop("encrypted_content", None)
            changed = True

    for item in value.values():
        if sanitize_invalid_reasoning_encrypted_content(item):
            changed = True

    return changed


def sanitize_session_lines_for_official(lines: list[str]) -> list[str]:
    sanitized = list(lines)
    for index, line in enumerate(lines):
        content, line_ending = split_line_ending(line)
        try:
            record = json.loads(content)
        except json.JSONDecodeError:
            continue
        if sanitize_invalid_reasoning_encrypted_content(record):
            sanitized[index] = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + line_ending
    return sanitized


def read_first_line(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            return handle.readline()
    except OSError:
        return ""


def rewritten_provider_line(first_line: str, record: dict[str, Any], source_provider: str, target_provider: str) -> str:
    pattern = rf'("model_provider"\s*:\s*"){re.escape(source_provider)}(")'
    rewritten, count = re.subn(pattern, rf"\1{target_provider}\2", first_line, count=1)
    if count == 1:
        return rewritten

    content, line_ending = split_line_ending(first_line)
    payload = record["payload"]
    payload["model_provider"] = target_provider
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + line_ending


def write_first_line_in_place(path: Path, old_line: str, new_line: str) -> None:
    old_bytes = old_line.encode("utf-8")
    new_bytes = new_line.encode("utf-8")
    if len(old_bytes) != len(new_bytes):
        raise ValueError("rewritten session_meta line changed byte length")

    with path.open("r+b") as handle:
        current = handle.readline()
        if current != old_bytes:
            raise ValueError("session_meta line changed while rewriting")
        handle.seek(0)
        handle.write(new_bytes)


def skipped_session_file_provider_fast(entry: dict[str, str], reason: str, current_line: str = "") -> dict[str, str]:
    result = dict(entry)
    result["status"] = "skipped"
    result["reason"] = reason
    if current_line:
        result["current_first_line"] = current_line
    return result


def write_rewritten_lines(path: Path, lines: list[str]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.writelines(lines)
        temp_path.replace(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def rewrite_session_file(
    path: Path,
    codex_dir: Path,
    backup_root: Path,
    source_provider: str,
    target_provider: str,
    allowed_session_ids: set[str] | None = None,
    sanitize_for_official: bool = False,
) -> dict[str, str] | None:
    first_line = read_first_line(path)
    if not first_line:
        return None

    session_id, provider, record = parse_session_meta(first_line)
    if record is None or provider != source_provider:
        return None
    if allowed_session_ids is not None and session_id not in allowed_session_ids:
        return None

    new_first_line = rewritten_provider_line(first_line, record, source_provider, target_provider)
    backup_file(path, codex_dir, backup_root, "jsonl")
    if sanitize_for_official:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        if not lines:
            return None
        lines[0] = new_first_line
        sanitized_lines = sanitize_session_lines_for_official(lines)
        if sanitized_lines != lines:
            write_rewritten_lines(path, sanitized_lines)
        else:
            write_first_line_in_place(path, first_line, new_first_line)
    else:
        write_first_line_in_place(path, first_line, new_first_line)

    return {"path": relative_to_codex_dir(path, codex_dir), "session_id": session_id or ""}


def apply_history_overlay(codex_dir: Path, backup_root: Path, ledger_path: Path) -> dict[str, Any]:
    backup_root.mkdir(parents=True, exist_ok=True)
    started_at = time.perf_counter()
    preexisting_target_jsonl = collect_session_provider_entries(codex_dir, TARGET_PROVIDER)
    preexisting_done_at = time.perf_counter()
    state_entries = [migrate_state_db(path, codex_dir, backup_root) for path in sqlite_db_paths(codex_dir)]
    state_done_at = time.perf_counter()
    jsonl_entries = [
        entry
        for path in collect_jsonl_files(codex_dir)
        if (entry := rewrite_session_file(path, codex_dir, backup_root, SOURCE_PROVIDER, TARGET_PROVIDER)) is not None
    ]
    jsonl_done_at = time.perf_counter()
    ledger = {
        "version": 1,
        "created_at": utc_now_iso(),
        "codex_dir": str(codex_dir),
        "source_provider": SOURCE_PROVIDER,
        "target_provider": TARGET_PROVIDER,
        "state": state_entries,
        "jsonl": jsonl_entries,
        "preexisting_target_jsonl": preexisting_target_jsonl,
        "timings": {
            "preexisting_scan_seconds": round(preexisting_done_at - started_at, 3),
            "state_apply_seconds": round(state_done_at - preexisting_done_at, 3),
            "jsonl_apply_seconds": round(jsonl_done_at - state_done_at, 3),
            "total_apply_seconds": round(jsonl_done_at - started_at, 3),
        },
    }
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(ledger_path, ledger)
    return ledger


def restore_history_overlay(ledger_path: Path) -> dict[str, int]:
    if not ledger_path.exists():
        return {"state_rows": 0, "jsonl_files": 0, "new_state_rows": 0, "new_jsonl_files": 0, "timings": {}}
    started_at = time.perf_counter()
    ledger = json.loads(ledger_path.read_text(encoding="utf-8-sig"))
    codex_dir = Path(str(ledger["codex_dir"]))
    backup_root = ledger_path.parent / "restore-backup"
    state_rows = sum(restore_state_db(entry) for entry in ledger.get("state", []))
    state_restore_done_at = time.perf_counter()

    allowed_ids = {
        str(entry.get("session_id"))
        for entry in ledger.get("jsonl", [])
        if entry.get("session_id")
    }
    jsonl_files = 0
    if allowed_ids:
        for entry in ledger.get("jsonl", []):
            relative_path = str(entry.get("path", ""))
            if not relative_path:
                continue
            path = codex_dir / relative_path
            if rewrite_session_file(
                path,
                codex_dir,
                backup_root,
                TARGET_PROVIDER,
                SOURCE_PROVIDER,
                allowed_ids,
            ):
                jsonl_files += 1
    ledger_jsonl_done_at = time.perf_counter()

    return {
        "state_rows": state_rows,
        "jsonl_files": jsonl_files,
        "new_state_rows": 0,
        "new_jsonl_files": 0,
        "timings": {
            "state_restore_seconds": round(state_restore_done_at - started_at, 3),
            "new_state_restore_seconds": 0,
            "ledger_jsonl_restore_seconds": round(ledger_jsonl_done_at - state_restore_done_at, 3),
            "new_jsonl_scan_restore_seconds": 0,
            "total_restore_seconds": round(ledger_jsonl_done_at - started_at, 3),
        },
    }


def convert_state_provider(
    db_path: Path,
    backup_root: Path,
    source_provider: str,
    target_provider: str,
    codex_dir: Path | None = None,
) -> int:
    if not db_path.exists():
        return 0

    backup_sqlite(db_path, sqlite_backup_path(db_path, codex_dir or db_path.parent, backup_root))
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.execute(
            "UPDATE threads SET model_provider = ? WHERE model_provider = ?",
            (target_provider, source_provider),
        )
        connection.commit()
        return cursor.rowcount if cursor.rowcount is not None else 0
    finally:
        connection.close()


def state_provider_dirty_count(
    db_path: Path,
    source_provider: str,
    allowed_thread_ids: set[str] | None = None,
) -> int:
    if not db_path.exists():
        return 0
    connection = sqlite3.connect(db_path)
    try:
        columns = table_columns(connection, "threads")
        if "model_provider" not in columns:
            return 0
        if allowed_thread_ids is not None:
            if "id" not in columns or not allowed_thread_ids:
                return 0
            total = 0
            for group in chunks(sorted(allowed_thread_ids)):
                placeholders = ",".join("?" for _ in group)
                row = connection.execute(
                    f"SELECT COUNT(*) FROM threads WHERE model_provider = ? AND id IN ({placeholders})",
                    [source_provider, *group],
                ).fetchone()
                total += int(row[0]) if row else 0
            return total
        row = connection.execute(
            "SELECT COUNT(*) FROM threads WHERE model_provider = ?",
            (source_provider,),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()


def state_openai_dirty_count(db_path: Path) -> int:
    return state_provider_dirty_count(db_path, SOURCE_PROVIDER)


def resolve_rollout_path(codex_dir: Path, value: Any) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    path = Path(text)
    return path if path.is_absolute() else codex_dir / path


def repair_state_db_provider_only(
    db_path: Path,
    codex_dir: Path,
    backup_root: Path,
    source_provider: str,
    target_provider: str,
    allowed_thread_ids: set[str] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": relative_to_codex_dir(db_path, codex_dir),
        "source_provider": source_provider,
        "target_provider": target_provider,
        "dirty_rows": 0,
        "rows": 0,
        "thread_ids": [],
        "jsonl_candidates": [],
        "missing_rollout_paths": 0,
    }
    if not db_path.exists():
        entry["skipped"] = "missing"
        return entry

    connection = sqlite3.connect(db_path)
    try:
        columns = table_columns(connection, "threads")
        if "model_provider" not in columns:
            entry["skipped"] = "missing_model_provider"
            return entry

        has_id = "id" in columns
        if allowed_thread_ids is not None and not has_id:
            entry["skipped"] = "missing_id_for_targeted_restore"
            return entry
        has_rollout_path = "rollout_path" in columns
        select_columns = []
        select_columns.append("id" if has_id else "rowid")
        select_columns.append("rollout_path" if has_rollout_path else "NULL")
        rows = connection.execute(
            f"SELECT {', '.join(select_columns)} FROM threads WHERE model_provider = ?",
            (source_provider,),
        ).fetchall()
        if allowed_thread_ids is not None:
            rows = [row for row in rows if str(row[0]) in allowed_thread_ids]
        entry["dirty_rows"] = len(rows)
        if not rows:
            return entry

        thread_ids = [str(row[0]) for row in rows if row[0] is not None]
        entry["thread_ids"] = thread_ids
        candidates: list[dict[str, str]] = []
        missing_rollout_paths = 0
        seen_paths: set[str] = set()
        for thread_id, rollout_path in rows:
            path = resolve_rollout_path(codex_dir, rollout_path)
            if path is None:
                missing_rollout_paths += 1
                continue
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            candidates.append(
                {
                    "path": relative_to_codex_dir(path, codex_dir),
                    "session_id": str(thread_id) if thread_id is not None else "",
                    "source_provider": source_provider,
                    "target_provider": target_provider,
                }
            )
        entry["jsonl_candidates"] = candidates
        entry["missing_rollout_paths"] = missing_rollout_paths

        backup_sqlite(db_path, sqlite_backup_path(db_path, codex_dir, backup_root))
        if has_id and thread_ids:
            updated = 0
            for chunk in chunks(thread_ids):
                placeholders = ",".join("?" for _ in chunk)
                cursor = connection.execute(
                    f"UPDATE threads SET model_provider = ? WHERE model_provider = ? AND id IN ({placeholders})",
                    [target_provider, source_provider, *chunk],
                )
                updated += cursor.rowcount if cursor.rowcount is not None else 0
        else:
            cursor = connection.execute(
                "UPDATE threads SET model_provider = ? WHERE model_provider = ?",
                (target_provider, source_provider),
            )
            updated = cursor.rowcount if cursor.rowcount is not None else 0
        connection.commit()
        entry["rows"] = updated
        return entry
    finally:
        connection.close()


def repair_state_db_to_unified_provider_only(db_path: Path, codex_dir: Path, backup_root: Path) -> dict[str, Any]:
    return repair_state_db_provider_only(
        db_path,
        codex_dir,
        backup_root,
        SOURCE_PROVIDER,
        TARGET_PROVIDER,
    )


def collect_state_provider_mismatch_jsonl_candidates(
    codex_dir: Path,
    db_paths: list[Path],
    expected_provider: str,
    source_provider: str,
    target_provider: str,
    allowed_thread_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for db_path in db_paths:
        if not db_path.exists():
            continue
        connection = sqlite3.connect(db_path)
        try:
            columns = table_columns(connection, "threads")
            if "model_provider" not in columns or "rollout_path" not in columns:
                continue
            has_id = "id" in columns
            select_columns = []
            select_columns.append("id" if has_id else "rowid")
            select_columns.append("rollout_path")
            rows = connection.execute(
                f"""
                SELECT {', '.join(select_columns)} FROM threads
                WHERE model_provider = ?
                  AND rollout_path IS NOT NULL
                  AND rollout_path != ''
                """,
                (expected_provider,),
            ).fetchall()
        finally:
            connection.close()

        for thread_id, rollout_path in rows:
            session_id = str(thread_id) if thread_id is not None else ""
            if allowed_thread_ids is not None and session_id not in allowed_thread_ids:
                continue
            path = resolve_rollout_path(codex_dir, rollout_path)
            if path is None or not path.exists():
                continue
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen_paths:
                continue
            first_line = read_first_line(path)
            _, provider, record = parse_session_meta(first_line)
            if record is None or provider != source_provider:
                continue
            seen_paths.add(key)
            candidates.append(
                {
                    "path": relative_to_codex_dir(path, codex_dir),
                    "session_id": session_id,
                    "source_provider": source_provider,
                    "target_provider": target_provider,
                }
            )
    return candidates


def collect_filesystem_jsonl_candidates(
    codex_dir: Path,
    source_provider: str,
    target_provider: str,
    allowed_session_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for path in collect_jsonl_files(codex_dir):
        first_line = read_first_line(path)
        if not first_line:
            continue
        session_id, provider, record = parse_session_meta(first_line)
        if record is None or provider != source_provider:
            continue
        if allowed_session_ids is not None and (not session_id or session_id not in allowed_session_ids):
            continue
        candidates.append(
            {
                "path": relative_to_codex_dir(path, codex_dir),
                "session_id": session_id or "",
                "source_provider": source_provider,
                "target_provider": target_provider,
            }
        )
    return candidates


def dedupe_jsonl_candidates(candidates: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for candidate in candidates:
        key = str(candidate.get("path", ""))
        if not key or key in seen_paths:
            continue
        seen_paths.add(key)
        output.append(candidate)
    return output


def load_unified_history_ledgers(*roots: Path | None) -> list[dict[str, Any]]:
    paths: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if root is None or not root.exists():
            continue
        candidates: list[Path]
        if root.is_file():
            candidates = [root]
        else:
            candidates = []
            direct = root / "ledger.json"
            if direct.exists():
                candidates.append(direct)
            candidates.extend(root.rglob("ledger.json"))
        for path in candidates:
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)

    ledgers: list[dict[str, Any]] = []
    for path in paths:
        try:
            ledger = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        mode = ledger.get("mode")
        target_provider = ledger.get("target_provider")
        if target_provider == TARGET_PROVIDER or mode in {"ensure-unified", "migrate-official-to-unified"}:
            ledger["_ledger_path"] = str(path)
            ledgers.append(ledger)
    ledgers.sort(key=lambda item: str(item.get("created_at", "")))
    return ledgers


def unified_ledger_session_ids(ledgers: Iterable[dict[str, Any]]) -> set[str]:
    session_ids: set[str] = set()
    for ledger in ledgers:
        for state_entry in ledger.get("state", []):
            for thread_id in state_entry.get("thread_ids", []):
                if thread_id:
                    session_ids.add(str(thread_id))
        for jsonl_entry in ledger.get("jsonl", []):
            session_id = jsonl_entry.get("session_id")
            if session_id:
                session_ids.add(str(session_id))
    return session_ids


def unified_ledger_jsonl_restore_candidates(ledgers: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for ledger in ledgers:
        for entry in ledger.get("jsonl", []):
            if entry.get("status") not in (None, "applied"):
                continue
            path = str(entry.get("path", ""))
            if not path:
                continue
            candidates.append(
                {
                    "path": path,
                    "session_id": str(entry.get("session_id", "")),
                    "source_provider": TARGET_PROVIDER,
                    "target_provider": SOURCE_PROVIDER,
                }
            )
    return dedupe_jsonl_candidates(candidates)


def repair_history_bucket(
    codex_dir: Path,
    backup_root: Path,
    target_provider: str,
    ledger_root: Path | None = None,
) -> dict[str, Any]:
    if target_provider not in (SOURCE_PROVIDER, TARGET_PROVIDER):
        raise ValueError(f"unsupported target provider: {target_provider}")

    started_at = time.perf_counter()
    source_provider = TARGET_PROVIDER if target_provider == SOURCE_PROVIDER else SOURCE_PROVIDER
    db_paths = sqlite_db_paths(codex_dir)
    ledgers: list[dict[str, Any]] = []
    allowed_thread_ids: set[str] | None = None
    ledger_jsonl_candidates: list[dict[str, str]] = []
    if target_provider == SOURCE_PROVIDER:
        ledgers = load_unified_history_ledgers(backup_root, ledger_root)
        allowed_thread_ids = unified_ledger_session_ids(ledgers)
        ledger_jsonl_candidates = unified_ledger_jsonl_restore_candidates(ledgers)
        if not allowed_thread_ids and not ledger_jsonl_candidates:
            return {
                "version": 1,
                "mode": "repair-history",
                "status": "no-ledger",
                "created_at": utc_now_iso(),
                "codex_dir": str(codex_dir),
                "source_provider": source_provider,
                "target_provider": target_provider,
                "dirty_state_rows": 0,
                "state_rows": 0,
                "state_model_rows": 0,
                "state": [],
                "jsonl": [],
                "jsonl_planned": 0,
                "jsonl_applied": 0,
                "jsonl_restored": 0,
                "jsonl_skipped": 0,
                "plan_source": "ledger_missing",
                "timings": {"total_seconds": round(time.perf_counter() - started_at, 3)},
            }

    dirty_state_rows = sum(
        state_provider_dirty_count(path, source_provider, allowed_thread_ids)
        for path in db_paths
    )
    state_scan_done_at = time.perf_counter()

    backup_root.mkdir(parents=True, exist_ok=True)
    ledger_path = backup_root / "ledger.json"
    state_entries = [
        repair_state_db_provider_only(
            path,
            codex_dir,
            backup_root,
            source_provider,
            target_provider,
            allowed_thread_ids,
        )
        for path in db_paths
    ]
    state_done_at = time.perf_counter()

    jsonl_candidates: list[dict[str, str]] = []
    missing_rollout_paths = 0
    for state_entry in state_entries:
        missing_rollout_paths += int(state_entry.get("missing_rollout_paths", 0))
        jsonl_candidates.extend(state_entry.get("jsonl_candidates", []))
    jsonl_candidates.extend(ledger_jsonl_candidates)
    jsonl_candidates.extend(
        collect_state_provider_mismatch_jsonl_candidates(
            codex_dir,
            db_paths,
            target_provider,
            source_provider,
            target_provider,
            allowed_thread_ids,
        )
    )
    if missing_rollout_paths or (dirty_state_rows > 0 and not jsonl_candidates):
        jsonl_candidates.extend(
            collect_filesystem_jsonl_candidates(
                codex_dir,
                source_provider,
                target_provider,
                allowed_thread_ids,
            )
        )
    jsonl_candidates = dedupe_jsonl_candidates(jsonl_candidates)

    jsonl_results = [
        apply_session_file_provider_fast(candidate, codex_dir, backup_root)
        for candidate in jsonl_candidates
    ]
    jsonl_done_at = time.perf_counter()
    jsonl_applied = sum(1 for entry in jsonl_results if entry.get("status") == "applied")
    jsonl_skipped = sum(1 for entry in jsonl_results if entry.get("status") == "skipped")
    state_rows = sum(int(entry.get("rows", 0)) for entry in state_entries)

    if dirty_state_rows == 0 and not jsonl_applied and not jsonl_skipped:
        status = "already-unified" if target_provider == TARGET_PROVIDER else "already-openai"
    else:
        status = "completed-with-skips" if jsonl_skipped else "completed"

    ledger = {
        "version": 1,
        "mode": "repair-history",
        "status": status,
        "created_at": utc_now_iso(),
        "completed_at": utc_now_iso(),
        "codex_dir": str(codex_dir),
        "source_provider": source_provider,
        "target_provider": target_provider,
        "dirty_state_rows": dirty_state_rows,
        "state_rows": state_rows,
        "state_model_rows": 0,
        "state": state_entries,
        "jsonl": jsonl_results,
        "jsonl_planned": len(jsonl_candidates),
        "jsonl_applied": jsonl_applied,
        "jsonl_restored": jsonl_applied if target_provider == SOURCE_PROVIDER else 0,
        "jsonl_skipped": jsonl_skipped,
        "missing_rollout_paths": missing_rollout_paths,
        "ledger_count": len(ledgers),
        "plan_source": "dirty_rollout_path",
        "timings": {
            "dirty_check_seconds": round(state_scan_done_at - started_at, 3),
            "state_seconds": round(state_done_at - state_scan_done_at, 3),
            "jsonl_seconds": round(jsonl_done_at - state_done_at, 3),
            "total_seconds": round(jsonl_done_at - started_at, 3),
        },
    }
    if status not in {"already-unified", "already-openai"}:
        write_json_atomic(ledger_path, ledger)
    return ledger


def ensure_unified_history_bucket(codex_dir: Path, backup_root: Path) -> dict[str, Any]:
    started_at = time.perf_counter()
    db_paths = sqlite_db_paths(codex_dir)
    dirty_state_rows = sum(state_openai_dirty_count(path) for path in db_paths)
    mismatch_candidates = collect_state_provider_mismatch_jsonl_candidates(
        codex_dir,
        db_paths,
        TARGET_PROVIDER,
        SOURCE_PROVIDER,
        TARGET_PROVIDER,
    )
    if dirty_state_rows == 0 and not mismatch_candidates:
        return {
            "version": 1,
            "mode": "ensure-unified",
            "status": "already-unified",
            "created_at": utc_now_iso(),
            "codex_dir": str(codex_dir),
            "dirty_state_rows": 0,
            "state_rows": 0,
            "state_model_rows": 0,
            "state": [],
            "jsonl": [],
            "jsonl_planned": 0,
            "jsonl_applied": 0,
            "jsonl_skipped": 0,
            "plan_source": "dirty_check",
            "timings": {"total_seconds": round(time.perf_counter() - started_at, 3)},
        }
    result = repair_history_bucket(codex_dir, backup_root, TARGET_PROVIDER)
    result["mode"] = "ensure-unified"
    result["status"] = "already-unified" if result.get("status") == "already-unified" else result.get("status", "completed")
    if result.get("status") != "already-unified":
        ledger_path = backup_root / "ledger.json"
        write_json_atomic(ledger_path, result)
    return result


def migrate_official_history_to_unified(codex_dir: Path, backup_root: Path) -> dict[str, Any]:
    if not live_config_routes_custom(codex_dir):
        return {
            "version": 1,
            "mode": "migrate-official-to-unified",
            "status": "skipped",
            "reason": "live_not_unified",
            "created_at": utc_now_iso(),
            "codex_dir": str(codex_dir),
            "state_rows": 0,
            "state_model_rows": 0,
            "jsonl_applied": 0,
            "jsonl_skipped": 0,
        }
    result = repair_history_bucket(codex_dir, backup_root, TARGET_PROVIDER)
    result["mode"] = "migrate-official-to-unified"
    if result.get("status") == "already-unified":
        return result
    ledger_path = backup_root / "ledger.json"
    write_json_atomic(ledger_path, result)
    return result


def restore_official_history_from_unified(
    codex_dir: Path,
    backup_root: Path,
    ledger_root: Path | None = None,
) -> dict[str, Any]:
    result = repair_history_bucket(codex_dir, backup_root, SOURCE_PROVIDER, ledger_root or backup_root)
    result["mode"] = "restore-official-from-unified"
    if result.get("status") not in {"already-openai", "no-ledger"}:
        ledger_path = backup_root / "ledger.json"
        write_json_atomic(ledger_path, result)
    return result


def normalize_state_provider_fast(db_path: Path, codex_dir: Path, backup_root: Path, target_provider: str) -> dict[str, int]:
    if not db_path.exists():
        return {"provider_rows": 0, "model_rows": 0}
    if target_provider not in (SOURCE_PROVIDER, TARGET_PROVIDER):
        raise ValueError(f"unsupported target provider: {target_provider}")

    source_provider = TARGET_PROVIDER if target_provider == SOURCE_PROVIDER else SOURCE_PROVIDER
    backup_sqlite(db_path, sqlite_backup_path(db_path, codex_dir, backup_root))
    connection = sqlite3.connect(db_path)
    try:
        columns = table_columns(connection, "threads")
        if "model_provider" not in columns:
            return {"provider_rows": 0, "model_rows": 0}
        cursor = connection.execute(
            "UPDATE threads SET model_provider = ? WHERE model_provider = ?",
            (target_provider, source_provider),
        )
        model_rows = 0
        connection.commit()
        return {
            "provider_rows": cursor.rowcount if cursor.rowcount is not None else 0,
            "model_rows": model_rows,
        }
    finally:
        connection.close()


def plan_session_file_provider_fast(path: Path, codex_dir: Path, target_provider: str) -> dict[str, str] | None:
    first_line = read_first_line(path)
    if not first_line:
        return None

    session_id, provider, record = parse_session_meta(first_line)
    if record is None or provider not in (SOURCE_PROVIDER, TARGET_PROVIDER) or provider == target_provider:
        return None

    new_first_line = rewritten_provider_line(first_line, record, provider, target_provider)
    if len(first_line.encode("utf-8")) != len(new_first_line.encode("utf-8")):
        raise ValueError(f"rewritten session_meta line changed byte length: {path}")
    return {
        "path": relative_to_codex_dir(path, codex_dir),
        "session_id": session_id or "",
        "source_provider": provider,
        "target_provider": target_provider,
        "old_first_line": first_line,
        "new_first_line": new_first_line,
    }


def apply_session_file_provider_fast(
    entry: dict[str, str],
    codex_dir: Path,
    backup_root: Path | None = None,
) -> dict[str, str]:
    path = codex_dir / entry["path"]
    target_provider = entry["target_provider"]
    source_provider = entry["source_provider"]

    for _attempt in range(3):
        first_line = read_first_line(path)
        if not first_line:
            return skipped_session_file_provider_fast(entry, "missing")

        session_id, provider, record = parse_session_meta(first_line)
        if record is None:
            return skipped_session_file_provider_fast(entry, "invalid_session_meta", first_line)
        if provider == target_provider:
            return skipped_session_file_provider_fast(entry, "already_target", first_line)
        if provider != source_provider:
            return skipped_session_file_provider_fast(entry, "provider_changed", first_line)

        new_first_line = rewritten_provider_line(first_line, record, provider, target_provider)
        if len(first_line.encode("utf-8")) != len(new_first_line.encode("utf-8")):
            return skipped_session_file_provider_fast(entry, "byte_length_changed", first_line)

        applied = dict(entry)
        applied.update(
            {
                "status": "applied",
                "session_id": session_id or entry.get("session_id", ""),
                "old_first_line": first_line,
                "new_first_line": new_first_line,
            }
        )
        try:
            if backup_root is not None:
                backup_file(path, codex_dir, backup_root, "jsonl")
            write_first_line_in_place(path, first_line, new_first_line)
            return applied
        except FileNotFoundError:
            return skipped_session_file_provider_fast(entry, "missing")
        except ValueError as error:
            if "session_meta line changed while rewriting" not in str(error):
                raise
            time.sleep(0.01)

    return skipped_session_file_provider_fast(entry, "changed_while_rewriting")


def rollback_session_file_provider_fast(entry: dict[str, str], codex_dir: Path) -> None:
    path = codex_dir / entry["path"]
    first_line = read_first_line(path)
    if first_line != entry["new_first_line"]:
        return
    write_first_line_in_place(path, entry["new_first_line"], entry["old_first_line"])


def restore_fast_state_backups(codex_dir: Path, backup_root: Path) -> int:
    restored = 0
    for path in sqlite_db_paths(codex_dir):
        backup = sqlite_backup_path(path, codex_dir, backup_root)
        if backup.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, path)
            restored += 1
    return restored


def normalize_history_provider_fast(codex_dir: Path, backup_root: Path, target_provider: str) -> dict[str, Any]:
    if target_provider not in (SOURCE_PROVIDER, TARGET_PROVIDER):
        raise ValueError(f"unsupported target provider: {target_provider}")

    backup_root.mkdir(parents=True, exist_ok=True)
    ledger_path = backup_root / "ledger.json"
    started_at = time.perf_counter()
    session_files, plan_source = collect_normalization_session_files(codex_dir)
    jsonl_entries = [
        entry
        for path in session_files
        if (entry := plan_session_file_provider_fast(path, codex_dir, target_provider)) is not None
    ]
    ledger = {
        "version": 2,
        "mode": "normalize-fast",
        "status": "prepared",
        "created_at": utc_now_iso(),
        "codex_dir": str(codex_dir),
        "target_provider": target_provider,
        "state_rows": None,
        "state_model_rows": None,
        "jsonl": jsonl_entries,
        "plan_source": plan_source,
        "planned_session_files": len(session_files),
        "timings": {
            "plan_seconds": round(time.perf_counter() - started_at, 3),
        },
    }
    write_json_atomic(ledger_path, ledger)

    written_entries: list[dict[str, str]] = []
    jsonl_results: list[dict[str, str]] = []
    try:
        state_start_at = time.perf_counter()
        state_results = [
            normalize_state_provider_fast(path, codex_dir, backup_root, target_provider)
            for path in sqlite_db_paths(codex_dir)
        ]
        state_rows = sum(result["provider_rows"] for result in state_results)
        state_model_rows = sum(result["model_rows"] for result in state_results)
        state_done_at = time.perf_counter()
        for entry in jsonl_entries:
            result = apply_session_file_provider_fast(entry, codex_dir)
            jsonl_results.append(result)
            if result.get("status") == "applied":
                written_entries.append(result)
        jsonl_done_at = time.perf_counter()
    except Exception:
        for entry in reversed(written_entries):
            try:
                rollback_session_file_provider_fast(entry, codex_dir)
            except Exception:
                pass
        restored_states = restore_fast_state_backups(codex_dir, backup_root)
        ledger["status"] = "rolled-back-after-error"
        ledger["rolled_back_jsonl"] = len(written_entries)
        ledger["restored_state_backups"] = restored_states
        ledger["failed_at"] = utc_now_iso()
        write_json_atomic(ledger_path, ledger)
        raise

    jsonl_applied = sum(1 for entry in jsonl_results if entry.get("status") == "applied")
    jsonl_skipped = sum(1 for entry in jsonl_results if entry.get("status") == "skipped")
    ledger["status"] = "completed-with-skips" if jsonl_skipped else "completed"
    ledger["completed_at"] = utc_now_iso()
    ledger["state_rows"] = state_rows
    ledger["state_model_rows"] = state_model_rows
    ledger["jsonl"] = jsonl_results
    ledger["jsonl_applied"] = jsonl_applied
    ledger["jsonl_skipped"] = jsonl_skipped
    ledger["timings"] = {
        **ledger["timings"],
        "state_seconds": round(state_done_at - state_start_at, 3),
        "jsonl_seconds": round(jsonl_done_at - state_done_at, 3),
        "total_seconds": round(jsonl_done_at - started_at, 3),
    }
    write_json_atomic(ledger_path, ledger)
    return ledger


def promote_custom_history_to_openai(codex_dir: Path, backup_root: Path) -> dict[str, Any]:
    return restore_official_history_from_unified(codex_dir, backup_root, backup_root.parent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Temporarily merge Codex openai history into the custom provider bucket.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--codex-dir", type=Path, default=default_codex_dir())
    apply_parser.add_argument("--backup-root", required=True, type=Path)
    apply_parser.add_argument("--ledger", required=True, type=Path)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--ledger", required=True, type=Path)

    promote_parser = subparsers.add_parser("promote-custom-to-openai")
    promote_parser.add_argument("--codex-dir", type=Path, default=default_codex_dir())
    promote_parser.add_argument("--backup-root", required=True, type=Path)

    ensure_unified_parser = subparsers.add_parser("ensure-unified")
    ensure_unified_parser.add_argument("--codex-dir", type=Path, default=default_codex_dir())
    ensure_unified_parser.add_argument("--backup-root", required=True, type=Path)

    repair_parser = subparsers.add_parser("repair-history")
    repair_parser.add_argument("--codex-dir", type=Path, default=default_codex_dir())
    repair_parser.add_argument("--backup-root", required=True, type=Path)
    repair_parser.add_argument("--target", required=True, choices=(SOURCE_PROVIDER, TARGET_PROVIDER))
    repair_parser.add_argument("--ledger-root", type=Path)

    migrate_parser = subparsers.add_parser("migrate-official-to-unified")
    migrate_parser.add_argument("--codex-dir", type=Path, default=default_codex_dir())
    migrate_parser.add_argument("--backup-root", required=True, type=Path)

    restore_official_parser = subparsers.add_parser("restore-official-from-unified")
    restore_official_parser.add_argument("--codex-dir", type=Path, default=default_codex_dir())
    restore_official_parser.add_argument("--backup-root", required=True, type=Path)
    restore_official_parser.add_argument("--ledger-root", type=Path)

    fast_parser = subparsers.add_parser("normalize-fast")
    fast_parser.add_argument("--codex-dir", type=Path, default=default_codex_dir())
    fast_parser.add_argument("--backup-root", required=True, type=Path)
    fast_parser.add_argument("--target", required=True, choices=(SOURCE_PROVIDER, TARGET_PROVIDER))

    args = parser.parse_args(argv)
    if args.command == "apply":
        ledger = apply_history_overlay(args.codex_dir, args.backup_root, args.ledger)
        state_rows = sum(len(entry.get("thread_ids", [])) for entry in ledger.get("state", []))
        print(f"state_rows={state_rows}")
        print(f"jsonl_files={len(ledger.get('jsonl', []))}")
        for key, value in ledger.get("timings", {}).items():
            print(f"{key}={value}")
    elif args.command == "restore":
        result = restore_history_overlay(args.ledger)
        print(f"state_rows={result['state_rows']}")
        print(f"jsonl_files={result['jsonl_files']}")
        print(f"new_state_rows={result['new_state_rows']}")
        print(f"new_jsonl_files={result['new_jsonl_files']}")
        for key, value in result.get("timings", {}).items():
            print(f"{key}={value}")
    elif args.command == "promote-custom-to-openai":
        result = promote_custom_history_to_openai(args.codex_dir, args.backup_root)
        print(f"status={result.get('status', 'completed')}")
        print(f"state_rows={result['state_rows']}")
        print(f"state_model_rows={result.get('state_model_rows', 0)}")
        print(f"jsonl_restored={result.get('jsonl_restored', 0)}")
        for key, value in result.get("timings", {}).items():
            print(f"{key}={value}")
    elif args.command == "ensure-unified":
        result = ensure_unified_history_bucket(args.codex_dir, args.backup_root)
        print(f"status={result.get('status', 'completed')}")
        print(f"dirty_state_rows={result.get('dirty_state_rows', 0)}")
        print(f"state_rows={result.get('state_rows', 0)}")
        print(f"state_model_rows={result.get('state_model_rows', 0)}")
        print(f"jsonl_planned={result.get('jsonl_planned', 0)}")
        print(f"jsonl_applied={result.get('jsonl_applied', 0)}")
        print(f"jsonl_skipped={result.get('jsonl_skipped', 0)}")
        for key, value in result.get("timings", {}).items():
            print(f"{key}={value}")
    elif args.command == "repair-history":
        result = repair_history_bucket(args.codex_dir, args.backup_root, args.target, args.ledger_root)
        print(f"status={result.get('status', 'completed')}")
        print(f"target_provider={result.get('target_provider', args.target)}")
        print(f"dirty_state_rows={result.get('dirty_state_rows', 0)}")
        print(f"state_rows={result.get('state_rows', 0)}")
        print(f"state_model_rows={result.get('state_model_rows', 0)}")
        print(f"jsonl_planned={result.get('jsonl_planned', 0)}")
        print(f"jsonl_applied={result.get('jsonl_applied', 0)}")
        print(f"jsonl_restored={result.get('jsonl_restored', 0)}")
        print(f"jsonl_skipped={result.get('jsonl_skipped', 0)}")
        for key, value in result.get("timings", {}).items():
            print(f"{key}={value}")
    elif args.command == "migrate-official-to-unified":
        result = migrate_official_history_to_unified(args.codex_dir, args.backup_root)
        print(f"status={result.get('status', 'completed')}")
        if result.get("reason"):
            print(f"reason={result.get('reason')}")
        print(f"state_rows={result.get('state_rows', 0)}")
        print(f"state_model_rows={result.get('state_model_rows', 0)}")
        print(f"jsonl_applied={result.get('jsonl_applied', 0)}")
        print(f"jsonl_skipped={result.get('jsonl_skipped', 0)}")
    elif args.command == "restore-official-from-unified":
        result = restore_official_history_from_unified(args.codex_dir, args.backup_root, args.ledger_root)
        print(f"status={result.get('status', 'completed')}")
        print(f"state_rows={result.get('state_rows', 0)}")
        print(f"state_model_rows={result.get('state_model_rows', 0)}")
        print(f"jsonl_restored={result.get('jsonl_restored', 0)}")
        print(f"jsonl_skipped={result.get('jsonl_skipped', 0)}")
    elif args.command == "normalize-fast":
        result = normalize_history_provider_fast(args.codex_dir, args.backup_root, args.target)
        print(f"status={result.get('status', 'completed')}")
        print(f"state_rows={result['state_rows']}")
        print(f"state_model_rows={result.get('state_model_rows', 0)}")
        print(f"jsonl_files={len(result.get('jsonl', []))}")
        print(f"jsonl_applied={result.get('jsonl_applied', len(result.get('jsonl', [])))}")
        print(f"jsonl_skipped={result.get('jsonl_skipped', 0)}")
        for key, value in result.get("timings", {}).items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
