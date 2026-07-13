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


class BrokenPipeStdin:
    closed = False

    def close(self) -> None:
        self.closed = True
        raise BrokenPipeError


class BrokenPipeProcess:
    stdin = BrokenPipeStdin()
    stdout: tuple[str, ...] = ()


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


def test_catalog_comparison_requires_fresh_official_and_connected_controls() -> None:
    official = {
        "account": {"authenticated": False, "requiresOpenaiAuth": True},
        "catalog": {
            "modelCount": 7,
            "requestedOfficialModelListed": True,
            "requestedOfficialModelSupportsMax": True,
            "externalModelListed": False,
        },
    }
    connected = {
        "account": {"authenticated": False, "requiresOpenaiAuth": True},
        "catalog": {
            "modelCount": 29,
            "requestedOfficialModelListed": True,
            "requestedOfficialModelSupportsMax": True,
            "externalModelListed": True,
        },
    }

    RUNNER.assert_catalog_comparison(official, connected)

    connected["catalog"]["externalModelListed"] = False
    with pytest.raises(RUNNER.AppServerFailure, match="external_model_missing"):
        RUNNER.assert_catalog_comparison(official, connected)


def test_repeated_green_result_requires_every_cleanup_boundary() -> None:
    run = {
        "outcome": "passed",
        "nativeCleanup": "passed",
        "clientClose": "passed",
        "appServerCleanup": "passed",
        "temporaryHomeCleanup": "passed",
    }

    assert RUNNER.is_clean_issue106_green_run(run)

    run["appServerCleanup"] = "failed"
    assert not RUNNER.is_clean_issue106_green_run(run)


def test_client_close_sanitizes_a_broken_pipe() -> None:
    client = RUNNER.JsonRpcClient(BrokenPipeProcess())

    with pytest.raises(RUNNER.AppServerFailure, match="app_server_client_close_failed"):
        client.close()


def test_app_server_stop_finishes_shutdown_after_broken_pipe_failures() -> None:
    events: list[str] = []

    class FailingInput:
        closed = False

        def close(self) -> None:
            self.closed = True
            events.append("stdin")
            raise BrokenPipeError

    class FailingOutput:
        def close(self) -> None:
            events.append("stdout")
            raise OSError

    class Process:
        stdin = FailingInput()
        stdout = FailingOutput()
        waits = 0

        def wait(self, timeout: float) -> int:
            del timeout
            self.waits += 1
            events.append("wait")
            if self.waits == 1:
                raise RUNNER.subprocess.TimeoutExpired("app-server", 5)
            return 0

        def terminate(self) -> None:
            events.append("terminate")

        def kill(self) -> None:
            events.append("kill")

        def poll(self) -> int:
            return 0

    with pytest.raises(RUNNER.AppServerFailure, match="app_server_pipe_close_failed"):
        RUNNER.stop_issue106_app_server(Process())

    assert events == ["stdin", "wait", "terminate", "wait", "stdout"]


def test_isolated_teardown_runs_after_a_client_close_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    home = tmp_path / "issue106-home"
    home.mkdir()

    class FailingClient:
        def close(self) -> None:
            events.append("client")
            raise RUNNER.AppServerFailure("app_server_client_close_failed")

    monkeypatch.setattr(RUNNER, "create_temporary_home", lambda: home)
    monkeypatch.setattr(RUNNER, "start_issue106_app_server", lambda *_args: object())
    monkeypatch.setattr(RUNNER, "JsonRpcClient", lambda _process: FailingClient())
    monkeypatch.setattr(
        RUNNER,
        "stop_issue106_app_server",
        lambda _process: events.append("app-server"),
    )
    monkeypatch.setattr(
        RUNNER,
        "remove_temporary_home",
        lambda _home: events.append("temporary-home"),
    )

    with pytest.raises(RUNNER.AppServerFailure, match="app_server_client_close_failed"):
        RUNNER.with_isolated_client(
            codex_command=Path("codex"),
            connected=False,
            gateway_base_url="http://127.0.0.1:9099",
            gateway_key=None,
            action=lambda _client, _home: {"outcome": "passed"},
        )

    assert events == ["client", "app-server", "temporary-home"]


def test_red_control_reads_completed_system_error_before_classifying(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshots = iter(
        [
            snapshot("systemError", ["completed"], [1]),
            snapshot("systemError", ["completed", "completed"], [1, 1]),
        ]
    )
    turn_ids = iter(("first-turn", "continuation-turn"))

    class ResumableClient:
        def request(
            self, method: str, params: dict[str, object], timeout: float
        ) -> dict[str, object]:
            del method, params, timeout
            return {"result": {}}

    monkeypatch.setattr(RUNNER, "read_issue106_model_list", lambda *_args: [])
    monkeypatch.setattr(
        RUNNER,
        "start_issue106_custom_thread",
        lambda *_args: "red-thread",
    )
    monkeypatch.setattr(RUNNER, "turn_started", lambda *_args, **_kwargs: next(turn_ids))
    monkeypatch.setattr(
        RUNNER,
        "wait_for_turn",
        lambda *_args, **_kwargs: {"status": "completed"},
    )
    monkeypatch.setattr(RUNNER, "thread_snapshot", lambda *_args: next(snapshots))
    monkeypatch.setattr(RUNNER, "cleanup_issue106_thread", lambda *_args: "passed")

    result = RUNNER.run_red_missing_model(
        ResumableClient(),
        tmp_path,
        timeout=1,
        red_timeout=1,
        red_model="unlisted-model",
    )

    assert result["outcome"] == "non_atomic_missing_model"
    assert result["initialState"] == "failed_without_output"
    assert result["continuation"]["status"] == "no_usable_rollout"


def test_green_defaults_to_two_runs_and_rejects_a_single_run() -> None:
    assert RUNNER.parse_args(["--scenario", "green"]).repeat == 2

    with pytest.raises(SystemExit):
        RUNNER.parse_args(["--scenario", "green", "--repeat", "1"])


def test_json_rpc_error_is_distinct_from_an_app_server_transport_failure() -> None:
    with pytest.raises(RUNNER.AppServerRequestRejected):
        RUNNER.response_result({"error": {"code": -1}}, "thread_start")


def test_temporary_home_guard_allows_only_this_runner_immediate_temp_children() -> None:
    temporary_root = Path(tempfile.gettempdir())
    safe = temporary_root / f"{RUNNER.TEMP_HOME_PREFIX}synthetic"

    assert RUNNER.is_task_owned_temporary_home(safe)
    assert not RUNNER.is_task_owned_temporary_home(temporary_root / "unrelated")
    assert not RUNNER.is_task_owned_temporary_home(safe / "nested")
