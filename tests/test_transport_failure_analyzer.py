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
            "provider_scope": "official",
            "provider_id": "official",
            "model_canonical": "openai/gpt-5.5",
            "client_id": "codex-app",
            "event": "official_passthrough_stream_closed",
            "failure_phase": "stream_body",
            "failure_side": "upstream_read",
            "failure_class": "connection_reset",
            "error": "ConnectionResetError",
            "size_bucket": "256KB-512KB",
            "time_window": "2026-07-09T02:20:00Z/2026-07-09T02:25:00Z",
            "count": 1,
            "request_ids": ["req-stream"],
            "statuses": [502],
            "min_duration_ms": None,
            "max_duration_ms": None,
            "total_lines_streamed": 27,
            "total_bytes_streamed": 4096,
            "examples": [
                {
                    "request_id": "req-stream",
                    "content_length": 388558,
                    "error": "ConnectionResetError",
                    "detail": "ConnectionResetError: [WinError 10054] remote host closed the connection",
                }
            ],
        }
    ]


def test_analyzer_excludes_downstream_client_cancellations_from_transport_groups():
    events = [
        {
            "ts": "2026-07-09T04:22:12Z",
            "event": "request_start",
            "request_id": "req-downstream",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
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

    assert report["failure_count"] == 0
    assert report["groups"] == []
    assert report["excluded_downstream_cancellation_count"] == 1


def test_analyzer_keeps_downstream_write_event_without_cancellation_status():
    events = [
        {
            "ts": "2026-07-09T04:22:59Z",
            "event": "official_passthrough_stream_closed",
            "request_id": "synthetic-downstream-control",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "status": 502,
            "error": "ConnectionAbortedError",
            "detail": "ConnectionAbortedError: [WinError 10053]",
            "failure_phase": "downstream_write",
            "failure_side": "downstream_write",
            "failure_class": "downstream_client_closed",
        }
    ]

    report = analyze_events(events)

    assert report["failure_count"] == 1
    assert report["excluded_downstream_cancellation_count"] == 0
    assert report["groups"][0]["failure_side"] == "downstream_write"


def test_analyzer_infers_ssl_eof_phase_and_class_for_older_retry_events():
    events = [
        {
            "ts": "2026-07-09T02:24:50Z",
            "event": "request_start",
            "request_id": "req-eof",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
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
    assert {group["failure_class"] for group in report["groups"]} == {"tls_eof"}


def test_analyzer_keeps_read_timeout_unknown_without_connect_evidence():
    events = [
        {
            "ts": "2026-07-09T02:24:50Z",
            "event": "request_start",
            "request_id": "synthetic-read-timeout",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "content_length": 388558,
        },
        {
            "ts": "2026-07-09T02:24:51Z",
            "event": "upstream_retry",
            "request_id": "synthetic-read-timeout",
            "status": 502,
            "error": "URLError",
            "detail": "TimeoutError: upstream stream read timed out",
        },
    ]

    report = analyze_events(events)

    assert report["failure_count"] == 1
    group = report["groups"][0]
    assert group["failure_phase"] == "unknown"
    assert group["failure_class"] == "unknown"


def test_analyzer_separates_official_and_third_party_transport_failures():
    events = [
        {
            "ts": "2026-07-09T00:50:00Z",
            "event": "request_start",
            "request_id": "third-party-eof",
            "client_id": "opencode",
            "provider_id": "ollama_cloud",
            "upstream": "ollama_cloud",
            "model_canonical": "ollama-cloud/glm-5.2",
            "content_length": 2048,
        },
        {
            "ts": "2026-07-09T00:50:01Z",
            "event": "request_error",
            "request_id": "third-party-eof",
            "status": 502,
            "error": "URLError",
            "detail": "SSLEOFError: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol",
        },
        {
            "ts": "2026-07-09T02:23:00Z",
            "event": "request_start",
            "request_id": "official-stream",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "content_length": 388558,
        },
        {
            "ts": "2026-07-09T02:24:00Z",
            "event": "official_passthrough_stream_closed",
            "request_id": "official-stream",
            "status": 502,
            "error": "ConnectionResetError",
            "detail": "ConnectionResetError: [WinError 10054] remote host closed the connection",
        },
    ]

    report = analyze_events(events)

    grouped = {
        (
            group["provider_scope"],
            group["provider_id"],
            group["model_canonical"],
            group["client_id"],
            group["failure_phase"],
        )
        for group in report["groups"]
    }
    assert ("official", "official", "openai/gpt-5.5", "codex-app", "stream_body") in grouped
    assert ("third_party", "ollama_cloud", "ollama-cloud/glm-5.2", "opencode", "tls_handshake") in grouped


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


def test_analyzer_groups_by_failure_class_and_keeps_diagnostic_examples():
    events = [
        {
            "ts": "2026-07-09T02:24:50Z",
            "event": "request_start",
            "request_id": "req-class-a",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "content_length": 388558,
        },
        {
            "ts": "2026-07-09T02:24:51Z",
            "event": "official_passthrough_stream_closed",
            "request_id": "req-class-a",
            "status": 502,
            "duration_ms": 94689,
            "error": "ConnectionResetError",
            "detail": "WinError 10054",
            "failure_class": "upstream_stream_interrupted",
        },
        {
            "ts": "2026-07-09T02:24:52Z",
            "event": "request_start",
            "request_id": "req-class-b",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "content_length": 388558,
        },
        {
            "ts": "2026-07-09T02:24:53Z",
            "event": "official_passthrough_stream_closed",
            "request_id": "req-class-b",
            "status": 499,
            "duration_ms": 10060,
            "error": "ConnectionAbortedError",
            "detail": "WinError 10053",
            "failure_class": "downstream_client_closed",
        },
    ]

    report = analyze_events(events)

    assert report["group_count"] == 2
    groups = {group["failure_class"]: group for group in report["groups"]}
    upstream_example = groups["upstream_stream_interrupted"]["examples"][0]
    assert upstream_example == {
        "request_id": "req-class-a",
        "content_length": 388558,
        "duration_ms": 94689,
        "error": "ConnectionResetError",
        "detail": "WinError 10054",
    }
    assert groups["downstream_client_closed"]["examples"][0]["duration_ms"] == 10060


def test_analyzer_groups_cross_request_failures_by_utc_time_window_without_correlation_id():
    events = [
        {
            "ts": "2026-07-09T02:24:50Z",
            "event": "request_start",
            "request_id": "synthetic-request-a",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "window_id": "synthetic-correlation-a",
            "content_length": 388558,
        },
        {
            "ts": "2026-07-09T02:24:51Z",
            "event": "request_error",
            "request_id": "synthetic-request-a",
            "status": 502,
            "error": "URLError",
            "detail": "SSLEOFError: EOF occurred in violation of protocol",
        },
        {
            "ts": "2026-07-09T02:24:52Z",
            "event": "request_start",
            "request_id": "synthetic-request-b",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
            "window_id": "synthetic-correlation-b",
            "content_length": 388558,
        },
        {
            "ts": "2026-07-09T02:24:53Z",
            "event": "request_error",
            "request_id": "synthetic-request-b",
            "status": 502,
            "error": "URLError",
            "detail": "SSLEOFError: EOF occurred in violation of protocol",
        },
    ]

    report = analyze_events(events)

    assert report["group_count"] == 1
    group = report["groups"][0]
    assert group["count"] == 2
    assert group["time_window"] == "2026-07-09T02:20:00Z/2026-07-09T02:25:00Z"
    assert "window_id" not in group


def test_analyzer_uses_timestamp_boundaries_for_selected_window():
    events = [
        {
            "ts": "2026-07-09T02:00:00.999Z",
            "event": "request_error",
            "request_id": "synthetic-boundary",
            "status": 502,
            "error": "URLError",
            "detail": "SSLEOFError: EOF occurred in violation of protocol",
        }
    ]

    report = analyze_events(
        events,
        since="2026-07-09T02:00:00Z",
        until="2026-07-09T02:00:00Z",
    )

    assert report["failure_count"] == 0
    assert report["groups"] == []
    assert report["skipped_out_of_window_count"] == 1


def test_analyzer_reports_missing_and_invalid_timestamps_for_selected_window():
    events = [
        {
            "event": "request_error",
            "request_id": "synthetic-missing-timestamp",
            "status": 502,
            "error": "URLError",
            "detail": "SSLEOFError: EOF occurred in violation of protocol",
        },
        {
            "ts": "not-an-iso-timestamp",
            "event": "request_error",
            "request_id": "synthetic-invalid-timestamp",
            "status": 502,
            "error": "URLError",
            "detail": "SSLEOFError: EOF occurred in violation of protocol",
        },
    ]

    report = analyze_events(events, since="2026-07-09T02:00:00Z")

    assert report["failure_count"] == 0
    assert report["skipped_missing_timestamp_count"] == 1
    assert report["skipped_invalid_timestamp_count"] == 1


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


def test_analyzer_classifies_only_connect_specific_legacy_request_errors_as_tcp_connect():
    events = [
        {
            "ts": "2026-07-09T02:24:53Z",
            "event": "request_start",
            "request_id": "req-legacy",
            "client_id": "codex-app",
            "provider_id": "official",
            "upstream": "official",
            "model_canonical": "openai/gpt-5.5",
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
    assert ("request_error", "tcp_connect", "URLError", 2) in grouped
    assert ("request_error", "unknown", "URLError", 1) in grouped
    assert ("request_error", "unknown", "OSError", 1) in grouped


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


def test_analyzer_cli_reads_sanitized_fixture_and_separates_provider_scopes():
    import subprocess

    fixture = ROOT / "tests" / "fixtures" / "transport_failures.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "analyze_transport_failures.py"),
            "--input",
            str(fixture),
            "--window-minutes",
            "30",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(result.stdout)

    assert report["failure_count"] == 5
    assert report["excluded_downstream_cancellation_count"] == 1
    assert report["time_window_minutes"] == 30
    grouped = {
        (
            group["provider_scope"],
            group["provider_id"],
            group["event"],
            group["failure_class"],
            group["time_window"],
            group["count"],
        )
        for group in report["groups"]
    }
    assert (
        "official",
        "official",
        "official_passthrough_stream_closed",
        "connection_reset",
        "2026-07-09T02:00:00Z/2026-07-09T02:30:00Z",
        1,
    ) in grouped
    assert (
        "third_party",
        "ollama_cloud",
        "request_error",
        "tls_eof",
        "2026-07-09T00:30:00Z/2026-07-09T01:00:00Z",
        1,
    ) in grouped
    assert (
        "third_party",
        "ollama_cloud",
        "request_error",
        "connect_timeout",
        "2026-07-09T01:00:00Z/2026-07-09T01:30:00Z",
        1,
    ) in grouped
