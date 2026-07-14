"""Bounded, privacy-safe Gateway diagnostic recordings.

The recorder is deliberately a deep module: request paths only submit tiny,
allow-listed observations to a bounded asynchronous writer.  Segment rotation,
snapshotting, retention, recovery, and artifact reads stay behind this module
so a recorder failure cannot affect Gateway behavior.

This first slice accepts a *compile-selected* flavor from its caller.  It does
not inspect an environment variable or settings file, which keeps a normal
build from gaining a runtime enablement path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

import bounded_event_writer


SCHEMA_VERSION = 1
ARTIFACT_VERSION = 1
ROLLING_WINDOW_SECONDS = 2 * 60 * 60
ROLLING_MAX_BYTES = 32 * 1024 * 1024
MAX_SEGMENT_BYTES = 512 * 1024
SEGMENT_SECONDS = 5 * 60
INCIDENT_TAIL_SECONDS = 5 * 60
INCIDENT_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_INCIDENTS = 3
MAX_CHECKPOINTS_PER_REQUEST = 16
WRITER_QUEUE_MAX_RECORDS = 2048
WRITER_QUEUE_MAX_BYTES = 2 * 1024 * 1024
MAINTENANCE_INTERVAL_SECONDS = 60
MAX_RECORD_ELAPSED_MS = 7 * 24 * 60 * 60 * 1000
MAX_RECORD_COUNTER = (1 << 63) - 1
MAX_TRACKED_REQUESTS = 4096
MAX_REQUEST_KEY_CHARACTERS = 512


_SAFE_MODELS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,127}\Z")
_SAFE_INCIDENT_IDS = re.compile(r"i[0-9]{6,}\Z")
_SAFE_REQUEST_LABELS = re.compile(r"r[0-9]{6,}\Z")

_PHASES = {
    "downstream_accept",
    "upstream_attempt",
    "upstream_headers",
    "sse_first",
    "sse_checkpoint",
    "upstream_terminal",
    "downstream_terminal",
    "upstream_close",
    "downstream_close",
    "retry",
    "request_complete",
    "request_error",
    "incident_marker",
    "recorder_writer_recovered",
}
_OUTCOMES = {"ok", "error", "closed", "eof", "unknown"}
_CONNECTION_DISPOSITIONS = {"new", "reused", "unobserved"}
_FAILURE_PHASES = {
    "dns",
    "tcp_connect",
    "tls",
    "request_write",
    "headers",
    "first_sse",
    "stream_body",
    "downstream_write",
    "local_close",
    "unknown",
}
_MARKER_CATEGORIES = {
    "manual",
    "abnormal_terminal",
    "downstream_write_failure",
    "retry_exhausted",
    "upstream_failure",
    "unknown",
}
_PROVIDERS = {"official", "external", "local", "unknown"}
_ROUTES = {"official", "codexhub", "local", "unknown"}
_HEADER_COUNT_BUCKETS = {"0", "1-4", "5-16", "17+"}
_CONTENT_LENGTH_BUCKETS = {"0", "1-1k", "1k-64k", "64k-1m", "1m+", "unknown"}
_CONTENT_TYPE_CLASSES = {"event-stream", "json", "other", "absent", "unknown"}


class DiagnosticRecorderProtocol(Protocol):
    """Small observation seam used by the Gateway."""

    def observe_proxy_event(self, event: str, fields: Mapping[str, Any]) -> None: ...

    def observe_upstream_attempt(
        self,
        request_key: str | None,
        *,
        attempt: int,
        retry_budget: int,
        elapsed_ms: int,
        outcome: str,
        failure_phase: str | None = None,
        connection_disposition: str = "unobserved",
        provider: str | None = None,
        model: str | None = None,
    ) -> None: ...

    def observe_upstream_headers(
        self,
        request_key: str | None,
        *,
        status: int | None,
        headers: Any,
    ) -> None: ...

    def observe_sse_line(self, request_key: str | None, byte_count: int) -> None: ...

    def observe_terminal(self, request_key: str | None, *, forwarded: bool) -> None: ...


@dataclass(frozen=True)
class RecorderStatus:
    """The public, content-free status shape consumed by later UI/export work."""

    active: bool
    paused: bool
    flavor: str
    rolling_bytes: int
    rolling_window_seconds: int
    incident_count: int
    last_marker_category: str | None
    last_marker_at_ms: int | None
    rolling_evicted_segments: int
    incident_evicted_count: int
    truncated: bool
    schema_version: int
    writer_failure_count: int
    writer_queue_dropped_records: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _RequestStreamState:
    label: str
    lines: int = 0
    byte_count: int = 0
    first_seen: bool = False
    checkpoint_count: int = 0
    next_checkpoint_lines: int = 2


@dataclass(frozen=True)
class _PendingIncident:
    incident_id: str
    category: str
    marker_at_ms: int
    cutoff_at_ms: int


@dataclass
class _Segment:
    path: Path
    ordinal: int
    start_at_ms: int
    end_at_ms: int
    byte_count: int
    sink: bounded_event_writer.JsonlFileSink | None = None


class DisabledDiagnosticRecorder:
    """A no-op recorder used by normal builds until trusted flavor wiring exists."""

    def observe_proxy_event(self, event: str, fields: Mapping[str, Any]) -> None:
        return None

    def observe_upstream_attempt(
        self,
        request_key: str | None,
        *,
        attempt: int,
        retry_budget: int,
        elapsed_ms: int,
        outcome: str,
        failure_phase: str | None = None,
        connection_disposition: str = "unobserved",
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        return None

    def observe_upstream_headers(self, request_key: str | None, *, status: int | None, headers: Any) -> None:
        return None

    def observe_sse_line(self, request_key: str | None, byte_count: int) -> None:
        return None

    def observe_terminal(self, request_key: str | None, *, forwarded: bool) -> None:
        return None

    def mark_incident(self, category: str = "manual") -> None:
        return None

    def pause(self) -> RecorderStatus:
        return self.status()

    def resume(self) -> RecorderStatus:
        return self.status()

    def delete_incident(self, incident_id: str) -> bool:
        return False

    def read_incident(self, incident_id: str) -> dict[str, Any] | None:
        return None

    def process_due_incidents(self) -> int:
        return 0

    def flush(self, timeout: float = 0.0) -> bool:
        return True

    def shutdown(self, timeout: float = 0.0) -> bool:
        return True

    def status(self) -> RecorderStatus:
        return RecorderStatus(
            active=False,
            paused=False,
            flavor="normal",
            rolling_bytes=0,
            rolling_window_seconds=0,
            incident_count=0,
            last_marker_category=None,
            last_marker_at_ms=None,
            rolling_evicted_segments=0,
            incident_evicted_count=0,
            truncated=False,
            schema_version=SCHEMA_VERSION,
            writer_failure_count=0,
            writer_queue_dropped_records=0,
        )


def for_compile_flavor(runtime_home: Path, compile_flavor: str) -> DiagnosticRecorderProtocol:
    """Construct the recorder only for a caller-provided compile identity.

    The caller must supply an identity embedded by the packaged build.  There
    is intentionally no environment or settings fallback here.
    """

    if compile_flavor == "debug":
        return DiagnosticRecorder(runtime_home)
    return DisabledDiagnosticRecorder()


class DiagnosticRecorder:
    """Persist bounded, sanitized observations without participating in requests."""

    def __init__(
        self,
        runtime_home: Path,
        *,
        clock: Callable[[], float] = time.time,
        rolling_window_seconds: int = ROLLING_WINDOW_SECONDS,
        rolling_max_bytes: int = ROLLING_MAX_BYTES,
        max_segment_bytes: int = MAX_SEGMENT_BYTES,
        segment_seconds: int = SEGMENT_SECONDS,
        incident_tail_seconds: int = INCIDENT_TAIL_SECONDS,
        incident_retention_seconds: int = INCIDENT_RETENTION_SECONDS,
        max_incidents: int = MAX_INCIDENTS,
    ) -> None:
        if rolling_window_seconds < 1 or rolling_max_bytes < 1024:
            raise ValueError("rolling recorder bounds must be positive")
        if max_segment_bytes < 512 or max_segment_bytes > rolling_max_bytes:
            raise ValueError("segment bound must fit within the rolling cap")
        if max_incidents < 1:
            raise ValueError("max_incidents must be positive")

        self._clock = clock
        self._root = Path(runtime_home) / "diagnostics"
        self._rolling_dir = self._root / "rolling"
        self._incidents_dir = self._root / "incidents"
        self._rolling_window_seconds = rolling_window_seconds
        self._incident_tail_seconds = incident_tail_seconds
        self._incident_retention_seconds = incident_retention_seconds
        self._max_incidents = max_incidents
        self._lock = threading.RLock()
        self._control_event = threading.Event()
        self._control_thread: threading.Thread | None = None
        self._stopping = False
        self._paused = False
        self._sequence = 0
        self._label_counter = 0
        self._incident_counter = 0
        self._streams: dict[str, _RequestStreamState] = {}
        self._pending_incidents: list[_PendingIncident] = []
        self._incident_ids: set[str] = set()
        self._last_marker_category: str | None = None
        self._last_marker_at_ms: int | None = None
        self._incident_evicted_count = 0
        self._truncated = False
        self._control_failure_count = 0

        self._sink = _RollingSegmentSink(
            self._rolling_dir,
            clock=clock,
            rolling_window_seconds=rolling_window_seconds,
            rolling_max_bytes=rolling_max_bytes,
            max_segment_bytes=max_segment_bytes,
            segment_seconds=segment_seconds,
        )
        self._writer = bounded_event_writer.BoundedEventWriter(
            self._sink,
            max_records=WRITER_QUEUE_MAX_RECORDS,
            max_bytes=WRITER_QUEUE_MAX_BYTES,
            recovery_record_factory=self._writer_recovery_record,
            clock=clock,
            thread_name="codex-diagnostic-recorder",
        )
        self._recover_artifacts()
        self._enforce_incident_retention()
        try:
            highest_sequence, highest_label = self._sink.highest_ordering()
        except Exception:
            highest_sequence, highest_label = 0, 0
        self._sequence = max(self._sequence, highest_sequence)
        self._label_counter = max(self._label_counter, highest_label)
        with self._lock:
            self._ensure_control_thread_locked()

    @property
    def root(self) -> Path:
        """The dedicated diagnostics subtree; useful to the read-only exporter."""

        return self._root

    def observe_proxy_event(self, event: str, fields: Mapping[str, Any]) -> None:
        """Map existing Gateway events to recorder phases without retaining payloads."""

        request_key = _string_or_none(fields.get("request_id"))
        if not request_key:
            return
        provider = _provider(fields.get("upstream"))
        model = _safe_model(fields.get("model_canonical") or fields.get("model"))
        route = _route(fields.get("route_mode"))
        status = _status(fields.get("status"))
        elapsed_ms = _bounded_counter(fields.get("duration_ms"), MAX_RECORD_ELAPSED_MS)
        if event == "request_start":
            self.record_phase(
                request_key,
                "downstream_accept",
                provider=provider,
                model=model,
                route=route,
                status=status,
            )
        elif event == "upstream_retry":
            self.record_phase(
                request_key,
                "retry",
                provider=provider,
                model=model,
                attempt=_attempt(fields.get("attempt")),
                retry_budget=_attempt(fields.get("max_attempts")),
                elapsed_ms=elapsed_ms,
                failure_phase=_failure_phase(fields.get("failure_phase")),
                outcome="error",
            )
        elif event in {"request_complete", "usage_observed"}:
            self.record_phase(
                request_key,
                "request_complete",
                provider=provider,
                model=model,
                route=route,
                status=status,
                elapsed_ms=elapsed_ms,
                outcome="ok",
            )
        elif event in {"request_error", "upstream_stream_error_event", "upstream_stream_incomplete"}:
            self.record_phase(
                request_key,
                "request_error",
                provider=provider,
                model=model,
                route=route,
                status=status,
                elapsed_ms=elapsed_ms,
                failure_phase=_failure_phase(fields.get("failure_phase")),
                outcome="error",
                automatic_marker=True,
            )
        elif event in {"client_write_failed", "downstream_stream_closed"}:
            self.observe_close(
                request_key,
                side="downstream",
                outcome="error",
                status=status,
                elapsed_ms=elapsed_ms,
                automatic_marker=True,
            )
        elif event in {
            "upstream_stream_interrupted",
            "upstream_stream_idle_timeout",
            "official_passthrough_stream_closed",
            "transparent_stream_closed",
        }:
            side = "downstream" if fields.get("failure_side") == "downstream_write" else "upstream"
            self.observe_close(
                request_key,
                side=side,
                outcome="error",
                status=status,
                elapsed_ms=elapsed_ms,
                automatic_marker=True,
            )

    def observe_upstream_attempt(
        self,
        request_key: str | None,
        *,
        attempt: int,
        retry_budget: int,
        elapsed_ms: int,
        outcome: str,
        failure_phase: str | None = None,
        connection_disposition: str = "unobserved",
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        self.record_phase(
            request_key,
            "upstream_attempt",
            attempt=attempt,
            retry_budget=retry_budget,
            elapsed_ms=elapsed_ms,
            outcome=outcome,
            failure_phase=failure_phase,
            connection_disposition=connection_disposition,
            provider=provider,
            model=model,
            automatic_marker=outcome == "error" and attempt >= retry_budget,
        )

    def observe_upstream_headers(self, request_key: str | None, *, status: int | None, headers: Any) -> None:
        header_count, content_type_class, content_length_bucket = _header_summary(headers)
        self.record_phase(
            request_key,
            "upstream_headers",
            status=status,
            header_count_bucket=header_count,
            content_type_class=content_type_class,
            content_length_bucket=content_length_bucket,
            outcome="ok",
        )

    def observe_sse_line(self, request_key: str | None, byte_count: int) -> None:
        """Coalesce arbitrary SSE traffic to a first event and <=16 aggregates."""

        if (
            not isinstance(request_key, str)
            or len(request_key) > MAX_REQUEST_KEY_CHARACTERS
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
        ):
            return
        with self._lock:
            if self._paused or self._stopping:
                return
            state = self._stream_state_locked(request_key)
            state.lines = min(MAX_RECORD_COUNTER, state.lines + 1)
            state.byte_count = min(MAX_RECORD_COUNTER, state.byte_count + min(byte_count, MAX_RECORD_COUNTER))
            if not state.first_seen:
                state.first_seen = True
                if self._record_locked(
                    "sse_first",
                    state.label,
                    {"lines": state.lines, "bytes": state.byte_count, "outcome": "ok"},
                ):
                    self._ensure_control_thread_locked()
                return
            if state.checkpoint_count >= MAX_CHECKPOINTS_PER_REQUEST:
                return
            if state.lines < state.next_checkpoint_lines:
                return
            state.checkpoint_count += 1
            state.next_checkpoint_lines = min(MAX_RECORD_COUNTER, state.next_checkpoint_lines * 2)
            if self._record_locked(
                "sse_checkpoint",
                state.label,
                {
                    "lines": state.lines,
                    "bytes": state.byte_count,
                    "checkpoint": state.checkpoint_count,
                    "outcome": "ok",
                },
            ):
                self._ensure_control_thread_locked()

    def observe_terminal(self, request_key: str | None, *, forwarded: bool) -> None:
        phase = "downstream_terminal" if forwarded else "upstream_terminal"
        self.record_phase(request_key, phase, outcome="ok")

    def observe_close(
        self,
        request_key: str | None,
        *,
        side: str,
        outcome: str,
        status: int | None = None,
        elapsed_ms: int | None = None,
        automatic_marker: bool = False,
    ) -> None:
        phase = "upstream_close" if side == "upstream" else "downstream_close"
        self.record_phase(
            request_key,
            phase,
            status=status,
            elapsed_ms=elapsed_ms,
            outcome=outcome,
            automatic_marker=automatic_marker,
        )

    def record_phase(
        self,
        request_key: str | None,
        phase: str,
        *,
        automatic_marker: bool = False,
        **values: Any,
    ) -> None:
        if (
            not isinstance(phase, str)
            or phase not in _PHASES
            or not isinstance(request_key, str)
            or len(request_key) > MAX_REQUEST_KEY_CHARACTERS
        ):
            return
        with self._lock:
            if self._paused or self._stopping:
                return
            state = self._stream_state_locked(request_key)
            fields = _sanitize_fields(values)
            if phase in {"upstream_terminal", "downstream_terminal", "upstream_close", "downstream_close"}:
                fields.setdefault("lines", state.lines)
                fields.setdefault("bytes", state.byte_count)
            if self._record_locked(phase, state.label, fields):
                self._ensure_control_thread_locked()
            if automatic_marker:
                category = "downstream_write_failure" if phase == "downstream_close" else "abnormal_terminal"
                self._schedule_incident_locked(category)
            if phase in {"downstream_terminal", "downstream_close", "request_complete", "request_error"}:
                self._streams.pop(request_key, None)

    def mark_incident(self, category: str = "manual") -> str | None:
        """Schedule a bounded post-marker freeze without stopping traffic."""

        with self._lock:
            if self._paused or self._stopping:
                return None
            return self._schedule_incident_locked(category)

    def pause(self) -> RecorderStatus:
        with self._lock:
            self._paused = True
        return self.status()

    def resume(self) -> RecorderStatus:
        with self._lock:
            if not self._stopping:
                self._paused = False
        return self.status()

    def delete_incident(self, incident_id: str) -> bool:
        """Delete one frozen artifact deterministically; unknown ids are a no-op."""

        if not isinstance(incident_id, str) or not _SAFE_INCIDENT_IDS.fullmatch(incident_id):
            return False
        path = self._incidents_dir / f"incident-{incident_id}"
        try:
            if not path.is_dir():
                return False
            shutil.rmtree(path)
        except OSError:
            return False
        with self._lock:
            self._incident_ids.discard(incident_id)
        return True

    def read_incident(self, incident_id: str) -> dict[str, Any] | None:
        """Read a complete artifact through the versioned, read-only contract."""

        if not isinstance(incident_id, str) or not _SAFE_INCIDENT_IDS.fullmatch(incident_id):
            return None
        directory = self._incidents_dir / f"incident-{incident_id}"
        manifest_path = directory / "manifest.json"
        records_path = directory / "records.jsonl"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not _valid_manifest(manifest, incident_id) or not records_path.is_file():
                return None
            records = list(_read_jsonl_records(records_path))
        except (OSError, json.JSONDecodeError):
            return None
        if manifest["record_count"] != len(records):
            return None
        return {"manifest": manifest, "records": records}

    def process_due_incidents(self) -> int:
        """Run due freezes.  The daemon uses this; tests can drive it directly."""

        now_ms = self._now_ms()
        with self._lock:
            due = [pending for pending in self._pending_incidents if pending.cutoff_at_ms <= now_ms]
            if due:
                self._pending_incidents = [
                    pending for pending in self._pending_incidents if pending.cutoff_at_ms > now_ms
                ]
        for pending in due:
            self._freeze_incident(pending)
        return len(due)

    def flush(self, timeout: float = 5.0) -> bool:
        try:
            return self._writer.flush(timeout).completed
        except Exception:
            return False

    def shutdown(self, timeout: float = 5.0) -> bool:
        with self._lock:
            self._stopping = True
            self._control_event.set()
            control_thread = self._control_thread
        deadline = time.monotonic() + max(0.0, timeout)
        if control_thread is not None and control_thread is not threading.current_thread():
            control_thread.join(max(0.0, deadline - time.monotonic()))
        try:
            return self._writer.shutdown(max(0.0, deadline - time.monotonic())).completed
        except Exception:
            return False

    def status(self) -> RecorderStatus:
        sink_status = self._sink.status()
        writer_status = self._writer.status()
        with self._lock:
            return RecorderStatus(
                active=not self._paused and not self._stopping,
                paused=self._paused,
                flavor="debug",
                rolling_bytes=sink_status.rolling_bytes,
                rolling_window_seconds=sink_status.rolling_window_seconds,
                incident_count=len(self._incident_ids),
                last_marker_category=self._last_marker_category,
                last_marker_at_ms=self._last_marker_at_ms,
                rolling_evicted_segments=sink_status.evicted_segments,
                incident_evicted_count=self._incident_evicted_count,
                truncated=self._truncated or sink_status.truncated,
                schema_version=SCHEMA_VERSION,
                writer_failure_count=writer_status.failure_count + self._control_failure_count,
                writer_queue_dropped_records=writer_status.dropped_records,
            )

    def _schedule_incident_locked(self, category: str) -> str | None:
        category = category if isinstance(category, str) and category in _MARKER_CATEGORIES else "unknown"
        if len(self._pending_incidents) >= self._max_incidents:
            self._truncated = True
            return None
        now_ms = self._now_ms()
        self._incident_counter += 1
        incident_id = f"i{self._incident_counter:06d}"
        pending = _PendingIncident(
            incident_id=incident_id,
            category=category,
            marker_at_ms=now_ms,
            cutoff_at_ms=now_ms + int(self._incident_tail_seconds * 1000),
        )
        self._pending_incidents.append(pending)
        self._last_marker_category = category
        self._last_marker_at_ms = now_ms
        self._record_locked(
            "incident_marker",
            None,
            {"incident": incident_id, "reason_category": category, "outcome": "ok"},
        )
        self._ensure_control_thread_locked()
        self._control_event.set()
        return incident_id

    def _ensure_control_thread_locked(self) -> None:
        if self._control_thread is not None and self._control_thread.is_alive():
            return
        try:
            self._control_thread = threading.Thread(
                target=self._control_loop,
                name="codex-diagnostic-freezer",
                daemon=True,
            )
            self._control_thread.start()
        except Exception:
            self._control_thread = None
            self._control_failure_count += 1

    def _control_loop(self) -> None:
        while True:
            with self._lock:
                if self._stopping:
                    return
                now_ms = self._now_ms()
                due_at = min(
                    (pending.cutoff_at_ms for pending in self._pending_incidents),
                    default=None,
                )
            if due_at is not None and due_at <= now_ms:
                self.process_due_incidents()
                continue
            timeout = (
                MAINTENANCE_INTERVAL_SECONDS
                if due_at is None
                else max(0.01, min(MAINTENANCE_INTERVAL_SECONDS, (due_at - now_ms) / 1000))
            )
            triggered = self._control_event.wait(timeout)
            self._control_event.clear()
            with self._lock:
                if self._stopping:
                    return
            if not triggered:
                self._maintain_storage()

    def _maintain_storage(self) -> None:
        """Expire old complete segments and incident artifacts off request paths."""

        try:
            result = self._writer.rotate(self._sink.maintain, timeout=5.0)
            if not result.completed:
                raise OSError("diagnostic maintenance did not drain")
            self._enforce_incident_retention()
        except Exception:
            with self._lock:
                self._control_failure_count += 1

    def _freeze_incident(self, pending: _PendingIncident) -> None:
        artifact: dict[str, Any] | None = None

        def rotate_and_freeze() -> None:
            nonlocal artifact
            artifact = self._sink.freeze(
                incidents_dir=self._incidents_dir,
                incident_id=pending.incident_id,
                category=pending.category,
                marker_at_ms=pending.marker_at_ms,
                cutoff_at_ms=pending.cutoff_at_ms,
                created_at_ms=self._now_ms(),
            )

        try:
            result = self._writer.rotate(rotate_and_freeze, timeout=5.0)
        except Exception:
            result = None
        if result is None or not result.completed or artifact is None:
            with self._lock:
                self._control_failure_count += 1
            return
        with self._lock:
            self._incident_ids.add(pending.incident_id)
        self._enforce_incident_retention()

    def _enforce_incident_retention(self) -> None:
        now_ms = self._now_ms()
        entries: list[tuple[int, str, Path]] = []
        try:
            self._incidents_dir.mkdir(parents=True, exist_ok=True)
            for path in self._incidents_dir.glob("incident-i*"):
                manifest_path = path / "manifest.json"
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    incident_id = manifest.get("incident_id")
                    created_at_ms = manifest.get("created_at_ms")
                    if not _valid_manifest(manifest, incident_id) or not isinstance(created_at_ms, int):
                        continue
                    entries.append((created_at_ms, incident_id, path))
                except (OSError, json.JSONDecodeError):
                    continue
        except OSError:
            with self._lock:
                self._control_failure_count += 1
            return
        entries.sort()
        expired_before_ms = now_ms - int(self._incident_retention_seconds * 1000)
        removals = [entry for entry in entries if entry[0] < expired_before_ms]
        survivors = [entry for entry in entries if entry[0] >= expired_before_ms]
        removals.extend(survivors[: max(0, len(survivors) - self._max_incidents)])
        for _created_at, incident_id, path in removals:
            try:
                shutil.rmtree(path)
            except OSError:
                continue
            with self._lock:
                self._incident_ids.discard(incident_id)
                self._incident_evicted_count += 1

    def _recover_artifacts(self) -> None:
        """Discard incomplete snapshots; never advertise a partially frozen artifact."""

        try:
            self._incidents_dir.mkdir(parents=True, exist_ok=True)
            for temporary in self._incidents_dir.glob(".incident-*.tmp"):
                shutil.rmtree(temporary, ignore_errors=True)
            valid: list[str] = []
            highest = 0
            for path in self._incidents_dir.glob("incident-i*"):
                manifest_path = path / "manifest.json"
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    incident_id = manifest.get("incident_id")
                    if not _valid_manifest(manifest, incident_id) or not (path / "records.jsonl").is_file():
                        continue
                    valid.append(incident_id)
                    highest = max(highest, int(incident_id[1:]))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
            with self._lock:
                self._incident_ids.update(valid)
                self._incident_counter = max(self._incident_counter, highest)
        except OSError:
            with self._lock:
                self._control_failure_count += 1

    def _writer_recovery_record(
        self,
        summary: bounded_event_writer.RecoverySummary,
    ) -> Mapping[str, Any]:
        with self._lock:
            self._sequence += 1
            return {
                "schema_version": SCHEMA_VERSION,
                "seq": self._sequence,
                "at_ms": self._now_ms(),
                "kind": "recorder_writer_recovered",
                "overflow_records": min(MAX_RECORD_COUNTER, summary.overflow_records),
                "overflow_bytes": min(MAX_RECORD_COUNTER, summary.overflow_bytes),
                "failed_records": min(MAX_RECORD_COUNTER, summary.failed_records),
                "failure_count": min(MAX_RECORD_COUNTER, summary.failure_count),
            }

    def _stream_state_locked(self, request_key: str) -> _RequestStreamState:
        state = self._streams.get(request_key)
        if state is not None:
            return state
        if len(self._streams) >= MAX_TRACKED_REQUESTS:
            self._streams.pop(next(iter(self._streams)))
        self._label_counter += 1
        label = f"r{self._label_counter:06d}"
        state = _RequestStreamState(label=label)
        self._streams[request_key] = state
        return state

    def _record_locked(self, kind: str, request_label: str | None, fields: Mapping[str, Any]) -> bool:
        if kind not in _PHASES:
            return False
        self._sequence += 1
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "seq": self._sequence,
            "at_ms": self._now_ms(),
            "kind": kind,
        }
        if request_label is not None:
            record["request"] = request_label
        record.update(fields)
        try:
            accepted = self._writer.enqueue(record)
        except Exception:
            accepted = False
        if not accepted:
            self._truncated = True
        return accepted

    def _now_ms(self) -> int:
        return max(0, int(self._clock() * 1000))


@dataclass(frozen=True)
class _RollingStatus:
    rolling_bytes: int
    rolling_window_seconds: int
    evicted_segments: int
    truncated: bool


class _RollingSegmentSink:
    """Writer-thread-owned rolling storage with complete-segment eviction."""

    def __init__(
        self,
        rolling_dir: Path,
        *,
        clock: Callable[[], float],
        rolling_window_seconds: int,
        rolling_max_bytes: int,
        max_segment_bytes: int,
        segment_seconds: int,
    ) -> None:
        self._rolling_dir = rolling_dir
        self._clock = clock
        self._rolling_window_ms = rolling_window_seconds * 1000
        self._rolling_max_bytes = rolling_max_bytes
        self._max_segment_bytes = max_segment_bytes
        self._segment_ms = segment_seconds * 1000
        self._lock = threading.RLock()
        self._loaded = False
        self._segments: list[_Segment] = []
        self._active: _Segment | None = None
        self._next_ordinal = 1
        self._evicted_segments = 0
        self._truncated = False

    def append(self, records: Sequence[bytes]) -> None:
        if not records:
            return
        with self._lock:
            self._ensure_loaded_locked()
            for record in records:
                if not record.endswith(b"\n"):
                    raise ValueError("recorder records must be complete JSONL lines")
                at_ms = _record_time_ms(record, self._now_ms())
                self._expire_locked(at_ms)
                self._ensure_capacity_locked(len(record), at_ms)
                segment = self._ensure_active_locked(at_ms)
                assert segment.sink is not None
                segment.sink.append([record])
                segment.byte_count += len(record)
                segment.end_at_ms = max(segment.end_at_ms, at_ms)

    def freeze(
        self,
        *,
        incidents_dir: Path,
        incident_id: str,
        category: str,
        marker_at_ms: int,
        cutoff_at_ms: int,
        created_at_ms: int,
    ) -> dict[str, Any]:
        """Create a complete artifact while the writer rotation fence is held."""

        if not _SAFE_INCIDENT_IDS.fullmatch(incident_id):
            raise ValueError("invalid incident id")
        with self._lock:
            self._ensure_loaded_locked()
            self._close_active_locked()
            self._expire_locked(cutoff_at_ms)
            records: list[dict[str, Any]] = []
            for segment in sorted(self._segments, key=lambda item: (item.start_at_ms, item.ordinal)):
                for record in _read_jsonl_records(segment.path):
                    at_ms = record.get("at_ms")
                    if isinstance(at_ms, int) and at_ms <= cutoff_at_ms:
                        records.append(record)
            records.sort(key=lambda record: _safe_sequence(record.get("seq")))
            classification = classify_frozen_records(records)
            incidents_dir.mkdir(parents=True, exist_ok=True)
            temporary = incidents_dir / f".incident-{incident_id}.tmp"
            final = incidents_dir / f"incident-{incident_id}"
            shutil.rmtree(temporary, ignore_errors=True)
            if final.exists():
                raise FileExistsError(final)
            temporary.mkdir(parents=True)
            try:
                records_path = temporary / "records.jsonl"
                with records_path.open("wb") as handle:
                    for record in records:
                        handle.write(_json_line(record))
                    handle.flush()
                    os.fsync(handle.fileno())
                manifest = {
                    "schema_version": SCHEMA_VERSION,
                    "artifact_version": ARTIFACT_VERSION,
                    "complete": True,
                    "incident_id": incident_id,
                    "reason_category": category if category in _MARKER_CATEGORIES else "unknown",
                    "marker_at_ms": marker_at_ms,
                    "cutoff_at_ms": cutoff_at_ms,
                    "created_at_ms": created_at_ms,
                    "record_count": len(records),
                    "classification": classification,
                    "records_file": "records.jsonl",
                    "truncated": self._truncated,
                }
                manifest_path = temporary / "manifest.json"
                with manifest_path.open("wb") as handle:
                    handle.write(_json_line(manifest, newline=False))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, final)
                return manifest
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise

    def status(self) -> _RollingStatus:
        with self._lock:
            try:
                self._ensure_loaded_locked()
            except OSError:
                pass
            return _RollingStatus(
                rolling_bytes=sum(segment.byte_count for segment in self._segments),
                rolling_window_seconds=self._rolling_window_ms // 1000,
                evicted_segments=self._evicted_segments,
                truncated=self._truncated,
            )

    def highest_ordering(self) -> tuple[int, int]:
        """Recover opaque sequence and request-label ordering across restarts."""

        with self._lock:
            self._ensure_loaded_locked()
            highest_sequence = 0
            highest_label = 0
            for segment in self._segments:
                for record in _read_jsonl_records(segment.path):
                    highest_sequence = max(highest_sequence, _safe_sequence(record.get("seq")))
                    request_label = record.get("request")
                    if isinstance(request_label, str) and _SAFE_REQUEST_LABELS.fullmatch(request_label):
                        highest_label = max(highest_label, int(request_label[1:]))
            return highest_sequence, highest_label

    def maintain(self) -> None:
        """Expire only complete rolling segments from the writer/control path."""

        with self._lock:
            self._ensure_loaded_locked()
            self._expire_locked(self._now_ms())

    def _ensure_loaded_locked(self) -> None:
        if self._loaded:
            return
        self._rolling_dir.mkdir(parents=True, exist_ok=True)
        segments: list[_Segment] = []
        highest = 0
        for path in self._rolling_dir.glob("segment-*.jsonl"):
            parsed = _parse_segment_name(path.name)
            if parsed is None:
                continue
            ordinal, start_at_ms = parsed
            _repair_partial_jsonl_tail(path)
            try:
                byte_count = path.stat().st_size
            except OSError:
                continue
            end_at_ms = _last_record_time_ms(path, start_at_ms)
            segments.append(
                _Segment(
                    path=path,
                    ordinal=ordinal,
                    start_at_ms=start_at_ms,
                    end_at_ms=end_at_ms,
                    byte_count=byte_count,
                )
            )
            highest = max(highest, ordinal)
        self._segments = sorted(segments, key=lambda item: (item.start_at_ms, item.ordinal))
        self._next_ordinal = highest + 1
        self._loaded = True
        self._expire_locked(self._now_ms())
        self._ensure_capacity_locked(0, self._now_ms())

    def _ensure_active_locked(self, at_ms: int) -> _Segment:
        if self._active is not None:
            return self._active
        ordinal = self._next_ordinal
        self._next_ordinal += 1
        path = self._rolling_dir / f"segment-{ordinal:08d}-{at_ms:013d}.jsonl"
        segment = _Segment(
            path=path,
            ordinal=ordinal,
            start_at_ms=at_ms,
            end_at_ms=at_ms,
            byte_count=0,
            sink=bounded_event_writer.JsonlFileSink(path),
        )
        self._segments.append(segment)
        self._active = segment
        return segment

    def _close_active_locked(self) -> None:
        if self._active is None:
            return
        self._active.sink = None
        self._active = None

    def _ensure_capacity_locked(self, required_bytes: int, at_ms: int) -> None:
        if required_bytes > self._rolling_max_bytes:
            self._truncated = True
            raise OSError("diagnostic record exceeds rolling storage cap")
        if required_bytes > self._max_segment_bytes:
            self._truncated = True
            raise OSError("diagnostic record exceeds segment cap")
        active = self._active
        if active is not None and (
            active.byte_count + required_bytes > self._max_segment_bytes
            or at_ms - active.start_at_ms >= self._segment_ms
        ):
            self._close_active_locked()
        while sum(segment.byte_count for segment in self._segments) + required_bytes > self._rolling_max_bytes:
            candidates = [segment for segment in self._segments if segment is not self._active]
            if not candidates:
                self._close_active_locked()
                candidates = list(self._segments)
            if not candidates:
                self._truncated = True
                raise OSError("diagnostic rolling storage cannot evict a complete segment")
            oldest = min(candidates, key=lambda item: (item.start_at_ms, item.ordinal))
            oldest.path.unlink()
            self._segments.remove(oldest)
            self._evicted_segments += 1
            self._truncated = True

    def _expire_locked(self, now_ms: int) -> None:
        cutoff = now_ms - self._rolling_window_ms
        if self._active is not None and self._active.start_at_ms < cutoff:
            self._close_active_locked()
        expired = [
            segment
            for segment in self._segments
            if segment is not self._active and segment.end_at_ms < cutoff
        ]
        for segment in expired:
            try:
                segment.path.unlink()
            except FileNotFoundError:
                pass
            self._segments.remove(segment)
            self._evicted_segments += 1
            self._truncated = True

    def _now_ms(self) -> int:
        return max(0, int(self._clock() * 1000))


def classify_frozen_records(records: Iterable[Mapping[str, Any]]) -> str:
    """Classify ordering only; content and exception text never participate."""

    ordered = sorted(records, key=lambda record: _safe_sequence(record.get("seq")))
    downstream_close = _first_sequence(ordered, "downstream_close")
    upstream_close = _first_sequence(ordered, "upstream_close")
    upstream_terminal = _first_sequence(ordered, "upstream_terminal")
    downstream_terminal = _first_sequence(ordered, "downstream_terminal")
    if upstream_terminal is not None and downstream_terminal is None:
        return "terminal-not-forwarded"
    if downstream_close is not None and (upstream_close is None or downstream_close < upstream_close):
        return "downstream-first"
    if upstream_close is not None and (downstream_close is None or upstream_close < downstream_close):
        return "upstream-first"
    return "unknown"


def _first_sequence(records: Iterable[Mapping[str, Any]], kind: str) -> int | None:
    for record in records:
        if record.get("kind") == kind:
            return _safe_sequence(record.get("seq"))
    return None


def _sanitize_fields(values: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key in ("attempt", "retry_budget", "checkpoint", "lines", "bytes", "elapsed_ms"):
        maximum = MAX_RECORD_ELAPSED_MS if key == "elapsed_ms" else MAX_RECORD_COUNTER
        value = _bounded_counter(values.get(key), maximum)
        if value is not None:
            fields[key] = value
    status = _status(values.get("status"))
    if status is not None:
        fields["status"] = status
    outcome = values.get("outcome")
    if isinstance(outcome, str) and outcome in _OUTCOMES:
        fields["outcome"] = outcome
    connection = values.get("connection_disposition")
    if isinstance(connection, str) and connection in _CONNECTION_DISPOSITIONS:
        fields["connection_disposition"] = connection
    failure_phase = _failure_phase(values.get("failure_phase"))
    if failure_phase is not None:
        fields["failure_phase"] = failure_phase
    provider = _provider(values.get("provider"))
    if provider is not None:
        fields["provider"] = provider
    route = _route(values.get("route"))
    if route is not None:
        fields["route"] = route
    model = _safe_model(values.get("model"))
    if model is not None:
        fields["model"] = model
    header_count_bucket = values.get("header_count_bucket")
    if isinstance(header_count_bucket, str) and header_count_bucket in _HEADER_COUNT_BUCKETS:
        fields["header_count_bucket"] = header_count_bucket
    content_length_bucket = values.get("content_length_bucket")
    if isinstance(content_length_bucket, str) and content_length_bucket in _CONTENT_LENGTH_BUCKETS:
        fields["content_length_bucket"] = content_length_bucket
    content_type_class = values.get("content_type_class")
    if isinstance(content_type_class, str) and content_type_class in _CONTENT_TYPE_CLASSES:
        fields["content_type_class"] = content_type_class
    reason = values.get("reason_category")
    if isinstance(reason, str) and reason in _MARKER_CATEGORIES:
        fields["reason_category"] = reason
    incident = values.get("incident")
    if isinstance(incident, str) and _SAFE_INCIDENT_IDS.fullmatch(incident):
        fields["incident"] = incident
    return fields


def _provider(value: Any) -> str | None:
    if isinstance(value, str) and value in _PROVIDERS:
        return value
    if value is None:
        return None
    return "unknown"


def _route(value: Any) -> str | None:
    if isinstance(value, str) and value in _ROUTES:
        return value
    if value is None:
        return None
    return "unknown"


def _safe_model(value: Any) -> str | None:
    if not isinstance(value, str) or not _SAFE_MODELS.fullmatch(value):
        return None
    return value


def _status(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
        return None
    return value


def _attempt(value: Any) -> int | None:
    return _bounded_counter(value, 1000)


def _bounded_counter(value: Any, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, maximum)


def _failure_phase(value: Any) -> str | None:
    if isinstance(value, str) and value in _FAILURE_PHASES:
        return value
    return None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _header_summary(headers: Any) -> tuple[str, str, str]:
    try:
        items = list(headers.items()) if headers is not None else []
    except Exception:
        return "unknown", "unknown", "unknown"
    count = len(items)
    count_bucket = "0" if count == 0 else "1-4" if count <= 4 else "5-16" if count <= 16 else "17+"
    content_type = None
    content_length = None
    for key, value in items:
        if not isinstance(key, str):
            continue
        lowered = key.lower()
        if lowered == "content-type" and isinstance(value, str):
            content_type = value.lower()
        elif lowered == "content-length":
            try:
                content_length = int(value)
            except (TypeError, ValueError):
                content_length = None
    if content_type is None:
        content_type_class = "absent"
    elif "text/event-stream" in content_type:
        content_type_class = "event-stream"
    elif "json" in content_type:
        content_type_class = "json"
    else:
        content_type_class = "other"
    if content_length is None or content_length < 0:
        length_bucket = "unknown"
    elif content_length == 0:
        length_bucket = "0"
    elif content_length <= 1024:
        length_bucket = "1-1k"
    elif content_length <= 64 * 1024:
        length_bucket = "1k-64k"
    elif content_length <= 1024 * 1024:
        length_bucket = "64k-1m"
    else:
        length_bucket = "1m+"
    return count_bucket, content_type_class, length_bucket


def _json_line(value: Mapping[str, Any], *, newline: bool = True) -> bytes:
    encoded = json.dumps(dict(value), ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return encoded + (b"\n" if newline else b"")


def _record_time_ms(record: bytes, fallback: int) -> int:
    try:
        value = json.loads(record)
        at_ms = value.get("at_ms") if isinstance(value, Mapping) else None
        return at_ms if isinstance(at_ms, int) and at_ms >= 0 else fallback
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback


def _read_jsonl_records(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sanitized = _sanitize_record(value)
                if sanitized is not None:
                    yield sanitized
    except OSError:
        return


def _sanitize_record(record: Any) -> dict[str, Any] | None:
    """Fail closed when reading recovered files or artifacts.

    Records written by this module are already allow-listed. Applying the same
    schema on reads prevents a corrupted or foreign JSONL line from becoming a
    privacy escape through a later frozen artifact.
    """

    if not isinstance(record, Mapping):
        return None
    kind = record.get("kind")
    sequence = _safe_sequence(record.get("seq"))
    at_ms = record.get("at_ms")
    if (
        record.get("schema_version") != SCHEMA_VERSION
        or not isinstance(kind, str)
        or kind not in _PHASES
        or sequence == MAX_RECORD_COUNTER
        or isinstance(at_ms, bool)
        or not isinstance(at_ms, int)
        or not 0 <= at_ms < MAX_RECORD_COUNTER
    ):
        return None
    sanitized: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "seq": sequence,
        "at_ms": at_ms,
        "kind": kind,
    }
    request_label = record.get("request")
    if isinstance(request_label, str) and _SAFE_REQUEST_LABELS.fullmatch(request_label):
        sanitized["request"] = request_label
    sanitized.update(_sanitize_fields(record))
    for key in ("overflow_records", "overflow_bytes", "failed_records", "failure_count"):
        value = _bounded_counter(record.get(key), MAX_RECORD_COUNTER)
        if value is not None:
            sanitized[key] = value
    return sanitized


def _last_record_time_ms(path: Path, fallback: int) -> int:
    last = fallback
    for record in _read_jsonl_records(path):
        at_ms = record.get("at_ms")
        if isinstance(at_ms, int):
            last = max(last, at_ms)
    return last


def _repair_partial_jsonl_tail(path: Path) -> None:
    try:
        with path.open("r+b") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if not size:
                return
            handle.seek(0)
            content = handle.read()
            if content.endswith(b"\n"):
                return
            newline = content.rfind(b"\n")
            handle.seek(0 if newline < 0 else newline + 1)
            handle.truncate()
            handle.flush()
    except OSError:
        return


def _parse_segment_name(name: str) -> tuple[int, int] | None:
    matched = re.fullmatch(r"segment-([0-9]{8})-([0-9]{13})\.jsonl", name)
    if matched is None:
        return None
    return int(matched.group(1)), int(matched.group(2))


def _safe_sequence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < MAX_RECORD_COUNTER:
        return MAX_RECORD_COUNTER
    return value


def _valid_manifest(manifest: Any, incident_id: Any) -> bool:
    classification = manifest.get("classification") if isinstance(manifest, Mapping) else None
    record_count = manifest.get("record_count") if isinstance(manifest, Mapping) else None
    reason_category = manifest.get("reason_category") if isinstance(manifest, Mapping) else None
    return (
        isinstance(manifest, Mapping)
        and manifest.get("schema_version") == SCHEMA_VERSION
        and manifest.get("artifact_version") == ARTIFACT_VERSION
        and manifest.get("complete") is True
        and isinstance(incident_id, str)
        and _SAFE_INCIDENT_IDS.fullmatch(incident_id) is not None
        and manifest.get("records_file") == "records.jsonl"
        and isinstance(record_count, int)
        and record_count >= 0
        and isinstance(reason_category, str)
        and reason_category in _MARKER_CATEGORIES
        and all(
            isinstance(manifest.get(key), int) and manifest[key] >= 0
            for key in ("marker_at_ms", "cutoff_at_ms", "created_at_ms")
        )
        and isinstance(manifest.get("truncated"), bool)
        and isinstance(classification, str)
        and classification in {
            "downstream-first",
            "upstream-first",
            "terminal-not-forwarded",
            "unknown",
        }
    )
