from __future__ import annotations

import argparse
from datetime import datetime, timezone
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


SOURCE_PROVIDER = "openai"
TARGET_PROVIDER = "custom"
STATE_DB_FILENAME = "state_5.sqlite"
SESSION_DIR_NAMES = ("sessions", "archived_sessions")
SQLITE_ID_CHUNK = 500
OFFICIAL_ENCRYPTED_CONTENT_PREFIX = "gAAAA"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def iso_to_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def default_codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


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


def chunks(values: list[str], size: int = SQLITE_ID_CHUNK) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def migrate_state_db(db_path: Path, codex_dir: Path, backup_root: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"path": str(db_path), "thread_ids": [], "target_thread_ids": [], "skipped": "missing"}

    backup_sqlite(db_path, backup_root / "state" / db_path.name)
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


def restore_new_state_rows(entry: dict[str, Any]) -> int:
    db_path = Path(str(entry.get("path", "")))
    if not db_path.exists():
        return 0

    preexisting_target_ids = {str(value) for value in entry.get("target_thread_ids", []) if value}
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT id FROM threads WHERE model_provider = ?",
            (TARGET_PROVIDER,),
        ).fetchall()
        new_thread_ids = [str(row[0]) for row in rows if str(row[0]) not in preexisting_target_ids]
        restored = 0
        for group in chunks(new_thread_ids):
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
    if state_files:
        return state_files, "state_rollout_path"
    return collect_jsonl_files(codex_dir), "filesystem_scan"


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
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
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
    new_state_rows = sum(restore_new_state_rows(entry) for entry in ledger.get("state", []))
    new_state_restore_done_at = time.perf_counter()

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
                sanitize_for_official=True,
            ):
                jsonl_files += 1
    ledger_jsonl_done_at = time.perf_counter()

    preexisting_target_session_ids = {
        str(entry.get("session_id"))
        for entry in ledger.get("preexisting_target_jsonl", [])
        if entry.get("session_id")
    }
    preexisting_target_paths = {
        str(entry.get("path"))
        for entry in ledger.get("preexisting_target_jsonl", [])
        if entry.get("path")
    }
    new_jsonl_files = 0
    created_at_timestamp = iso_to_timestamp(ledger.get("created_at"))
    modified_after = created_at_timestamp - 1 if created_at_timestamp is not None else None
    for path in collect_jsonl_files(codex_dir, modified_after=modified_after):
        first_line = read_first_line(path)
        if not first_line:
            continue
        session_id, provider, record = parse_session_meta(first_line)
        relative_path = relative_to_codex_dir(path, codex_dir)
        if record is None or provider != TARGET_PROVIDER:
            continue
        if (session_id and session_id in preexisting_target_session_ids) or relative_path in preexisting_target_paths:
            continue
        if rewrite_session_file(path, codex_dir, backup_root, TARGET_PROVIDER, SOURCE_PROVIDER, sanitize_for_official=True):
            new_jsonl_files += 1
    new_jsonl_done_at = time.perf_counter()

    return {
        "state_rows": state_rows,
        "jsonl_files": jsonl_files,
        "new_state_rows": new_state_rows,
        "new_jsonl_files": new_jsonl_files,
        "timings": {
            "state_restore_seconds": round(state_restore_done_at - started_at, 3),
            "new_state_restore_seconds": round(new_state_restore_done_at - state_restore_done_at, 3),
            "ledger_jsonl_restore_seconds": round(ledger_jsonl_done_at - new_state_restore_done_at, 3),
            "new_jsonl_scan_restore_seconds": round(new_jsonl_done_at - ledger_jsonl_done_at, 3),
            "total_restore_seconds": round(new_jsonl_done_at - started_at, 3),
        },
    }


def convert_state_provider(db_path: Path, backup_root: Path, source_provider: str, target_provider: str) -> int:
    if not db_path.exists():
        return 0

    backup_sqlite(db_path, backup_root / "state" / db_path.name)
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


def normalize_state_provider_fast(db_path: Path, backup_root: Path, target_provider: str) -> int:
    if not db_path.exists():
        return 0
    if target_provider not in (SOURCE_PROVIDER, TARGET_PROVIDER):
        raise ValueError(f"unsupported target provider: {target_provider}")

    source_provider = TARGET_PROVIDER if target_provider == SOURCE_PROVIDER else SOURCE_PROVIDER
    backup_sqlite(db_path, backup_root / "state" / db_path.name)
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


def apply_session_file_provider_fast(entry: dict[str, str], codex_dir: Path) -> None:
    path = codex_dir / entry["path"]
    write_first_line_in_place(path, entry["old_first_line"], entry["new_first_line"])


def rollback_session_file_provider_fast(entry: dict[str, str], codex_dir: Path) -> None:
    path = codex_dir / entry["path"]
    write_first_line_in_place(path, entry["new_first_line"], entry["old_first_line"])


def restore_fast_state_backups(codex_dir: Path, backup_root: Path) -> int:
    restored = 0
    for path in sqlite_db_paths(codex_dir):
        backup = backup_root / "state" / path.name
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
        "jsonl": jsonl_entries,
        "plan_source": plan_source,
        "planned_session_files": len(session_files),
        "timings": {
            "plan_seconds": round(time.perf_counter() - started_at, 3),
        },
    }
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    written_entries: list[dict[str, str]] = []
    try:
        state_start_at = time.perf_counter()
        state_rows = sum(
            normalize_state_provider_fast(path, backup_root, target_provider)
            for path in sqlite_db_paths(codex_dir)
        )
        state_done_at = time.perf_counter()
        for entry in jsonl_entries:
            apply_session_file_provider_fast(entry, codex_dir)
            written_entries.append(entry)
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
        ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        raise

    ledger["status"] = "completed"
    ledger["completed_at"] = utc_now_iso()
    ledger["state_rows"] = state_rows
    ledger["timings"] = {
        **ledger["timings"],
        "state_seconds": round(state_done_at - state_start_at, 3),
        "jsonl_seconds": round(jsonl_done_at - state_done_at, 3),
        "total_seconds": round(jsonl_done_at - started_at, 3),
    }
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return ledger


def promote_custom_history_to_openai(codex_dir: Path, backup_root: Path) -> dict[str, Any]:
    backup_root.mkdir(parents=True, exist_ok=True)
    started_at = time.perf_counter()
    state_rows = sum(
        convert_state_provider(path, backup_root, TARGET_PROVIDER, SOURCE_PROVIDER)
        for path in sqlite_db_paths(codex_dir)
    )
    state_done_at = time.perf_counter()
    jsonl_files = 0
    for path in collect_jsonl_files(codex_dir):
        if rewrite_session_file(
            path,
            codex_dir,
            backup_root,
            TARGET_PROVIDER,
            SOURCE_PROVIDER,
            sanitize_for_official=True,
        ):
            jsonl_files += 1
    jsonl_done_at = time.perf_counter()
    return {
        "state_rows": state_rows,
        "jsonl_files": jsonl_files,
        "timings": {
            "state_seconds": round(state_done_at - started_at, 3),
            "jsonl_seconds": round(jsonl_done_at - state_done_at, 3),
            "total_seconds": round(jsonl_done_at - started_at, 3),
        },
    }


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
        print(f"state_rows={result['state_rows']}")
        print(f"jsonl_files={result['jsonl_files']}")
        for key, value in result.get("timings", {}).items():
            print(f"{key}={value}")
    elif args.command == "normalize-fast":
        result = normalize_history_provider_fast(args.codex_dir, args.backup_root, args.target)
        print(f"state_rows={result['state_rows']}")
        print(f"jsonl_files={len(result.get('jsonl', []))}")
        for key, value in result.get("timings", {}).items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
