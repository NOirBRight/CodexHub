from __future__ import annotations

import json

import pytest

from scripts import beta42_evidence as evidence


def _case(case_id: str, *, outcome: str = "not_run", model: str = "fixture-model") -> dict[str, object]:
    return {
        "id": case_id,
        "outcome": outcome,
        "protocol": "responses",
        "model": model,
    }


def _summary(
    *,
    outcome: str = "not_run",
    cases: list[dict[str, object]] | None = None,
    complete: bool = True,
) -> dict[str, object]:
    supplied = {str(case["id"]): case for case in cases or []}
    if complete:
        supplied.update(
            {
                case_id: _case(case_id)
                for case_id in evidence.CASE_IDS
                if case_id not in supplied
            }
        )
    return {
        "schema": evidence.SCHEMA,
        "candidate_sha": "a" * 40,
        "cli_version": evidence.CLI_VERSION,
        "outcome": outcome,
        "failure_classification": "none" if outcome == "passed" else "not_run",
        "upstream_v2_boundary": evidence.UPSTREAM_BLOCKED,
        "cases": list(supplied.values()) or [_case("v1-lifecycle")],
    }


def test_parse_summary_accepts_json_bytes_and_rejects_non_object_roots() -> None:
    parsed = evidence.parse_summary(json.dumps(_summary()).encode("utf-8"))
    assert parsed["schema"] == evidence.SCHEMA

    with pytest.raises(evidence.EvidenceValidationError, match="summary_root_invalid"):
        evidence.parse_summary(b"[]")


def test_validator_rejects_secret_or_raw_request_fields() -> None:
    summary = _summary()
    summary["authorization"] = "Bearer redacted"
    with pytest.raises(evidence.EvidenceValidationError, match="secret_or_raw_field"):
        evidence.validate_summary(summary)


def test_validator_rejects_passing_summary_without_all_matrix_cases() -> None:
    with pytest.raises(evidence.EvidenceValidationError, match="case_coverage_incomplete"):
        evidence.validate_summary(
            _summary(
                outcome="passed",
                cases=[_case("v1-lifecycle", outcome="passed")],
                complete=False,
            )
        )


def test_validator_rejects_passing_summary_with_a_nonpassing_case() -> None:
    cases = [_case(case_id) for case_id in evidence.CASE_IDS]
    cases[0]["outcome"] = "failed"
    with pytest.raises(evidence.EvidenceValidationError, match="case_outcome_incomplete"):
        evidence.validate_summary(_summary(outcome="passed", cases=cases))


def test_stable_alias_summary_requires_identical_replays_and_cache_key() -> None:
    case = _case("stable-tool-alias-replay", outcome="passed")
    case.update(
        {
            "prompt_cache_key_preserved": True,
            "replays": [
                {
                    "caller_body_sha256": "sha256:" + "1" * 64,
                    "upstream_body_sha256": "sha256:" + "2" * 64,
                    "aliases": ["__codexhub_ns_a"],
                },
                {
                    "caller_body_sha256": "sha256:" + "1" * 64,
                    "upstream_body_sha256": "sha256:" + "2" * 64,
                    "aliases": ["__codexhub_ns_a"],
                },
            ],
        }
    )
    assert evidence.validate_summary(_summary(cases=[case]))["case_count"] == len(evidence.CASE_IDS)

    case["replays"][1]["upstream_body_sha256"] = "sha256:" + "3" * 64
    with pytest.raises(evidence.EvidenceValidationError, match="upstream_body_not_stable"):
        evidence.validate_summary(_summary(cases=[case]))


def test_external_v2_summary_is_gateway_adapter_evidence_not_native_capability() -> None:
    case = _case("external-v2-lifecycle", outcome="passed", model="glm-5.2")
    case.update(
        {
            "provider": "ollama-cloud",
            "upstream_endpoint": evidence.EXTERNAL_V2_ENDPOINT,
            "native_namespace_count": 0,
            "upstream_function_tool_count": 6,
            "stream_history": True,
            "restart_readback": True,
            "terminal_replay": True,
            "error_replay": True,
            "support_claim": "gateway_adapter",
            "lifecycle": list(evidence.EXTERNAL_V2_LIFECYCLE),
            "adapter_evidence": {
                "backend": "codexhub_gateway",
                "upstream_surface": "ordinary_function_tools",
                "client_surface": "collaboration_v2",
                "inverse_mapping": "client_owned_v2",
                "inverse_mapping_count": 8,
            },
            "aliases": [f"__codexhub_ns_{name}" for name in evidence.EXTERNAL_V2_TOOL_NAMES],
            "alias_map": [
                {"tool": name, "alias": f"__codexhub_ns_{name}"}
                for name in evidence.EXTERNAL_V2_TOOL_NAMES
            ],
            "alias_replays": [
                {
                    "aliases": [f"__codexhub_ns_{name}" for name in evidence.EXTERNAL_V2_TOOL_NAMES],
                    "alias_map": [
                        {"tool": name, "alias": f"__codexhub_ns_{name}"}
                        for name in evidence.EXTERNAL_V2_TOOL_NAMES
                    ],
                },
                {
                    "aliases": [f"__codexhub_ns_{name}" for name in evidence.EXTERNAL_V2_TOOL_NAMES],
                    "alias_map": [
                        {"tool": name, "alias": f"__codexhub_ns_{name}"}
                        for name in evidence.EXTERNAL_V2_TOOL_NAMES
                    ],
                },
            ],
            "call_output_pairs": [
                {
                    "tool": name,
                    "alias": f"__codexhub_ns_{name}",
                    "call_count": 1,
                    "output_count": 1,
                    "inverse_mapped": True,
                    "call_id_sha256": "sha256:" + f"{index + 10:064x}",
                    "output_id_sha256": "sha256:" + f"{index + 30:064x}",
                    "output_call_id_sha256": "sha256:" + f"{index + 10:064x}",
                    "gateway_correlation_sha256": "sha256:" + f"{index + 50:064x}",
                }
                for index, name in enumerate(evidence.EXTERNAL_V2_LIFECYCLE)
            ],
            "target_id_sha256": "sha256:" + "4" * 64,
            "final_readback_target_id_sha256": "sha256:" + "4" * 64,
            "final_readback_terminal": True,
            "final_readback_tool": "list_agents",
            "final_readback_contains_target": True,
            "final_readback_state": "terminal",
            "final_readback_gateway_correlation_sha256": "sha256:" + "5" * 64,
        }
    )
    assert evidence.validate_summary(_summary(cases=[case]))["outcome"] == "not_run"

    case["support_claim"] = "provider_native"
    with pytest.raises(evidence.EvidenceValidationError, match="provider_native_claim"):
        evidence.validate_summary(_summary(cases=[case]))


def test_external_v2_rejects_weak_adapter_evidence() -> None:
    case = _case("external-v2-lifecycle", outcome="passed", model="glm-5.2")
    case.update(
        {
            "provider": "ollama-cloud",
            "upstream_endpoint": evidence.EXTERNAL_V2_ENDPOINT,
            "native_namespace_count": 0,
            "upstream_function_tool_count": 6,
            "stream_history": True,
            "restart_readback": True,
            "terminal_replay": True,
            "error_replay": True,
            "support_claim": "gateway_adapter",
            "lifecycle": list(evidence.EXTERNAL_V2_LIFECYCLE),
        }
    )
    with pytest.raises(evidence.EvidenceValidationError, match="adapter_evidence_missing"):
        evidence.validate_summary(_summary(cases=[case]))


def test_external_v2_requires_two_stable_alias_map_replays() -> None:
    case = _case("external-v2-lifecycle", outcome="passed", model="glm-5.2")
    case.update(
        {
            "provider": "ollama-cloud",
            "upstream_endpoint": evidence.EXTERNAL_V2_ENDPOINT,
            "native_namespace_count": 0,
            "upstream_function_tool_count": 6,
            "stream_history": True,
            "restart_readback": True,
            "terminal_replay": True,
            "error_replay": True,
            "support_claim": "gateway_adapter",
            "lifecycle": list(evidence.EXTERNAL_V2_LIFECYCLE),
            "adapter_evidence": {
                "backend": "codexhub_gateway",
                "upstream_surface": "ordinary_function_tools",
                "client_surface": "collaboration_v2",
                "inverse_mapping": "client_owned_v2",
                "inverse_mapping_count": 8,
            },
            "aliases": [f"__codexhub_ns_{name}" for name in evidence.EXTERNAL_V2_TOOL_NAMES],
            "alias_map": [
                {"tool": name, "alias": f"__codexhub_ns_{name}"}
                for name in evidence.EXTERNAL_V2_TOOL_NAMES
            ],
            "alias_replays": [
                {
                    "aliases": [f"__codexhub_ns_{name}" for name in evidence.EXTERNAL_V2_TOOL_NAMES],
                    "alias_map": [
                        {"tool": name, "alias": f"__codexhub_ns_{name}"}
                        for name in evidence.EXTERNAL_V2_TOOL_NAMES
                    ],
                }
            ],
            "call_output_pairs": [],
            "target_id_sha256": "sha256:" + "4" * 64,
            "final_readback_target_id_sha256": "sha256:" + "4" * 64,
            "final_readback_terminal": True,
            "final_readback_tool": "list_agents",
            "final_readback_contains_target": True,
            "final_readback_state": "terminal",
            "final_readback_gateway_correlation_sha256": "sha256:" + "5" * 64,
        }
    )
    with pytest.raises(evidence.EvidenceValidationError, match="external_alias_replays_invalid"):
        evidence.validate_summary(_summary(cases=[case]))


def test_digest_bytes_is_stable_and_does_not_return_body_content() -> None:
    digest = evidence.digest_bytes(b"private request")
    assert digest == evidence.digest_bytes(b"private request")
    assert digest.startswith("sha256:")
    assert "private" not in digest
