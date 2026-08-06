from __future__ import annotations

import json
import sys
from pathlib import Path
import tempfile
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from e2e_codex_active_call_regression import (  # noqa: E402
    FIXTURE_GATEWAY_KEY,
    MODEL_A,
    MODEL_B,
    FakeResponsesScenario,
    ActiveCallFailure,
    _cleanup_active_call_resources,
    _safe_environment,
)


def test_fake_responses_fixture_has_one_barrier_delayed_function_call() -> None:
    scenario = FakeResponsesScenario()
    index = scenario.record_request(
        {"model": MODEL_A, "input": []},
        f"Bearer {FIXTURE_GATEWAY_KEY}",
        "/v1/responses",
    )
    events = scenario.response_events(index, MODEL_A, {})

    assert [event["type"] for event in events] == [
        "response.created",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
    ]
    assert events[1]["item"]["type"] == "function_call"
    assert events[-1]["item"]["call_id"] == "fixture-call-1"
    assert "active-call-fixture" in json.dumps(events, sort_keys=True)

    waiter = threading.Thread(target=scenario.wait_for_release, daemon=True)
    waiter.start()
    assert scenario.active_call_ready.wait(1)
    assert not scenario.release_terminal.is_set()
    scenario.release_terminal.set()
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert scenario.barrier_timed_out is False


def test_fake_responses_fixture_records_only_model_labels_and_distinguishes_models() -> None:
    scenario = FakeResponsesScenario()
    assert MODEL_A != MODEL_B
    assert scenario.record_request(
        {"model": MODEL_A, "input": [{"type": "text", "text": "secret"}]},
        f"Bearer {FIXTURE_GATEWAY_KEY}",
        "/v1/responses",
    ) == 1
    assert scenario.models == [MODEL_A]
    assert "secret" not in repr(scenario.models)
    assert scenario.invalid_auth is False
    assert scenario.unexpected_path is False


def test_isolated_environment_removes_shared_config_and_provider_secrets(monkeypatch, tmp_path) -> None:
    for name in (
        "CODEX_CONFIG",
        "CODEXHUB_CODEX_TARGET_HOME",
        "CODEXHUB_RUNTIME_HOME",
        "OPENAI_API_KEY",
        "OLLAMA_API_KEY",
    ):
        monkeypatch.setenv(name, "shared-value")

    environment = _safe_environment(tmp_path / "isolated-home")

    assert environment["CODEX_HOME"] == str(tmp_path / "isolated-home")
    for name in (
        "CODEX_CONFIG",
        "CODEXHUB_CODEX_TARGET_HOME",
        "CODEXHUB_RUNTIME_HOME",
        "OPENAI_API_KEY",
        "OLLAMA_API_KEY",
    ):
        assert name not in environment


def test_cleanup_failure_is_not_swallowed_after_a_candidate_pass() -> None:
    class FakeServer:
        def shutdown(self) -> None:
            return

        def server_close(self) -> None:
            return

    home = Path(tempfile.mkdtemp(prefix="codexhub-active-call-cleanup-test-"))
    scenario = FakeResponsesScenario()

    def fail_stop(_process) -> None:
        raise ActiveCallFailure("forced_app_server_stop_failure")

    module = sys.modules["e2e_codex_active_call_regression"]
    original_stop = module._stop_app_server
    module._stop_app_server = fail_stop
    try:
        with pytest.raises(ActiveCallFailure, match="app_server_cleanup_failed"):
            _cleanup_active_call_resources(
                scenario,
                home,
                client=None,
                process=object(),
                server=FakeServer(),
                server_thread=None,
            )
    finally:
        module._stop_app_server = original_stop
