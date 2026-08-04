#!/usr/bin/env python3
"""Explicitly enabled, standalone Issue #62 live-evidence sidecar.

This module is evidence tooling. Nothing in the Gateway or Desktop imports it,
and invoking it without ``--enable-live-capture`` cannot open a listener.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlsplit
import uuid


SCHEMA = "codexhub.issue62.live-evidence-lane.v1"
VERIFICATION_SCOPE = "capture_only_not_qualification"
_HOPS = frozenset({"pre", "post"})
_OUTCOMES = frozenset({"complete", "incomplete"})
_FAILURES = frozenset(
    {
        "capture_record_exists",
        "capture_record_mutated",
        "capture_record_write_failed",
        "client_cancelled",
        "correlation_binding_invalid",
        "correlation_token_replayed",
        "downstream_cancelled",
        "forwarding_failed",
        "invalid_content_length",
        "request_incomplete",
        "request_overflow",
        "response_overflow",
        "server_start_failed",
        "server_thread_failed",
        "shutdown_drain_failed",
        "sse_frame_incomplete",
        "sse_frame_after_terminal",
        "sse_terminal_missing",
        "upstream_timeout",
    }
)
_CONTENT_TYPE_CLASSES = frozenset({"event-stream", "json", "other", "absent", "unknown"})
_TERMINAL_CLASSES = frozenset({"done", "error", "response.completed", "response.failed"})
_ROOT_FIELDS = frozenset(
    {
        "schema",
        "verification_scope",
        "capture_id",
        "run_nonce",
        "producer_hmac_sha256",
        "hop",
        "outcome",
        "failure",
        "status",
        "content_type_class",
        "request",
        "response",
        "sse",
    }
)
_BODY_FIELDS = frozenset({"bytes", "sha256", "hmac_sha256", "complete"})
_SSE_FIELDS = frozenset(
    {
        "complete",
        "frame_count",
        "frame_bytes",
        "sequence_sha256",
        "sequence_hmac_sha256",
        "terminal_classes",
    }
)
_CAPTURE_ID = re.compile(r"c[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CORRELATION_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_RUN_NONCE = _CORRELATION_TOKEN
_CORRELATION_BINDING = re.compile(r"([0-9a-f]{32})\.([0-9a-f]{64})\Z")
_CORRELATION_HEADER = "X-CodexHub-Issue62-Capture"


class ConfigurationError(ValueError):
    """A fixed-code startup configuration failure."""


class ArtifactValidationError(ValueError):
    """A fixed-code sanitized-artifact contract failure."""


class SidecarStartError(RuntimeError):
    """The isolated listener failed before it could accept evidence traffic."""


@dataclass(frozen=True)
class SidecarConfig:
    hop: str
    listen_host: str
    listen_port: int
    forward_base_url: str
    output_dir: Path
    hmac_key_file: Path
    max_request_bytes: int
    max_response_bytes: int
    connect_timeout_seconds: float
    read_timeout_seconds: float
    overall_timeout_seconds: float
    # Direct library callers may omit this and receive a fresh nonce.  The
    # command-line entrypoint requires an explicit nonce so pre/post hops can
    # be bound to the same operator-created run.
    run_nonce: str | None = None


class BodyFingerprint:
    """Incrementally fingerprint exact application-body bytes."""

    def __init__(self, key: bytes, domain: bytes) -> None:
        self._sha256 = hashlib.sha256()
        self._hmac_sha256 = hmac.new(key, domain + b"\0", hashlib.sha256)
        self._bytes = 0

    def update(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise TypeError("fingerprint_chunk_not_bytes")
        self._sha256.update(chunk)
        self._hmac_sha256.update(chunk)
        self._bytes += len(chunk)

    def snapshot(self, *, complete: bool) -> dict[str, Any]:
        return {
            "bytes": self._bytes,
            "sha256": self._sha256.hexdigest() if complete else None,
            "hmac_sha256": self._hmac_sha256.hexdigest() if complete else None,
            "complete": complete,
        }

    def complete(self) -> dict[str, Any]:
        return self.snapshot(complete=True)


class SseSequenceFingerprint:
    """Hash ordered complete SSE frames independent of transport chunking."""

    def __init__(self, key: bytes) -> None:
        self._sha256 = hashlib.sha256()
        self._hmac_sha256 = hmac.new(key, b"sse-sequence\0", hashlib.sha256)
        self._buffer = bytearray()
        self._frame_count = 0
        self._frame_bytes = 0
        self._terminal_classes: set[str] = set()
        self._terminal_seen = False
        self._invalid_after_terminal = False

    def update(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise TypeError("sse_chunk_not_bytes")
        self._buffer.extend(chunk)
        while True:
            boundary = self._next_boundary()
            if boundary is None:
                return
            index, width = boundary
            frame = bytes(self._buffer[:index])
            del self._buffer[: index + width]
            encoded = len(frame).to_bytes(8, "big") + frame
            self._sha256.update(encoded)
            self._hmac_sha256.update(encoded)
            self._frame_count += 1
            self._frame_bytes += len(frame)
            if self._terminal_seen:
                self._invalid_after_terminal = True
            terminal = _classify_sse_terminal(frame)
            if terminal is not None:
                if self._terminal_seen:
                    self._invalid_after_terminal = True
                self._terminal_seen = True
                self._terminal_classes.add(terminal)

    def _next_boundary(self) -> tuple[int, int] | None:
        candidates = [
            (index, len(marker))
            for marker in (b"\r\n\r\n", b"\n\n")
            if (index := self._buffer.find(marker)) >= 0
        ]
        return min(candidates) if candidates else None

    def complete(self) -> dict[str, Any]:
        complete = not self._buffer and self._terminal_seen and not self._invalid_after_terminal
        return self._snapshot(complete=complete)

    def incomplete(self) -> dict[str, Any]:
        return self._snapshot(complete=False)

    def _snapshot(self, *, complete: bool) -> dict[str, Any]:
        return {
            "complete": complete,
            "frame_count": self._frame_count,
            "frame_bytes": self._frame_bytes,
            "sequence_sha256": self._sha256.hexdigest() if complete else None,
            "sequence_hmac_sha256": self._hmac_sha256.hexdigest() if complete else None,
            "terminal_classes": sorted(self._terminal_classes),
        }

    @property
    def has_trailing_bytes(self) -> bool:
        return bool(self._buffer)

    @property
    def invalid_after_terminal(self) -> bool:
        return self._invalid_after_terminal


def _classify_sse_terminal(frame: bytes) -> str | None:
    event_name: bytes | None = None
    data_lines: list[bytes] = []
    for line in frame.splitlines():
        if line.startswith(b"event:"):
            event_name = line[6:].lstrip(b" ")
        elif line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip(b" "))
    if event_name == b"error":
        return "error"
    data = b"\n".join(data_lines)
    if data == b"[DONE]":
        return "done"
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    event_type = payload.get("type")
    if event_type in {"response.completed", "response.failed", "error"}:
        return str(event_type)
    return None


def validate_config(config: SidecarConfig) -> bytes:
    """Validate the isolated lane and return its already-provisioned HMAC key."""

    if config.hop not in _HOPS:
        raise ConfigurationError("hop_invalid")
    if config.run_nonce is not None and (
        not isinstance(config.run_nonce, str) or _RUN_NONCE.fullmatch(config.run_nonce) is None
    ):
        raise ConfigurationError("run_nonce_invalid")
    try:
        listen_address = ipaddress.ip_address(config.listen_host)
    except ValueError as error:
        raise ConfigurationError("listen_not_loopback") from error
    if not listen_address.is_loopback:
        raise ConfigurationError("listen_not_loopback")
    if isinstance(config.listen_port, bool) or not isinstance(config.listen_port, int):
        raise ConfigurationError("listen_port_invalid")
    if not 0 <= config.listen_port <= 65535:
        raise ConfigurationError("listen_port_invalid")

    target = urlsplit(config.forward_base_url)
    try:
        target_port = target.port
    except ValueError as error:
        raise ConfigurationError("forward_base_url_invalid") from error
    if (
        target.scheme not in {"http", "https"}
        or not target.hostname
        or target.username is not None
        or target.password is not None
        or target.query
        or target.fragment
        or (target_port is not None and not 1 <= target_port <= 65535)
    ):
        raise ConfigurationError("forward_base_url_invalid")

    for value, code in (
        (config.max_request_bytes, "max_request_bytes_invalid"),
        (config.max_response_bytes, "max_response_bytes_invalid"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigurationError(code)
    for value, code in (
        (config.connect_timeout_seconds, "connect_timeout_invalid"),
        (config.read_timeout_seconds, "read_timeout_invalid"),
        (config.overall_timeout_seconds, "overall_timeout_invalid"),
    ):
        if not _is_positive_timeout(value):
            raise ConfigurationError(code)

    try:
        key = Path(config.hmac_key_file).read_bytes()
    except OSError as error:
        raise ConfigurationError("hmac_key_unavailable") from error
    if not 32 <= len(key) <= 4096:
        raise ConfigurationError("hmac_key_invalid")
    return key


def _is_positive_timeout(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(numeric) and numeric > 0


def _require_exact_fields(value: Mapping[str, Any], expected: frozenset[str], code: str) -> None:
    if frozenset(value) != expected:
        raise ArtifactValidationError(code)


def _validate_digest(value: Any, code: str) -> None:
    if value is not None and (not isinstance(value, str) or _DIGEST.fullmatch(value) is None):
        raise ArtifactValidationError(code)


def _validate_body(value: Any, code: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ArtifactValidationError(code)
    _require_exact_fields(value, _BODY_FIELDS, code)
    if isinstance(value["bytes"], bool) or not isinstance(value["bytes"], int) or value["bytes"] < 0:
        raise ArtifactValidationError(code)
    if not isinstance(value["complete"], bool):
        raise ArtifactValidationError(code)
    _validate_digest(value["sha256"], code)
    _validate_digest(value["hmac_sha256"], code)
    if value["complete"] != (value["sha256"] is not None and value["hmac_sha256"] is not None):
        raise ArtifactValidationError(code)


def _validate_sse(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ArtifactValidationError("sse_invalid")
    _require_exact_fields(value, _SSE_FIELDS, "sse_fields_invalid")
    for key in ("frame_count", "frame_bytes"):
        if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0:
            raise ArtifactValidationError("sse_invalid")
    if not isinstance(value["complete"], bool):
        raise ArtifactValidationError("sse_invalid")
    _validate_digest(value["sequence_sha256"], "sse_invalid")
    _validate_digest(value["sequence_hmac_sha256"], "sse_invalid")
    has_digests = value["sequence_sha256"] is not None and value["sequence_hmac_sha256"] is not None
    if value["complete"] != has_digests:
        raise ArtifactValidationError("sse_invalid")
    if not isinstance(value["terminal_classes"], list) or any(
        item not in _TERMINAL_CLASSES for item in value["terminal_classes"]
    ):
        raise ArtifactValidationError("sse_invalid")


def validate_capture_record(record: Mapping[str, Any], hop: str) -> None:
    """Reject any field outside the deliberately tiny sanitized schema."""

    if not isinstance(record, Mapping):
        raise ArtifactValidationError("record_invalid")
    _require_exact_fields(record, _ROOT_FIELDS, "record_fields_invalid")
    if record["schema"] != SCHEMA or record["verification_scope"] != VERIFICATION_SCOPE:
        raise ArtifactValidationError("record_schema_invalid")
    if hop not in _HOPS or record["hop"] != hop:
        raise ArtifactValidationError("record_hop_invalid")
    capture_id = record["capture_id"]
    if not isinstance(capture_id, str) or _CAPTURE_ID.fullmatch(capture_id) is None:
        raise ArtifactValidationError("capture_id_invalid")
    run_nonce = record["run_nonce"]
    if not isinstance(run_nonce, str) or _RUN_NONCE.fullmatch(run_nonce) is None:
        raise ArtifactValidationError("run_nonce_invalid")
    producer_hmac = record["producer_hmac_sha256"]
    if not isinstance(producer_hmac, str) or _DIGEST.fullmatch(producer_hmac) is None:
        raise ArtifactValidationError("producer_binding_invalid")
    if record["outcome"] not in _OUTCOMES:
        raise ArtifactValidationError("record_outcome_invalid")
    if record["failure"] is not None and record["failure"] not in _FAILURES:
        raise ArtifactValidationError("record_failure_invalid")
    if (record["outcome"] == "complete") != (record["failure"] is None):
        raise ArtifactValidationError("record_completion_invalid")
    status = record["status"]
    if status is not None and (isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599):
        raise ArtifactValidationError("record_status_invalid")
    if record["content_type_class"] not in _CONTENT_TYPE_CLASSES:
        raise ArtifactValidationError("record_content_type_invalid")
    _validate_body(record["request"], "request_fingerprint_invalid")
    _validate_body(record["response"], "response_fingerprint_invalid")
    _validate_sse(record["sse"])
    if record["outcome"] == "complete":
        request = record["request"]
        response = record["response"]
        sse = record["sse"]
        if (
            not isinstance(request, Mapping)
            or request.get("complete") is not True
            or not isinstance(response, Mapping)
            or response.get("complete") is not True
        ):
            raise ArtifactValidationError("record_completion_invalid")
        if record["content_type_class"] == "event-stream":
            if not isinstance(sse, Mapping) or sse.get("complete") is not True:
                raise ArtifactValidationError("record_completion_invalid")
        elif sse is not None:
            raise ArtifactValidationError("record_completion_invalid")


def _capture_id_for_token(
    key: bytes, correlation_token: str, run_nonce: str | None = None
) -> str:
    if not isinstance(key, bytes) or not 32 <= len(key) <= 4096:
        raise ValueError("capture_key_invalid")
    if (
        not isinstance(correlation_token, str)
        or _CORRELATION_TOKEN.fullmatch(correlation_token) is None
    ):
        raise ValueError("correlation_token_invalid")
    context = b"issue62-capture\0"
    if run_nonce is not None:
        if _RUN_NONCE.fullmatch(run_nonce) is None:
            raise ValueError("run_nonce_invalid")
        context += run_nonce.encode("ascii") + b"\0"
    digest = hmac.new(key, context + correlation_token.encode("ascii"), hashlib.sha256).hexdigest()
    return "c" + digest[:32]


def _new_correlation_token() -> str:
    return uuid.uuid4().hex


def _correlation_binding(
    key: bytes, correlation_token: str, run_nonce: str | None = None
) -> str:
    if (
        not isinstance(correlation_token, str)
        or _CORRELATION_TOKEN.fullmatch(correlation_token) is None
    ):
        raise ValueError("correlation_token_invalid")
    context = b"issue62-correlation\0"
    if run_nonce is not None:
        if _RUN_NONCE.fullmatch(run_nonce) is None:
            raise ValueError("run_nonce_invalid")
        context += run_nonce.encode("ascii") + b"\0"
    signature = hmac.new(key, context + correlation_token.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{correlation_token}.{signature}"


def _correlation_token_from_header(
    key: bytes, value: str | None, run_nonce: str | None = None
) -> str | None:
    if not isinstance(value, str):
        return None
    match = _CORRELATION_BINDING.fullmatch(value)
    if match is None:
        return None
    correlation_token, signature = match.groups()
    context = b"issue62-correlation\0"
    if run_nonce is not None:
        if _RUN_NONCE.fullmatch(run_nonce) is None:
            return None
        context += run_nonce.encode("ascii") + b"\0"
    expected = hmac.new(key, context + correlation_token.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    return correlation_token


def _canonical_record_bytes(record: Mapping[str, Any]) -> bytes:
    payload = dict(record)
    payload.pop("producer_hmac_sha256", None)
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )


def _render_record_bytes(record: Mapping[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    ) + b"\n"


def _producer_hmac_sha256(
    key: bytes, record: Mapping[str, Any], run_nonce: str | None = None
) -> str:
    if not isinstance(key, bytes) or not 32 <= len(key) <= 4096:
        raise ValueError("capture_key_invalid")
    nonce = run_nonce if run_nonce is not None else record.get("run_nonce")
    hop = record.get("hop")
    capture_id = record.get("capture_id")
    if (
        not isinstance(nonce, str)
        or _RUN_NONCE.fullmatch(nonce) is None
        or not isinstance(hop, str)
        or hop not in _HOPS
        or not isinstance(capture_id, str)
        or _CAPTURE_ID.fullmatch(capture_id) is None
    ):
        raise ValueError("producer_binding_invalid")
    material = (
        b"issue62-producer\0"
        + nonce.encode("ascii")
        + b"\0"
        + hop.encode("ascii")
        + b"\0"
        + capture_id.encode("ascii")
        + b"\0"
        + _canonical_record_bytes(record)
    )
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def write_capture_record(
    output_dir: Path,
    hop: str,
    record: Mapping[str, Any],
    *,
    capture_key: bytes | None = None,
    correlation_token: str | None = None,
    run_nonce: str | None = None,
) -> Path:
    """Atomically publish one live-bound sanitized record and remove its partial."""

    validate_capture_record(record, hop)
    if capture_key is None or correlation_token is None:
        raise ArtifactValidationError("capture_binding_missing")
    expected_nonce = run_nonce if run_nonce is not None else record.get("run_nonce")
    if not isinstance(expected_nonce, str) or _RUN_NONCE.fullmatch(expected_nonce) is None:
        raise ArtifactValidationError("capture_binding_invalid")
    try:
        expected_capture_id = _capture_id_for_token(capture_key, correlation_token, expected_nonce)
    except (TypeError, ValueError):
        raise ArtifactValidationError("capture_binding_invalid") from None
    if not hmac.compare_digest(str(record["capture_id"]), expected_capture_id):
        raise ArtifactValidationError("capture_binding_invalid")
    if str(record["run_nonce"]) != expected_nonce:
        raise ArtifactValidationError("capture_binding_invalid")
    try:
        expected_producer_hmac = _producer_hmac_sha256(capture_key, record, expected_nonce)
    except (TypeError, ValueError):
        raise ArtifactValidationError("producer_binding_invalid") from None
    if not hmac.compare_digest(str(record["producer_hmac_sha256"]), expected_producer_hmac):
        raise ArtifactValidationError("producer_binding_invalid")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    capture_id = str(record["capture_id"])
    target = directory / f"{hop}-{capture_id}.json"
    if target.exists() or target.is_symlink():
        raise ArtifactValidationError("capture_record_exists")
    rendered = _render_record_bytes(record)
    # The capture id is deterministic for a correlation token, so two
    # concurrent handlers can legitimately race for the same destination.
    # Give each writer an independent temporary inode; sharing one ``.partial``
    # path lets a losing writer delete the winner's in-flight file.
    partial = directory / f".{hop}-{capture_id}.{uuid.uuid4().hex}.partial"
    try:
        with partial.open("x", encoding="ascii", newline="\n") as handle:
            handle.write(rendered.decode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        # Linking the fully written inode into place is atomic and refuses to
        # overwrite a record published by another handler.
        try:
            os.link(partial, target)
        except FileExistsError:
            raise ArtifactValidationError("capture_record_exists") from None
        partial.unlink()
        try:
            if target.read_bytes() != rendered:
                raise ArtifactValidationError("capture_record_mutated")
        except OSError:
            raise ArtifactValidationError("capture_record_write_failed") from None
    finally:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
    return target


_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def _content_type_class(value: str | None) -> str:
    if not value:
        return "absent"
    lowered = value.lower()
    if "text/event-stream" in lowered:
        return "event-stream"
    if "json" in lowered:
        return "json"
    return "other"


def _empty_record(
    hop: str,
    capture_id: str,
    failure: str,
    run_nonce: str,
    capture_key: bytes,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "verification_scope": VERIFICATION_SCOPE,
        "capture_id": capture_id,
        "run_nonce": run_nonce,
        "producer_hmac_sha256": "0" * 64,
        "hop": hop,
        "outcome": "incomplete",
        "failure": failure,
        "status": None,
        "content_type_class": "unknown",
        "request": None,
        "response": None,
        "sse": None,
    }
    record["producer_hmac_sha256"] = _producer_hmac_sha256(capture_key, record, run_nonce)
    return record


def _join_target_path(base_path: str, incoming_path: str) -> str:
    prefix = base_path.rstrip("/")
    suffix = incoming_path if incoming_path.startswith("/") else f"/{incoming_path}"
    return f"{prefix}{suffix}" or "/"


class _CaptureRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self) -> None:
        owner = self.server.capture_owner  # type: ignore[attr-defined]
        owner.handle_post(self)

    def log_message(self, format: str, *args: object) -> None:
        return None

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            owner = getattr(self.server, "capture_owner", None)
            if owner is not None:
                owner._finish_client(self.connection)


class _CaptureThreadingHttpServer(ThreadingHTTPServer):
    def process_request(self, request: Any, client_address: Any) -> None:
        owner = getattr(self, "capture_owner", None)
        if owner is not None:
            owner._register_client(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            if owner is not None:
                owner._finish_client(request)
            raise

    def handle_error(self, request: Any, client_address: Any) -> None:
        owner = getattr(self, "capture_owner", None)
        if owner is not None:
            owner._finish_client(request)
            owner._write_control_failure("client_cancelled")


class _ThreadingHttpServerV6(_CaptureThreadingHttpServer):
    address_family = socket.AF_INET6


class CaptureSidecarServer:
    """One explicitly constructed loopback capture hop."""

    def __init__(self, config: SidecarConfig) -> None:
        self._config = config
        self._key = validate_config(config)
        self._run_nonce = config.run_nonce or _new_correlation_token()
        self._target = urlsplit(config.forward_base_url)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._thread_failure: str | None = None
        self._active_condition = threading.Condition()
        self._active_connections: dict[
            socket.socket,
            http.client.HTTPConnection | None,
        ] = {}
        self._issued_tokens: set[str] = set()
        self._issued_lock = threading.Lock()
        self._published_records: dict[Path, bytes] = {}
        self._published_lock = threading.Lock()

    @property
    def listen_host(self) -> str:
        return self._config.listen_host

    @property
    def listen_port(self) -> int:
        if self._server is None:
            return self._config.listen_port
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        host = f"[{self.listen_host}]" if ":" in self.listen_host else self.listen_host
        return f"http://{host}:{self.listen_port}"

    def start(self) -> "CaptureSidecarServer":
        if self._server is not None:
            raise SidecarStartError("server_already_started")
        server_type = _ThreadingHttpServerV6 if ":" in self._config.listen_host else _CaptureThreadingHttpServer
        try:
            server = server_type(
                (self._config.listen_host, self._config.listen_port),
                _CaptureRequestHandler,
            )
            server.daemon_threads = True
            server.capture_owner = self  # type: ignore[attr-defined]
            thread = threading.Thread(
                target=self._serve,
                args=(server,),
                name=f"issue62-live-evidence-{self._config.hop}",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()
        except BaseException as error:
            if self._server is not None:
                self._server.server_close()
            self._server = None
            self._thread = None
            self._write_control_failure("server_start_failed")
            raise SidecarStartError("server_start_failed") from error
        return self

    def _serve(self, server: ThreadingHTTPServer) -> None:
        try:
            server.serve_forever(poll_interval=0.05)
        except BaseException:
            self._thread_failure = "server_thread_failed"
            self._write_control_failure("server_thread_failed")

    def shutdown(self) -> bool:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            if thread is not None and thread.is_alive():
                server.shutdown()
        self._close_active_connections()
        if server is not None:
            server.server_close()
        drain_timeout = min(self._config.overall_timeout_seconds, 5.0)
        drained = self._wait_for_active_connections(drain_timeout)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(max(0.1, self._config.overall_timeout_seconds), 5.0))
        thread_stopped = thread is None or not thread.is_alive()
        if drained and thread_stopped and not self._verify_published_records():
            self._thread_failure = "capture_record_mutated"
            self._write_control_failure("capture_record_mutated")
            drained = False
        return drained and thread_stopped

    def wait(self) -> bool:
        thread = self._thread
        if thread is None:
            return False
        while thread.is_alive():
            thread.join(timeout=0.25)
        return self._thread_failure is None

    def _write_control_failure(self, failure: str) -> None:
        correlation_token = _new_correlation_token()
        try:
            capture_id = _capture_id_for_token(self._key, correlation_token, self._run_nonce)
            record = _empty_record(
                self._config.hop,
                capture_id,
                failure,
                self._run_nonce,
                self._key,
            )
            record_path = write_capture_record(
                self._config.output_dir,
                self._config.hop,
                record,
                capture_key=self._key,
                correlation_token=correlation_token,
                run_nonce=self._run_nonce,
            )
            self._remember_published_record(record_path, _render_record_bytes(record))
        except Exception:
            return

    def _claim_token(self, correlation_token: str) -> bool:
        with self._issued_lock:
            if correlation_token in self._issued_tokens:
                return False
            self._issued_tokens.add(correlation_token)
            return True

    def _remember_published_record(self, path: Path, rendered: bytes) -> None:
        with self._published_lock:
            self._published_records[path.resolve()] = bytes(rendered)

    def _verify_published_records(self) -> bool:
        with self._published_lock:
            snapshots = dict(self._published_records)
        for path, expected in snapshots.items():
            try:
                if path.read_bytes() != expected:
                    return False
            except OSError:
                return False
        return True

    def _register_client(self, connection: socket.socket) -> None:
        with self._active_condition:
            self._active_connections[connection] = None
            self._active_condition.notify_all()

    def _set_active_upstream(
        self,
        connection: socket.socket,
        upstream: http.client.HTTPConnection | None,
    ) -> None:
        with self._active_condition:
            if connection in self._active_connections:
                self._active_connections[connection] = upstream

    def _finish_client(self, connection: socket.socket) -> None:
        with self._active_condition:
            self._active_connections.pop(connection, None)
            self._active_condition.notify_all()

    def _close_active_connections(self) -> None:
        with self._active_condition:
            active = list(self._active_connections.items())
        for connection, upstream in active:
            if upstream is not None:
                if upstream.sock is not None:
                    try:
                        upstream.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                try:
                    upstream.close()
                except OSError:
                    pass
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def _wait_for_active_connections(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._active_condition:
            while self._active_connections:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._active_condition.wait(timeout=remaining)
            return True

    def handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        correlation_token = _new_correlation_token()
        capture_id = _capture_id_for_token(self._key, correlation_token, self._run_nonce)
        started_at = time.monotonic()
        request_fingerprint = BodyFingerprint(self._key, b"request-body")
        response_fingerprint: BodyFingerprint | None = None
        sse_fingerprint: SseSequenceFingerprint | None = None
        status: int | None = None
        content_class = "unknown"
        failure: str | None = None
        upstream: http.client.HTTPConnection | None = None
        request_complete = False
        response_complete = False
        try:
            if self._config.hop == "post":
                bound_token = _correlation_token_from_header(
                    self._key,
                    handler.headers.get(_CORRELATION_HEADER),
                    self._run_nonce,
                )
                if bound_token is None:
                    failure = "correlation_binding_invalid"
                    self._send_bounded_error(handler, 400)
                    return
                correlation_token = bound_token
                capture_id = _capture_id_for_token(self._key, correlation_token, self._run_nonce)
                if not self._claim_token(correlation_token):
                    failure = "correlation_token_replayed"
                    # Preserve the original capture record and publish this
                    # rejected attempt under a fresh, run-bound ID.
                    correlation_token = _new_correlation_token()
                    capture_id = _capture_id_for_token(
                        self._key, correlation_token, self._run_nonce
                    )
                    self._send_bounded_error(handler, 409)
                    return
            raw_length = handler.headers.get("Content-Length")
            try:
                content_length = int(raw_length) if raw_length is not None else -1
            except ValueError:
                content_length = -1
            if content_length < 0:
                failure = "invalid_content_length"
                self._send_bounded_error(handler, 400)
                return
            if content_length > self._config.max_request_bytes:
                failure = "request_overflow"
                self._send_bounded_error(handler, 413)
                return
            handler.connection.settimeout(
                min(self._config.read_timeout_seconds, self._config.overall_timeout_seconds)
            )
            request_body = handler.rfile.read(content_length)
            request_fingerprint.update(request_body)
            if len(request_body) != content_length:
                failure = "request_incomplete"
                self._send_bounded_error(handler, 400)
                return
            request_complete = True
            self._check_deadline(started_at)

            connection_type = (
                http.client.HTTPSConnection
                if self._target.scheme == "https"
                else http.client.HTTPConnection
            )
            upstream = connection_type(
                self._target.hostname,
                self._target.port,
                timeout=self._config.connect_timeout_seconds,
            )
            self._set_active_upstream(handler.connection, upstream)
            headers = {
                name: value
                for name, value in handler.headers.items()
                if name.lower()
                not in _HOP_BY_HOP_HEADERS | {"host", "content-length", _CORRELATION_HEADER.lower()}
            }
            if self._config.hop == "pre":
                headers[_CORRELATION_HEADER] = _correlation_binding(
                    self._key, correlation_token, self._run_nonce
                )
            target_path = _join_target_path(self._target.path, handler.path)
            upstream.request("POST", target_path, body=request_body, headers=headers)
            if upstream.sock is not None:
                upstream.sock.settimeout(self._remaining_timeout(started_at))
            upstream_response = upstream.getresponse()
            status = int(upstream_response.status)
            content_class = _content_type_class(upstream_response.getheader("Content-Type"))
            response_fingerprint = BodyFingerprint(self._key, b"response-body")
            if content_class == "event-stream":
                sse_fingerprint = SseSequenceFingerprint(self._key)

            handler.send_response(status)
            for name, value in upstream_response.getheaders():
                if name.lower() not in _HOP_BY_HOP_HEADERS | {"content-length"}:
                    handler.send_header(name, value)
            handler.send_header("Connection", "close")
            handler.end_headers()

            while True:
                self._check_deadline(started_at)
                if upstream.sock is not None:
                    upstream.sock.settimeout(self._remaining_timeout(started_at))
                chunk = upstream_response.read(64 * 1024)
                if not chunk:
                    break
                if response_fingerprint.snapshot(complete=False)["bytes"] + len(chunk) > self._config.max_response_bytes:
                    failure = "response_overflow"
                    return
                response_fingerprint.update(chunk)
                if sse_fingerprint is not None:
                    sse_fingerprint.update(chunk)
                try:
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    failure = "downstream_cancelled"
                    return
            response_complete = True
            if sse_fingerprint is not None:
                sse_result = sse_fingerprint.complete()
                if not sse_result["complete"]:
                    if sse_fingerprint.invalid_after_terminal:
                        failure = "sse_frame_after_terminal"
                    elif sse_fingerprint.has_trailing_bytes:
                        failure = "sse_frame_incomplete"
                    else:
                        failure = "sse_terminal_missing"
                    response_complete = False
        except (TimeoutError, socket.timeout):
            failure = "upstream_timeout"
            if status is None:
                self._send_bounded_error(handler, 504)
        except (BrokenPipeError, ConnectionResetError):
            failure = "client_cancelled"
        except OSError:
            failure = "forwarding_failed"
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass
                self._set_active_upstream(handler.connection, None)
            outcome = "complete" if failure is None and request_complete and response_complete else "incomplete"
            if outcome == "incomplete" and failure is None:
                failure = "forwarding_failed"
            record = {
                "schema": SCHEMA,
                "verification_scope": VERIFICATION_SCOPE,
                "capture_id": capture_id,
                "run_nonce": self._run_nonce,
                "producer_hmac_sha256": "0" * 64,
                "hop": self._config.hop,
                "outcome": outcome,
                "failure": failure,
                "status": status,
                "content_type_class": content_class,
                "request": request_fingerprint.snapshot(complete=request_complete),
                "response": (
                    response_fingerprint.snapshot(complete=response_complete)
                    if response_fingerprint is not None
                    else None
                ),
                "sse": (
                    (
                        sse_fingerprint.complete()
                        if response_complete
                        else sse_fingerprint.incomplete()
                    )
                    if sse_fingerprint is not None
                    else None
                ),
            }
            record["producer_hmac_sha256"] = _producer_hmac_sha256(
                self._key, record, self._run_nonce
            )
            try:
                record_path = write_capture_record(
                    self._config.output_dir,
                    self._config.hop,
                    record,
                    capture_key=self._key,
                    correlation_token=correlation_token,
                    run_nonce=self._run_nonce,
                )
                self._remember_published_record(
                    record_path,
                    _render_record_bytes(record),
                )
            except ArtifactValidationError as error:
                failure_code = str(error)
                if failure_code not in _FAILURES:
                    failure_code = "capture_record_write_failed"
                # A duplicate deterministic capture id is a real failed
                # attempt, not a benign no-op.  Surface a fresh, sanitized
                # failure record so the run cannot silently lose evidence.
                self._thread_failure = failure_code
                self._write_control_failure(failure_code)
            except Exception:
                self._thread_failure = "capture_record_write_failed"
                self._write_control_failure("capture_record_write_failed")
                return

    def _check_deadline(self, started_at: float) -> None:
        if time.monotonic() - started_at > self._config.overall_timeout_seconds:
            raise TimeoutError("capture_deadline")

    def _remaining_timeout(self, started_at: float) -> float:
        remaining = self._config.overall_timeout_seconds - (time.monotonic() - started_at)
        if remaining <= 0:
            raise TimeoutError("capture_deadline")
        return min(self._config.read_timeout_seconds, remaining)

    @staticmethod
    def _send_bounded_error(handler: BaseHTTPRequestHandler, status: int) -> None:
        body = b"isolated evidence sidecar rejected the request\n"
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "text/plain; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(body)
        except OSError:
            return


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an isolated Issue #62 live-evidence sidecar.")
    parser.add_argument("--enable-live-capture", action="store_true")
    parser.add_argument("--hop", choices=sorted(_HOPS), required=True)
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--forward-base-url", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--hmac-key-file", type=Path, required=True)
    parser.add_argument("--max-request-bytes", type=int, required=True)
    parser.add_argument("--max-response-bytes", type=int, required=True)
    parser.add_argument("--connect-timeout-seconds", type=float, required=True)
    parser.add_argument("--read-timeout-seconds", type=float, required=True)
    parser.add_argument("--overall-timeout-seconds", type=float, required=True)
    parser.add_argument("--run-nonce", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--enable-live-capture" not in arguments:
        print("live capture is disabled; pass --enable-live-capture explicitly", file=sys.stderr)
        return 2
    options = _build_parser().parse_args(arguments)
    config = SidecarConfig(
        hop=options.hop,
        listen_host=options.listen_host,
        listen_port=options.listen_port,
        forward_base_url=options.forward_base_url,
        output_dir=options.output_dir,
        hmac_key_file=options.hmac_key_file,
        max_request_bytes=options.max_request_bytes,
        max_response_bytes=options.max_response_bytes,
        connect_timeout_seconds=options.connect_timeout_seconds,
        read_timeout_seconds=options.read_timeout_seconds,
        overall_timeout_seconds=options.overall_timeout_seconds,
        run_nonce=options.run_nonce,
    )
    if config.listen_port == 0:
        print("listen_port_zero_not_allowed", file=sys.stderr)
        return 2
    try:
        validate_config(config)
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2
    server = CaptureSidecarServer(config)
    result_code = 1
    failure_code: str | None = None
    try:
        server.start()
        if server.wait():
            result_code = 0
        else:
            failure_code = "server_thread_failed"
    except KeyboardInterrupt:
        result_code = 0
    except SidecarStartError as error:
        failure_code = str(error)
    finally:
        if not server.shutdown():
            result_code = 1
            failure_code = "shutdown_drain_failed"
    if failure_code is not None:
        print(failure_code, file=sys.stderr)
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
