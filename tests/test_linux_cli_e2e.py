from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "e2e_linux_cli_clients", ROOT / "scripts" / "e2e_linux_cli_clients.py"
)
assert SPEC and SPEC.loader
E2E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E2E)
CONTRACT = json.loads(E2E.CLI_CONTRACT_PATH.read_text(encoding="utf-8"))


def test_cli_contract_is_versioned_and_complete() -> None:
    assert CONTRACT["schema"] == "codexhub.real-client-cli-contract.v1"
    assert set(CONTRACT["minimum_versions"]) == {"codex_cli", "opencode", "pi", "omp"}
    assert {
        case["client"] for case in CONTRACT["cases"]
    } == {"codex_cli", "opencode", "pi", "omp"}
    assert {case["provider_id"] for case in CONTRACT["cases"]} == {
        "official",
        "opencode-go",
    }
    assert len(CONTRACT["cases"]) == 8
    assert len({case["case_ids"]["linux"] for case in CONTRACT["cases"]}) == 8
    assert len({case["case_ids"]["windows"] for case in CONTRACT["cases"]}) == 8
    required_evidence = {
        "case_id",
        "client",
        "provider_id",
        "client_selector",
        "canonical_model",
        "gateway_model",
        "endpoint_binding",
        "protocol",
        "outcome",
    }
    assert required_evidence <= set(CONTRACT["evidence_fields"])


def test_linux_cli_matrix_covers_four_clients_and_two_required_models() -> None:
    linux = CONTRACT["platforms"]["linux"]
    expected_cases = {
        (linux["client_names"][case["client"]], linux["provider_names"][case["provider_id"]])
        for case in CONTRACT["cases"]
    }
    assert {(case.client, case.provider) for case in E2E.CASES} == expected_cases
    assert {case.gateway_model for case in E2E.CASES} == {
        case.get("platform_overrides", {}).get("linux", {}).get("gateway", case["models"]["gateway"])
        for case in CONTRACT["cases"]
    }


def test_client_selectors_keep_gateway_provider_identity() -> None:
    linux = CONTRACT["platforms"]["linux"]
    for case, expected in zip(E2E.CASES, CONTRACT["cases"], strict=True):
        models = expected["models"]
        overrides = expected.get("platform_overrides", {}).get("linux", {})
        assert case.client == linux["client_names"][expected["client"]]
        assert case.provider == linux["provider_names"][expected["provider_id"]]
        assert case.case_id == expected["case_ids"]["linux"]
        assert case.managed_model == models["managed"]
        assert case.selector == models["selector"]
        assert case.gateway_model == overrides.get("gateway", models["gateway"])
