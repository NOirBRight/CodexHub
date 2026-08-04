from __future__ import annotations

import dataclasses
from contextlib import contextmanager
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import socket
import struct
import sys
import threading
import time
from typing import Iterator

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "capture_issue_62_live_evidence.py"
SPEC = importlib.util.spec_from_file_location("capture_issue_62_live_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
sidecar = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sidecar
SPEC.loader.exec_module(sidecar)

KEY = b"h" * 32
CORRELATION_TOKEN = "1" * 32
RUN_NONCE = "a" * 32


def _capture_id(correlation_token: str = CORRELATION_TOKEN) -> str:
    return sidecar._capture_id_for_token(KEY, correlation_token, RUN_NONCE)


def _bound_record(record: dict[str, object], *, key: bytes = KEY) -> dict[str, object]:
    record["run_nonce"] = RUN_NONCE
    record["producer_hmac_sha256"] = "0" * 64
    record["producer_hmac_sha256"] = sidecar._producer_hmac_sha256(key, record, RUN_NONCE)
    return record


def test_capture_id_is_deterministically_bound_to_correlation_token() -> None:
    assert _capture_id() == _capture_id(CORRELATION_TOKEN)
    assert _capture_id() != _capture_id("2" * 32)


def test_correlation_binding_is_bound_to_the_current_run_nonce() -> None:
    header = sidecar._correlation_binding(KEY, CORRELATION_TOKEN, RUN_NONCE)

    assert sidecar._correlation_token_from_header(KEY, header, RUN_NONCE) == CORRELATION_TOKEN
    assert sidecar._correlation_token_from_header(KEY, header, "b" * 32) is None


class _FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        self.server.observed_request = self.rfile.read(length)  # type: ignore[attr-defined]
        self.server.observed_headers = {  # type: ignore[attr-defined]
            name.lower(): value for name, value in self.headers.items()
        }
        body = self.server.response_body  # type: ignore[attr-defined]
        delay = self.server.response_delay  # type: ignore[attr-defined]
        if delay:
            time.sleep(delay)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk_size = self.server.response_chunk_size  # type: ignore[attr-defined]
        for offset in range(0, len(body), chunk_size):
            chunk = body[offset : offset + chunk_size]
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except OSError:
                return
            if self.server.chunk_delay:  # type: ignore[attr-defined]
                time.sleep(self.server.chunk_delay)  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        return None


@contextmanager
def _fake_upstream(
    response_body: bytes,
    *,
    response_delay: float = 0.0,
    chunk_size: int = 36,
    chunk_delay: float = 0.0,
) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstreamHandler)
    server.response_body = response_body  # type: ignore[attr-defined]
    server.observed_request = None  # type: ignore[attr-defined]
    server.observed_headers = {}  # type: ignore[attr-defined]
    server.response_delay = response_delay  # type: ignore[attr-defined]
    server.response_chunk_size = chunk_size  # type: ignore[attr-defined]
    server.chunk_delay = chunk_delay  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def _running_sidecar(config):
    server = sidecar.CaptureSidecarServer(config)
    server.start()
    try:
        yield server
    finally:
        server.shutdown()


def _read_only_record(output_dir: Path) -> dict[str, object]:
    records = list(output_dir.glob("*.json"))
    assert len(records) == 1
    return json.loads(records[0].read_text(encoding="utf-8"))


def _wait_for_record(output_dir: Path, timeout: float = 3.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = list(output_dir.glob("*.json"))
        if records:
            assert len(records) == 1
            return json.loads(records[0].read_text(encoding="utf-8"))
        time.sleep(0.01)
    raise AssertionError("capture record was not published")


def _sidecar_config(
    tmp_path: Path,
    hmac_key_file: Path,
    forward_base_url: str,
    **overrides: object,
):
    values: dict[str, object] = {
        "hop": "pre",
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "forward_base_url": forward_base_url,
        "output_dir": tmp_path / "records",
        "hmac_key_file": hmac_key_file,
        "max_request_bytes": 4096,
        "max_response_bytes": 8192,
        "connect_timeout_seconds": 1.0,
        "read_timeout_seconds": 1.0,
        "overall_timeout_seconds": 3.0,
        "run_nonce": RUN_NONCE,
    }
    values.update(overrides)
    return sidecar.SidecarConfig(**values)


@pytest.fixture
def hmac_key_file(tmp_path: Path) -> Path:
    path = tmp_path / "capture.key"
    path.write_bytes(b"k" * 32)
    return path


@pytest.fixture
def base_config(tmp_path: Path, hmac_key_file: Path):
    return sidecar.SidecarConfig(
        hop="pre",
        listen_host="127.0.0.1",
        listen_port=0,
        forward_base_url="http://127.0.0.1:1",
        output_dir=tmp_path / "records",
        hmac_key_file=hmac_key_file,
        max_request_bytes=1024,
        max_response_bytes=4096,
        connect_timeout_seconds=1.0,
        read_timeout_seconds=1.0,
        overall_timeout_seconds=2.0,
        run_nonce=RUN_NONCE,
    )


def test_main_requires_explicit_enable(capsys: pytest.CaptureFixture[str]) -> None:
    assert sidecar.main([]) == 2
    assert "live capture is disabled" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10"])
def test_config_rejects_non_loopback(host: str, base_config) -> None:
    with pytest.raises(sidecar.ConfigurationError, match="listen_not_loopback"):
        sidecar.validate_config(dataclasses.replace(base_config, listen_host=host))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:not-a-port",
    ],
)
def test_config_rejects_malformed_or_out_of_range_forward_port(url: str, base_config) -> None:
    with pytest.raises(sidecar.ConfigurationError, match="forward_base_url_invalid"):
        sidecar.validate_config(dataclasses.replace(base_config, forward_base_url=url))


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("connect_timeout_seconds", float("nan"), "connect_timeout_invalid"),
        ("read_timeout_seconds", float("inf"), "read_timeout_invalid"),
        ("overall_timeout_seconds", float("-inf"), "overall_timeout_invalid"),
    ],
)
def test_config_rejects_nonfinite_timeouts(field: str, value: float, code: str, base_config) -> None:
    with pytest.raises(sidecar.ConfigurationError, match=code):
        sidecar.validate_config(dataclasses.replace(base_config, **{field: value}))


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("connect_timeout_seconds", "connect_timeout_invalid"),
        ("read_timeout_seconds", "read_timeout_invalid"),
        ("overall_timeout_seconds", "overall_timeout_invalid"),
    ],
)
def test_config_rejects_arbitrarily_large_positive_integer_timeout(
    field: str,
    code: str,
    base_config,
) -> None:
    with pytest.raises(sidecar.ConfigurationError, match=code):
        sidecar.validate_config(dataclasses.replace(base_config, **{field: 10**1000}))


def test_atomic_record_is_sanitized_and_leaves_no_partial(tmp_path: Path) -> None:
    record = _bound_record({
        "schema": "codexhub.issue62.live-evidence-lane.v1",
        "verification_scope": "capture_only_not_qualification",
        "capture_id": _capture_id(),
        "hop": "pre",
        "outcome": "complete",
        "failure": None,
        "status": 200,
        "content_type_class": "json",
        "request": {
            "bytes": 2,
            "sha256": "a" * 64,
            "hmac_sha256": "b" * 64,
            "complete": True,
        },
        "response": {
            "bytes": 3,
            "sha256": "c" * 64,
            "hmac_sha256": "d" * 64,
            "complete": True,
        },
        "sse": None,
    })

    record_path = sidecar.write_capture_record(
        tmp_path,
        "pre",
        record,
        capture_key=KEY,
        correlation_token=CORRELATION_TOKEN,
    )

    assert json.loads(record_path.read_text(encoding="utf-8")) == record
    assert record_path.name.startswith("pre-")
    assert record_path.suffix == ".json"
    assert not list(tmp_path.glob("*.partial"))


def test_atomic_record_does_not_overwrite_same_correlation_record(
    tmp_path: Path,
) -> None:
    record = _bound_record({
        "schema": "codexhub.issue62.live-evidence-lane.v1",
        "verification_scope": "capture_only_not_qualification",
        "capture_id": _capture_id(),
        "hop": "pre",
        "outcome": "complete",
        "failure": None,
        "status": 200,
        "content_type_class": "json",
        "request": {"bytes": 1, "sha256": "a" * 64, "hmac_sha256": "b" * 64, "complete": True},
        "response": {"bytes": 1, "sha256": "c" * 64, "hmac_sha256": "d" * 64, "complete": True},
        "sse": None,
    })

    first = sidecar.write_capture_record(
        tmp_path,
        "pre",
        record,
        capture_key=KEY,
        correlation_token=CORRELATION_TOKEN,
    )
    with pytest.raises(sidecar.ArtifactValidationError, match="capture_record_exists"):
        sidecar.write_capture_record(
            tmp_path,
            "pre",
            record,
            capture_key=KEY,
            correlation_token=CORRELATION_TOKEN,
        )
    assert json.loads(first.read_text(encoding="utf-8")) == record
    assert not list(tmp_path.glob("*.partial"))


def test_atomic_record_rejects_prebuilt_record_without_live_capture_binding(tmp_path: Path) -> None:
    record = _bound_record({
        "schema": "codexhub.issue62.live-evidence-lane.v1",
        "verification_scope": "capture_only_not_qualification",
        "capture_id": _capture_id("2" * 32),
        "hop": "pre",
        "outcome": "complete",
        "failure": None,
        "status": 200,
        "content_type_class": "json",
        "request": {
            "bytes": 2,
            "sha256": "a" * 64,
            "hmac_sha256": "b" * 64,
            "complete": True,
        },
        "response": {
            "bytes": 3,
            "sha256": "c" * 64,
            "hmac_sha256": "d" * 64,
            "complete": True,
        },
        "sse": None,
    })

    with pytest.raises(sidecar.ArtifactValidationError, match="capture_binding_missing"):
        sidecar.write_capture_record(tmp_path, "pre", record)


def test_atomic_record_rejects_capture_id_from_different_live_correlation(tmp_path: Path) -> None:
    record = _bound_record({
        "schema": "codexhub.issue62.live-evidence-lane.v1",
        "verification_scope": "capture_only_not_qualification",
        "capture_id": _capture_id("2" * 32),
        "hop": "pre",
        "outcome": "complete",
        "failure": None,
        "status": 200,
        "content_type_class": "json",
        "request": {
            "bytes": 2,
            "sha256": "a" * 64,
            "hmac_sha256": "b" * 64,
            "complete": True,
        },
        "response": {
            "bytes": 3,
            "sha256": "c" * 64,
            "hmac_sha256": "d" * 64,
            "complete": True,
        },
        "sse": None,
    })

    with pytest.raises(sidecar.ArtifactValidationError, match="capture_binding_invalid"):
        sidecar.write_capture_record(
            tmp_path,
            "pre",
            record,
            capture_key=KEY,
            correlation_token=CORRELATION_TOKEN,
        )


def test_atomic_record_rejects_unapproved_fields(tmp_path: Path) -> None:
    with pytest.raises(sidecar.ArtifactValidationError, match="record_fields_invalid"):
        sidecar.write_capture_record(
            tmp_path,
            "pre",
            {
                "schema": "codexhub.issue62.live-evidence-lane.v1",
                "verification_scope": "capture_only_not_qualification",
                "capture_id": "c" + "2" * 32,
                "run_nonce": RUN_NONCE,
                "producer_hmac_sha256": "0" * 64,
                "hop": "pre",
                "outcome": "incomplete",
                "failure": "forwarding_failed",
                "status": None,
                "content_type_class": "unknown",
                "request": None,
                "response": None,
                "sse": None,
                "authorization": "Bearer forbidden",
            },
        )


def test_atomic_record_rejects_inconsistent_sse_completion(tmp_path: Path) -> None:
    with pytest.raises(sidecar.ArtifactValidationError, match="sse_invalid"):
        sidecar.write_capture_record(
            tmp_path,
            "post",
            {
                "schema": "codexhub.issue62.live-evidence-lane.v1",
                "verification_scope": "capture_only_not_qualification",
                "capture_id": "c" + "3" * 32,
                "run_nonce": RUN_NONCE,
                "producer_hmac_sha256": "0" * 64,
                "hop": "post",
                "outcome": "incomplete",
                "failure": "sse_terminal_missing",
                "status": 200,
                "content_type_class": "event-stream",
                "request": None,
                "response": None,
                "sse": {
                    "complete": False,
                    "frame_count": 1,
                    "frame_bytes": 12,
                    "sequence_sha256": "e" * 64,
                    "sequence_hmac_sha256": "f" * 64,
                    "terminal_classes": [],
                },
            },
        )


@pytest.mark.parametrize(
    ("content_type_class", "response", "sse"),
    [
        ("json", None, None),
        (
            "event-stream",
            {
                "bytes": 3,
                "sha256": "c" * 64,
                "hmac_sha256": "d" * 64,
                "complete": True,
            },
            None,
        ),
    ],
)
def test_complete_record_requires_complete_response_and_sse_when_streaming(
    tmp_path: Path,
    content_type_class: str,
    response: object,
    sse: object,
) -> None:
    with pytest.raises(sidecar.ArtifactValidationError, match="record_completion_invalid"):
        sidecar.write_capture_record(
            tmp_path,
            "pre",
            {
                "schema": "codexhub.issue62.live-evidence-lane.v1",
                "verification_scope": "capture_only_not_qualification",
                "capture_id": "c" + ("4" if response is None else "5") * 32,
                "run_nonce": RUN_NONCE,
                "producer_hmac_sha256": "0" * 64,
                "hop": "pre",
                "outcome": "complete",
                "failure": None,
                "status": 200,
                "content_type_class": content_type_class,
                "request": {
                    "bytes": 2,
                    "sha256": "a" * 64,
                    "hmac_sha256": "b" * 64,
                    "complete": True,
                },
                "response": response,
                "sse": sse,
            },
        )


def test_cli_rejects_ephemeral_port(
    tmp_path: Path,
    hmac_key_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = sidecar.main(
        [
            "--enable-live-capture",
            "--hop",
            "pre",
            "--listen-host",
            "127.0.0.1",
            "--listen-port",
            "0",
            "--forward-base-url",
            "http://127.0.0.1:1",
            "--output-dir",
            str(tmp_path / "records"),
            "--hmac-key-file",
            str(hmac_key_file),
            "--max-request-bytes",
            "1024",
            "--max-response-bytes",
            "4096",
            "--connect-timeout-seconds",
            "1",
            "--read-timeout-seconds",
            "1",
            "--overall-timeout-seconds",
            "2",
            "--run-nonce",
            RUN_NONCE,
        ]
    )

    assert result == 2
    assert capsys.readouterr().err.strip() == "listen_port_zero_not_allowed"


def test_body_fingerprint_hashes_every_byte() -> None:
    observed = sidecar.BodyFingerprint(KEY, b"request-body")
    observed.update(b"abc")
    observed.update(b"\x00def")

    assert observed.complete() == {
        "bytes": 7,
        "sha256": hashlib.sha256(b"abc\x00def").hexdigest(),
        "hmac_sha256": hmac.new(
            KEY,
            b"request-body\0abc\x00def",
            hashlib.sha256,
        ).hexdigest(),
        "complete": True,
    }


def _consume_sse(chunks: list[bytes]) -> dict[str, object]:
    observed = sidecar.SseSequenceFingerprint(KEY)
    for chunk in chunks:
        observed.update(chunk)
    return observed.complete()


def test_sse_sequence_digest_is_independent_of_transport_chunks() -> None:
    wire = (
        b'data: {"type":"response.output_text.delta","delta":"secret"}\n\n'
        b'data: {"type":"response.completed"}\n\n'
    )

    one = _consume_sse([wire])
    split = _consume_sse([wire[:5], wire[5:41], wire[41:]])

    assert split == one
    assert split["complete"] is True
    assert split["frame_count"] == 2
    assert split["terminal_classes"] == ["response.completed"]
    assert "secret" not in json.dumps(split, sort_keys=True)


def test_sse_sequence_fails_closed_without_terminal_or_complete_frame() -> None:
    missing_terminal = _consume_sse([b'data: {"type":"response.output_text.delta"}\n\n'])
    incomplete_frame = _consume_sse([b'data: {"type":"response.completed"}\n'])

    assert missing_terminal["complete"] is False
    assert incomplete_frame["complete"] is False
    assert missing_terminal["sequence_sha256"] is None
    assert missing_terminal["sequence_hmac_sha256"] is None
    assert incomplete_frame["sequence_sha256"] is None
    assert incomplete_frame["sequence_hmac_sha256"] is None


def test_sse_sequence_fails_closed_when_any_frame_follows_terminal() -> None:
    observed = _consume_sse(
        [
            b'data: {"type":"response.completed"}\n\n'
            b'data: {"type":"response.output_text.delta","delta":"after"}\n\n'
        ]
    )

    assert observed["complete"] is False
    assert observed["sequence_sha256"] is None
    assert observed["sequence_hmac_sha256"] is None
    assert observed["terminal_classes"] == ["response.completed"]


def test_terminal_followed_by_frame_uses_fixed_failure_code(
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    response_body = (
        b'data: {"type":"response.completed"}\n\n'
        b'data: {"type":"response.output_text.delta","delta":"after"}\n\n'
    )
    with _fake_upstream(response_body) as upstream:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        config = _sidecar_config(tmp_path, hmac_key_file, upstream_url)
        with _running_sidecar(config) as server:
            connection = http.client.HTTPConnection(server.listen_host, server.listen_port, timeout=2)
            connection.request("POST", "/responses", body=b"{}")
            response = connection.getresponse()
            response.read()
            connection.close()

    record = _read_only_record(config.output_dir)
    assert record["outcome"] == "incomplete"
    assert record["failure"] == "sse_frame_after_terminal"
    assert record["response"]["complete"] is False  # type: ignore[index]
    assert record["sse"]["complete"] is False  # type: ignore[index]


def test_two_hops_capture_matching_complete_request_response_and_sse(
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    request_body = b'{"input":"private prompt","stream":true}'
    sse_body = (
        b'data: {"type":"response.output_text.delta","delta":"private output"}\n\n'
        b'data: {"type":"response.completed"}\n\n'
    )
    post_output = tmp_path / "post-records"
    pre_output = tmp_path / "pre-records"

    with _fake_upstream(sse_body) as upstream:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        post_config = sidecar.SidecarConfig(
            hop="post",
            listen_host="127.0.0.1",
            listen_port=0,
            forward_base_url=upstream_url,
            output_dir=post_output,
            hmac_key_file=hmac_key_file,
            max_request_bytes=4096,
            max_response_bytes=8192,
            connect_timeout_seconds=1.0,
            read_timeout_seconds=1.0,
            overall_timeout_seconds=3.0,
            run_nonce=RUN_NONCE,
        )

        with _running_sidecar(post_config) as post:
            pre_config = dataclasses.replace(
                post_config,
                hop="pre",
                forward_base_url=post.url,
                output_dir=pre_output,
            )
            with _running_sidecar(pre_config) as pre:
                connection = http.client.HTTPConnection(
                    pre.listen_host,
                    pre.listen_port,
                    timeout=3,
                )
                connection.request(
                    "POST",
                    "/responses?opaque=wire-id",
                    body=request_body,
                    headers={
                        "Authorization": "Bearer forbidden-token",
                        "Content-Type": "application/json",
                        "X-CodexHub-Issue62-Capture": ("0" * 32) + "." + ("f" * 64),
                    },
                )
                response = connection.getresponse()
                response_body = response.read()
                status = response.status
                connection.close()

        assert upstream.observed_request == request_body  # type: ignore[attr-defined]

    assert status == 200
    assert response_body == sse_body
    pre_record = _read_only_record(pre_output)
    post_record = _read_only_record(post_output)
    assert pre_record["request"] == post_record["request"]
    assert pre_record["capture_id"] == post_record["capture_id"]
    assert pre_record["response"] == post_record["response"]
    assert pre_record["sse"]["sequence_sha256"] == post_record["sse"]["sequence_sha256"]  # type: ignore[index]
    assert pre_record["sse"]["sequence_hmac_sha256"] == post_record["sse"]["sequence_hmac_sha256"]  # type: ignore[index]
    assert pre_record["outcome"] == post_record["outcome"] == "complete"
    assert "x-codexhub-issue62-capture" not in upstream.observed_headers  # type: ignore[attr-defined]
    serialized = json.dumps([pre_record, post_record], sort_keys=True)
    for sensitive in (
        "private prompt",
        "private output",
        "forbidden-token",
        "wire-id",
        upstream_url,
        str(pre_output),
        str(post_output),
        str(hmac_key_file),
    ):
        assert sensitive not in serialized
    assert not list(tmp_path.rglob("*.partial"))


def test_atomic_record_rejects_a_mutated_producer_snapshot(tmp_path: Path) -> None:
    record = _bound_record({
        "schema": "codexhub.issue62.live-evidence-lane.v1",
        "verification_scope": "capture_only_not_qualification",
        "capture_id": _capture_id(),
        "hop": "pre",
        "outcome": "complete",
        "failure": None,
        "status": 200,
        "content_type_class": "json",
        "request": {"bytes": 1, "sha256": "a" * 64, "hmac_sha256": "b" * 64, "complete": True},
        "response": {"bytes": 1, "sha256": "c" * 64, "hmac_sha256": "d" * 64, "complete": True},
        "sse": None,
    })
    record["status"] = 201

    with pytest.raises(sidecar.ArtifactValidationError, match="producer_binding_invalid"):
        sidecar.write_capture_record(
            tmp_path,
            "pre",
            record,
            capture_key=KEY,
            correlation_token=CORRELATION_TOKEN,
        )


@pytest.mark.parametrize(
    "correlation_header",
    [None, ("0" * 32) + "." + ("f" * 64)],
)
def test_post_rejects_missing_or_invalid_correlation_before_forwarding(
    correlation_header: str | None,
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    with _fake_upstream(b'data: {"type":"response.completed"}\n\n') as upstream:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        config = _sidecar_config(
            tmp_path,
            hmac_key_file,
            upstream_url,
            hop="post",
        )
        with _running_sidecar(config) as server:
            connection = http.client.HTTPConnection(server.listen_host, server.listen_port, timeout=2)
            headers = {}
            if correlation_header is not None:
                headers["X-CodexHub-Issue62-Capture"] = correlation_header
            connection.request("POST", "/responses", body=b"{}", headers=headers)
            response = connection.getresponse()
            assert response.status == 400
            response.read()
            connection.close()

    assert upstream.observed_request is None  # type: ignore[attr-defined]
    record = _read_only_record(config.output_dir)
    assert record["outcome"] == "incomplete"
    assert record["failure"] == "correlation_binding_invalid"
    assert "X-CodexHub-Issue62-Capture" not in json.dumps(record, sort_keys=True)
    assert not list(tmp_path.rglob("*.partial"))


def test_request_overflow_fails_closed_before_forwarding(
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    with _fake_upstream(b'data: {"type":"response.completed"}\n\n') as upstream:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        config = _sidecar_config(
            tmp_path,
            hmac_key_file,
            upstream_url,
            max_request_bytes=4,
        )
        with _running_sidecar(config) as server:
            connection = http.client.HTTPConnection(server.listen_host, server.listen_port, timeout=2)
            connection.request("POST", "/secret-path", body=b"private-body")
            response = connection.getresponse()
            assert response.status == 413
            response.read()
            connection.close()
        assert upstream.observed_request is None  # type: ignore[attr-defined]

    record = _read_only_record(config.output_dir)
    assert record["outcome"] == "incomplete"
    assert record["failure"] == "request_overflow"
    assert record["request"]["complete"] is False  # type: ignore[index]
    assert record["request"]["sha256"] is None  # type: ignore[index]
    assert "private-body" not in json.dumps(record, sort_keys=True)
    assert "secret-path" not in json.dumps(record, sort_keys=True)
    assert not list(tmp_path.rglob("*.partial"))


def test_response_overflow_nulls_partial_response_digests(
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    response_body = b'data: {"type":"response.completed","secret":"private-output"}\n\n'
    with _fake_upstream(response_body, chunk_size=7) as upstream:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        config = _sidecar_config(
            tmp_path,
            hmac_key_file,
            upstream_url,
            max_response_bytes=8,
        )
        with _running_sidecar(config) as server:
            connection = http.client.HTTPConnection(server.listen_host, server.listen_port, timeout=2)
            connection.request("POST", "/responses", body=b"{}")
            response = connection.getresponse()
            response.read()
            connection.close()

    record = _read_only_record(config.output_dir)
    assert record["outcome"] == "incomplete"
    assert record["failure"] == "response_overflow"
    assert record["response"]["complete"] is False  # type: ignore[index]
    assert record["response"]["sha256"] is None  # type: ignore[index]
    assert record["response"]["hmac_sha256"] is None  # type: ignore[index]
    assert "private-output" not in json.dumps(record, sort_keys=True)
    assert not list(tmp_path.rglob("*.partial"))


def _terminal_first_response(extra: bytes) -> bytes:
    terminal = b'data: {"type":"response.completed"}\n\n'
    padding_size = (64 * 1024) - len(terminal) - 3
    assert padding_size > 0
    return terminal + b":" + (b"x" * padding_size) + b"\n\n" + extra


@pytest.mark.parametrize("failure_mode", ["overflow", "timeout"])
def test_incomplete_response_invalidates_previously_complete_sse_digest(
    failure_mode: str,
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    response_body = _terminal_first_response(b":after-terminal\n\n")
    upstream_options = {"chunk_size": 64 * 1024}
    config_options: dict[str, object] = {"max_response_bytes": 64 * 1024}
    expected_failure = "response_overflow"
    if failure_mode == "timeout":
        upstream_options["chunk_delay"] = 0.2
        config_options = {
            "max_response_bytes": len(response_body) + 1024,
            "read_timeout_seconds": 0.05,
            "overall_timeout_seconds": 0.15,
        }
        expected_failure = "upstream_timeout"
    with _fake_upstream(response_body, **upstream_options) as upstream:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        config = _sidecar_config(
            tmp_path,
            hmac_key_file,
            upstream_url,
            **config_options,
        )
        with _running_sidecar(config) as server:
            connection = http.client.HTTPConnection(server.listen_host, server.listen_port, timeout=2)
            connection.request("POST", "/responses", body=b"{}")
            response = connection.getresponse()
            response.read()
            connection.close()

    record = _read_only_record(config.output_dir)
    assert record["outcome"] == "incomplete"
    assert record["failure"] == expected_failure
    assert record["response"]["complete"] is False  # type: ignore[index]
    assert record["response"]["sha256"] is None  # type: ignore[index]
    assert record["sse"]["complete"] is False  # type: ignore[index]
    assert record["sse"]["sequence_sha256"] is None  # type: ignore[index]
    assert record["sse"]["sequence_hmac_sha256"] is None  # type: ignore[index]
    assert not list(tmp_path.rglob("*.partial"))


def test_upstream_timeout_is_bounded_and_sanitized(
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    with _fake_upstream(
        b'data: {"type":"response.completed"}\n\n',
        response_delay=0.3,
    ) as upstream:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        config = _sidecar_config(
            tmp_path,
            hmac_key_file,
            upstream_url,
            read_timeout_seconds=0.05,
            overall_timeout_seconds=0.1,
        )
        with _running_sidecar(config) as server:
            connection = http.client.HTTPConnection(server.listen_host, server.listen_port, timeout=2)
            connection.request("POST", "/responses", body=b"{}")
            response = connection.getresponse()
            response.read()
            connection.close()

    record = _read_only_record(config.output_dir)
    assert record["outcome"] == "incomplete"
    assert record["failure"] == "upstream_timeout"
    assert upstream_url not in json.dumps(record, sort_keys=True)
    assert str(config.output_dir) not in json.dumps(record, sort_keys=True)
    assert str(hmac_key_file) not in json.dumps(record, sort_keys=True)
    assert not list(tmp_path.rglob("*.partial"))


def test_downstream_cancellation_fails_closed_and_cleans_partial(
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    response_body = (
        b'data: {"type":"response.output_text.delta","delta":"private"}\n\n' * 4096
        + b'data: {"type":"response.completed"}\n\n'
    )
    with _fake_upstream(response_body, chunk_size=1024, chunk_delay=0.001) as upstream:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        config = _sidecar_config(
            tmp_path,
            hmac_key_file,
            upstream_url,
            max_response_bytes=len(response_body) + 1024,
        )
        with _running_sidecar(config) as server:
            client = socket.create_connection((server.listen_host, server.listen_port), timeout=2)
            client.sendall(
                b"POST /responses HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Length: 2\r\n"
                b"Connection: close\r\n\r\n{}"
            )
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
            client.close()
            record = _wait_for_record(config.output_dir)

    assert record["outcome"] == "incomplete"
    assert record["failure"] in {"client_cancelled", "downstream_cancelled"}
    assert record["response"] is None or record["response"]["complete"] is False  # type: ignore[index]
    assert "private" not in json.dumps(record, sort_keys=True)
    assert not list(tmp_path.rglob("*.partial"))


def test_listener_start_failure_writes_bounded_record_without_sensitive_paths(
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    port = occupied.getsockname()[1]
    config = _sidecar_config(
        tmp_path,
        hmac_key_file,
        "http://user:secret@127.0.0.1:1".replace("user:secret@", ""),
        listen_port=port,
    )
    server = sidecar.CaptureSidecarServer(config)
    try:
        with pytest.raises(sidecar.SidecarStartError, match="server_start_failed"):
            server.start()
    finally:
        server.shutdown()
        occupied.close()

    record = _read_only_record(config.output_dir)
    assert record["failure"] == "server_start_failed"
    serialized = json.dumps(record, sort_keys=True)
    assert config.forward_base_url not in serialized
    assert str(config.output_dir) not in serialized
    assert str(hmac_key_file) not in serialized
    assert not list(tmp_path.rglob("*.partial"))


def test_server_thread_failure_writes_bounded_record_and_cleans_partial(
    tmp_path: Path,
    hmac_key_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("private thread detail")

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", explode)
    config = _sidecar_config(tmp_path, hmac_key_file, "http://127.0.0.1:1")
    server = sidecar.CaptureSidecarServer(config)
    server.start()
    assert server.wait() is False
    server.shutdown()

    record = _read_only_record(config.output_dir)
    assert record["failure"] == "server_thread_failed"
    assert "private thread detail" not in json.dumps(record, sort_keys=True)
    assert not list(tmp_path.rglob("*.partial"))


def test_post_rejects_a_header_issued_by_an_older_run(
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    response_body = b'data: {"type":"response.completed"}\n\n'
    with _fake_upstream(response_body) as upstream:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        config = _sidecar_config(
            tmp_path,
            hmac_key_file,
            upstream_url,
            hop="post",
            run_nonce="b" * 32,
        )
        old_header = sidecar._correlation_binding(
            hmac_key_file.read_bytes(), CORRELATION_TOKEN, RUN_NONCE
        )
        with _running_sidecar(config) as server:
            connection = http.client.HTTPConnection(server.listen_host, server.listen_port, timeout=2)
            connection.request(
                "POST",
                "/responses",
                body=b"{}",
                headers={"X-CodexHub-Issue62-Capture": old_header},
            )
            response = connection.getresponse()
            assert response.status == 400
            response.read()
            connection.close()

    assert upstream.observed_request is None  # type: ignore[attr-defined]
    record = _read_only_record(config.output_dir)
    assert record["failure"] == "correlation_binding_invalid"


def test_post_rejects_reusing_a_token_within_the_current_run(
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    response_body = b'data: {"type":"response.completed"}\n\n'
    with _fake_upstream(response_body) as upstream:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        config = _sidecar_config(
            tmp_path,
            hmac_key_file,
            upstream_url,
            hop="post",
        )
        header = sidecar._correlation_binding(
            hmac_key_file.read_bytes(), CORRELATION_TOKEN, RUN_NONCE
        )
        with _running_sidecar(config) as server:
            for expected_status in (200, 409):
                connection = http.client.HTTPConnection(server.listen_host, server.listen_port, timeout=2)
                connection.request(
                    "POST",
                    "/responses",
                    body=b"{}",
                    headers={"X-CodexHub-Issue62-Capture": header},
                )
                response = connection.getresponse()
                assert response.status == expected_status
                response.read()
                connection.close()

    records = [json.loads(path.read_text(encoding="utf-8")) for path in config.output_dir.glob("*.json")]
    assert len(records) == 2
    assert any(record["failure"] == "correlation_token_replayed" for record in records)
    assert upstream.observed_request == b"{}"  # type: ignore[attr-defined]


def test_shutdown_fails_closed_when_a_published_record_is_mutated(
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    response_body = b'data: {"type":"response.completed"}\n\n'
    with _fake_upstream(response_body) as upstream:
        config = _sidecar_config(
            tmp_path,
            hmac_key_file,
            f"http://127.0.0.1:{upstream.server_address[1]}",
        )
        server = sidecar.CaptureSidecarServer(config)
        server.start()
        connection = http.client.HTTPConnection(server.listen_host, server.listen_port, timeout=2)
        connection.request("POST", "/responses", body=b"{}")
        response = connection.getresponse()
        response.read()
        connection.close()
        record_path = next(config.output_dir.glob("*.json"))
        record_path.write_text(record_path.read_text(encoding="utf-8").replace('"status":200', '"status":201'), encoding="utf-8")
        assert server.shutdown() is False

    records = [json.loads(path.read_text(encoding="utf-8")) for path in config.output_dir.glob("*.json")]
    assert any(record["failure"] == "capture_record_mutated" for record in records)


def test_capture_write_failure_is_published_as_sanitized_record(
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    config = _sidecar_config(tmp_path, hmac_key_file, "http://127.0.0.1:1")
    server = sidecar.CaptureSidecarServer(config)
    server._write_control_failure("capture_record_exists")

    record = _read_only_record(config.output_dir)
    assert record["outcome"] == "incomplete"
    assert record["failure"] == "capture_record_exists"
    assert not list(tmp_path.rglob("*.partial"))


def test_cli_configuration_error_does_not_echo_sensitive_values(
    tmp_path: Path,
    hmac_key_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_url = "http://user:private-password@127.0.0.1:1"
    result = sidecar.main(
        [
            "--enable-live-capture",
            "--hop",
            "pre",
            "--listen-host",
            "127.0.0.1",
            "--listen-port",
            "1",
            "--forward-base-url",
            secret_url,
            "--output-dir",
            str(tmp_path / "private-output-path"),
            "--hmac-key-file",
            str(hmac_key_file),
            "--max-request-bytes",
            "1024",
            "--max-response-bytes",
            "4096",
            "--connect-timeout-seconds",
            "1",
            "--read-timeout-seconds",
            "1",
                "--overall-timeout-seconds",
                "2",
                "--run-nonce",
                RUN_NONCE,
            ]
        )

    assert result == 2
    stderr = capsys.readouterr().err.strip()
    assert stderr == "forward_base_url_invalid"
    assert "private-password" not in stderr
    assert "private-output-path" not in stderr
    assert str(hmac_key_file) not in stderr


def test_cli_explicit_enable_starts_and_cleans_up_on_interrupt(
    tmp_path: Path,
    hmac_key_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    def interrupt_wait(_server: object) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(sidecar.CaptureSidecarServer, "wait", interrupt_wait)
    result = sidecar.main(
        [
            "--enable-live-capture",
            "--hop",
            "pre",
            "--listen-host",
            "127.0.0.1",
            "--listen-port",
            str(port),
            "--forward-base-url",
            "http://127.0.0.1:1",
            "--output-dir",
            str(tmp_path / "records"),
            "--hmac-key-file",
            str(hmac_key_file),
            "--max-request-bytes",
            "1024",
            "--max-response-bytes",
            "4096",
            "--connect-timeout-seconds",
            "1",
            "--read-timeout-seconds",
            "1",
            "--overall-timeout-seconds",
            "2",
            "--run-nonce",
            RUN_NONCE,
        ]
    )

    assert result == 0
    rebound = socket.socket()
    try:
        rebound.bind(("127.0.0.1", port))
    finally:
        rebound.close()
    assert not list(tmp_path.rglob("*.partial"))


def _unused_loopback_port() -> int:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def _enabled_cli_args(tmp_path: Path, hmac_key_file: Path, port: int) -> list[str]:
    return [
        "--enable-live-capture",
        "--hop",
        "pre",
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        str(port),
        "--forward-base-url",
        "http://127.0.0.1:1",
        "--output-dir",
        str(tmp_path / "records"),
        "--hmac-key-file",
        str(hmac_key_file),
        "--max-request-bytes",
        "1024",
        "--max-response-bytes",
        "4096",
        "--connect-timeout-seconds",
        "1",
        "--read-timeout-seconds",
        "1",
        "--overall-timeout-seconds",
        "2",
        "--run-nonce",
        RUN_NONCE,
    ]


def test_cli_reports_false_shutdown_drain_with_fixed_failure(
    tmp_path: Path,
    hmac_key_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_shutdown = sidecar.CaptureSidecarServer.shutdown

    def interrupt_wait(_server: object) -> bool:
        raise KeyboardInterrupt

    def cleanup_but_report_false(server: object) -> bool:
        original_shutdown(server)
        return False

    monkeypatch.setattr(sidecar.CaptureSidecarServer, "wait", interrupt_wait)
    monkeypatch.setattr(sidecar.CaptureSidecarServer, "shutdown", cleanup_but_report_false)

    result = sidecar.main(_enabled_cli_args(tmp_path, hmac_key_file, _unused_loopback_port()))

    assert result == 1
    assert capsys.readouterr().err.strip() == "shutdown_drain_failed"


def test_cli_reports_server_thread_failure_with_fixed_failure(
    tmp_path: Path,
    hmac_key_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sidecar.CaptureSidecarServer, "wait", lambda _server: False)

    result = sidecar.main(_enabled_cli_args(tmp_path, hmac_key_file, _unused_loopback_port()))

    assert result == 1
    assert capsys.readouterr().err.strip() == "server_thread_failed"


def test_shutdown_closes_active_client_and_upstream_and_waits_bounded(
    tmp_path: Path,
    hmac_key_file: Path,
) -> None:
    response_body = b'data: {"type":"response.completed"}\n\n'
    with _fake_upstream(response_body, response_delay=0.25) as upstream:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        config = _sidecar_config(
            tmp_path,
            hmac_key_file,
            upstream_url,
            read_timeout_seconds=5.0,
            overall_timeout_seconds=5.0,
        )
        server = sidecar.CaptureSidecarServer(config)
        server.start()
        client_done = threading.Event()

        def request() -> None:
            connection = http.client.HTTPConnection(server.listen_host, server.listen_port, timeout=6)
            try:
                connection.request("POST", "/responses", body=b"{}")
                response = connection.getresponse()
                response.read()
            except OSError:
                pass
            finally:
                connection.close()
                client_done.set()

        client_thread = threading.Thread(target=request, daemon=True)
        client_thread.start()
        deadline = time.monotonic() + 2
        while upstream.observed_request is None and time.monotonic() < deadline:  # type: ignore[attr-defined]
            time.sleep(0.01)
        assert upstream.observed_request == b"{}"  # type: ignore[attr-defined]

        started = time.monotonic()
        drained = server.shutdown()
        elapsed = time.monotonic() - started
        client_thread.join(timeout=1)

    assert drained is True
    assert elapsed < 1.0
    assert client_done.is_set()
    record = _read_only_record(config.output_dir)
    assert record["outcome"] == "incomplete"
    assert record["failure"] in {"client_cancelled", "forwarding_failed", "upstream_timeout"}
    assert not list(tmp_path.rglob("*.partial"))
