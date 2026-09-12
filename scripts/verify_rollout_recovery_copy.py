"""Opt-in, copy-only verification of one explicitly identified damaged rollout.

The original rollout and SQLite database are opened read-only. The database
backup is evidence only and is never installed into a running client's home.
No missing events or terminal records are invented.
"""
from __future__ import annotations

from python_runtime_contract import require_python_313
require_python_313(__file__)

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _projection(connection, thread_id):
    return connection.execute(
        "SELECT next_rollout_byte_offset, next_rollout_ordinal "
        "FROM thread_history_projection_state WHERE thread_id = ?", (thread_id,),
    ).fetchone()


def recover_copy(source: Path, database: Path, output: Path, *, thread_id: str,
                 bad_line: int, bad_offset: int) -> dict:
    source, database, output = source.resolve(), database.resolve(), output.resolve()
    if output.exists() or source.parent == output or source.parent in output.parents or database.parent in output.parents:
        raise ValueError("output_must_be_a_new_isolated_directory")
    before = digest(source)
    output.mkdir(mode=0o700, parents=True)
    evidence = output / "evidence-only"
    evidence.mkdir(mode=0o700)
    original = evidence / source.name
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as origin:
        origin.execute("BEGIN")
        projection = _projection(origin, thread_id)
        with sqlite3.connect(evidence / "history.sqlite") as backup:
            origin.backup(backup)
        shutil.copyfile(source, original)
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as current:
        stable_projection = _projection(current, thread_id) == projection
    if digest(original) != before or digest(source) != before or not stable_projection:
        raise ValueError("source_changed_during_snapshot")
    if projection != (bad_offset, bad_line - 1):
        raise ValueError("projection_does_not_match_damage")
    recovered = output / "recovered" / source.name
    recovered.parent.mkdir(mode=0o700)
    bad, offset, count, valid_digest = [], 0, 0, hashlib.sha256()
    starts, terminals = [], {}
    with original.open("rb") as reader, recovered.open("xb") as writer:
        for number, raw in enumerate(reader, 1):
            try:
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise ValueError("non_object_record")
            except (ValueError, UnicodeError):
                if number != bad_line or offset != bad_offset or bad:
                    raise ValueError("unexpected_additional_damage") from None
                (evidence / "quarantined-line.bin").write_bytes(raw)
                bad.append({"line": number, "offset": offset, "bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest()})
            else:
                writer.write(raw)
                valid_digest.update(raw)
                count += 1
                payload = row.get("payload", {})
                if row.get("type") == "event_msg":
                    kind, identity = payload.get("type"), payload.get("turn_id")
                    if kind == "task_started":
                        starts.append(identity)
                    elif kind in {"task_complete", "turn_aborted"}:
                        terminals[identity] = kind
            offset += len(raw)
    if len(bad) != 1 or digest(recovered) != valid_digest.hexdigest() or digest(source) != before:
        raise ValueError("copy_integrity_failed")
    for path in output.rglob("*"):
        if path.is_file():
            path.chmod(0o600)
    report = {
        "schema_version": 1, "status": "passed", "source_unchanged": True,
        "source_sha256": before, "database_copy_sha256": digest(evidence / "history.sqlite"),
        "recovered_sha256": digest(recovered), "valid_records_preserved": count,
        "quarantine": bad, "valid_record_order_and_bytes_preserved": True,
        "turn_count": len(starts), "unclosed_turns": [identity for identity in starts if identity not in terminals],
        "projection_before": {"offset": projection[0], "ordinal": projection[1]},
        "client_readback": "unverified", "desktop_projection": "unverified",
        "production_history_modified": False,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollout", type=Path, required=True)
    parser.add_argument("--history-db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--bad-line", type=int, required=True)
    parser.add_argument("--bad-offset", type=int, required=True)
    args = parser.parse_args()
    report = recover_copy(args.rollout, args.history_db, args.output_dir,
                          thread_id=args.thread_id, bad_line=args.bad_line, bad_offset=args.bad_offset)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
