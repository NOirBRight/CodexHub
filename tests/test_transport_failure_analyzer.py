import json
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_transport_failures import analyze_events, main  # noqa: E402


def test_analyzer_groups_official_stream_close_with_request_start_metadata():
    events = [
        {
            "ts": "2026-07-09T02:23:15Z",
            "event": "request_start",
            "request_id": "req-stream",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "window_id": "019f4247-8b5e-7f93-940b-765be510251b:turn",
            "content_length": 388558,
            "is_stream": 1,
        },
        {
            "ts": "2026-07-09T02:24:49Z",
            "event": "official_passthrough_stream_closed",
            "request_id": "req-stream",
            "status": 502,
            "error": "ConnectionResetError",
            "detail": "ConnectionResetError: [WinError 10054] remote host closed the connection",
            "failure_phase": "stream_body",
            "failure_side": "upstream_read",
            "lines_streamed": 27,
            "bytes_streamed": 4096,
            "last_upstream_byte_age_ms": 93,
            "headers_sent_downstream": True,
            "downstream_sse_started": True,
        },
    ]

    report = analyze_events(events)

    assert report["failure_count"] == 1
    assert report["groups"] == [
        {
            "provider_id": "official",
            "model_canonical": "openai/gpt-5.5",
            "client_id": "codex-app",
            "event": "official_passthrough_stream_closed",
            "failure_phase": "stream_body",
            "failure_side": "upstream_read",
            "error": "ConnectionResetError",
            "size_bucket": "256KB-512KB",
            "window_id": "019f4247-8b5e-7f93-940b-765be510251b:turn",
            "count": 1,
            "request_ids": ["req-stream"],
            "statuses": [502],
            "min_duration_ms": None,
            "max_duration_ms": None,
            "total_lines_streamed": 27,
            "total_bytes_streamed": 4096,
        }
    ]


def test_analyzer_replaces_unknown_placeholders_with_request_start_metadata():
    events = [
        {
            "ts": "2026-07-09T04:22:12Z",
            "event": "request_start",
            "request_id": "req-downstream",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "window_id": "019f4519-70f4-7d81-861b-530b3849aa01:0",
            "content_length": 418614,
            "is_stream": 1,
        },
        {
            "ts": "2026-07-09T04:22:59Z",
            "event": "official_passthrough_stream_closed",
            "request_id": "req-downstream",
            "client_id": "unknown",
            "provider_id": "unknown",
            "model_canonical": "unknown",
            "window_id": "unknown",
            "status": 499,
            "error": "ConnectionAbortedError",
            "detail": "ConnectionAbortedError: [WinError 10053]",
            "failure_phase": "downstream_write",
            "failure_side": "downstream_write",
            "failure_class": "downstream_client_closed",
            "lines_streamed": 8164,
            "bytes_streamed": 1403277,
        },
    ]

    report = analyze_events(events)

    group = report["groups"][0]
    assert group["provider_id"] == "official"
    assert group["client_id"] == "codex-app"
    assert group["model_canonical"] == "openai/gpt-5.5"
    assert group["window_id"] == "019f4519-70f4-7d81-861b-530b3849aa01:0"
    assert group["statuses"] == [499]


def test_analyzer_infers_ssl_eof_phase_for_older_retry_events():
    events = [
        {
            "ts": "2026-07-09T02:24:50Z",
            "event": "request_start",
            "request_id": "req-eof",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "window_id": "019f446c-0015-7643-a75a-94c486480235:turn",
            "content_length": 388558,
        },
        {
            "ts": "2026-07-09T02:24:51Z",
            "event": "upstream_retry",
            "request_id": "req-eof",
            "status": 502,
            "error": "URLError",
            "detail": "SSLEOFError: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol",
        },
        {
            "ts": "2026-07-09T02:24:52Z",
            "event": "request_error",
            "request_id": "req-eof",
            "status": 502,
            "duration_ms": 214,
            "error": "URLError",
            "detail": "SSLEOFError: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol",
        },
    ]

    report = analyze_events(events)

    phases = {(group["event"], group["failure_phase"], group["error"], group["count"]) for group in report["groups"]}
    assert ("upstream_retry", "tls_handshake", "URLError", 1) in phases
    assert ("request_error", "tls_handshake", "URLError", 1) in phases


def test_analyzer_excludes_provider_capacity_upstream_retry_without_transport_phase():
    events = [
        {
            "ts": "2026-07-09T02:24:50Z",
            "event": "request_start",
            "request_id": "req-capacity",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "window_id": "019f446c-0015-7643-a75a-94c486480235:turn",
            "content_length": 388558,
        },
        {
            "ts": "2026-07-09T02:24:51Z",
            "event": "upstream_retry",
            "request_id": "req-capacity",
            "status": 429,
            "error": "HTTPError",
            "detail": "HTTPError 429 Too Many Requests: rate limit exceeded",
            "failure_class": "capacity",
        },
        {
            "ts": "2026-07-09T02:24:52Z",
            "event": "upstream_retry",
            "request_id": "req-capacity",
            "status": 503,
            "error": "HTTPError",
            "detail": "HTTPError 503 Service Unavailable: provider overloaded",
            "failure_class": "provider_overloaded",
        },
    ]

    report = analyze_events(events)

    assert report["failure_count"] == 0
    assert report["groups"] == []


def test_analyzer_excludes_provider_http_retry_without_transport_signal():
    events = [
        {
            "ts": "2026-07-09T02:24:50Z",
            "event": "request_start",
            "request_id": "req-provider-5xx",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "window_id": "019f446c-0015-7643-a75a-94c486480235:turn",
            "content_length": 388558,
        },
        {
            "ts": "2026-07-09T02:24:51Z",
            "event": "upstream_retry",
            "request_id": "req-provider-5xx",
            "status": 500,
            "error": "HTTPError",
            "detail": "HTTPError 500 Internal Server Error",
            "failure_class": "quick_transient",
            "failure_phase": "response_headers",
        },
        {
            "ts": "2026-07-09T02:24:52Z",
            "event": "upstream_retry",
            "request_id": "req-provider-5xx",
            "status": 502,
            "error": "HTTPError",
            "detail": "HTTPError 502 Bad Gateway",
            "failure_class": "quick_transient",
            "failure_phase": "response_headers",
        },
    ]

    report = analyze_events(events)

    assert report["failure_count"] == 0
    assert report["groups"] == []


def test_analyzer_excludes_non_transport_request_errors_without_phase_or_detail_signals():
    events = [
        {
            "ts": "2026-07-09T02:24:53Z",
            "event": "request_start",
            "request_id": "req-app-error",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "window_id": "019f446c-0015-7643-a75a-94c486480235:turn",
            "content_length": 388558,
        },
        {
            "ts": "2026-07-09T02:24:54Z",
            "event": "request_error",
            "request_id": "req-app-error",
            "status": 502,
            "error": "ValueError",
            "detail": "invalid request payload",
        },
    ]

    report = analyze_events(events)

    assert report["failure_count"] == 0
    assert report["groups"] == []


def test_analyzer_includes_generic_legacy_request_errors_with_tcp_connect_phase():
    events = [
        {
            "ts": "2026-07-09T02:24:53Z",
            "event": "request_start",
            "request_id": "req-legacy",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "window_id": "019f446c-0015-7643-a75a-94c486480235:turn",
            "content_length": 388558,
        },
        {
            "ts": "2026-07-09T02:24:54Z",
            "event": "request_error",
            "request_id": "req-legacy",
            "status": 502,
            "error": "URLError",
            "detail": "gaierror: [Errno 11001] getaddrinfo failed",
        },
        {
            "ts": "2026-07-09T02:24:55Z",
            "event": "request_error",
            "request_id": "req-legacy",
            "status": 502,
            "error": "URLError",
            "detail": "Connection refused",
        },
        {
            "ts": "2026-07-09T02:24:56Z",
            "event": "request_error",
            "request_id": "req-legacy",
            "status": 502,
            "error": "URLError",
            "detail": "legacy urllib request failed",
        },
        {
            "ts": "2026-07-09T02:24:57Z",
            "event": "request_error",
            "request_id": "req-legacy",
            "status": 502,
            "error": "OSError",
            "detail": "socket setup failed",
        },
    ]

    report = analyze_events(events)

    grouped = {(group["event"], group["failure_phase"], group["error"], group["count"]) for group in report["groups"]}
    assert ("request_error", "tcp_connect", "URLError", 3) in grouped
    assert ("request_error", "tcp_connect", "OSError", 1) in grouped


def test_analyzer_buckets_zero_content_length_as_small_request():
    events = [
        {
            "ts": "2026-07-09T02:24:55Z",
            "event": "request_start",
            "request_id": "req-zero",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "window_id": "019f446c-0015-7643-a75a-94c486480235:turn",
            "content_length": 0,
        },
        {
            "ts": "2026-07-09T02:24:56Z",
            "event": "official_passthrough_stream_closed",
            "request_id": "req-zero",
            "status": 502,
            "error": "ConnectionResetError",
            "detail": "ConnectionResetError: [WinError 10054] remote host closed the connection",
        },
    ]

    report = analyze_events(events)

    assert report["groups"][0]["size_bucket"] == "<64KB"


def test_cli_reads_temp_jsonl_and_emits_json_report(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-07-09T02:24:57Z",
                        "event": "request_start",
                        "request_id": "req-cli",
                        "client_id": "codex-app",
                        "provider_id": "official",
                        "upstream": "official",
                        "model_canonical": "openai/gpt-5.5",
                        "window_id": "019f446c-0015-7643-a75a-94c486480235:turn",
                        "content_length": 1,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-07-09T02:24:58Z",
                        "event": "official_passthrough_stream_closed",
                        "request_id": "req-cli",
                        "status": 502,
                        "error": "ConnectionResetError",
                        "detail": "ConnectionResetError: [WinError 10054] remote host closed the connection",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = main(["--input", str(path)])

    report = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert report["failure_count"] == 1
    assert report["groups"][0]["request_ids"] == ["req-cli"]


def test_analyzer_cli_reads_checked_in_fixture(tmp_path):
    import subprocess

    fixture = ROOT / "tests" / "fixtures" / "transport_failures.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "analyze_transport_failures.py"),
            "--input",
            str(fixture),
            "--since",
            "2026-07-09T02:23:00Z",
            "--until",
            "2026-07-09T02:25:30Z",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(result.stdout)

    assert report["failure_count"] == 3
    grouped = {(group["event"], group["failure_phase"], group["count"]) for group in report["groups"]}
    assert ("official_passthrough_stream_closed", "stream_body", 1) in grouped
    assert ("upstream_retry", "tls_handshake", 1) in grouped
    assert ("request_error", "tls_handshake", 1) in grouped
