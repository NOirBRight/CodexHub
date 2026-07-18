import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

import atomic_io
from atomic_io import _classify_lock_bytes, _parse_legacy_pid, atomic_read_or_create_text, atomic_write_text, file_lock_for
from lock_fixtures import write_dead_legacy_lock


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


def test_atomic_write_text_recovers_provably_dead_legacy_lock_without_unlinking(tmp_path: Path) -> None:
    target = tmp_path / "catalog.json"
    target.write_text("old", encoding="utf-8")
    lock = target.with_name("catalog.json.lock")
    _dead_child = write_dead_legacy_lock(lock)

    atomic_write_text(target, "new", encoding="utf-8")

    assert target.read_text(encoding="utf-8") == "new"
    assert lock.read_text(encoding="ascii") == "codexhub-atomic-lock=1\n"


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


@pytest.mark.parametrize(
    "error",
    [PermissionError("denied"), IsADirectoryError("directory"), UnicodeDecodeError("utf-8", b"\\xff", 0, 1, "invalid")],
)
def test_atomic_read_or_create_text_propagates_read_errors_without_creating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError | UnicodeDecodeError
) -> None:
    target = tmp_path / "secret"
    invoked = False

    def fail_read(self: Path, *, encoding: str, errors: str | None = None) -> str:
        raise error

    def creator() -> str:
        nonlocal invoked
        invoked = True
        return "created"

    monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(type(error)):
        atomic_read_or_create_text(target, creator)
    assert not invoked


def test_atomic_read_or_create_text_creates_only_for_missing_or_empty_content(tmp_path: Path) -> None:
    target = tmp_path / "value"
    calls = 0

    def creator() -> str:
        nonlocal calls
        calls += 1
        return "created"

    assert atomic_read_or_create_text(target, creator) == "created"
    target.write_text("   \n", encoding="utf-8")
    assert atomic_read_or_create_text(target, creator) == "created"
    target.write_text("existing\n", encoding="utf-8")
    assert atomic_read_or_create_text(target, creator) == "existing"
    assert calls == 2


def test_legacy_timestamp_or_malformed_lock_fails_safely(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    lock = target.with_name("settings.json.lock")
    lock.write_text("acquired_at_millis=0\n", encoding="ascii")
    with pytest.raises(TimeoutError, match="unavailable"):
        atomic_write_text(target, "new")
    lock.write_text("not-a-lock\n", encoding="ascii")
    with pytest.raises(TimeoutError, match="unavailable"):
        atomic_write_text(target, "new")


def test_existing_empty_lock_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "settings.json"
    lock = target.with_name("settings.json.lock")
    lock.touch()
    monkeypatch.setattr(atomic_io, "LOCK_WAIT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda _: None)

    with pytest.raises(TimeoutError, match="unavailable|timed out"):
        atomic_write_text(target, "new")

    assert lock.read_bytes() == b""


def test_dead_legacy_lock_is_recovered_without_unlinking_its_inode(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    lock = target.with_name("settings.json.lock")
    _dead_child = write_dead_legacy_lock(lock)
    inode = lock.stat().st_ino

    atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert lock.stat().st_ino == inode
    assert lock.read_text(encoding="ascii") == "codexhub-atomic-lock=1\n"


@pytest.mark.parametrize(
    "metadata",
    [
        "acquired_at_millis=0\n",
        "not-a-lock\n",
        "codexhub-atomic-lock=2\n",
        "codexhub-atomic-lock=1\nextra=value\n",
        "pid=1\npid=2\nacquired_at_millis=0\n",
        "pid=0\nacquired_at_millis=0\n",
        "pid=-1\nacquired_at_millis=0\n",
        "pid=999999999999999999999999\nacquired_at_millis=0\n",
    ],
)
def test_legacy_and_future_lock_metadata_fails_closed(tmp_path: Path, metadata: str) -> None:
    target = tmp_path / "settings.json"
    target.with_name("settings.json.lock").write_text(metadata, encoding="ascii")

    with pytest.raises(TimeoutError, match="unavailable"):
        atomic_write_text(target, "new")


def test_lock_symlink_is_rejected_without_modifying_target(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    victim = tmp_path / "victim"
    lock = target.with_name("settings.json.lock")
    victim.write_text("do not modify", encoding="ascii")
    try:
        lock.symlink_to(victim)
    except (OSError, NotImplementedError) as error:
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(f"CI must provide a symlink/reparse fixture: {error}")
        pytest.skip("symlinks are unavailable locally")

    with pytest.raises(OSError, match="atomic write lock") as raised:
        atomic_write_text(target, "new")

    assert "settings.json.lock" not in str(raised.value)
    assert victim.read_text(encoding="ascii") == "do not modify"


def test_lock_hardlink_is_rejected_without_modifying_target(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    victim = tmp_path / "victim"
    lock = target.with_name("settings.json.lock")
    victim.write_text("do not modify", encoding="ascii")
    try:
        os.link(victim, lock)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable")

    with pytest.raises(OSError, match="atomic write lock") as raised:
        atomic_write_text(target, "new")

    assert "settings.json.lock" not in str(raised.value)
    assert victim.read_text(encoding="ascii") == "do not modify"


def test_lock_identity_change_after_open_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "settings.json"
    lock = target.with_name("settings.json.lock")
    lock.write_text("codexhub-atomic-lock=1\n", encoding="ascii")
    replacement = tmp_path / "replacement.lock"
    replacement.write_text("codexhub-atomic-lock=1\n", encoding="ascii")
    real_lstat = atomic_io.os.lstat
    calls = 0

    def report_replaced_path(path: Path) -> os.stat_result:
        nonlocal calls
        if path == lock:
            calls += 1
            if calls == 2:
                return real_lstat(replacement)
        return real_lstat(path)

    monkeypatch.setattr(atomic_io.os, "lstat", report_replaced_path)
    with pytest.raises(OSError, match="atomic write lock"):
        atomic_write_text(target, "new")

    assert not target.exists()


def test_empty_lock_is_retried_as_transient_until_protocol_is_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "settings.json"
    lock = target.with_name("settings.json.lock")
    lock.write_text("codexhub-atomic-lock=1\n", encoding="ascii")
    reads = iter(["empty", "protocol"])
    monkeypatch.setattr(atomic_io, "_read_lock_state", lambda _: next(reads))
    monkeypatch.setattr(atomic_io.time, "sleep", lambda _: None)

    atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"


def test_lock_path_churn_obeys_deadline_and_backoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "settings.json"
    clock = iter([0.0, 0.0, 0.0, 2.0])
    sleeps: list[float] = []
    calls = 0
    monkeypatch.setattr(atomic_io, "LOCK_WAIT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(atomic_io.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(atomic_io.time, "sleep", sleeps.append)

    def churn(_: Path) -> tuple[int, bool]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("retry did not honor deadline")
        raise FileNotFoundError()

    monkeypatch.setattr(atomic_io, "_open_lock_file", churn)

    with pytest.raises(TimeoutError, match="timed out waiting"):
        atomic_write_text(target, "new")

    assert sleeps


def test_lock_release_is_idempotent_and_coordination_file_persists(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    with file_lock_for(target):
        pass
    with file_lock_for(target):
        pass
    assert target.with_name("settings.json.lock").read_text(encoding="ascii") == "codexhub-atomic-lock=1\n"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"codexhub-atomic-lock=1\n", "protocol"),
        (b"codexhub-atomic-lock=1\r\n", "protocol"),
        (b"codexhub-atomic-lock=1", "unknown"),
        (b"codexhub-atomic-lock=2\n", "unknown"),
        (b"codexhub-atomic-lock=1\nextra=value\n", "unknown"),
        (b"pid=0\nacquired_at_millis=0\n", "unknown"),
        (b"pid=-1\nacquired_at_millis=0\n", "unknown"),
        (b"pid=999999999999999999999999\nacquired_at_millis=0\n", "unknown"),
        (b"pid=1\npid=2\nacquired_at_millis=0\n", "unknown"),
        (b"pid=1\nacquired_at_millis=0\xff\n", "unknown"),
    ],
)
def test_python_parser_uses_the_shared_protocol_classification(data: bytes, expected: str) -> None:
    assert _classify_lock_bytes(data) == expected


def test_python_parser_rejects_timestamp_outside_shared_u128_range() -> None:
    assert _parse_legacy_pid("pid=1\nacquired_at_millis=340282366920938463463374607431768211456\n") is None




def test_python_production_path_rejects_invalid_utf8_lock_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    lock = target.with_name("settings.json.lock")
    lock.write_bytes(b"pid=1\nacquired_at_millis=0\n\xff")

    with pytest.raises(TimeoutError, match="unavailable") as raised:
        atomic_write_text(target, "new")

    assert str(raised.value) == "atomic write lock is unavailable"
    assert not target.exists()
    assert lock.read_bytes().endswith(b"\xff")


def test_guard_replacement_after_acquire_is_rejected_by_writer(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    guard = target.with_name("settings.json.lock.guard")

    def replace_guard_after_acquire(event: str) -> None:
        if event == "acquired":
            guard.unlink()
            guard.write_text("codexhub-atomic-lock=1\n", encoding="ascii")

    atomic_io._set_test_lock_hook(replace_guard_after_acquire)
    try:
        with pytest.raises(OSError, match="atomic write lock"):
            atomic_write_text(target, "new")
    finally:
        atomic_io._set_test_lock_hook(None)

    assert not target.exists()
    assert guard.read_text(encoding="ascii") == "codexhub-atomic-lock=1\n"


def _readline_with_watchdog(stream: object, timeout: float = 30.0) -> str:
    result: list[str] = []
    error: list[BaseException] = []

    def read_line() -> None:
        try:
            result.append(stream.readline())  # type: ignore[attr-defined]
        except BaseException as exc:  # pragma: no cover - only used for broken pipes
            error.append(exc)

    reader = threading.Thread(target=read_line, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        pytest.fail("subprocess handshake watchdog expired")
    if error:
        raise error[0]
    return result[0]


def test_live_legacy_lock_is_never_reclaimed(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    lock = target.with_name("settings.json.lock")
    metadata = f"pid={os.getpid()}\nacquired_at_millis=0\n"
    lock.write_text(metadata, encoding="ascii")

    with pytest.raises(TimeoutError, match="unavailable"):
        atomic_write_text(target, "new")

    assert lock.read_text(encoding="ascii") == metadata
    assert not target.exists()


def test_held_lock_stays_exclusive_beyond_former_age_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "settings.json"
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with file_lock_for(target):
            entered.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(10)
    # Backdate both artifacts far beyond the former 60-second staleness rule.
    ancient = time.time() - 3600
    os.utime(target.with_name("settings.json.lock"), (ancient, ancient))
    os.utime(target.with_name("settings.json.lock.guard"), (ancient, ancient))

    monkeypatch.setattr(atomic_io, "LOCK_WAIT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="timed out"):
        atomic_write_text(target, "new")

    release.set()
    thread.join(10)
    assert not target.exists()


def test_same_language_ab_c_choreography_keeps_single_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A holds; B waits and legitimately follows A; C cannot overlap B."""
    target = tmp_path / "settings.json"
    lock = target.with_name("settings.json.lock")
    a_acquired = threading.Event()
    a_release = threading.Event()
    b_events: list[str] = []
    b_acquired = threading.Event()
    b_release = threading.Event()

    def a_worker() -> None:
        with file_lock_for(target):
            a_acquired.set()
            a_release.wait(10)

    def b_worker() -> None:
        def hook(event: str) -> None:
            b_events.append(event)
            if event == "acquired":
                b_acquired.set()

        atomic_io._set_test_lock_hook(hook)
        with file_lock_for(target):
            b_release.wait(10)

    thread_a = threading.Thread(target=a_worker)
    thread_b = threading.Thread(target=b_worker)
    thread_a.start()
    assert a_acquired.wait(10)
    thread_b.start()
    # B is provably blocked while A holds the namespace guard.
    deadline = time.monotonic() + 10
    while "blocked" not in b_events and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "blocked" in b_events

    a_release.set()
    thread_a.join(10)
    # A's release must not remove the instance B is about to own.
    assert lock.read_text(encoding="ascii") == "codexhub-atomic-lock=1\n"
    assert b_acquired.wait(10)
    atomic_io._set_test_lock_hook(None)

    # C times out instead of overlapping B's critical section.
    monkeypatch.setattr(atomic_io, "LOCK_WAIT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="timed out"):
        atomic_write_text(target, "c")
    assert not target.exists()

    b_release.set()
    thread_b.join(10)
    # After B legitimately releases, C enters and the protocol instance persists.
    atomic_write_text(target, "c")
    assert target.read_text(encoding="utf-8") == "c"
    assert lock.read_text(encoding="ascii") == "codexhub-atomic-lock=1\n"


def test_release_after_external_replacement_keeps_replacement_instance(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    lock = target.with_name("settings.json.lock")
    with file_lock_for(target):
        try:
            lock.unlink()
            lock.write_text("codexhub-atomic-lock=1\n", encoding="ascii")
        except PermissionError as error:
            if os.environ.get("CI", "").lower() == "true":
                pytest.fail(f"CI must provide an unlink/recreate fixture: {error}")
            pytest.skip("platform denied unlinking an open lock artifact")
    # The owner's release operates on its own handle and never unlinks, so the
    # external replacement instance survives untouched.
    assert lock.read_text(encoding="ascii") == "codexhub-atomic-lock=1\n"


def test_real_unlink_recreate_cannot_overlap_guarded_contender(tmp_path: Path) -> None:
    target = tmp_path / "shared.json"
    lock = target.with_name("shared.json.lock")
    source = str(Path(__file__).resolve().parents[1] / "src-python")
    python = os.environ.get("PYTHON", "python")
    holder_script = "import pathlib, sys; from atomic_io import file_lock_for; target = pathlib.Path(sys.argv[1]);\nwith file_lock_for(target):\n    print('held', flush=True);\n    if sys.stdin.readline().strip() != 'release': raise SystemExit(2);\n    print('released', flush=True)"
    contender_script = "import pathlib, sys; from atomic_io import _set_test_lock_hook, atomic_write_text; target = pathlib.Path(sys.argv[1]); state = {'phase': 0};\ndef hook(event):\n    if event == 'attempt' and state['phase'] == 0:\n        print('attempt', flush=True); state['phase'] = 1\n    elif event == 'blocked' and state['phase'] == 1:\n        print('blocked', flush=True); state['phase'] = 2;\n        if sys.stdin.readline().strip() != 'replacement-verified': raise SystemExit(3)\n        print('replacement-verified', flush=True)\n    elif event == 'acquired':\n        print('acquired', flush=True)\n_set_test_lock_hook(hook); atomic_write_text(target, 'contender'); print('entered', flush=True)"
    holder = subprocess.Popen([python, "-c", holder_script, str(target)], env={**os.environ, "PYTHONPATH": source}, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    contender = None
    try:
        assert holder.stdout is not None
        assert holder.stdin is not None
        assert _readline_with_watchdog(holder.stdout).strip() == "held"
        contender = subprocess.Popen([python, "-c", contender_script, str(target)], env={**os.environ, "PYTHONPATH": source}, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert contender.stdout is not None
        assert contender.stdin is not None
        assert _readline_with_watchdog(contender.stdout).strip() == "attempt"
        assert _readline_with_watchdog(contender.stdout).strip() == "blocked"
        try:
            lock.unlink()
            lock.write_text("codexhub-atomic-lock=1\n", encoding="ascii")
        except PermissionError as error:
            if os.environ.get("CI", "").lower() == "true":
                pytest.fail(f"CI must provide an unlink/recreate race fixture: {error}")
            pytest.skip("platform denied unlinking an open lock artifact")
        contender.stdin.write("replacement-verified\n")
        contender.stdin.flush()
        assert _readline_with_watchdog(contender.stdout).strip() == "replacement-verified"
        holder.stdin.write("release\n")
        holder.stdin.flush()
        assert _readline_with_watchdog(holder.stdout).strip() == "released"
        assert _readline_with_watchdog(contender.stdout).strip() == "acquired"
        assert _readline_with_watchdog(contender.stdout).strip() == "entered"
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait()
        if contender is not None:
            if contender.poll() is None:
                contender.kill()
            contender.wait()
