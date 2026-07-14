"""Debug-only, content-free control bridge for the diagnostic recorder.

The Gateway child is the sole writer and authority for diagnostic state.  Tauri
places tiny, versioned requests in the recorder's runtime subtree, and this
bridge applies them off Gateway request paths.  The protocol deliberately has
no payload, headers, error text, or artifact contents.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Protocol


CONTROL_SCHEMA_VERSION = 1
CONTROL_POLL_INTERVAL_SECONDS = 0.1
CONTROL_RESPONSE_RETENTION_SECONDS = 5 * 60
MAX_COMMANDS_PER_TICK = 64

_SAFE_REQUEST_ID = re.compile(r"c[0-9a-f]{16,64}\Z")
_SAFE_INCIDENT_ID = re.compile(r"i[0-9]{6,}\Z")
_OPERATIONS = {"status", "mark", "pause", "resume", "delete"}
_STATUS_KEYS = {
    "active",
    "paused",
    "flavor",
    "rolling_bytes",
    "rolling_window_seconds",
    "incident_count",
    "incident_ids",
    "last_marker_category",
    "last_marker_at_ms",
    "rolling_evicted_segments",
    "incident_evicted_count",
    "truncated",
    "schema_version",
    "writer_failure_count",
    "writer_queue_dropped_records",
}


class _ControllableRecorder(Protocol):
    def status(self) -> Any: ...

    def mark_incident(self, category: str = "manual") -> str | None: ...

    def pause(self) -> Any: ...

    def resume(self) -> Any: ...

    def delete_incident(self, incident_id: str) -> bool: ...


class DiagnosticControlBridge:
    """Bounded file control plane owned by the debug Gateway child only."""

    def __init__(
        self,
        recorder: _ControllableRecorder,
        runtime_home: Path,
        *,
        clock: Callable[[], float] = time.time,
        poll_interval_seconds: float = CONTROL_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._recorder = recorder
        self._clock = clock
        self._poll_interval_seconds = max(0.01, poll_interval_seconds)
        self._root = Path(runtime_home) / "diagnostics"
        self._control_dir = self._root / "control"
        self._request_dir = self._control_dir / "requests"
        self._response_dir = self._control_dir / "responses"
        self._status_path = self._control_dir / "status.json"
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.RLock()
        self._io_lock = threading.RLock()

    @property
    def root(self) -> Path:
        """The authoritative, versioned diagnostics runtime subtree."""

        return self._root

    def start(self) -> None:
        """Start best-effort control polling without participating in traffic."""

        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            try:
                self._request_dir.mkdir(parents=True, exist_ok=True)
                self._response_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                return
            self._stopped.clear()
            self._publish_status()
            try:
                self._thread = threading.Thread(
                    target=self._run,
                    name="codex-diagnostic-control",
                    daemon=True,
                )
                self._thread.start()
            except Exception:
                self._thread = None

    def shutdown(self, timeout: float = 1.0) -> None:
        """Stop the bridge promptly; recorder shutdown remains independent."""

        self._stopped.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, timeout))

    def process_once(self) -> int:
        """Process a bounded batch for deterministic tests and the daemon loop."""

        try:
            requests = sorted(self._request_dir.glob("*.json"))[:MAX_COMMANDS_PER_TICK]
        except OSError:
            return 0
        processed = 0
        for path in requests:
            if self._stopped.is_set():
                break
            try:
                self._process_request(path)
                processed += 1
            except Exception:
                # A malformed control file can never affect Gateway traffic.
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        self._publish_status()
        self._remove_expired_responses()
        return processed

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                self.process_once()
            except Exception:
                pass
            self._stopped.wait(self._poll_interval_seconds)

    def _process_request(self, path: Path) -> None:
        request = _read_json(path)
        request_id = request.get("request_id") if isinstance(request, dict) else None
        response = self._execute(request)
        if isinstance(request_id, str) and _SAFE_REQUEST_ID.fullmatch(request_id):
            response["request_id"] = request_id
            self._write_response(request_id, response)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _execute(self, request: Any) -> dict[str, Any]:
        status = self._status_dict()
        if not isinstance(request, dict):
            return _response(False, status, code="invalid_request")
        request_id = request.get("request_id")
        operation = request.get("operation")
        if (
            not isinstance(request_id, str)
            or _SAFE_REQUEST_ID.fullmatch(request_id) is None
            or not isinstance(operation, str)
            or operation not in _OPERATIONS
            or not _valid_request_shape(request, operation)
            or not _valid_expiry(request.get("expires_at_ms"), self._now_ms())
        ):
            return _response(False, status, code="invalid_request")

        try:
            if operation == "mark":
                incident_id = self._recorder.mark_incident("manual")
                status = self._status_dict()
                return _response(
                    True,
                    status,
                    result={
                        "accepted": incident_id is not None,
                        "incident_id": incident_id if isinstance(incident_id, str) else None,
                    },
                )
            if operation == "pause":
                self._recorder.pause()
            elif operation == "resume":
                self._recorder.resume()
            elif operation == "delete":
                incident_id = request["incident_id"]
                deleted = self._recorder.delete_incident(incident_id)
                status = self._status_dict()
                return _response(True, status, result={"deleted": bool(deleted)})
            return _response(True, self._status_dict())
        except Exception:
            return _response(False, self._status_dict(), code="unavailable")

    def _status_dict(self) -> dict[str, Any]:
        try:
            status = self._recorder.status().as_dict()
        except Exception:
            return _empty_status()
        return _safe_status(status)

    def _publish_status(self) -> None:
        try:
            self._write_json_atomic(self._status_path, self._status_dict())
        except OSError:
            return

    def _write_response(self, request_id: str, response: dict[str, Any]) -> None:
        try:
            self._write_json_atomic(self._response_dir / f"{request_id}.json", response)
        except OSError:
            return

    def _write_json_atomic(self, path: Path, value: dict[str, Any]) -> None:
        encoded = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        with self._io_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(
                f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                with temporary.open("wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _remove_expired_responses(self) -> None:
        cutoff = self._now_ms() - CONTROL_RESPONSE_RETENTION_SECONDS * 1000
        try:
            for path in self._response_dir.glob("*.json"):
                try:
                    if int(path.stat().st_mtime * 1000) < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            return

    def _now_ms(self) -> int:
        try:
            return max(0, int(self._clock() * 1000))
        except Exception:
            return 0


def _valid_request_shape(request: dict[str, Any], operation: str) -> bool:
    required = {"schema_version", "request_id", "operation", "expires_at_ms"}
    if operation == "delete":
        required.add("incident_id")
    if set(request) != required or request.get("schema_version") != CONTROL_SCHEMA_VERSION:
        return False
    incident_id = request.get("incident_id")
    return operation != "delete" or (
        isinstance(incident_id, str) and _SAFE_INCIDENT_ID.fullmatch(incident_id) is not None
    )


def _valid_expiry(value: Any, now_ms: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and now_ms <= value <= now_ms + 60_000


def _response(
    ok: bool,
    status: dict[str, Any],
    *,
    code: str | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "ok": ok,
        "status": status,
    }
    if code is not None:
        response["code"] = code
    if result is not None:
        response["result"] = result
    return response


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _STATUS_KEYS:
        return _empty_status()
    if (
        not isinstance(value["active"], bool)
        or not isinstance(value["paused"], bool)
        or value["flavor"] != "debug"
        or not isinstance(value["truncated"], bool)
        or value["schema_version"] != CONTROL_SCHEMA_VERSION
    ):
        return _empty_status()
    integer_keys = {
        "rolling_bytes",
        "rolling_window_seconds",
        "incident_count",
        "rolling_evicted_segments",
        "incident_evicted_count",
        "writer_failure_count",
        "writer_queue_dropped_records",
    }
    if any(
        isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0
        for key in integer_keys
    ):
        return _empty_status()
    marker_at = value["last_marker_at_ms"]
    if marker_at is not None and (
        isinstance(marker_at, bool) or not isinstance(marker_at, int) or marker_at < 0
    ):
        return _empty_status()
    category = value["last_marker_category"]
    if category is not None and not isinstance(category, str):
        return _empty_status()
    incident_ids = value["incident_ids"]
    if not isinstance(incident_ids, (list, tuple)) or any(
        not isinstance(item, str) or _SAFE_INCIDENT_ID.fullmatch(item) is None for item in incident_ids
    ):
        return _empty_status()
    if value["incident_count"] != len(incident_ids):
        return _empty_status()
    return {
        **value,
        "incident_ids": list(incident_ids),
    }


def _empty_status() -> dict[str, Any]:
    return {
        "active": False,
        "paused": False,
        "flavor": "debug",
        "rolling_bytes": 0,
        "rolling_window_seconds": 0,
        "incident_count": 0,
        "incident_ids": [],
        "last_marker_category": None,
        "last_marker_at_ms": None,
        "rolling_evicted_segments": 0,
        "incident_evicted_count": 0,
        "truncated": False,
        "schema_version": CONTROL_SCHEMA_VERSION,
        "writer_failure_count": 0,
        "writer_queue_dropped_records": 0,
    }
