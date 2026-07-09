from __future__ import annotations

import contextlib
import errno
import os
import time
import uuid
from pathlib import Path
from typing import Iterator

LOCK_WAIT_TIMEOUT_SECONDS = 10.0
LOCK_RETRY_DELAY_SECONDS = 0.025
LOCK_STALE_AFTER_SECONDS = 60.0


@contextlib.contextmanager
def file_lock_for(path: Path) -> Iterator[None]:
    """Acquire a simple cross-process lock file for a target path."""
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            if _lock_is_stale(lock_path):
                with contextlib.suppress(OSError):
                    lock_path.unlink()
                continue
            if time.monotonic() - started >= LOCK_WAIT_TIMEOUT_SECONDS:
                raise TimeoutError(f"timed out waiting for config lock {lock_path}")
            time.sleep(LOCK_RETRY_DELAY_SECONDS)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                if _lock_is_stale(lock_path):
                    with contextlib.suppress(OSError):
                        lock_path.unlink()
                    continue
                if time.monotonic() - started >= LOCK_WAIT_TIMEOUT_SECONDS:
                    raise TimeoutError(f"timed out waiting for config lock {lock_path}") from exc
                time.sleep(LOCK_RETRY_DELAY_SECONDS)
                continue
            raise
    try:
        os.write(fd, f"pid={os.getpid()}\nacquired_at_millis={_current_millis()}\n".encode("ascii"))
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            lock_path.unlink()


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock_for(path):
        temp_path = _unique_temp_path(path)
        try:
            with temp_path.open("wb") as temp_file:
                temp_file.write(data)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, path)
            _fsync_directory_best_effort(path.parent)
        except Exception:
            with contextlib.suppress(OSError):
                temp_path.unlink()
            raise


def _unique_temp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def _fsync_directory_best_effort(path: Path) -> None:
    if os.name == "nt":
        return
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _current_millis() -> int:
    return int(time.time() * 1000)


def _lock_is_stale(lock_path: Path) -> bool:
    try:
        text = lock_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    acquired_at = _parse_lock_acquired_at_millis(text)
    if acquired_at is None:
        return True
    age_seconds = max(0.0, (_current_millis() - acquired_at) / 1000)
    return age_seconds >= LOCK_STALE_AFTER_SECONDS


def _parse_lock_acquired_at_millis(text: str) -> int | None:
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "acquired_at_millis":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None
