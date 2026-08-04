import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_issue_62_runtime_inventory.py"
INVENTORY = ROOT / "docs" / "evidence" / "issue-62" / "runtime-wire-inventory.json"
SOURCE_CONTRACT = ROOT / "docs" / "evidence" / "issue-62" / "codex-0.146-source-contract.json"
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
    assert inventory["cli_version_floor"] == "0.146.0"
    candidate = inventory["candidate_identity"]
    assert candidate["cli_version"] == "0.146.0"
    assert candidate["source_commit"] == "e363b08c9175ac1cbe5893615dd2cb9ddf95043b"
    assert candidate["codex_source_commit"] == candidate["source_commit"]
    assert candidate["candidate_revision"] == "accab8ff6eb4d6ebd93cda84585fb5f6cb89da82"
    assert candidate["cli_binary_sha256"] == "bc343ba420dc2e2e9f59e6fc5e5bf0aae1cd8c771fc319665241fc9c0271fddb"
    assert candidate["cli_source_commit_status"] == "published_attested"
    assert candidate["cli_source_tag"] == "rust-v0.146.0"
    assert candidate["route_upstream"] == "official"
    assert candidate["inbound_format"] == "responses"
    assert candidate["upstream_format"] == "responses"
    assert candidate["catalog_binding"] == "official Codex catalog entry for openai/gpt-5.6-sol"
    assert candidate["catalog_snapshot_sha256"] == "307a09f22b0c827ae77192a4beaf7059efcf8a698ec4f252aa4ba4787f8d1876"
    assert candidate["catalog_model_entry_id"] == "gpt-5.6-sol"
    assert candidate["route_behavior_profile"] == "official_codex_app_http_passthrough"
    assert len(candidate["evidence_manifest_sha256"]) == 64
    assert inventory["qualification"]["candidate_version_status"] == "eligible"
    assert inventory["qualification"]["candidate_version_eligible"] is True
    assert inventory["qualification"]["ready_for_beta2"] is False
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
    assert inventory["evidence_binding"]["source_contract"]["file"] == SOURCE_CONTRACT.name
    assert len(inventory["evidence_binding"]["source_contract"]["sha256"]) == 64


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


def test_inventory_records_all_structural_declaration_families() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))

    families = inventory["declaration_families"]
    assert [entry["family"] for entry in families] == [
        "plain_function",
        "custom_freeform",
        "namespace",
        "client_executed_tool_discovery",
        "selected_provider_hosted",
        "unknown_future_kind",
    ]
    by_family = {entry["family"]: entry for entry in families}
    assert by_family["client_executed_tool_discovery"]["executor"] == "codex_client"
    assert by_family["selected_provider_hosted"]["executor"] == "selected_provider"
    assert by_family["selected_provider_hosted"]["representative"]["cross_provider_proxy"] == "forbidden"
    assert by_family["unknown_future_kind"]["selected_protocol_disposition"] == "omit"
    assert by_family["plain_function"]["representative"]["streaming"]["arguments_done"] == "response.function_call_arguments.done"
    assert by_family["custom_freeform"]["representative"]["streaming"]["input_done"] == "response.custom_tool_call_input.done"
    assert by_family["client_executed_tool_discovery"]["representative"]["streaming"]["event_order"] == [
        "response.output_item.done",
        "response.completed",
    ]
    for entry in families:
        representative = entry["representative"]
        assert representative["terminal"]["classification"] in {"not_observed", "unqualified"}
        assert representative["error"]["classification"] in {"not_observed", "unqualified"}
        assert representative["loss_boundary"]


def test_declaration_family_evidence_sources_resolve_to_bound_fixtures() -> None:
    module = load_inventory_module()
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    wire = json.loads(WIRE_FIXTURE.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))

    module._validate_structural_evidence_pointers(
        source_contract=json.loads(SOURCE_CONTRACT.read_text(encoding="utf-8")),
        wire=wire,
        audit=audit,
        declaration_families=inventory["declaration_families"],
    )

    dangling = json.loads(json.dumps(inventory["declaration_families"]))
    dangling[-1]["evidence_source"] = (
        "codex-0.146-source-contract.json#runtime_wire_surface.unknown_future"
    )
    with pytest.raises(ValueError, match="evidence source is invalid"):
        module._validate_structural_evidence_pointers(
            source_contract=json.loads(SOURCE_CONTRACT.read_text(encoding="utf-8")),
            wire=wire,
            audit=audit,
            declaration_families=dangling,
        )


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
    assert inventory["qualification"]["ready_for_beta2"] is False
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
        cli_version_floor="0.146.0",
        candidate_cli_version="0.146.0",
        candidate_source_commit="e363b08c9175ac1cbe5893615dd2cb9ddf95043b",
    )

    assert inventory["schema_version"] == 1
    assert inventory["cli_version_floor"] == "0.146.0"
    assert inventory["candidate_identity"]["cli_version"] == "0.146.0"
    scopes = {entry["scope"] for entry in inventory["items"]}
    assert "core_text_streaming" in scopes
    assert "code_mode" in scopes
    assert inventory["identity_control"]["unclassified_core_items"] == 0
    assert inventory["qualification"]["candidate_version_status"] == "eligible"


def test_build_inventory_rejects_candidate_metadata_drift() -> None:
    module = load_inventory_module()

    with pytest.raises(ValueError, match="candidate CLI version"):
        module.build_inventory(
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
            candidate_cli_version="0.145.0",
            candidate_source_commit="e363b08c9175ac1cbe5893615dd2cb9ddf95043b",
        )

    with pytest.raises(ValueError, match="candidate source commit"):
        module.build_inventory(
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
            candidate_cli_version="0.146.0",
            candidate_source_commit="0" * 40,
        )


def test_build_inventory_rejects_a_floor_other_than_supported_candidate_floor() -> None:
    module = load_inventory_module()

    with pytest.raises(ValueError, match="supported CLI floor"):
        module.build_inventory(
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
            cli_version_floor="0.145.0",
            candidate_cli_version="0.146.0",
            candidate_source_commit="e363b08c9175ac1cbe5893615dd2cb9ddf95043b",
        )


def test_build_inventory_rejects_malformed_candidate_provenance() -> None:
    module = load_inventory_module()

    with pytest.raises(ValueError, match="candidate CLI version"):
        module.build_inventory(
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
            candidate_cli_version="not-a-version",
            candidate_source_commit="e363b08c9175ac1cbe5893615dd2cb9ddf95043b",
        )

    with pytest.raises(ValueError, match="candidate source commit"):
        module.build_inventory(
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
            candidate_cli_version="0.146.0",
            candidate_source_commit="A" * 40,
        )


def test_build_inventory_rejects_missing_capture_provenance(tmp_path: Path) -> None:
    module = load_inventory_module()
    trace = json.loads(TRACE.read_text(encoding="utf-8"))
    trace["source"].pop("capture_id", None)
    trace_path = tmp_path / TRACE.name
    trace_path.write_text(json.dumps(trace, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="capture_id"):
        module.build_inventory(
            trace=trace_path,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
        )


def test_build_inventory_rejects_audit_candidate_provenance_drift(tmp_path: Path) -> None:
    module = load_inventory_module()
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    audit["provenance"]["source_commit"] = "0" * 40
    audit_path = tmp_path / AUDIT.name
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="audit provenance"):
        module.build_inventory(
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=audit_path,
        )


def test_build_inventory_rejects_audit_historical_capture_drift(tmp_path: Path) -> None:
    module = load_inventory_module()
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    audit["provenance"]["historical_capture"]["captured_at"] = "2026-07-13T14:57:55+08:00"
    audit_path = tmp_path / AUDIT.name
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="historical capture"):
        module.build_inventory(
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=audit_path,
        )


def test_build_inventory_rejects_observed_source_contract_family(tmp_path: Path) -> None:
    module = load_inventory_module()
    source_contract = json.loads(SOURCE_CONTRACT.read_text(encoding="utf-8"))
    source_contract["runtime_wire_surface"]["declaration_families"][0]["observed"] = True
    source_contract_path = tmp_path / SOURCE_CONTRACT.name
    source_contract_path.write_text(
        json.dumps(source_contract, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="cannot claim an observed"):
        module.build_inventory(
            source_contract=source_contract_path,
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
        )


@pytest.mark.parametrize(
    "family, field, value",
    [
        ("selected_provider_hosted", "status", "captured"),
        ("unknown_future_kind", "status", "captured"),
        ("plain_function", "status", "future_status"),
        ("plain_function", "unknown_field", "future_status"),
    ],
)
def test_build_inventory_rejects_source_contract_status_or_unknown_fields(
    tmp_path: Path, family: str, field: str, value: str
) -> None:
    module = load_inventory_module()
    source_contract = json.loads(SOURCE_CONTRACT.read_text(encoding="utf-8"))
    source_contract["runtime_wire_surface"]["declaration_family_examples"][family][
        field
    ] = value
    source_contract_path = tmp_path / SOURCE_CONTRACT.name
    source_contract_path.write_text(
        json.dumps(source_contract, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(
        ValueError, match="(?:status is invalid|unknown status field|unknown example fields)"
    ):
        module.build_inventory(
            source_contract=source_contract_path,
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
        )


def test_build_inventory_rejects_captured_source_contract_control(tmp_path: Path) -> None:
    module = load_inventory_module()
    source_contract = json.loads(SOURCE_CONTRACT.read_text(encoding="utf-8"))
    source_contract["runtime_wire_surface"]["request_shape"]["non_streaming_control"][
        "captured"
    ] = True
    source_contract_path = tmp_path / SOURCE_CONTRACT.name
    source_contract_path.write_text(
        json.dumps(source_contract, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="captured non-streaming"):
        module.build_inventory(
            source_contract=source_contract_path,
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "runtime_type",
        "wire_declaration_type",
        "declaration_type",
        "call_type",
        "result_type",
    ],
)
def test_build_inventory_rejects_source_contract_schema_mutations(
    tmp_path: Path, mutation: str
) -> None:
    module = load_inventory_module()
    source_contract = json.loads(SOURCE_CONTRACT.read_text(encoding="utf-8"))
    if mutation in {"runtime_type", "wire_declaration_type"}:
        source_contract["runtime_wire_surface"]["declaration_families"][0][mutation] = "bogus"
    else:
        source_contract["runtime_wire_surface"]["declaration_family_examples"]["plain_function"][
            {"declaration_type": "declaration", "call_type": "call", "result_type": "result"}[mutation]
        ]["type"] = "web_search_call"
    source_contract_path = tmp_path / SOURCE_CONTRACT.name
    source_contract_path.write_text(
        json.dumps(source_contract, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="canonical family schema"):
        module.build_inventory(
            source_contract=source_contract_path,
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "plain_call_item_id_empty",
        "plain_history_call_id_empty",
        "namespace_owner_empty",
        "plain_added_event",
        "custom_delta_event",
        "namespace_done_event",
        "tool_search_added_event",
        "unknown_event_label",
    ],
)
def test_build_inventory_rejects_source_contract_identity_and_sse_mutations(
    tmp_path: Path, mutation: str
) -> None:
    module = load_inventory_module()
    source_contract = json.loads(SOURCE_CONTRACT.read_text(encoding="utf-8"))
    examples = source_contract["runtime_wire_surface"]["declaration_family_examples"]
    if mutation == "plain_call_item_id_empty":
        examples["plain_function"]["call"]["item_id"] = ""
    elif mutation == "plain_history_call_id_empty":
        examples["plain_function"]["history"]["call_id"] = ""
    elif mutation == "namespace_owner_empty":
        examples["namespace"]["call"]["namespace"] = ""
    elif mutation == "plain_added_event":
        examples["plain_function"]["streaming"]["added"] = "bogus.event"
    elif mutation == "custom_delta_event":
        examples["custom_freeform"]["streaming"]["delta"] = "bogus.event"
    elif mutation == "namespace_done_event":
        examples["namespace"]["streaming"]["arguments_done"] = "bogus.event"
    elif mutation == "tool_search_added_event":
        examples["client_executed_tool_discovery"]["streaming"]["added"] = (
            "response.output_item.added"
        )
    elif mutation == "unknown_event_label":
        examples["unknown_future_kind"]["streaming"]["event_order"][0] = "bogus.event"
    source_contract_path = tmp_path / SOURCE_CONTRACT.name
    source_contract_path.write_text(
        json.dumps(source_contract, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="(?:non-empty string|canonical SSE)"):
        module.build_inventory(
            source_contract=source_contract_path,
            trace=TRACE,
            wire_fixture=WIRE_FIXTURE,
            audit=AUDIT,
        )


def test_inventory_reconcile_rejects_mutated_family_schema_without_evidence_root() -> None:
    module = load_inventory_module()
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    inventory["declaration_families"][0]["representative"]["call"]["type"] = "web_search_call"

    report = module.reconcile_inventory(inventory)

    assert report["reconciled"] is False
    assert any("canonical family schema" in mismatch for mismatch in report["mismatches"])

    id_mutation = json.loads(INVENTORY.read_text(encoding="utf-8"))
    id_mutation["declaration_families"][0]["representative"]["call"]["item_id"] = ""
    id_report = module.reconcile_inventory(id_mutation)
    assert id_report["reconciled"] is False
    assert any("non-empty string" in mismatch for mismatch in id_report["mismatches"])


def test_inventory_reconcile_rejects_observed_family_without_evidence_root() -> None:
    module = load_inventory_module()
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    inventory["declaration_families"][0]["observed"] = True

    report = module.reconcile_inventory(inventory)

    assert report["reconciled"] is False
    assert any("observed must remain false" in mismatch for mismatch in report["mismatches"])


def test_structural_reconcile_allows_observed_family_when_bound_evidence_is_authoritative() -> None:
    module = load_inventory_module()
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    inventory["declaration_families"][0]["observed"] = True

    assert not any(
        "observed must remain false"
        in mismatch
        for mismatch in module._structural_inventory_mismatches(
            inventory["declaration_families"], require_unobserved=False
        )
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("source_commit", "0" * 40),
        ("route_upstream", "custom"),
        ("model", "gpt-5.5"),
    ],
)
def test_inventory_reconcile_rejects_candidate_binding_mutation_without_evidence_root(
    field: str, value: str
) -> None:
    module = load_inventory_module()
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    inventory["candidate_identity"][field] = value

    report = module.reconcile_inventory(inventory)

    assert report["reconciled"] is False
    assert any("retained Issue #62 candidate" in mismatch for mismatch in report["mismatches"])


def test_inventory_reconcile_rejects_evidence_pointer_and_self_reported_readiness_mutation() -> None:
    module = load_inventory_module()
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    inventory["evidence_binding"]["trace"]["file"] = "evil.json"
    inventory["qualification"]["evidence_gates"] = {
        gate: "complete" for gate in inventory["qualification"]["evidence_gates"]
    }
    inventory["qualification"]["blocking_gates"] = []
    inventory["qualification"]["ready_for_beta2"] = True

    report = module.reconcile_inventory(inventory)

    assert report["reconciled"] is False
    assert any("retained fixture" in mismatch for mismatch in report["mismatches"])
    assert any("cannot be asserted without bound evidence" in mismatch for mismatch in report["mismatches"])
    assert any("blocking_gates cannot be empty" in mismatch for mismatch in report["mismatches"])


def test_inventory_reconcile_rejects_legacy_beta1_readiness_key() -> None:
    module = load_inventory_module()
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    inventory["qualification"]["ready_for_beta1"] = inventory["qualification"].pop(
        "ready_for_beta2"
    )

    report = module.reconcile_inventory(inventory)

    assert report["reconciled"] is False
    assert any("ready_for_beta1 is stale" in mismatch for mismatch in report["mismatches"])


def test_build_inventory_rejects_unbound_response_identity_pointer(tmp_path: Path) -> None:
    module = load_inventory_module()
    wire = json.loads(WIRE_FIXTURE.read_text(encoding="utf-8"))
    wire["post_gateway"]["response"]["streaming"]["response_id"] = "response_other"
    wire_path = tmp_path / WIRE_FIXTURE.name
    wire_path.write_text(json.dumps(wire, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="identity_response_ids"):
        module.build_inventory(
            trace=TRACE,
            wire_fixture=wire_path,
            audit=AUDIT,
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
    assert qualification["ready_for_beta2"] is False


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

    structural_mutation = json.loads(json.dumps(base))
    structural_mutation["declaration_families"][4]["representative"]["cross_provider_proxy"] = "allowed"
    report = module.reconcile_inventory(structural_mutation)
    assert report["reconciled"] is False
    assert any("declaration_families" in mismatch for mismatch in report["mismatches"])


def test_inventory_reconcile_rejects_provenance_contradictions_without_evidence_root() -> None:
    module = load_inventory_module()
    base = json.loads(INVENTORY.read_text(encoding="utf-8"))

    contradictory = json.loads(json.dumps(base))
    contradictory["candidate_identity"]["codex_source_commit"] = "0" * 40
    report = module.reconcile_inventory(contradictory)

    assert report["reconciled"] is False
    assert any("contradict" in mismatch for mismatch in report["mismatches"])


def test_inventory_reconcile_rejects_nonfinal_core_disposition_without_evidence_root() -> None:
    module = load_inventory_module()
    base = json.loads(INVENTORY.read_text(encoding="utf-8"))

    mutated = json.loads(json.dumps(base))
    mutated["items"] = [
        {
            **item,
            "disposition": "local_consume",
        }
        if item["scope"] == "core_text_streaming"
        else item
        for item in mutated["items"]
    ]
    report = module.reconcile_inventory(mutated)

    assert report["reconciled"] is False
    assert any("final" in mismatch for mismatch in report["mismatches"])


def test_inventory_reconcile_binds_every_scope_evidence_source() -> None:
    module = load_inventory_module()
    base = json.loads(INVENTORY.read_text(encoding="utf-8"))

    mutated = json.loads(json.dumps(base))
    mutated["items"] = [
        {
            **item,
            "evidence_source": "unrelated-fixture.json#claim",
        }
        if item["scope"] == "tool_search"
        else item
        for item in mutated["items"]
    ]
    report = module.reconcile_inventory(mutated)

    assert report["reconciled"] is False
    assert any("expected fixture path/scope" in mismatch for mismatch in report["mismatches"])


def test_identity_response_evidence_source_is_a_bound_pre_post_pair() -> None:
    module = load_inventory_module()
    base = json.loads(INVENTORY.read_text(encoding="utf-8"))
    identity = next(
        item for item in base["items"] if item["scope"] == "identity_response_ids"
    )

    assert identity["evidence_source"] == module.IDENTITY_RESPONSE_EVIDENCE
    assert module.reconcile_inventory(base, evidence_root=INVENTORY.parent) == {
        "reconciled": True,
        "mismatches": [],
    }

    stale_pointer = json.loads(json.dumps(base))
    stale_pointer["items"] = [
        {
            **item,
            "evidence_source": "codexhub-runtime-wire-fixture.json#response.streaming.response_id",
        }
        if item["scope"] == "identity_response_ids"
        else item
        for item in stale_pointer["items"]
    ]
    report = module.reconcile_inventory(stale_pointer)
    assert report["reconciled"] is False
    assert any("expected fixture path/scope" in mismatch for mismatch in report["mismatches"])


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
    tampered_status["qualification"]["candidate_version_status"] = "legacy_below_floor"
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
    assert qualification["ready_for_beta2"] is True


@pytest.mark.parametrize("case", ["mutation", "deletion", "loss"])
def test_replay_cases_each_touch_a_distinct_scope(case: str) -> None:
    module = load_inventory_module()
    base = json.loads(INVENTORY.read_text(encoding="utf-8"))

    mutated = module.replay_inventory(base, case)
    report = module.reconcile_inventory(mutated)
    assert not report["reconciled"]
    assert any(case in mismatch for mismatch in report["mismatches"])
