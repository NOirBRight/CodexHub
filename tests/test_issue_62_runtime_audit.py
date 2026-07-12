import importlib.util
import json
import re
import sqlite3
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_issue_62_runtime_artifacts.py"
AUDIT = ROOT / "docs" / "evidence" / "issue-62" / "read-only-gate-audit.json"


def load_audit_module():
    spec = importlib.util.spec_from_file_location("issue_62_runtime_audit", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_audit(
    module,
    codex_db: Path,
    gateway_db: Path,
    *,
    config_written_at: str = "1970-01-01T00:06:00Z",
):
    return module.audit_artifacts(
        codex_log_db=codex_db,
        gateway_db=gateway_db,
        model="gpt-5.6-sol",
        gateway_started_at="1970-01-01T00:03:00Z",
        app_server_started_at="1970-01-01T00:05:00Z",
        config_written_at=config_written_at,
        catalog_written_at="1970-01-01T00:02:00Z",
        snapshot_ended_at="1970-01-01T00:10:00Z",
    )


def create_codex_log_db(
    path: Path,
    *,
    gateway_tool_choice: object = "auto",
    post_start_gateway_request: bool = False,
) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            target TEXT NOT NULL,
            feedback_log_body TEXT
        )
        """
    )

    gateway_payload = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {
                        "type": "function",
                        "name": "shell_command",
                        "description": "must not be retained",
                        "parameters": {"secret": "must not be retained"},
                    },
                    {
                        "type": "namespace",
                        "name": "codex_app",
                        "tools": [
                            {"type": "function", "name": "read_thread_terminal"},
                        ],
                    },
                    {
                        "type": "tool_search",
                        "execution": "client",
                        "parameters": {"secret": "must not be retained"},
                    },
                ],
            },
            {"type": "message", "content": "must not be retained"},
            {
                "type": "function_call",
                "call_id": "must-not-be-retained",
                "arguments": "must not be retained",
            },
            {
                "type": "function_call_output",
                "call_id": "must-not-be-retained",
                "output": "must not be retained",
            },
        ],
        "tool_choice": gateway_tool_choice,
        "parallel_tool_calls": False,
        "stream": True,
        "client_metadata": {"session_id": "must-not-be-retained"},
    }
    direct_payload = {
        "model": "gpt-5.6-sol",
        "input": [{"type": "message", "content": "must not be retained"}],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "stream": True,
    }

    gateway_body = json.dumps(gateway_payload, separators=(",", ":"))
    direct_body = json.dumps(direct_payload, separators=(",", ":"))
    rows = [
        (
            200,
            "codex_http_client::transport",
            f"span: POST to http://127.0.0.1:9099/v1/responses: {gateway_body}",
        ),
        (
            201,
            "codex_http_client::transport",
            f"span: POST to http://127.0.0.1:9099/v1/responses: {gateway_body}",
        ),
        (
            400,
            "codex_http_client::transport",
            f"span: POST to https://chatgpt.com/backend-api/codex/responses: {direct_body}",
        ),
    ]
    if post_start_gateway_request:
        rows.append(
            (
                350,
                "codex_http_client::transport",
                f"span: POST to http://127.0.0.1:9099/v1/responses: {gateway_body}",
            )
        )
    connection.executemany(
        "INSERT INTO logs (ts, target, feedback_log_body) VALUES (?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()


def create_gateway_db(
    path: Path,
    *,
    prefix_available: bool = True,
    prefix_mismatch: bool = False,
    prefix_pair: tuple[str, str] | None = None,
    request_hmac_pair: tuple[str, str] | None = None,
    response_fingerprint_column: bool = False,
    response_fingerprint_pair: tuple[str | None, str | None] | None = None,
    request_started_at: str = "1970-01-01T00:03:20Z",
    request_completed_at: str = "1970-01-01T00:03:21Z",
) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE gateway_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            event TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE gateway_requests (
            request_id TEXT PRIMARY KEY,
            request_body_hmac TEXT,
            request_prefix_hmac TEXT
        )
        """
    )
    if response_fingerprint_column:
        connection.execute(
            "ALTER TABLE gateway_requests ADD COLUMN downstream_response_body_sha256 TEXT"
        )
    if response_fingerprint_pair is not None:
        connection.execute(
            "ALTER TABLE gateway_requests ADD COLUMN upstream_response_body_sha256 TEXT"
        )
        connection.execute(
            "ALTER TABLE gateway_requests ADD COLUMN downstream_response_body_sha256 TEXT"
        )
        connection.execute(
            """
            INSERT INTO gateway_requests (
                request_id,
                upstream_response_body_sha256,
                downstream_response_body_sha256
            ) VALUES (?, ?, ?)
            """,
            (
                "must-not-be-retained",
                response_fingerprint_pair[0],
                response_fingerprint_pair[1],
            ),
        )

    request_start = {
        "event": "request_start",
        "upstream": "official",
        "route_mode": "official",
        "behavior_profile": "official_codex_app_http_passthrough",
        "inbound_format": "responses",
        "upstream_format": "responses",
        "wire_format_adapter": "transparent",
        "codex_semantic_adapter": "none",
        "repair_policy": "none",
        "is_stream": True,
        "request_id": "must-not-be-retained",
    }
    if prefix_available:
        caller_prefix, upstream_prefix = prefix_pair or (
            "prefix-a",
            "prefix-b" if prefix_mismatch else "prefix-a",
        )
        request_start["caller_request_prefix_hmac"] = caller_prefix
        request_start["upstream_request_prefix_hmac"] = upstream_prefix
    if request_hmac_pair is None:
        request_start["caller_request_body_hmac_skipped"] = True
        request_start["upstream_request_body_hmac_skipped"] = True
    else:
        request_start["caller_request_body_hmac"] = request_hmac_pair[0]
        request_start["upstream_request_body_hmac"] = request_hmac_pair[1]
    request_complete = {
        "event": "request_complete",
        "upstream": "official",
        "status": 200,
        "sse_event_types": ["response.created", "response.completed"],
        "request_id": "must-not-be-retained",
    }
    connection.executemany(
        "INSERT INTO gateway_events (ts, event, payload_json) VALUES (?, ?, ?)",
        [
            (request_started_at, "request_start", json.dumps(request_start)),
            (request_completed_at, "request_complete", json.dumps(request_complete)),
        ],
    )
    connection.commit()
    connection.close()


def test_audit_reports_only_sanitized_schema_and_gate_facts(tmp_path: Path) -> None:
    module = load_audit_module()
    codex_db = tmp_path / "codex.sqlite"
    gateway_db = tmp_path / "gateway.sqlite"
    create_codex_log_db(codex_db)
    create_gateway_db(gateway_db)

    audit = run_audit(module, codex_db, gateway_db)

    assert audit["schema_version"] == 1
    planner = audit["model_visible_request_plan"]
    assert planner["transport_log_rows"] == 2
    assert planner["unclassified_item_types"] == []
    assert planner["plan_variants"][0]["tool_choice"] == "auto"
    assert planner["plan_variants"][0]["parallel_tool_calls"] is False
    assert planner["plan_variants"][0]["tool_surface"] == "surface_01"
    assert planner["tool_surfaces"]["surface_01"] == [
        {
            "defer_loading_present": False,
            "name": "shell_command",
            "type": "function",
        },
        {
            "defer_loading_present": False,
            "name": "codex_app",
            "namespace_tools": ["read_thread_terminal"],
            "type": "namespace",
        },
        {
            "defer_loading_present": False,
            "execution": "client",
            "name": None,
            "type": "tool_search",
        },
    ]

    gateway = audit["gateway_identity_route"]
    assert gateway["request_starts"] == 1
    assert gateway["streaming_requests"] == 1
    assert gateway["non_streaming_requests"] == 0
    assert gateway["prefix_equal"] == 1
    assert gateway["full_body_hmac_pairs"] == 0
    assert gateway["full_body_hmac_both_skipped"] == 1
    assert gateway["response_body_fingerprint_fields_present"] is False

    timeline = audit["runtime_timeline"]
    assert timeline["config_written_after_app_server_start"] is True
    assert timeline["gateway_requests_after_app_server_start"] == 0
    assert timeline["current_request_endpoint_classes"] == {"official_direct": 1}
    assert audit["gate_classification"]["choice_controls"] == "observed"
    assert (
        audit["gate_classification"]["clean_cold_start_current_binding"]
        == "live_control_required"
    )

    serialized = json.dumps(audit, sort_keys=True)
    for forbidden in (
        "must not be retained",
        "must-not-be-retained",
        "prefix-a",
        "chatgpt.com",
        "127.0.0.1",
        str(codex_db),
        str(gateway_db),
    ):
        assert forbidden not in serialized


def test_clean_cold_start_requires_correlated_current_binding_identity(
    tmp_path: Path,
) -> None:
    module = load_audit_module()
    codex_db = tmp_path / "codex.sqlite"
    gateway_db = tmp_path / "gateway.sqlite"
    create_codex_log_db(codex_db, post_start_gateway_request=True)
    create_gateway_db(
        gateway_db,
        request_started_at="1970-01-01T00:05:20Z",
        request_completed_at="1970-01-01T00:05:21Z",
    )

    audit = run_audit(
        module,
        codex_db,
        gateway_db,
        config_written_at="1970-01-01T00:04:00Z",
    )

    timeline = audit["runtime_timeline"]
    assert timeline["config_written_after_app_server_start"] is False
    assert timeline["catalog_written_before_app_server_start"] is True
    assert timeline["current_request_endpoint_classes"]["codexhub_local"] == 1
    assert timeline["gateway_requests_after_app_server_start"] == 1
    assert timeline["clean_cold_start_for_current_binding_proven"] is False
    assert (
        audit["gate_classification"]["clean_cold_start_current_binding"]
        == "live_control_required"
    )


def test_audit_surfaces_unclassified_items_and_prefix_mismatch(tmp_path: Path) -> None:
    module = load_audit_module()
    codex_db = tmp_path / "codex.sqlite"
    gateway_db = tmp_path / "gateway.sqlite"
    create_codex_log_db(codex_db)
    create_gateway_db(gateway_db, prefix_mismatch=True)

    connection = sqlite3.connect(codex_db)
    payload = {
        "model": "gpt-5.6-sol",
        "input": [{"type": "future_item", "opaque": "must not be retained"}],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "stream": True,
    }
    connection.execute(
        "INSERT INTO logs (ts, target, feedback_log_body) VALUES (?, ?, ?)",
        (
            202,
            "codex_http_client::transport",
            "span: POST to http://127.0.0.1:9099/v1/responses: "
            + json.dumps(payload),
        ),
    )
    connection.commit()
    connection.close()

    audit = run_audit(module, codex_db, gateway_db)

    assert audit["model_visible_request_plan"]["unclassified_item_types"] == [
        "future_item"
    ]
    assert audit["gateway_identity_route"]["prefix_mismatch"] == 1
    assert audit["gate_classification"]["zero_unclassified_identity"] == "not_met"


def test_committed_audit_preserves_the_bounded_fact_and_sanitization_boundary() -> None:
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))

    assert audit["gateway_identity_route"]["request_starts"] == 525
    assert audit["gateway_identity_route"]["prefix_equal"] == 525
    assert audit["gateway_identity_route"]["prefix_mismatch"] == 0
    assert audit["gateway_identity_route"]["full_body_hmac_pairs"] == 0
    assert audit["gateway_identity_route"]["non_streaming_requests"] == 0
    assert audit["model_visible_request_plan"]["unclassified_item_types"] == []
    assert {
        variant["tool_choice"]
        for variant in audit["model_visible_request_plan"]["plan_variants"]
    } == {"auto"}
    assert audit["runtime_timeline"]["config_written_after_app_server_start"] is True
    assert audit["runtime_timeline"]["gateway_requests_after_app_server_start"] == 0
    assert audit["recovery_observation"]["route_level_cause"] == "unknown"
    assert audit["recovery_observation"]["intervening_shared_state_mutation"] is False

    serialized = json.dumps(audit, sort_keys=True)
    assert "http://" not in serialized
    assert "https://" not in serialized
    assert ".codex" not in serialized.lower()
    assert not re.search(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", serialized)
    assert not re.search(r'(?<![A-Za-z0-9])[a-f0-9]{64}(?![A-Za-z0-9])', serialized)


def test_audit_detects_generic_response_body_fingerprint_fields(tmp_path: Path) -> None:
    module = load_audit_module()
    codex_db = tmp_path / "codex.sqlite"
    gateway_db = tmp_path / "gateway.sqlite"
    create_codex_log_db(codex_db)
    create_gateway_db(gateway_db, response_fingerprint_column=True)

    audit = run_audit(module, codex_db, gateway_db)

    assert (
        audit["gateway_identity_route"]["response_body_fingerprint_fields_present"]
        is True
    )


def test_full_pre_post_stays_live_when_request_hmacs_differ(tmp_path: Path) -> None:
    module = load_audit_module()
    codex_db = tmp_path / "codex.sqlite"
    gateway_db = tmp_path / "gateway.sqlite"
    create_codex_log_db(codex_db)
    create_gateway_db(
        gateway_db,
        request_hmac_pair=("caller-value", "upstream-value"),
        response_fingerprint_column=True,
    )

    audit = run_audit(module, codex_db, gateway_db)

    identity = audit["gateway_identity_route"]
    assert identity["full_body_hmac_equal"] == 0
    assert identity["full_body_hmac_mismatch"] == 1
    assert identity["full_body_hmac_unavailable"] == 0
    assert (
        audit["gate_classification"]["full_pre_post_request_response"]
        == "live_control_required"
    )


def test_full_pre_post_stays_live_when_response_fingerprints_are_empty(
    tmp_path: Path,
) -> None:
    module = load_audit_module()
    codex_db = tmp_path / "codex.sqlite"
    gateway_db = tmp_path / "gateway.sqlite"
    create_codex_log_db(codex_db)
    create_gateway_db(
        gateway_db,
        request_hmac_pair=("same-request-value", "same-request-value"),
        response_fingerprint_pair=(None, ""),
    )

    audit = run_audit(module, codex_db, gateway_db)

    identity = audit["gateway_identity_route"]
    assert identity["response_body_fingerprint_equal"] == 0
    assert identity["response_body_fingerprint_mismatch"] == 0
    assert identity["response_body_fingerprint_unavailable"] == 1
    assert (
        audit["gate_classification"]["full_pre_post_request_response"]
        == "live_control_required"
    )


def test_zero_unclassified_identity_rejects_unavailable_prefixes(
    tmp_path: Path,
) -> None:
    module = load_audit_module()
    codex_db = tmp_path / "codex.sqlite"
    gateway_db = tmp_path / "gateway.sqlite"
    create_codex_log_db(codex_db)
    create_gateway_db(gateway_db, prefix_available=False)

    audit = run_audit(module, codex_db, gateway_db)

    assert audit["model_visible_request_plan"]["unclassified_item_types"] == []
    assert audit["gateway_identity_route"]["prefix_equal"] == 0
    assert audit["gateway_identity_route"]["prefix_mismatch"] == 0
    assert audit["gateway_identity_route"]["prefix_unavailable"] == 1
    assert audit["gate_classification"]["zero_unclassified_identity"] == "not_met"


@pytest.mark.parametrize("prefix_pair", [("", ""), ("   ", "   ")])
def test_zero_unclassified_identity_rejects_blank_prefixes(
    tmp_path: Path,
    prefix_pair: tuple[str, str],
) -> None:
    module = load_audit_module()
    codex_db = tmp_path / "codex.sqlite"
    gateway_db = tmp_path / "gateway.sqlite"
    create_codex_log_db(codex_db)
    create_gateway_db(gateway_db, prefix_pair=prefix_pair)

    audit = run_audit(module, codex_db, gateway_db)

    identity = audit["gateway_identity_route"]
    assert identity["prefix_equal"] == 0
    assert identity["prefix_mismatch"] == 0
    assert identity["prefix_unavailable"] == 1
    assert audit["gate_classification"]["zero_unclassified_identity"] == "not_met"


@pytest.mark.parametrize(
    "tool_choice",
    [True, 7, 1.5, "sometimes", {"unexpected": "shape"}],
)
def test_choice_controls_reject_unclassified_and_invalid_scalar_shapes(
    tmp_path: Path,
    tool_choice: object,
) -> None:
    module = load_audit_module()
    codex_db = tmp_path / "codex.sqlite"
    gateway_db = tmp_path / "gateway.sqlite"
    create_codex_log_db(codex_db, gateway_tool_choice=tool_choice)
    create_gateway_db(gateway_db)

    audit = run_audit(module, codex_db, gateway_db)

    assert [
        variant["tool_choice"]
        for variant in audit["model_visible_request_plan"]["plan_variants"]
    ] == ["unclassified"]
    assert audit["gate_classification"]["choice_controls"] == "unclassified"


@pytest.mark.parametrize(
    "tool_choice",
    [
        {"type": "function", "name": "   "},
        {
            "type": "function",
            "name": "first_selector",
            "function": {"name": "second_selector"},
        },
        {"type": "function", "name": "selector", "unexpected": True},
        {"type": "function", "function": {"name": "selector"}},
    ],
)
def test_choice_controls_reject_malformed_function_objects(
    tmp_path: Path,
    tool_choice: object,
) -> None:
    module = load_audit_module()
    codex_db = tmp_path / "codex.sqlite"
    gateway_db = tmp_path / "gateway.sqlite"
    create_codex_log_db(codex_db, gateway_tool_choice=tool_choice)
    create_gateway_db(gateway_db)

    audit = run_audit(module, codex_db, gateway_db)

    assert [
        variant["tool_choice"]
        for variant in audit["model_visible_request_plan"]["plan_variants"]
    ] == ["unclassified"]
    assert audit["gate_classification"]["choice_controls"] == "unclassified"
