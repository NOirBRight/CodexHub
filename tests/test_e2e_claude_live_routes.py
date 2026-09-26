from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import time
from pathlib import Path
from shutil import copy2

import pytest

from scripts import e2e_claude_live_routes as live_routes
from scripts.e2e_claude_live_routes import (
    deepseek_key,
    snapshot_claude_subscription,
    usage_evidence,
    verify_candidate_binding,
)


def test_deepseek_provider_source_extracts_only_official_key_in_memory(tmp_path: Path) -> None:
    provider_source = tmp_path / "providers.toml"
    provider_source.write_text(
        '[[providers]]\n'
        'id = "deepseek"\n'
        'base_url = "https://api.deepseek.com"\n'
        'api_key = "synthetic-deepseek-test-token"\n',
        encoding="utf-8",
    )

    assert deepseek_key(None, provider_source) == "synthetic-deepseek-test-token"
    assert provider_source.read_text(encoding="utf-8").endswith(
        'api_key = "synthetic-deepseek-test-token"\n'
    )


def test_deepseek_provider_source_rejects_nonofficial_origin_without_secret_detail(tmp_path: Path) -> None:
    provider_source = tmp_path / "providers.toml"
    provider_source.write_text(
        '[[providers]]\n'
        'id = "deepseek"\n'
        'base_url = "https://attacker.example"\n'
        'api_key = "synthetic-deepseek-test-token"\n',
        encoding="utf-8",
    )

    with pytest.raises(AssertionError) as error:
        deepseek_key(None, provider_source)
    assert "synthetic-deepseek-test-token" not in str(error.value)


def test_claude_subscription_snapshot_omits_refresh_and_private_metadata(tmp_path: Path) -> None:
    source = tmp_path / "host-credentials.json"
    destination = tmp_path / "isolated" / ".claude" / ".credentials.json"
    source.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "synthetic-access-token",
                    "refreshToken": "synthetic-refresh-token",
                    "expiresAt": int((time.time() + 1800) * 1000),
                    "scopes": ["user:inference"],
                    "subscriptionType": "synthetic",
                    "rateLimitTier": "synthetic",
                    "userId": "private-account-id",
                }
            }
        ),
        encoding="utf-8",
    )

    remaining = snapshot_claude_subscription(
        source,
        destination,
        minimum_remaining_seconds=900,
    )
    copied = json.loads(destination.read_text(encoding="utf-8"))
    oauth = copied["claudeAiOauth"]

    assert remaining > 900
    assert oauth == {
        "accessToken": "synthetic-access-token",
        "expiresAt": json.loads(source.read_text(encoding="utf-8"))["claudeAiOauth"]["expiresAt"],
        "scopes": ["user:inference"],
        "subscriptionType": "synthetic",
        "rateLimitTier": "synthetic",
    }
    assert "refreshToken" not in destination.read_text(encoding="utf-8")
    assert "private-account-id" not in destination.read_text(encoding="utf-8")
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
        assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert source.is_file()


def test_claude_subscription_snapshot_fails_before_copy_when_expiring(tmp_path: Path) -> None:
    source = tmp_path / "credentials.json"
    destination = tmp_path / "isolated" / ".credentials.json"
    source.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "synthetic-access-token",
                    "refreshToken": "synthetic-refresh-token",
                    "expiresAt": int((time.time() + 60) * 1000),
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="expires before"):
        snapshot_claude_subscription(source, destination, minimum_remaining_seconds=900)
    assert not destination.exists()


def test_usage_evidence_requires_complete_actual_usage_from_public_snapshot() -> None:
    snapshot = {
        "summary": {
            "requests": 1,
            "successful_requests": 1,
            "missing_usage_requests": 0,
            "partial_usage_requests": 0,
            "input_tokens": 15,
            "output_tokens": 4,
            "total_tokens": 19,
            "cached_input_tokens": 3,
            "cache_write_input_tokens": 2,
            "cache_hit_rate": 20,
        },
        "events": [{
            "request_id": "synthetic-private-id",
            "model": "claude-haiku-4-5-20251001",
            "upstream": "claude_subscription",
            "client_id": "claude",
            "status": 200,
            "duration_ms": 1200,
            "usage_source": "upstream",
            "input_tokens": 15,
            "cached_input_tokens": 3,
            "cache_write_input_tokens": 2,
            "output_tokens": 4,
            "total_tokens": 19,
        }],
        "telemetry_status": {"backfill_pending": False, "lag_bytes": 0},
    }

    evidence = usage_evidence(
        snapshot,
        case="claude-native-haiku",
        model="claude-haiku-4-5-20251001",
        provider="claude_subscription",
        gateway_request_ids=["synthetic-private-id"],
    )

    assert evidence["provider"] == "claude_subscription"
    assert evidence["total_tokens"] == 19
    assert evidence["request_id"] == "synthetic-private-id"


def test_usage_evidence_accepts_official_async_upstream_usage() -> None:
    snapshot = {
        "summary": {"requests": 1, "total_tokens": 19},
        "events": [{
            "request_id": "synthetic-luna-id",
            "model": "openai/gpt-6-luna",
            "upstream": "openai",
            "status": 200,
            "usage_source": "upstream_async",
            "input_tokens": 15,
            "output_tokens": 4,
            "total_tokens": 19,
        }],
        "telemetry_status": {"backfill_pending": False},
    }

    evidence = usage_evidence(
        snapshot, case="responses-luna", model="gpt-6-luna",
        provider="openai", gateway_request_ids=["synthetic-luna-id"],
    )
    assert evidence["usage_source"] == "upstream_async"


def test_claude_luna_usage_counts_as_codex_luna_release_evidence() -> None:
    rows = [{"case": "claude-luna"}]

    assert live_routes.usage_case_acceptance_status(
        rows, [], cases=("claude-luna", "responses-luna"),
    ) == "verified"
    assert live_routes.usage_case_acceptance_status(
        [], ["claude-luna: no Usage row"], cases=("claude-luna", "responses-luna"),
    ) == "failed"


def test_claude_luna_messages_route_correlates_complete_usage() -> None:
    request_id = "synthetic-claude-luna-request"
    evidence = usage_evidence(
        {
            "summary": {"requests": 1, "total_tokens": 19},
            "events": [{
                "request_id": request_id,
                "model": "openai/gpt-6-luna",
                "upstream": "openai",
                "status": 200,
                "usage_source": "upstream_async",
                "input_tokens": 15,
                "output_tokens": 4,
                "total_tokens": 19,
            }],
            "telemetry_status": {"backfill_pending": False},
        },
        case="claude-luna",
        model="gpt-6-luna",
        provider="openai",
        gateway_request_ids=[request_id],
    )

    assert evidence["case"] == "claude-luna"
    assert evidence["request_id"] == request_id
    assert evidence["total_tokens"] == 19


@pytest.mark.parametrize(
    ("message", "return_code", "expected"),
    [
        ("Authentication required. Login first.", 1, "auth_needed"),
        ("Not logged in · Please run /login", 1, "auth_needed"),
        ("Unknown model: gpt-6-luna", 1, "invalid_model"),
        ("connect ECONNREFUSED 127.0.0.1:9099", 1, "connection_refused"),
        ("HTTP 429 rate limit exceeded", 1, "rate_limited"),
        ("HTTP 503 Gateway unavailable", 1, "gateway_unavailable"),
        ("unclassified private response", 1, "cli_error"),
    ],
)
def test_claude_cli_error_classification_is_fixed_and_content_free(
    message: str, return_code: int, expected: str,
) -> None:
    classified = live_routes.classify_claude_cli_failure(message, "", return_code)

    assert classified == expected
    assert classified in live_routes.CLAUDE_CLI_FAILURE_CLASSES


def test_run_claude_failure_does_not_retain_result_or_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = "Bearer private-token; Not logged in · Please run /login"
    monkeypatch.setattr(
        live_routes.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1,
            stdout=json.dumps({
                "subtype": "success", "is_error": True, "result": private,
            }),
            stderr=private,
        ),
    )

    with pytest.raises(AssertionError) as error:
        live_routes.run_claude(tmp_path / "claude", {}, tmp_path, "sentinel")

    assert "failure_class=auth_needed" in str(error.value)
    assert private not in str(error.value)


def test_isolated_gateway_environment_does_not_inherit_operator_identity_or_paths(
    tmp_path: Path,
) -> None:
    isolated = tmp_path / "isolated"
    claude_bin = tmp_path / "tools" / "claude"
    gateway_env = live_routes.build_isolated_e2e_environment(
        {
            "HOME": "/operator/home",
            "USERPROFILE": r"C:\\Users\\operator",
            "HOMEDRIVE": "C:",
            "HOMEPATH": r"\\Users\\operator",
            "APPDATA": r"C:\\Users\\operator\\AppData\\Roaming",
            "LOCALAPPDATA": r"C:\\Users\\operator\\AppData\\Local",
            "TEMP": r"C:\\Users\\operator\\Temp",
            "TMP": r"C:\\Users\\operator\\Temp",
            "TMPDIR": "/operator/tmp",
            "XDG_CONFIG_HOME": "/operator/config",
            "XDG_RUNTIME_DIR": "/operator/runtime",
            "PATH": "/usr/bin",
            "ANTHROPIC_AUTH_TOKEN": "operator-auth-token",
            "ANTHROPIC_API_KEY": "operator-api-key",
            "CLAUDE_CONFIG_DIR": "/operator/claude",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "999999",
            "CODEX_HOME": "/operator/codex",
            "CODEXHUB_GATEWAY_SETTINGS": "/operator/gateway.json",
            "CODEXHUB_GATEWAY_API_KEY": "operator-gateway-key",
            "OPENAI_API_KEY": "operator-openai-key",
            "DEEPSEEK_API_KEY": "operator-deepseek-key",
            "UNRELATED_SETTING": "preserved",
        },
        root=isolated,
        codex_home=isolated / "codex",
        claude_home=isolated / "claude",
        runtime_home=isolated / "runtime",
        xdg_runtime=isolated / "xdg-runtime",
        resource_root=tmp_path / "resource",
        claude_bin=claude_bin,
        deepseek_key="fixture-deepseek-key",
    )

    assert gateway_env["HOME"] == str(isolated)
    assert gateway_env["USERPROFILE"] == str(isolated)
    assert gateway_env["APPDATA"] == str(isolated / "AppData" / "Roaming")
    assert gateway_env["LOCALAPPDATA"] == str(isolated / "AppData" / "Local")
    assert gateway_env["TEMP"] == gateway_env["TMP"] == gateway_env["TMPDIR"] == str(isolated / "tmp")
    assert gateway_env["XDG_CONFIG_HOME"] == str(isolated / "config")
    assert gateway_env["XDG_RUNTIME_DIR"] == str(isolated / "xdg-runtime")
    assert gateway_env["CLAUDE_CONFIG_DIR"] == str(isolated / "claude")
    assert gateway_env["CODEX_HOME"] == str(isolated / "codex")
    assert gateway_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == str(live_routes.CLAUDE_OUTPUT_TOKEN_CAP)
    assert gateway_env["DEEPSEEK_API_KEY"] == "fixture-deepseek-key"
    assert gateway_env["UNRELATED_SETTING"] == "preserved"
    for name in (
        "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CODEXHUB_GATEWAY_SETTINGS",
        "CODEXHUB_GATEWAY_API_KEY", "OPENAI_API_KEY",
    ):
        assert name not in gateway_env
    assert "/operator/" not in json.dumps(gateway_env)

    claude_env = live_routes.claude_client_environment(gateway_env)
    assert "DEEPSEEK_API_KEY" not in claude_env
    assert "ANTHROPIC_AUTH_TOKEN" not in claude_env
    assert claude_env["CLAUDE_CONFIG_DIR"] == str(isolated / "claude")


def test_usage_failure_diagnostic_uses_runtime_home_and_redacts_last_error(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    codex_home = tmp_path / "codex-target"
    proxy = runtime / "proxy"
    proxy.mkdir(parents=True)
    request_id = "synthetic-runtime-request"
    event_log = proxy / "codex-proxy-events.jsonl"
    event_log.write_text(
        json.dumps({
            "event": "request_complete", "request_id": request_id, "status": 200,
            "model": "openai/gpt-6-luna", "prompt": "private prompt text",
        }) + "\n"
        + json.dumps({
            "event": "usage_observed", "request_id": request_id,
            "usage_source": "missing", "model": "openai/gpt-6-luna; private model text",
        }) + "\n",
        encoding="utf-8",
    )
    database = proxy / "codex-proxy-telemetry.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE gateway_events(event_id INTEGER PRIMARY KEY, event TEXT, request_id TEXT);
            CREATE TABLE gateway_requests(
                request_id TEXT PRIMARY KEY, status INTEGER, usage_source TEXT, model TEXT
            );
            CREATE TABLE telemetry_meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        connection.executemany(
            "INSERT INTO gateway_events(event, request_id) VALUES (?, ?)",
            [("request_complete", request_id), ("usage_observed", request_id)],
        )
        connection.execute(
            "INSERT INTO gateway_requests VALUES (?, 200, 'missing', 'openai/gpt-6-luna')",
            (request_id,),
        )
        connection.execute(
            "INSERT INTO telemetry_meta VALUES ('last_ingest_error', 'private filesystem path')",
        )
    (codex_home / "proxy").mkdir(parents=True)
    (codex_home / "proxy" / "codex-proxy-telemetry.sqlite").write_bytes(b"wrong-home")

    diagnostic = live_routes.usage_persistence_failure_diagnostic(
        {
            "summary": {"requests": 1},
            "events": [],
            "telemetry_status": {
                "event_log_size": event_log.stat().st_size,
                "indexed_offset": 0,
                "lag_bytes": event_log.stat().st_size,
                "backfill_pending": True,
                "last_indexed_at": None,
                "last_error": "private filesystem path",
            },
        },
        runtime_home=runtime,
        gateway_request_ids=[request_id],
    )
    encoded = json.dumps(diagnostic)

    assert diagnostic["runtime_home"] == str(runtime)
    assert diagnostic["database"]["path"] == str(database)
    assert diagnostic["database"]["exists"] is True
    assert diagnostic["database"]["table_row_counts"]["gateway_events"] == 2
    assert diagnostic["database"]["gateway_request_match_count"] == 1
    assert diagnostic["event_log"]["request_id_match_count"] == 2
    event_rows = diagnostic["event_log"]["request_id_rows"]
    assert event_rows[0]["model"] == "openai/gpt-6-luna"
    assert "model" not in event_rows[1]
    assert diagnostic["database"]["gateway_request_rows"][0]["model"] == "openai/gpt-6-luna"
    assert diagnostic["telemetry_status"]["backfill_pending"] is True
    assert diagnostic["telemetry_status"]["last_error"] == "<REDACTED>"
    assert "private filesystem path" not in encoded
    assert "private prompt text" not in encoded
    assert "private model text" not in encoded
    assert str(codex_home) not in encoded


def test_usage_evidence_preserves_official_deepseek_identity_and_tokens() -> None:
    snapshot = {
        "summary": {
            "requests": 1,
            "successful_requests": 1,
            "missing_usage_requests": 0,
            "partial_usage_requests": 0,
            "input_tokens": 24,
            "output_tokens": 7,
            "total_tokens": 31,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "cache_hit_rate": 0,
        },
        "events": [{
            "request_id": "synthetic-deepseek-request-id",
            "model": "deepseek/deepseek-flash",
            "upstream": "deepseek",
            "client_id": "codexhub",
            "status": 200,
            "duration_ms": 1200,
            "usage_source": "upstream",
            "input_tokens": 24,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 7,
            "total_tokens": 31,
        }],
        "telemetry_status": {"backfill_pending": False, "lag_bytes": 0},
    }

    evidence = usage_evidence(
        snapshot,
        case="deepseek-chat",
        model="deepseek/deepseek-flash",
        provider="deepseek",
        gateway_request_ids=["synthetic-deepseek-request-id"],
    )

    assert evidence["model"] == "deepseek/deepseek-flash"
    assert evidence["provider"] == "deepseek"
    assert evidence["input_tokens"] == 24
    assert evidence["output_tokens"] == 7
    assert evidence["total_tokens"] == 31
    assert evidence["cached_input_tokens"] == 0
    assert evidence["cache_write_input_tokens"] == 0
    assert evidence["request_id"] == "synthetic-deepseek-request-id"


@pytest.mark.parametrize(
    ("case", "model", "provider", "request_id", "input_tokens", "output_tokens"),
    [
        (
            "claude-deepseek", "deepseek/deepseek-flash", "deepseek",
            "synthetic-deepseek-messages-id", 38, 9,
        ),
        (
            "responses-luna", "gpt-6-luna", "openai",
            "synthetic-luna-responses-id", 42, 11,
        ),
    ],
)
def test_usage_evidence_correlates_messages_and_responses_routes(
    case: str,
    model: str,
    provider: str,
    request_id: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    total_tokens = input_tokens + output_tokens
    snapshot = {
        "summary": {
            "requests": 1,
            "successful_requests": 1,
            "missing_usage_requests": 0,
            "partial_usage_requests": 0,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cached_input_tokens": 6,
            "cache_write_input_tokens": 0,
            "cache_hit_rate": 12,
        },
        "events": [{
            "request_id": request_id,
            "model": f"openai/{model}" if case == "responses-luna" else model,
            "upstream": provider,
            "client_id": "claude" if case == "claude-deepseek" else "codexhub",
            "status": 200,
            "duration_ms": 900,
            "usage_source": "upstream",
            "input_tokens": input_tokens,
            "cached_input_tokens": 6,
            "cache_write_input_tokens": 0,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }],
        "telemetry_status": {"backfill_pending": False, "lag_bytes": 0},
    }

    evidence = usage_evidence(
        snapshot,
        case=case,
        model=model,
        provider=provider,
        gateway_request_ids=[request_id],
    )

    assert evidence["case"] == case
    assert evidence["request_id"] == request_id
    assert evidence["model"] == (f"openai/{model}" if case == "responses-luna" else model)
    assert evidence["provider"] == provider
    assert evidence["input_tokens"] == input_tokens
    assert evidence["output_tokens"] == output_tokens
    assert evidence["total_tokens"] == total_tokens
    assert evidence["cached_input_tokens"] == 6


def test_usage_evidence_rejects_request_id_from_another_gateway_route() -> None:
    snapshot = {
        "summary": {"requests": 1, "total_tokens": 7},
        "events": [{
            "request_id": "snapshot-request-id",
            "model": "gpt-6-luna",
            "upstream": "official",
            "status": 200,
            "usage_source": "upstream",
            "input_tokens": 4,
            "output_tokens": 3,
            "total_tokens": 7,
        }],
        "telemetry_status": {"backfill_pending": False},
    }

    with pytest.raises(AssertionError, match="does not match"):
        usage_evidence(
            snapshot,
            case="responses-luna",
            model="gpt-6-luna",
            provider="official",
            gateway_request_ids=["other-request-id"],
        )


@pytest.mark.parametrize("usage_source", ["missing", "partial"])
def test_usage_evidence_rejects_noncomplete_upstream_usage(usage_source: str) -> None:
    snapshot = {
        "summary": {"requests": 1, "total_tokens": 7},
        "events": [{
            "request_id": "synthetic-incomplete-usage-id",
            "model": "claude-haiku-4-5-20251001",
            "upstream": "claude_subscription",
            "status": 200,
            "usage_source": usage_source,
            "input_tokens": 4,
            "output_tokens": 3,
            "total_tokens": 7,
        }],
        "telemetry_status": {"backfill_pending": False},
    }

    with pytest.raises(AssertionError, match="did not record upstream usage"):
        usage_evidence(
            snapshot,
            case="claude-native-haiku",
            model="claude-haiku-4-5-20251001",
            provider="claude_subscription",
            gateway_request_ids=["synthetic-incomplete-usage-id"],
        )


def test_route_request_count_and_total_deduplicate_gateway_request_ids() -> None:
    rows: list[dict[str, object]] = []
    live_routes.upsert_route_request_count(
        rows,
        case="claude-native-haiku",
        model="claude-haiku-4-5-20251001",
        inbound="anthropic_messages",
        outbound="anthropic_messages",
        request_ids=["request-1", "request-1"],
        successful_gateway_route_observed=True,
    )
    live_routes.upsert_route_request_count(
        rows,
        case="claude-native-haiku",
        model="claude-haiku-4-5-20251001",
        inbound="anthropic_messages",
        outbound="anthropic_messages",
        request_ids=["request-1"],
    )
    live_routes.upsert_route_request_count(
        rows,
        case="deepseek-chat",
        model="deepseek/deepseek-flash",
        inbound="chat_completions",
        outbound="chat_completions",
        request_ids=["request-1", "request-2"],
        successful_gateway_route_observed=True,
    )

    assert len(rows) == 2
    assert rows[0]["observed_gateway_request_ids"] == 1
    assert rows[0]["successful_gateway_route_observed"] is True
    assert live_routes.total_observed_gateway_request_ids(rows) == 2


def test_ui_failure_does_not_downgrade_a_successful_gateway_route() -> None:
    rows = [{
        "case": "claude-native-haiku",
        "successful_gateway_route_observed": True,
    }]

    assert live_routes.route_case_acceptance_status(
        rows,
        ["claude-native-haiku: packaged UI did not open"],
        case="claude-native-haiku",
    ) == "verified"


def test_packaged_ui_isolation_fails_closed_without_wrapper_marker() -> None:
    with pytest.raises(SystemExit, match="dbus-run-session -- xvfb-run"):
        live_routes.validate_packaged_ui_isolation({
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/synthetic-bus",
            "DISPLAY": ":99",
        })


def test_ocr_phrase_center_targets_only_the_requested_navigation_words() -> None:
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t0\t10\t10\t40\t20\t95\tUsage\n"
        "5\t1\t1\t1\t1\t1\t100\t10\t60\t20\t95\tProvider\n"
    )

    assert live_routes._ocr_phrase_center(tsv, "usage") == (30, 20)


def test_candidate_binding_checks_source_head_portable_name_and_resources(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    resources = (
        "config/providers.toml",
        "src-python/codex_proxy.py",
        "src-python/gateway_events.py",
        "src-python/gateway_relay_anthropic.py",
    )
    for relative in resources:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"synthetic resource {relative}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "synthetic candidate"], check=True)
    candidate_sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    portable = tmp_path / f"CodexHub_0.0.0_debug_linux_portable_{candidate_sha[:8]}"
    portable.mkdir()
    binary = portable / "codexhub"
    binary.write_bytes(b"synthetic binary")
    for relative in resources:
        target = portable / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copy2(source / relative, target)

    verify_candidate_binding(binary, portable, source, candidate_sha)

    (portable / "src-python" / "gateway_events.py").write_text("wrong candidate resource", encoding="utf-8")
    with pytest.raises(AssertionError, match="resource differs"):
        verify_candidate_binding(binary, portable, source, candidate_sha)


def test_failure_diagnostics_allowlist_omits_upstream_detail_and_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_events = [{
        "event": "request_error",
        "request_id": "req-safe-123",
        "model": "claude-haiku-4-5-20251001",
        "upstream": "claude_subscription",
        "inbound_format": "anthropic_messages",
        "upstream_format": "anthropic_messages",
        "status": 502,
        "error": "HTTPError",
        "failure_class": "quick_transient",
        "category": "external_upstream",
        "terminal_kind": "error",
        "detail": "Bearer do-not-record; prompt do-not-record; raw upstream response",
        "headers": {"authorization": "do-not-record"},
    }, {
        "event": "request_error",
        "request_id": "req-safe-124",
        "model": "claude-haiku-4-5-20251001",
        "upstream": "claude_subscription",
        "inbound_format": "anthropic_messages",
        "upstream_format": "anthropic_messages",
        "status": 502,
        "error": "raw error detail must not pass this label filter",
        "detail": "another private response body",
    }, {
        "event": "request_error",
        "request_id": "req-safe-123",
        "model": None,
        "upstream": "claude_subscription",
        "status": 502,
        "error": "HTTPError",
        "category": "external_upstream",
        "detail": "body omitted from the public event projection",
    }]
    monkeypatch.setattr(live_routes, "events", lambda _port: fixture_events)

    diagnostics = live_routes.gateway_route_diagnostics(
        12345,
        model="claude-haiku-4-5-20251001",
        inbound="anthropic_messages",
        outbound="anthropic_messages",
    )
    encoded = json.dumps(diagnostics)

    assert diagnostics[0]["upstream_http_status"] == 502
    assert diagnostics[0]["error_category"] == "external_upstream"
    assert diagnostics[0]["failure_class"] == "quick_transient"
    assert diagnostics[0]["sse_terminal_kind"] == "error"
    assert "error_class" not in diagnostics[1]
    assert diagnostics[2]["request_id"] == "req-safe-123"
    assert diagnostics[2]["model"] == "claude-haiku-4-5-20251001"
    assert "inbound_format" not in diagnostics[2]
    assert live_routes.gateway_route_request_count(
        12345,
        model="claude-haiku-4-5-20251001",
        inbound="anthropic_messages",
        outbound="anthropic_messages",
    ) == 2
    assert "do-not-record" not in encoded
    assert "private response body" not in encoded
    assert "headers" not in encoded


def test_private_failure_evidence_is_exclusive_and_mode_0600_on_posix(tmp_path: Path) -> None:
    destination = tmp_path / "private" / "failed-e2e.json"
    artifact = {
        "status": "failed",
        "failure_diagnostics": [{
            "event": "request_error",
            "model": "claude-haiku-4-5-20251001",
            "http_status": 502,
            "error_category": "external_upstream",
        }],
    }

    live_routes.write_private_evidence(destination, artifact)

    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert json.loads(destination.read_text(encoding="utf-8")) == artifact
    with pytest.raises(FileExistsError):
        live_routes.write_private_evidence(destination, artifact)


def test_preflight_requires_candidate_sha_and_portable_resource_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "codexhub"
    binary.write_bytes(b"candidate")
    monkeypatch.setattr(
        live_routes.sys,
        "argv",
        ["e2e_claude_live_routes.py", "--bin", str(binary), "--preflight-only"],
    )

    with pytest.raises(SystemExit, match="E2E requires --candidate-sha"):
        live_routes.main()


def test_preflight_verifies_candidate_binding_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "CodexHub_0.0.0_debug_linux_portable_aaaaaaaa" / "codexhub"
    binary.parent.mkdir()
    binary.write_bytes(b"candidate")
    claude_cli = tmp_path / "claude"
    claude_cli.write_bytes(b"claude")
    resource_root = binary.parent
    source_root = tmp_path / "source"
    candidate_sha = "a" * 40
    calls: list[tuple[Path, Path, Path, str]] = []
    launches: list[tuple[tuple[object, ...], dict[str, object]]] = []

    monkeypatch.setattr(
        live_routes.sys,
        "argv",
        [
            "e2e_claude_live_routes.py",
            "--bin", str(binary),
            "--resource-root", str(resource_root),
            "--claude-bin", str(claude_cli),
            "--source-root", str(source_root),
            "--candidate-sha", candidate_sha,
            "--preflight-only",
        ],
    )
    monkeypatch.setattr(
        live_routes,
        "verify_candidate_binding",
        lambda *args: calls.append(args),
    )
    monkeypatch.setattr(
        live_routes,
        "run",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )

    live_routes.main()

    assert calls == [(binary, resource_root, source_root, candidate_sha)]
    assert len(launches) == 1


def test_packaged_ui_without_isolated_wrapper_starts_no_candidate_or_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "candidate"
    monkeypatch.setattr(
        live_routes.sys,
        "argv",
        ["e2e_claude_live_routes.py", "--bin", str(binary), "--packaged-ui"],
    )
    monkeypatch.delenv(live_routes.PACKAGED_UI_ISOLATION_ENV, raising=False)
    monkeypatch.setattr(
        live_routes,
        "verify_candidate_binding",
        lambda *args: pytest.fail("candidate binding must not run without an isolated UI wrapper"),
    )
    monkeypatch.setattr(
        live_routes,
        "run",
        lambda *args, **kwargs: pytest.fail("candidate process or model request must not start"),
    )

    with pytest.raises(SystemExit, match="dbus-run-session -- xvfb-run"):
        live_routes.main()


@pytest.mark.parametrize("case", ["claude-deepseek", "claude-luna"])
def test_direct_claude_live_case_requires_subscription_source_before_candidate_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    binary = tmp_path / "candidate"
    binary.write_bytes(b"candidate")
    monkeypatch.setattr(
        live_routes.sys, "argv", ["e2e_claude_live_routes.py", "--bin", str(binary), "--case", case],
    )
    monkeypatch.setattr(
        live_routes, "verify_candidate_binding",
        lambda *args: pytest.fail("candidate binding must not run without subscription identity"),
    )
    monkeypatch.setattr(
        live_routes, "run",
        lambda *args, **kwargs: pytest.fail("candidate process or model request must not start"),
    )

    with pytest.raises(SystemExit, match="requires --claude-subscription-source"):
        live_routes.main()


def test_claude_launcher_luna_needs_no_subscription_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "candidate"
    binary.write_bytes(b"candidate")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        live_routes.sys, "argv", [
            "e2e_claude_live_routes.py", "--bin", str(binary),
            "--resource-root", str(tmp_path), "--source-root", str(tmp_path),
            "--claude-bin", str(binary), "--candidate-sha", "a" * 40,
            "--auth", str(tmp_path / "missing-auth.json"),
            "--catalog", str(tmp_path / "missing-catalog.json"),
            "--case", "claude-launcher-luna",
            "--evidence-out", str(tmp_path / "evidence.json"),
        ],
    )
    monkeypatch.setattr(live_routes, "verify_candidate_binding", lambda *args: None)
    monkeypatch.setattr(live_routes, "claude_cli_version", lambda _path: "2.1.283")
    monkeypatch.setattr(live_routes, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    live_routes.main()

    assert len(calls) == 1
    assert calls[0][0][6] == {"claude-launcher-luna"}
    assert calls[0][0][8] is None
    launcher = Path(live_routes.__file__).resolve().parent / "codexhub-claude-gateway.sh"
    assert 'export ANTHROPIC_AUTH_TOKEN="$client_key"' in launcher.read_text(encoding="utf-8")


def test_resume_invocation_passes_explicit_native_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout='{"is_error":false,"result":"OK"}', stderr="",
        )

    monkeypatch.setattr(live_routes.subprocess, "run", fake_run)
    live_routes.run_claude(
        tmp_path / "claude", {}, tmp_path, "OK",
        resume="session-id", model="claude-opus-5-5",
    )
    command = commands[0]
    assert command[command.index("--resume") + 1] == "session-id"
    assert command[command.index("--model") + 1] == "claude-opus-5-5"
    assert command.index("--resume") < command.index("--model")


@pytest.mark.parametrize("native_case", ["claude-native-opus-5-5", "claude-native-opus-5-5-resume"])
def test_native_opus_live_requires_isolated_subscription_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_case: str,
) -> None:
    binary = tmp_path / "candidate"
    binary.write_bytes(b"candidate")
    monkeypatch.setattr(
        live_routes.sys, "argv", [
            "e2e_claude_live_routes.py", "--bin", str(binary),
            "--claude-bin", str(binary),
            "--resource-root", str(tmp_path), "--candidate-sha", "a" * 40,
            "--case", native_case, "--evidence-out", str(tmp_path / "evidence.json"),
        ],
    )
    monkeypatch.setattr(live_routes, "verify_candidate_binding", lambda *args: None)
    monkeypatch.setattr(
        live_routes, "run", lambda *args, **kwargs: pytest.fail("native request must not start"),
    )
    with pytest.raises(SystemExit, match="requires --claude-subscription-source"):
        live_routes.main()
