from __future__ import annotations

import json
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


def test_private_failure_evidence_is_exclusive_and_mode_0600(tmp_path: Path) -> None:
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


def test_native_opus_live_requires_isolated_subscription_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "candidate"
    binary.write_bytes(b"candidate")
    monkeypatch.setattr(
        live_routes.sys, "argv", [
            "e2e_claude_live_routes.py", "--bin", str(binary),
            "--claude-bin", str(binary),
            "--resource-root", str(tmp_path), "--candidate-sha", "a" * 40,
            "--case", "claude-native-opus-5-5", "--evidence-out", str(tmp_path / "evidence.json"),
        ],
    )
    monkeypatch.setattr(live_routes, "verify_candidate_binding", lambda *args: None)
    monkeypatch.setattr(
        live_routes, "run", lambda *args, **kwargs: pytest.fail("native request must not start"),
    )
    with pytest.raises(SystemExit, match="requires --claude-subscription-source"):
        live_routes.main()
