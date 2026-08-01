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
    "live_control_required",
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
    assert candidate["route_upstream"] == "official"
    assert candidate["inbound_format"] == "responses"
    assert candidate["upstream_format"] == "responses"
    assert inventory["qualification"]["candidate_version_status"] == "legacy_below_floor"
    assert inventory["qualification"]["candidate_version_eligible"] is False
    assert inventory["qualification"]["ready_for_beta1"] is False
    assert inventory["evidence_binding"]["trace"]["file"] == TRACE.name
    assert len(inventory["evidence_binding"]["trace"]["sha256"]) == 64


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

    assert by_scope["core_function_replay"]["disposition"] == "live_control_required"


def test_advanced_capabilities_are_not_supported_or_unqualified() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    by_scope = {entry["scope"]: entry for entry in inventory["items"]}

    for scope in ("code_mode", "tool_search", "collaboration_v2", "chat_conversion"):
        assert by_scope[scope]["disposition"] in {
            "Unsupported",
            "Unqualified",
        }, scope


def test_live_control_gates_are_marked_live_control_required() -> None:
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
        assert entry["disposition"] == "live_control_required", scope


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


@pytest.mark.parametrize("case", ["mutation", "deletion", "loss"])
def test_replay_cases_each_touch_a_distinct_scope(case: str) -> None:
    module = load_inventory_module()
    base = json.loads(INVENTORY.read_text(encoding="utf-8"))

    mutated = module.replay_inventory(base, case)
    report = module.reconcile_inventory(mutated)
    assert not report["reconciled"]
    assert any(case in mismatch for mismatch in report["mismatches"])
