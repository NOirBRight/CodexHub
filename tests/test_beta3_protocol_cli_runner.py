from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import qualify_beta3_protocol_cli as runner
from scripts import validate_issue_278_evidence as validator


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "qualify_beta3_protocol_cli.py"


def _minimal_case(case_id: str = "native_explicit") -> dict[str, object]:
    explicit = case_id.endswith("explicit")
    path = "/v1/chat/completions" if case_id.startswith("adapted") else "/v1/responses"
    protocol = "chat_completions" if case_id.startswith("adapted") else "responses"
    # The fixture observes the provider-local model after Gateway binding.
    model_digest = validator._digest("glm-5.2")
    provider_digest = validator._digest(f"Bearer {validator.FIXTURE_KEY_BY_CASE[case_id]}")
    route_material = {
        "paths": [path],
        "model_digests": [model_digest],
        "provider_digests": [provider_digest],
        "protocols": [protocol],
    }
    gateway_records = [{
        "provider_digest": validator._digest(validator.PROVIDER_BY_CASE[case_id]),
        "model_requested_digest": validator._digest(validator.MODEL_BY_CASE[case_id]),
        "model_canonical_digest": validator._digest(validator.MODEL_BY_CASE[case_id]),
        "upstream_model_digest": validator._digest("glm-5.2"),
        "endpoint_digest": validator._digest({
            "scheme": "http",
            "hostname": "127.0.0.1",
            "path": path,
        }),
    }]
    gateway_observation = {
        "request_count": 1,
        "observations": gateway_records,
        "identity_digest": validator._digest({"observations": gateway_records}),
        "source": "gateway_request_complete",
    }
    route_observation = {
        **route_material,
        "request_count": 1,
        "observation_digests": [
            validator._digest(
                {
                    "path": path,
                    "model_digest": model_digest,
                    "provider_digest": provider_digest,
                    "protocol": protocol,
                }
            )
        ],
        "upstream_identity_digest": validator._digest(route_material),
        "gateway": gateway_observation,
        "identity_digest": validator._digest({
            "upstream": validator._digest(route_material),
            "gateway": gateway_observation["identity_digest"],
        }),
        "cross_provider_requests": 0,
    }
    return {
        "id": case_id,
        "protocol": "responses_structured" if case_id.startswith("native") else "chat_tools",
        "disposition": "native" if case_id.startswith("native") else "adapt",
        "planner_eligible": True,
        "tool_search_visible": True,
        "planner_eligibility_source": "client_tool_surface_observed",
        "selection": "selected" if explicit else "model_not_selected",
        "classification": "completed" if explicit else "model_not_selected",
        "sse_event_types": ["response.completed"],
        "history_order_digest": "sha256:" + "0" * 64,
        "identity_preserved": True,
        "identity_observation": {"response_stream": "observed", "request_history": "observed"},
        "mcp_tools_list_count": 1,
        "mcp_tools_call_count": 1 if explicit else 0,
        "workspace_mutation_verified": True,
        "route_identity_digest": route_observation["identity_digest"],
        "route_observation": route_observation,
        "gateway_owned_tool_execution_count": 0,
        "cli_event_shapes": [
            {
                "type": "turn.completed",
                "item_type": None,
                "identity_digest": validator._digest([]),
            }
        ],
        "cli_identity_digest": validator._digest([validator._digest([])]),
        "cli_event_ledger_digest": validator._digest([
            {
                "type": "turn.completed",
                "item_type": None,
                "identity_digest": validator._digest([]),
            }
        ]),
        "cli_event_shape_count": 1,
        "upstream_request_count": 1,
    }


def _explicit_provenance() -> dict[str, object]:
    trace = [
        "tool_search.declaration",
        "tool_search.call",
        "tool_search.result",
        "discovered.declaration",
        "discovered.call",
        "discovered.result",
        "code_mode.shell_command.call",
        "code_mode.shell_command.result",
        "code_mode.apply_patch.call",
        "code_mode.apply_patch.result",
        "code_mode.shell_command.call",
        "code_mode.shell_command.result",
    ]
    response_observation = {
        "tokens": [{"role": "response_id", "digest": "sha256:" + "3" * 64}],
        "identity_digest": validator._digest(
            [{"role": "response_id", "digest": "sha256:" + "3" * 64}]
        ),
    }
    request_observation = {
        "tokens": [{"role": "call_id", "digest": "sha256:" + "4" * 64}],
        "identity_digest": validator._digest(
            [{"role": "call_id", "digest": "sha256:" + "4" * 64}]
        ),
    }
    identity = {
        "response_identity_digests": [response_observation["identity_digest"]] * 6,
        "request_identity_digests": [request_observation["identity_digest"]] * 6,
        "response_identity_observations": [response_observation] * 6,
        "request_identity_observations": [request_observation] * 6,
        "identity_binding_count": 5,
        "search_identity_binding_count": 1,
        "identity_mismatch_count": 0,
        "request_identity_mismatch_count": 0,
    }
    return {
        "trace": trace,
        "trace_digest": "sha256:" + "0" * 64,
        "search": {
            "ordered_stages": trace[:6],
            "stage_count": 6,
            "call_count": 1,
            "result_count": 1,
            "discovered_declaration_count": 1,
            "subsequent_call_count": 1,
            "subsequent_result_count": 1,
            "order_digest": "sha256:" + "0" * 64,
        },
        "code_mode": {
            "ordered_steps": ["shell_command", "apply_patch", "shell_command"],
            "call_count": 3,
            "result_count": 3,
            "order_digest": "sha256:" + "0" * 64,
        },
        "history": {
            "request_count": 6,
            "response_count": 6,
            "protocol_sequence": ["responses"] * 6,
            "response_shapes": [],
            "order_digest": "sha256:" + "0" * 64,
            "identity_digest": validator._digest(identity),
            "identity": identity,
            "diagnostics": {
                "response_stage": "text",
                "awaiting_input": None,
                "pending_call_id_digest": None,
                "protocol_error": None,
            },
        },
    }


def test_runner_summary_is_bounded_and_sanitized() -> None:
    summary = runner._summary("a" * 40, "codex-cli fixture", cases=[], status="not_run", failure="codex_cli_missing")

    encoded = json.dumps(summary, ensure_ascii=True)
    assert summary["schema"] == "codexhub.issue278.cli-tool-search.v1"
    assert summary["evidence_status"] == "observed_synthetic_upstream"
    assert summary["sanitization"] == {
        "raw_bodies_retained": False,
        "prompts_retained": False,
        "credentials_retained": False,
        "ids_opaque_or_hashed": True,
    }
    assert "fixture-target.txt" not in encoded
    assert "FIXTURE_COMPLETE" not in encoded
    assert "Authorization" not in encoded


def test_negative_controls_are_bound_to_candidate_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    report = {
        "status": "pass",
        "cases": [
            {
                "controls": {
                    "unknown_alias": "unknown_alias",
                    "duplicate_identity": "invalid_custom_stream_identity",
                    "malformed_envelope": "invalid_envelope",
                }
            }
        ],
    }

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner._negative_control_statuses(candidate_sha="b" * 40) == {
        "unknown_alias": "passed",
        "duplicate_identity": "passed",
        "malformed_envelope": "passed",
    }
    assert calls and calls[0][-2:] == ["--candidate-sha", "b" * 40]


def test_validator_requires_structured_provenance_for_explicit_case() -> None:
    case = _minimal_case()
    with pytest.raises(validator.EvidenceValidationError, match="provenance_missing"):
        validator.validate_summary(
            {
                "schema": runner.SCHEMA,
                "evidence_status": "observed_synthetic_upstream",
                "qualification_status": "unqualified",
                "candidate_sha": None,
                "route": {
                    "selected_model": "opaque-selected-model",
                    "selected_provider": "opaque-selected-provider",
                    "cross_provider_requests": 0,
                    "hosted_search_substitution": False,
                    "identity_observation": "observed_request_route",
                    "identity_digests": ["sha256:" + "1" * 64],
                },
                "sanitization": {
                    "raw_bodies_retained": False,
                    "prompts_retained": False,
                    "credentials_retained": False,
                    "ids_opaque_or_hashed": True,
                },
                "negative_controls": {
                    "unknown_alias": "passed",
                    "duplicate_identity": "passed",
                    "malformed_envelope": "passed",
                },
                "cases": [case],
            }
        )


def test_validator_rejects_forged_provenance_digest() -> None:
    case = _minimal_case()
    case["provenance"] = _explicit_provenance()
    case["history_order_digest"] = "sha256:" + "0" * 64
    with pytest.raises(validator.EvidenceValidationError, match="provenance_digest_invalid"):
        validator.validate_summary(
            {
                "schema": runner.SCHEMA,
                "evidence_status": "observed_synthetic_upstream",
                "qualification_status": "unqualified",
                "candidate_sha": None,
                "route": {
                    "selected_model": "opaque-selected-model",
                    "selected_provider": "opaque-selected-provider",
                    "cross_provider_requests": 0,
                    "hosted_search_substitution": False,
                    "identity_observation": "observed_request_route",
                    "identity_digests": ["sha256:" + "1" * 64],
                },
                "sanitization": {
                    "raw_bodies_retained": False,
                    "prompts_retained": False,
                    "credentials_retained": False,
                    "ids_opaque_or_hashed": True,
                },
                "negative_controls": {
                    "unknown_alias": "passed",
                    "duplicate_identity": "passed",
                    "malformed_envelope": "passed",
                },
                "cases": [case],
            }
        )


def test_fixture_state_is_explicitly_protocol_controlled() -> None:
    native = runner.FixtureState("native_explicit")
    first = native.record_request(
        "/v1/responses",
        {"tools": [{"type": "tool_search", "execution": "client"}]},
        f"Bearer {runner.FIXTURE_KEY}",
    )
    first_events = native.response_events(first)
    search_item = first_events[1]["item"]
    assert search_item["type"] == "tool_search_call"
    assert search_item["execution"] == "client"
    assert search_item["call_id"] == "fixture-search-call"
    assert "status" not in search_item

    native.record_sse_events(first_events)
    second = native.record_request(
        "/v1/responses",
        {
            "input": [
                {
                    "type": "tool_search_output",
                    "execution": "client",
                    "item_id": "fixture-search-output-item",
                    "call_id": "fixture-search-call",
                    "tools": [{"type": "function", "name": runner.DISCOVERED_TOOL_NAME}],
                }
            ]
        },
        f"Bearer {runner.FIXTURE_KEY}",
    )
    discovered_item = native.response_events(second)[1]["item"]
    assert discovered_item["name"] == runner.DISCOVERED_TOOL_NAME
    assert discovered_item["namespace"] == "mcp__fixture"
    adapted = runner.FixtureState("adapted_explicit")
    adapted_first = adapted.record_request(
        "/v1/chat/completions",
        {"messages": [{"role": "user", "content": "start"}]},
        f"Bearer {runner.FIXTURE_KEY}",
    )
    assert adapted.response_events(adapted_first)[1]["item"]["type"] == "function_call"


def test_fixture_provenance_tracks_explicit_lifecycle_without_ids() -> None:
    state = runner.FixtureState("native_explicit")
    requests = [
        {"tools": [{"type": "tool_search", "execution": "client"}]},
        {
            "input": [
                {
                    "type": "tool_search_output",
                    "execution": "client",
                    "item_id": "fixture-search-output-item",
                    "call_id": "fixture-search-call",
                    "tools": [{"type": "function", "name": runner.DISCOVERED_TOOL_NAME}],
                }
            ]
        },
        {"input": [{"type": "function_call_output", "item_id": "fixture-discovered-output-item", "call_id": "fixture-discovered-call", "output": "ok"}]},
        {"input": [{"type": "function_call_output", "item_id": "fixture-code-output-item-1", "call_id": "fixture-code-call-1", "output": "ok"}]},
        {"input": [{"type": "function_call_output", "item_id": "fixture-code-output-item-2", "call_id": "fixture-code-call-2", "output": "ok"}]},
        {"input": [{"type": "function_call_output", "item_id": "fixture-code-output-item-3", "call_id": "fixture-code-call-3", "output": "ok"}]},
    ]
    for request in requests:
        index = state.record_request("/v1/responses", request, f"Bearer {runner.FIXTURE_KEY}")
        state.record_sse_events(state.response_events(index))

    provenance = runner._provenance_summary(state)
    assert provenance["trace"] == _explicit_provenance()["trace"]
    assert provenance["search"]["call_count"] == 1
    assert provenance["search"]["result_count"] == 1
    assert provenance["search"]["discovered_declaration_count"] == 1
    assert provenance["search"]["subsequent_call_count"] == 1
    assert provenance["search"]["subsequent_result_count"] == 1
    assert provenance["code_mode"]["ordered_steps"] == ["shell_command", "apply_patch", "shell_command"]
    assert provenance["history"]["request_count"] == 6
    assert provenance["history"]["identity"]["identity_binding_count"] == 5
    assert provenance["history"]["identity"]["identity_mismatch_count"] == 0
    assert provenance["history"]["identity"]["request_identity_mismatch_count"] == 0
    assert "response.output_item.added:custom_tool_call" in provenance["history"]["response_shapes"]
    assert provenance["history"]["diagnostics"] == {
        "response_stage": "text",
        "awaiting_input": None,
        "pending_call_id_digest": None,
        "protocol_error": None,
    }
    assert all("id" not in marker and "call_id" not in marker for marker in provenance["history"]["response_shapes"])


def test_responses_fixture_emits_named_sse_events_for_cli_0146() -> None:
    line = runner._event_line({"type": "response.completed", "response": {"status": "completed"}})
    assert line.startswith(b"event: response.completed\n")
    assert b'data: {"type":"response.completed"' in line


def test_chat_fixture_keeps_message_response_as_text() -> None:
    chunks = runner._chat_events(runner._text_response_events(response_id="fixture-text"))

    assert any(
        chunk["choices"][0]["delta"].get("content") == "FIXTURE_COMPLETE"
        for chunk in chunks
    )
    assert not any("tool_calls" in chunk["choices"][0]["delta"] for chunk in chunks)


def test_function_result_observation_ignores_non_string_types() -> None:
    body = {
        "input": [
            {"type": {"nested": True}, "call_id": "other-call"},
            {"type": "function_call_output", "call_id": "target-call"},
        ]
    }
    assert runner._request_has_function_result(body, "target-call")


def test_cli_command_contains_isolation_and_strict_config_controls(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return 0, b""

    monkeypatch.setattr(runner, "_run_cli_bounded", fake_run)
    code, events, identity_digest = runner._run_cli(
        Path("codex.exe"),
        "native_no_hint",
        "opaque/model",
        tmp_path,
        tmp_path,
        1,
        {"CODEX_HOME": str(tmp_path)},
    )

    command = captured["command"]
    assert code == 0
    assert events == []
    assert identity_digest.startswith("sha256:")
    assert isinstance(command, list)
    assert command[:2] == ["codex.exe", "exec"]
    assert "--json" in command
    assert "--ephemeral" in command
    assert "--strict-config" in command
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert "approval_policy=never" in command
    assert "features.apps=false" in command
    assert "-s" in command and command[command.index("-s") + 1] == "workspace-write"


def test_provider_files_are_scoped_and_protocol_bound(tmp_path) -> None:
    runner._write_provider_files(tmp_path, 12345, 23456, adapted=True, model="volc/glm-5.2")

    providers = (tmp_path / "proxy" / "config" / "providers.toml").read_text(encoding="utf-8")
    config = (tmp_path / "config.toml").read_text(encoding="utf-8")
    catalog = json.loads((tmp_path / "model-catalogs" / "codexhub-model-catalog.json").read_text(encoding="utf-8"))

    assert 'base_url = "http://127.0.0.1:12345/v1"' in providers
    assert 'upstream_format = "chat_completions"' in providers
    assert 'available_upstream_formats = ["chat_completions"]' in providers
    assert 'tool_protocol = "chat_tools"' in providers
    assert 'base_url = "http://127.0.0.1:23456/v1"' in config
    assert catalog["models"][0]["slug"] == "volc/glm-5.2"
    assert catalog["models"][0]["codex_proxy_metadata"]["tool_protocol"] == "chat_tools"


def test_runner_missing_cli_writes_bounded_not_run_summary(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--codex",
            str(tmp_path / "missing-codex.exe"),
            "--output",
            str(tmp_path / "out"),
            "--candidate-sha",
            "a" * 40,
            "--case",
            "native_no_hint",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stderr == ""
    summary = json.loads((tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
    assert summary["qualification_status"] == "not_run"
    assert summary["failure"] == "codex_cli_missing"
    assert "fixture_no_hint" not in json.dumps(summary).lower()
