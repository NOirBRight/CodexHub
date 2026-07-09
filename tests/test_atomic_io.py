import os
import stat
import threading
from pathlib import Path

import pytest

from atomic_io import atomic_read_or_create_text, atomic_write_text


def test_atomic_write_text_replaces_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("old", encoding="utf-8")

    atomic_write_text(target, "new", encoding="utf-8")

    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_text_keeps_existing_file_when_replace_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "providers.toml"
    target.write_text("old", encoding="utf-8")

    def fail_replace(source: str | bytes | os.PathLike[str] | os.PathLike[bytes], destination: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        atomic_write_text(target, "new", encoding="utf-8")

    assert target.read_text(encoding="utf-8") == "old"


def test_atomic_write_text_recovers_stale_lock_file(tmp_path: Path) -> None:
    target = tmp_path / "catalog.json"
    target.write_text("old", encoding="utf-8")
    target.with_name("catalog.json.lock").write_text("pid=0\nacquired_at_millis=0\n", encoding="utf-8")

    atomic_write_text(target, "new", encoding="utf-8")

    assert target.read_text(encoding="utf-8") == "new"
    assert not target.with_name("catalog.json.lock").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows private ACLs are documented separately")
def test_atomic_write_text_can_apply_private_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "auth.json"

    atomic_write_text(target, "secret\n", encoding="utf-8", mode=0o600)

    assert target.read_text(encoding="utf-8") == "secret\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_read_or_create_text_returns_single_winning_value_under_concurrency(tmp_path: Path) -> None:
    target = tmp_path / "telemetry-secret"
    barrier = threading.Barrier(8)
    counter = 0
    counter_lock = threading.Lock()
    results: list[str] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def create_secret() -> str:
        nonlocal counter
        with counter_lock:
            counter += 1
            value = f"secret-{counter}"
        return value

    def worker() -> None:
        try:
            barrier.wait()
            value = atomic_read_or_create_text(target, create_secret, encoding="utf-8", mode=0o600)
            with results_lock:
                results.append(value)
        except BaseException as error:
            with results_lock:
                errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 8
    assert len(set(results)) == 1
    assert target.read_text(encoding="utf-8") == results[0]
    assert counter == 1
