import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


def runner():
    spec = importlib.util.spec_from_file_location("recovery_copy", Path(__file__).resolve().parents[1] / "scripts/verify_rollout_recovery_copy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(tmp_path, extra_damage=False):
    source = tmp_path / "source"
    source.mkdir()
    valid = json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "unfinished"}}).encode() + b"\n"
    rollout = source / "rollout.jsonl"
    rollout.write_bytes(valid + b"\0\0\n" + valid + (b"broken\n" if extra_damage else b""))
    db = source / "history.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE thread_history_projection_state(thread_id TEXT, next_rollout_byte_offset INTEGER, next_rollout_ordinal INTEGER)")
        connection.executemany("INSERT INTO thread_history_projection_state VALUES(?,?,?)", [("target", len(valid), 1), ("unrelated", 99, 4)])
    return rollout, db, valid


def test_recovery_preserves_valid_bytes_and_never_fabricates_terminal(tmp_path):
    module = runner()
    rollout, db, valid = fixture(tmp_path)
    original, database = rollout.read_bytes(), db.read_bytes()
    output = tmp_path / "copy"
    result = module.recover_copy(rollout, db, output, thread_id="target", bad_line=2, bad_offset=len(valid))
    assert rollout.read_bytes() == original and db.read_bytes() == database
    assert (output / "recovered/rollout.jsonl").read_bytes() == valid + valid
    assert (output / "evidence-only/quarantined-line.bin").read_bytes() == b"\0\0\n"
    assert result["unclosed_turns"] == ["unfinished", "unfinished"]
    assert result["client_readback"] == result["desktop_projection"] == "unverified"
    with sqlite3.connect(output / "evidence-only/history.sqlite") as connection:
        assert connection.execute("SELECT * FROM thread_history_projection_state WHERE thread_id='unrelated'").fetchone() == ("unrelated", 99, 4)


def test_unexpected_damage_fails_closed(tmp_path):
    module = runner()
    rollout, db, valid = fixture(tmp_path, extra_damage=True)
    original = rollout.read_bytes()
    with pytest.raises(ValueError, match="unexpected_additional_damage"):
        module.recover_copy(rollout, db, tmp_path / "copy", thread_id="target", bad_line=2, bad_offset=len(valid))
    assert rollout.read_bytes() == original
    assert not (tmp_path / "copy/report.json").exists()


def test_output_cannot_be_inside_source_home(tmp_path):
    module = runner()
    rollout, db, valid = fixture(tmp_path)
    with pytest.raises(ValueError, match="isolated_directory"):
        module.recover_copy(rollout, db, rollout.parent / "copy", thread_id="target", bad_line=2, bad_offset=len(valid))
