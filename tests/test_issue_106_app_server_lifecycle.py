import importlib.util
from pathlib import Path
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts" / "run_issue_106_task_lifecycle.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("issue_106_lifecycle_runner", RUNNER_PATH)
assert RUNNER_SPEC is not None
assert RUNNER_SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(RUNNER)


def snapshot(
    thread_status: str, turn_statuses: list[str], turn_item_counts: list[int]
) -> dict[str, object]:
    return {
        "threadStatus": thread_status,
        "turnStatuses": turn_statuses,
        "turnItemCounts": turn_item_counts,
        "assistantOutputTurns": 0,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("danger-full-access", True),
        ({"type": "dangerFullAccess"}, True),
        ({"type": "workspaceWrite"}, False),
        (None, False),
    ],
)
def test_full_access_binding_accepts_only_supported_app_server_encodings(
    value: object, expected: bool
) -> None:
    assert RUNNER.is_danger_full_access(value) is expected


def test_red_classifier_preserves_both_observed_no_output_states() -> None:
    assert (
        RUNNER.classify_red_snapshot(snapshot("active", ["inProgress"], [1]))
        == "in_progress_without_output"
    )
    assert (
        RUNNER.classify_red_snapshot(snapshot("systemError", ["completed"], [1]))
        == "failed_without_output"
    )


def test_red_classifier_rejects_a_turn_that_has_any_agent_output() -> None:
    value = snapshot("systemError", ["completed"], [1])
    value["assistantOutputTurns"] = 1

    assert RUNNER.classify_red_snapshot(value) == "unexpected_missing_model_state"


def test_catalog_summary_does_not_retain_model_identifiers() -> None:
    summary = RUNNER.catalog_summary(
        [
            {
                "id": "gpt-5.6-terra",
                "supportedReasoningEfforts": [{"reasoningEffort": "max"}],
            },
            {"id": "glm-5.2"},
        ],
        "gpt-5.6-terra",
    )

    assert summary == {
        "modelCount": 2,
        "requestedOfficialModelListed": True,
        "requestedOfficialModelSupportsMax": True,
    }


def test_json_rpc_error_is_distinct_from_an_app_server_transport_failure() -> None:
    with pytest.raises(RUNNER.AppServerRequestRejected):
        RUNNER.response_result({"error": {"code": -1}}, "thread_start")


def test_temporary_home_guard_allows_only_this_runner_immediate_temp_children() -> None:
    temporary_root = Path(tempfile.gettempdir())
    safe = temporary_root / f"{RUNNER.TEMP_HOME_PREFIX}synthetic"

    assert RUNNER.is_task_owned_temporary_home(safe)
    assert not RUNNER.is_task_owned_temporary_home(temporary_root / "unrelated")
    assert not RUNNER.is_task_owned_temporary_home(safe / "nested")
