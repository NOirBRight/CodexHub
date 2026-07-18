from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator

LOCK_WAIT_TIMEOUT_SECONDS = 10.0
LOCK_RETRY_DELAY_SECONDS = 0.025
# Versioned lock record. Anything else is fail-closed:
# - unknown/future versions -> never recovered (fail closed);
# - legacy pid/timestamp records -> recovered only when the PID is provably
#   dead, otherwise fail closed;
# - mixed-version caveat: binaries older than this protocol may still reclaim
#   or unlink a protocol lock file (they classify anything non-legacy as
#   stale); old binaries cannot be patched, so upgrades must drain running
#   old processes before relying on overlap protection.
# Crash-recovery bound: a holder death releases its OS byte lock, so a new
# owner enters within LOCK_WAIT_TIMEOUT_SECONDS.
LOCK_PROTOCOL = "codexhub-atomic-lock=1\n"
# Win32 FILE_ATTRIBUTE_REPARSE_POINT; read via getattr so it stays zero on
# non-Windows platforms where the attribute never appears.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_TEST_LOCK_HOOK: Callable[[str], None] | None = None


def _set_test_lock_hook(hook: Callable[[str], None] | None) -> None:
    global _TEST_LOCK_HOOK
    _TEST_LOCK_HOOK = hook


def _notify_test_lock(event: str) -> None:
    if _TEST_LOCK_HOOK is not None:
        _TEST_LOCK_HOOK(event)


@contextlib.contextmanager
def file_lock_for(path: Path) -> Iterator[Callable[[], None]]:
    """Hold the cross-language advisory lock and its namespace guard."""
    lock_path = path.with_name(f"{path.name}.lock")
    namespace_path = lock_path.with_name(f"{lock_path.name}.guard")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    namespace_fd = _acquire_namespace_guard(lock_path, started)

    def verify_namespace() -> None:
        _verify_lock_identity(namespace_path, namespace_fd)

    try:
        while True:
            if time.monotonic() - started >= LOCK_WAIT_TIMEOUT_SECONDS:
                raise TimeoutError("timed out waiting for atomic write lock")
            try:
                fd, created = _open_lock_file(lock_path)
            except (FileExistsError, FileNotFoundError):
                _retry_after_failure(started)
                continue
            except OSError as exc:
                raise OSError("failed to open atomic write lock") from exc

            acquired = False
            retry = False
            try:
                acquired = _try_lock_exclusive(fd)
                if acquired:
                    _verify_lock_identity(lock_path, fd)
                    state = _read_lock_state(fd)
                    if created:
                        if state != "empty":
                            raise TimeoutError("atomic write lock is unavailable")
                        _write_protocol(fd)
                    elif state == "protocol" or state == "dead-legacy":
                        _write_protocol(fd)
                    elif state == "empty":
                        retry = True
                    else:
                        raise TimeoutError("atomic write lock is unavailable")
                    if not retry:
                        verify_namespace()
                        _verify_lock_identity(lock_path, fd)
                        yield verify_namespace
                        verify_namespace()
                        return
            finally:
                try:
                    if acquired:
                        _unlock(fd)
                finally:
                    os.close(fd)

            _retry_after_failure(started)
    finally:
        try:
            _unlock(namespace_fd)
        finally:
            os.close(namespace_fd)


def _acquire_namespace_guard(lock_path: Path, started: float) -> int:
    guard_path = lock_path.with_name(f"{lock_path.name}.guard")
    while True:
        if time.monotonic() - started >= LOCK_WAIT_TIMEOUT_SECONDS:
            raise TimeoutError("timed out waiting for atomic write lock")
        try:
            fd, _ = _open_lock_file(guard_path)
        except (FileExistsError, FileNotFoundError):
            _retry_after_failure(started)
            continue
        except OSError as exc:
            raise OSError("failed to open atomic write lock") from exc
        _notify_test_lock("attempt")
        try:
            if _try_lock_exclusive(fd):
                _verify_lock_identity(guard_path, fd)
                _notify_test_lock("acquired")
                return fd
            _notify_test_lock("blocked")
        except Exception:
            os.close(fd)
            raise
        os.close(fd)
        _retry_after_failure(started)


def _retry_after_failure(started: float) -> None:
    remaining = LOCK_WAIT_TIMEOUT_SECONDS - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutError("timed out waiting for atomic write lock")
    time.sleep(min(LOCK_RETRY_DELAY_SECONDS, remaining))


def _open_lock_file(lock_path: Path) -> tuple[int, bool]:
    """Open one regular, single-link lock artifact without following links."""
    try:
        existing_stat = os.lstat(lock_path)
    except FileNotFoundError:
        if os.name == "nt":
            fd = _open_lock_file_windows(lock_path, created=True)
        else:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(lock_path, flags | nofollow, 0o600)
        try:
            _validate_lock_stat(os.fstat(fd))
        except Exception:
            os.close(fd)
            raise
        return fd, True

    _validate_lock_stat(existing_stat)
    if os.name == "nt":
        fd = _open_lock_file_windows(lock_path, created=False)
    else:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags)
    try:
        opened_stat = os.fstat(fd)
        _validate_lock_stat(opened_stat)
        if not _same_file(existing_stat, opened_stat):
            raise OSError("atomic write lock path changed")
    except Exception:
        os.close(fd)
        raise
    return fd, False


def _verify_lock_identity(lock_path: Path, fd: int) -> None:
    path_stat = os.lstat(lock_path)
    _validate_lock_stat(path_stat)
    opened_stat = os.fstat(fd)
    _validate_lock_stat(opened_stat)
    if not _same_file(path_stat, opened_stat):
        raise OSError("atomic write lock path changed")


def _validate_lock_stat(metadata: os.stat_result) -> None:
    reparse_tag = getattr(metadata, "st_reparse_tag", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or reparse_tag
        or file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise OSError("atomic write lock is not a regular single-link file")


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8", mode: int | None = None) -> None:
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock_for(path) as verify_namespace:
        verify_namespace()
        _atomic_write_bytes_unlocked(path, data, mode=mode)


def atomic_read_or_create_text(
    path: Path,
    create_text: Callable[[], str],
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock_for(path) as verify_namespace:
        verify_namespace()
        try:
            raw = path.read_text(encoding=encoding).strip()
        except FileNotFoundError:
            raw = ""
        if raw:
            return raw
        text = create_text()
        verify_namespace()
        _atomic_write_bytes_unlocked(path, text.encode(encoding), mode=mode)
        return text


def _atomic_write_bytes_unlocked(path: Path, data: bytes, *, mode: int | None = None) -> None:
    temp_path = _unique_temp_path(path)
    try:
        with temp_path.open("wb") as temp_file:
            if mode is not None and os.name != "nt":
                with contextlib.suppress(AttributeError, OSError):
                    os.fchmod(temp_file.fileno(), mode)
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
        _fsync_directory_best_effort(path.parent)
    except Exception:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise


def _try_lock_exclusive(fd: int) -> bool:
    if os.name == "nt":
        return _try_lock_windows(fd)
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise


def _unlock(fd: int) -> None:
    if os.name == "nt":
        _unlock_windows(fd)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _read_lock_state(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        chunks.append(chunk)
    return _classify_lock_bytes(b"".join(chunks))


def _classify_lock_bytes(data: bytes) -> str:
    if not data:
        return "empty"
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return "unknown"
    if data in (b"codexhub-atomic-lock=1\n", b"codexhub-atomic-lock=1\r\n"):
        return "protocol"
    legacy = _parse_legacy_pid(text)
    if legacy is None:
        return "unknown"
    return "dead-legacy" if _pid_is_definitely_dead(legacy) else "live-legacy"


def _write_protocol(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, LOCK_PROTOCOL.encode("ascii"))
    os.fsync(fd)


def _parse_legacy_pid(text: str) -> int | None:
    """Parse exactly the former two-line record with a bounded PID."""
    if "\r\n" in text:
        if not text.endswith("\r\n") or "\r" in text[:-2].replace("\r\n", ""):
            return None
        lines = text[:-2].split("\r\n")
    else:
        if not text.endswith("\n") or "\r" in text:
            return None
        lines = text[:-1].split("\n")
    if len(lines) != 2:
        return None

    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or key in values:
            return None
        values[key] = value
    if set(values) != {"pid", "acquired_at_millis"}:
        return None
    if not all("0" <= char <= "9" for char in values["pid"]):
        return None
    if not all("0" <= char <= "9" for char in values["acquired_at_millis"]):
        return None
    try:
        pid = int(values["pid"])
        timestamp = int(values["acquired_at_millis"])
    except ValueError:
        return None
    if not 0 < pid <= 2_147_483_647 or timestamp > 2**128 - 1:
        return None
    return pid


def _pid_is_definitely_dead(pid: int) -> bool:
    if os.name == "nt":
        # A process that cannot be opened is deliberately treated as live or
        # unknown. This gives up recovery rather than risking an overlap.
        handle = _KERNEL32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not _KERNEL32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value != _STILL_ACTIVE
        finally:
            _KERNEL32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except (OverflowError, ValueError):
        return False
    except OSError as exc:
        return exc.errno == errno.ESRCH
    return False


if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

    _GENERIC_READ_WRITE = 0xC0000000
    _SHARE_READ_WRITE_DELETE = 0x00000007
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_SHARING_VIOLATION = 32
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_FILE_EXISTS = 80
    _STILL_ACTIVE = 259
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t), ("Offset", ctypes.c_ulong), ("OffsetHigh", ctypes.c_ulong), ("hEvent", ctypes.c_void_p)]

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
    _KERNEL32.LockFileEx.restype = wintypes.BOOL
    _KERNEL32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
    _KERNEL32.UnlockFileEx.restype = wintypes.BOOL
    _KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _KERNEL32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _KERNEL32.GetExitCodeProcess.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _KERNEL32.CreateFileW.restype = wintypes.HANDLE

    def _open_lock_file_windows(lock_path: Path, *, created: bool) -> int:
        handle = _KERNEL32.CreateFileW(
            str(lock_path),
            _GENERIC_READ_WRITE,
            _SHARE_READ_WRITE_DELETE,
            None,
            _CREATE_NEW if created else _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            error = ctypes.get_last_error()
            if created and error == _ERROR_FILE_EXISTS:
                raise FileExistsError(error, "lock already exists")
            if not created and error == _ERROR_FILE_NOT_FOUND:
                raise FileNotFoundError(error, "lock disappeared")
            raise OSError("failed to open atomic write lock")
        return msvcrt.open_osfhandle(handle, os.O_RDWR | getattr(os, "O_BINARY", 0))

    def _try_lock_windows(fd: int) -> bool:
        overlapped = _OVERLAPPED()
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        if _KERNEL32.LockFileEx(handle, _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, ctypes.byref(overlapped)):
            return True
        if ctypes.get_last_error() in (_ERROR_LOCK_VIOLATION, _ERROR_SHARING_VIOLATION):
            return False
        raise OSError("failed to acquire atomic write lock")

    def _unlock_windows(fd: int) -> None:
        overlapped = _OVERLAPPED()
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        if not _KERNEL32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
            raise OSError("failed to release atomic write lock")
else:
    # Imported only where available; keeping the Windows implementation free
    # of this import makes the module importable on both platforms.
    msvcrt = None  # type: ignore[assignment]


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
