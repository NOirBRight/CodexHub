import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_issue_62_runtime_inventory.py"
INVENTORY = ROOT / "docs" / "evidence" / "issue-62" / "runtime-wire-inventory.json"
TRACE = ROOT / "docs" / "evidence" / "issue-62" / "current-codexhub-thread-tool-surface.json"
WIRE_FIXTURE = ROOT / "docs" / "evidence" / "issue-62" / "codexhub-runtime-wire-fixture.json"
AUDIT = ROOT / "docs" / "evidence" / "issue-62" / "read-only-gate-audit.json"

DISPOSITIONS = (
    "preserved",
    "reversibly_adapted",
    "local_consume",
    "Unsupported",
    "Unqualified",
)


def load_inventory_module():
    spec = importlib.util.spec_from_file_location("issue_62_runtime_inventory", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inventory_is_bound_to_supported_cli_floor_and_candidate_identity() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))

    assert inventory["schema_version"] == 1
    assert inventory["artifact_kind"] == "runtime_wire_inventory"
    assert inventory["cli_version_floor"] == "0.145.0"
    candidate = inventory["candidate_identity"]
    assert candidate["cli_version"] == "0.144.0-alpha.4"
    assert candidate["source_commit"] == "9e552e9d15ba52bed7077d5357f3e18e330f8f38"
    assert candidate["codex_source_commit"] == candidate["source_commit"]
    assert candidate["route_upstream"] == "official"
    assert candidate["inbound_format"] == "responses"
    assert candidate["upstream_format"] == "responses"
    assert candidate["catalog_binding"] == "official Codex catalog entry for openai/gpt-5.6-sol"
    assert candidate["catalog_snapshot_sha256"] == "307a09f22b0c827ae77192a4beaf7059efcf8a698ec4f252aa4ba4787f8d1876"
    assert candidate["catalog_model_entry_id"] == "gpt-5.6-sol"
    assert candidate["route_behavior_profile"] == "official_codex_app_http_passthrough"
    assert len(candidate["evidence_manifest_sha256"]) == 64
    assert inventory["qualification"]["candidate_version_status"] == "legacy_below_floor"
    assert inventory["qualification"]["candidate_version_eligible"] is False
    assert inventory["qualification"]["ready_for_beta1"] is False
    assert inventory["identity_control"]["unknown_tagged_source_count"] == 2
    assert inventory["qualification"]["blocking_gates"] == [
        "clean_cold_start_current_binding",
        "complete_model_visible_plan",
        "error_events",
        "full_pre_post_request_response",
        "full_request_fingerprint",
        "full_response_fingerprint",
        "identity_replay",
        "non_streaming",
        "non_streaming_fixture",
        "sse_identity",
        "terminal_events",
        "wire_identity_replay",
    ]
    assert inventory["qualification"]["evidence_gates"] == {
        "clean_cold_start_current_binding": "not_run",
        "complete_model_visible_plan": "partial",
        "error_events": "not_captured",
        "full_pre_post_request_response": "live_control_required",
        "full_request_fingerprint": "not_captured",
        "full_response_fingerprint": "not_captured",
        "identity_replay": "partial",
        "non_streaming": "live_control_required",
        "non_streaming_fixture": "not_captured",
        "sse_identity": "not_captured",
        "terminal_events": "not_captured",
        "wire_identity_replay": "not_captured",
    }
    assert inventory["evidence_binding"]["trace"]["file"] == TRACE.name
    assert len(inventory["evidence_binding"]["trace"]["sha256"]) == 64


def test_inventory_uses_only_final_capability_disposition_vocabulary() -> None:
    module = load_inventory_module()
    assert module.ALLOWED_DISPOSITIONS == DISPOSITIONS

    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert "live_control_required" not in inventory["disposition_vocabulary"]
    assert all(
        item["disposition"] in DISPOSITIONS for item in inventory["items"]
    )


def test_inventory_covers_every_required_taxonomy_scope() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    scopes = {entry["scope"] for entry in inventory["items"]}
    required = {
        "core_text_streaming",
        "core_text_non_streaming",
        "core_history_multiturn",
        "core_history_item_ids",
        "core_history_call_ids",
        "core_sse_streaming_events",
        "core_sse_terminal_events",
        "core_sse_errors",
        "core_function_declaration",
        "core_function_call",
        "core_function_result",
        "core_function_replay",
        "identity_item_call_ids",
        "identity_response_ids",
        "identity_request_ids",
        "choice_controls",
        "terminal_events",
        "errors",
        "hosted_only_declarations",
        "unknown_tagged_sentinels",
        "default_runtime_fields",
        "code_mode",
        "tool_search",
        "collaboration_v2",
        "chat_conversion",
    }
    missing = required - scopes
    assert not missing, f"missing required scopes: {sorted(missing)}"


def test_every_item_carries_an_allowed_disposition() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))

    for entry in inventory["items"]:
        assert entry["disposition"] in DISPOSITIONS, entry
        assert "evidence_source" in entry
        assert isinstance(entry["evidence_source"], str)
        assert entry["evidence_source"]


def test_core_items_have_preserved_or_reversibly_adapted_disposition() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    by_scope = {entry["scope"]: entry for entry in inventory["items"]}

    preserved_or_adapted = {"preserved", "reversibly_adapted"}
    for scope in (
        "core_text_streaming",
        "core_history_multiturn",
        "core_history_item_ids",
        "core_history_call_ids",
        "core_function_declaration",
        "core_function_call",
        "core_function_result",
        "identity_item_call_ids",
        "identity_response_ids",
        "identity_request_ids",
    ):
        assert by_scope[scope]["disposition"] in preserved_or_adapted, scope

    assert by_scope["core_function_replay"]["disposition"] == "Unqualified"


def test_advanced_capabilities_are_not_supported_or_unqualified() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    by_scope = {entry["scope"]: entry for entry in inventory["items"]}

    for scope in ("code_mode", "tool_search", "collaboration_v2", "chat_conversion"):
        assert by_scope[scope]["disposition"] in {
            "Unsupported",
            "Unqualified",
        }, scope


def test_unqualified_items_remain_blocked_until_live_control() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    by_scope = {entry["scope"]: entry for entry in inventory["items"]}

    for scope in (
        "core_text_non_streaming",
        "choice_controls",
        "core_sse_terminal_events",
        "core_sse_errors",
        "terminal_events",
        "errors",
        "hosted_only_declarations",
        "unknown_tagged_sentinels",
        "default_runtime_fields",
    ):
        entry = by_scope[scope]
        assert entry["disposition"] == "Unqualified", scope


def test_inventory_reports_zero_unclassified_core_items() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))

    assert inventory["identity_control"]["unclassified_core_items"] == 0
    assert inventory["identity_control"]["unclassified_scopes"] == []
    assert inventory["qualification"]["ready_for_beta1"] is False
    assert "core_function_replay" in inventory["qualification"]["blocking_scopes"]


def test_inventory_replay_fails_visibly_on_mutation_deletion_and_loss() -> None:
    module = load_inventory_module()
    base = json.loads(INVENTORY.read_text(encoding="utf-8"))

    for case in ("mutation", "deletion", "loss"):
        mutated = module.replay_inventory(base, case)
        report = module.reconcile_inventory(mutated)
        assert report["reconciled"] is False, case
        assert report["mismatches"], case


def test_inventory_replay_passes_on_identity() -> None:
    module = load_inventory_module()
    base = json.loads(INVENTORY.read_text(encoding="utf-8"))

    report = module.reconcile_inventory(base)
    assert report["reconciled"] is True
    assert report["mismatches"] == []


def test_committed_inventory_preserves_sanitization_boundary() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    serialized = json.dumps(inventory, sort_keys=True)

    for forbidden in (
        "must not be retained",
        "must-not-be-retained",
        "chatgpt.com",
        "127.0.0.1",
        "http://",
        "https://",
    ):
        assert forbidden not in serialized, forbidden
    import re

    assert not re.search(
        r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
        serialized,
        re.IGNORECASE,
    )


def test_build_inventory_reads_existing_artifacts_and_clamps_live_gates() -> None:
    module = load_inventory_module()

    inventory = module.build_inventory(
        trace=TRACE,
        wire_fixture=WIRE_FIXTURE,
        audit=AUDIT,
        cli_version_floor="0.145.0",
        candidate_cli_version="0.144.0-alpha.4",
        candidate_source_commit="9e552e9d15ba52bed7077d5357f3e18e330f8f38",
    )

    assert inventory["schema_version"] == 1
    assert inventory["cli_version_floor"] == "0.145.0"
    assert inventory["candidate_identity"]["cli_version"] == "0.144.0-alpha.4"
    scopes = {entry["scope"] for entry in inventory["items"]}
    assert "core_text_streaming" in scopes
    assert "code_mode" in scopes
    assert inventory["identity_control"]["unclassified_core_items"] == 0
    assert inventory["qualification"]["candidate_version_status"] == "legacy_below_floor"


def test_build_inventory_rejects_candidate_metadata_drift() -> None:
    module = load_inventory_module()

    with pytest.raises(ValueError, match="candidate CLI version"):
        module.build_inventory(
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
            candidate_cli_version="0.145.0",
            candidate_source_commit="9e552e9d15ba52bed7077d5357f3e18e330f8f38",
        )

    with pytest.raises(ValueError, match="candidate source commit"):
        module.build_inventory(
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
            candidate_cli_version="0.144.0-alpha.4",
            candidate_source_commit="0" * 40,
        )


def test_build_inventory_rejects_a_floor_other_than_supported_candidate_floor() -> None:
    module = load_inventory_module()

    with pytest.raises(ValueError, match="supported CLI floor"):
        module.build_inventory(
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
            cli_version_floor="0.144.0",
            candidate_cli_version="0.144.0-alpha.4",
            candidate_source_commit="9e552e9d15ba52bed7077d5357f3e18e330f8f38",
        )


def test_build_inventory_rejects_malformed_candidate_provenance() -> None:
    module = load_inventory_module()

    with pytest.raises(ValueError, match="candidate CLI version"):
        module.build_inventory(
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
            candidate_cli_version="not-a-version",
            candidate_source_commit="9e552e9d15ba52bed7077d5357f3e18e330f8f38",
        )

    with pytest.raises(ValueError, match="candidate source commit"):
        module.build_inventory(
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
            candidate_cli_version="0.144.0-alpha.4",
            candidate_source_commit="A" * 40,
        )


def test_function_replay_stays_live_without_bound_wire_replay_cases() -> None:
    module = load_inventory_module()
    wire = json.loads(WIRE_FIXTURE.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    audit["gate_classification"]["full_pre_post_request_response"] = "met"
    audit["gate_classification"]["zero_unclassified_identity"] = "met"

    items = module._classify_core_items(
        wire,
        audit,
        wire_fixture_sha256=module._sha256_file(WIRE_FIXTURE),
    )
    replay = next(item for item in items if item["scope"] == "core_function_replay")
    assert replay["disposition"] == "Unqualified"


def test_captured_sse_status_does_not_satisfy_independent_sse_gate() -> None:
    module = load_inventory_module()
    trace = json.loads(TRACE.read_text(encoding="utf-8"))
    wire = json.loads(WIRE_FIXTURE.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    audit["sse_identity"] = {"status": "captured"}

    qualification = module._build_qualification(
        [],
        "eligible",
        trace=trace,
        wire=wire,
        audit=audit,
        wire_fixture_sha256=module._sha256_file(WIRE_FIXTURE),
    )
    assert qualification["evidence_gates"]["sse_identity"] == "captured"
    assert "sse_identity" in qualification["blocking_gates"]
    assert qualification["ready_for_beta1"] is False


def test_committed_inventory_matches_generator_output() -> None:
    module = load_inventory_module()

    generated = module.build_inventory(
        trace=TRACE,
        wire_fixture=WIRE_FIXTURE,
        audit=AUDIT,
    )
    committed = json.loads(INVENTORY.read_text(encoding="utf-8"))

    assert generated == committed


def test_inventory_reconcile_rejects_duplicate_and_wrong_core_evidence() -> None:
    module = load_inventory_module()
    base = json.loads(INVENTORY.read_text(encoding="utf-8"))

    duplicate = json.loads(json.dumps(base))
    duplicate["items"].append(json.loads(json.dumps(base["items"][0])))
    report = module.reconcile_inventory(duplicate)
    assert report["reconciled"] is False
    assert any("duplicate scope" in mismatch for mismatch in report["mismatches"])

    wrong_evidence = json.loads(json.dumps(base))
    wrong_evidence["items"][0]["evidence_source"] = "unrelated-fixture.json#claim"
    report = module.reconcile_inventory(wrong_evidence)
    assert report["reconciled"] is False
    assert any("evidence_source" in mismatch for mismatch in report["mismatches"])

    unknown_scope = json.loads(json.dumps(base))
    unknown_scope["items"].append(
        {
            "scope": "future_unclassified_scope",
            "disposition": "Unqualified",
            "evidence_source": "future-fixture.json#scope",
        }
    )
    report = module.reconcile_inventory(unknown_scope)
    assert report["reconciled"] is False
    assert any("unknown scope" in mismatch for mismatch in report["mismatches"])


def test_inventory_reconcile_binds_input_hashes_and_candidate_gates() -> None:
    module = load_inventory_module()
    base = json.loads(INVENTORY.read_text(encoding="utf-8"))
    evidence_root = INVENTORY.parent

    report = module.reconcile_inventory(base, evidence_root=evidence_root)
    assert report == {"reconciled": True, "mismatches": []}

    tampered_hash = json.loads(json.dumps(base))
    tampered_hash["evidence_binding"]["trace"]["sha256"] = "0" * 64
    report = module.reconcile_inventory(tampered_hash, evidence_root=evidence_root)
    assert report["reconciled"] is False
    assert any(
        "stale" in mismatch or "hash mismatch" in mismatch
        for mismatch in report["mismatches"]
    )

    tampered_status = json.loads(json.dumps(base))
    tampered_status["qualification"]["candidate_version_status"] = "eligible"
    report = module.reconcile_inventory(tampered_status)
    assert report["reconciled"] is False
    assert any("CLI floor" in mismatch for mismatch in report["mismatches"])

    tampered_unknown_count = json.loads(json.dumps(base))
    tampered_unknown_count["identity_control"]["unknown_tagged_source_count"] = 1
    report = module.reconcile_inventory(
        tampered_unknown_count, evidence_root=evidence_root
    )
    assert report["reconciled"] is False
    assert any("unknown_tagged_source_count" in mismatch for mismatch in report["mismatches"])

    tampered_identity_control = json.loads(json.dumps(base))
    tampered_identity_control["identity_control"]["fail_closed"] = False
    tampered_identity_control["identity_control"]["replay_cases"] = []
    report = module.reconcile_inventory(tampered_identity_control)
    assert report["reconciled"] is False
    assert any("identity_control" in mismatch for mismatch in report["mismatches"])


def test_inventory_reconcile_rejects_generated_artifact_drift() -> None:
    module = load_inventory_module()
    base = json.loads(INVENTORY.read_text(encoding="utf-8"))
    drifted = json.loads(json.dumps(base))
    drifted["items"][0]["notes"] = "hand-edited stale note"

    report = module.reconcile_inventory(drifted, evidence_root=INVENTORY.parent)

    assert report["reconciled"] is False
    assert any("generated inventory" in mismatch for mismatch in report["mismatches"])


def test_qualification_accepts_audit_met_status_when_all_gates_are_complete() -> None:
    module = load_inventory_module()
    qualification = module._build_qualification(
        [],
        "eligible",
        trace={
            "gateway_observability": {
                "full_request_body_fingerprint": "captured",
                "full_response_body_fingerprint": "captured",
            },
            "capture_coverage": {
                "complete_model_visible_plan": {"status": "complete"},
                "clean_cold_start_current_binding": {"status": "complete"},
            }
        },
        wire={
            "response": {
                "streaming": {
                    "events": [
                        {"event": "response.completed"},
                        {"event": "response.error"},
                    ]
                },
                "non_streaming": {
                    "captured": True,
                    "fixture_kind": "real_capture",
                    "request_stream": False,
                    "response_items": [{"type": "message"}],
                },
            }
        },
        audit={
            "gateway_identity_route": {
                "full_body_hmac_pairs": 1,
                "request_starts": 1,
                "full_body_hmac_equal": 1,
                "full_body_hmac_mismatch": 0,
                "full_body_hmac_unavailable": 0,
                "response_body_fingerprint_fields_present": True,
                "response_body_fingerprint_equal": 1,
                "response_body_fingerprint_mismatch": 0,
                "response_body_fingerprint_unavailable": 0,
            },
            "sse_identity": {
                "status": "met",
                "fail_closed": True,
                "wire_fixture_sha256": "a" * 64,
                "pre_stream_sequence_sha256": "b" * 64,
                "post_stream_sequence_sha256": "b" * 64,
                "event_count": 2,
            },
            "gate_classification": {
                "full_pre_post_request_response": "met",
                "full_request_body_fingerprint": "captured",
                "full_response_body_fingerprint": "captured",
                "non_streaming": "met",
                "zero_unclassified_identity": "met",
            },
            "wire_identity_replay": {
                "status": "met",
                "fail_closed": True,
                "wire_fixture_sha256": "a" * 64,
                "cases": {
                    "identity": {"status": "met", "observed": True, "output_sha256": "c" * 64},
                    "mutation": {"status": "met", "observed": True, "output_sha256": "d" * 64},
                    "deletion": {"status": "met", "observed": True, "output_sha256": "e" * 64},
                    "loss": {"status": "met", "observed": True, "output_sha256": "f" * 64},
                },
            },
        },
    )
    assert qualification["blocking_gates"] == []
    assert qualification["ready_for_beta1"] is True


@pytest.mark.parametrize("case", ["mutation", "deletion", "loss"])
def test_replay_cases_each_touch_a_distinct_scope(case: str) -> None:
    module = load_inventory_module()
    base = json.loads(INVENTORY.read_text(encoding="utf-8"))

    mutated = module.replay_inventory(base, case)
    report = module.reconcile_inventory(mutated)
    assert not report["reconciled"]
    assert any(case in mismatch for mismatch in report["mismatches"])
