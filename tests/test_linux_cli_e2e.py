from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "e2e_linux_cli_clients", ROOT / "scripts" / "e2e_linux_cli_clients.py"
)
assert SPEC and SPEC.loader
E2E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E2E)


def test_linux_cli_matrix_covers_four_clients_and_two_required_models() -> None:
    assert {(case.client, case.provider) for case in E2E.CASES} == {
        (client, provider)
        for client in ("codex", "opencode", "pi", "omp")
        for provider in ("openai", "opencode-go")
    }
    assert {case.gateway_model for case in E2E.CASES} == {
        "gpt-5.6-luna",
        "opencode-go/muse-spark-1.2-contributor",
    }


def test_client_selectors_keep_gateway_provider_identity() -> None:
    for case in E2E.CASES:
        if case.client == "codex":
            assert case.selector == case.gateway_model
            assert case.managed_model == case.gateway_model
        elif case.provider == "openai":
            assert case.managed_model == "openai/gpt-5.6-luna"
            assert case.selector == "codexhub-openai/gpt-5.6-luna"
        else:
            assert case.managed_model == ("opencode-go/muse-spark-1.2-contributor")
            assert case.selector == ("codexhub-opencode-go/muse-spark-1.2-contributor")
