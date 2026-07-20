import json
from pathlib import Path
import shutil
import subprocess
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "Run-RealClientE2E.ps1"
FIXTURES = ROOT / "tests" / "fixtures" / "real_client_e2e"
CANDIDATE_SHA = "a" * 40
LUNA_MODEL = "codexhub-openai/gpt-5.6-luna"
VOLC_MODEL = "codexhub-volc/glm-5.2"
PINNED_VERSIONS = {
    "desktop": "26.715.4045.0",
    "codex_cli": "0.144.5",
    "zcode": "3.3.6",
    "opencode": "1.18.3",
    "pi": "0.80.6",
    "omp": "17.0.3",
}


def _powershell() -> str:
    executable = shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("Windows PowerShell is required")
    return executable


def _manual_case(case_id: str, client: str, model: str) -> dict:
    return {
        "case_id": case_id,
        "client": client,
        "canonical_model": model,
        "human_finalized": True,
        "outcome": "passed",
        "terminal_classification": "completed",
        "reconnect_classification": "none",
        "request_complete_count": 1,
        "http_status": 200,
        "read_only_tool_call_count": 1,
        "sentinel_chunk_count": 1,
        "fallback_count": 0,
        "duplicate_terminal_count": 0,
    }


def _prepare_run(tmp_path: Path) -> tuple[Path, Path, Path]:
    output = tmp_path / "output"
    isolation = output / "isolated"
    for relative in ("account", "credentials", "config", "work"):
        (isolation / relative).mkdir(parents=True, exist_ok=True)
    (isolation / "account" / "profile.json").write_text("{}", encoding="utf-8")
    (isolation / "credentials" / "volc.json").write_text(
        '{"api_key":"fixture-secret"}', encoding="utf-8"
    )
    (isolation / "config" / "client-versions.json").write_text(
        json.dumps(PINNED_VERSIONS), encoding="utf-8"
    )

    debug_build = tmp_path / "CodexHub-debug.cmd"
    shutil.copyfile(FIXTURES / "fake-client-success.cmd", debug_build)
    Path(f"{debug_build}.candidate-sha").write_text(CANDIDATE_SHA, encoding="ascii")

    manual = {
        "schema": "codexhub.real-client-manual-evidence.v1",
        "candidate_sha": CANDIDATE_SHA,
        "cases": [
            _manual_case("desktop-luna", "desktop", "gpt-5.6-luna"),
            _manual_case("desktop-volc", "desktop", "volc/glm-5.2"),
            _manual_case("zcode-luna", "zcode", LUNA_MODEL),
            _manual_case("zcode-volc", "zcode", VOLC_MODEL),
        ],
    }
    (output / "manual-evidence.json").write_text(json.dumps(manual), encoding="utf-8")
    return output, isolation, debug_build


def _run(
    tmp_path: Path,
    fake: str = "fake-client-success.cmd",
    *,
    client_fakes: dict[str, str] | None = None,
    mutate=None,
    timeout_seconds: int = 3,
) -> subprocess.CompletedProcess[str]:
    output, isolation, debug_build = _prepare_run(tmp_path)
    if mutate is not None:
        mutate(output, isolation, debug_build)
    fake_path = FIXTURES / fake
    executable_arguments = {
        "CodexDesktopPath": fake_path,
        "CodexCliPath": fake_path,
        "ZCodePath": fake_path,
        "OpenCodePath": fake_path,
        "PiPath": fake_path,
        "OmpPath": fake_path,
    }
    for name, fixture_name in (client_fakes or {}).items():
        executable_arguments[name] = FIXTURES / fixture_name
    command = [
        _powershell(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-CandidateSha",
        CANDIDATE_SHA,
        "-DebugBuild",
        str(debug_build),
        "-LunaModel",
        LUNA_MODEL,
        "-VolcModel",
        VOLC_MODEL,
        "-OutputDirectory",
        str(output),
    ]
    for name, executable in executable_arguments.items():
        command.extend((f"-{name}", str(executable)))
    command.extend(("-TimeoutSeconds", str(timeout_seconds)))
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_successful_matrix_emits_one_sanitized_sha_bound_summary(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    summaries = list(tmp_path.rglob("summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    assert summary["schema"] == "codexhub.real-client-e2e-summary.v1"
    assert summary["candidate_sha"] == CANDIDATE_SHA
    assert summary["pinned_versions"] == PINNED_VERSIONS
    assert summary["counts"] == {
        "case_count": 12,
        "passed_count": 12,
        "failed_count": 0,
        "manual_case_count": 4,
        "automated_case_count": 8,
    }
    assert [case["case_id"] for case in summary["cases"]] == [
        "desktop-luna",
        "desktop-volc",
        "codex-cli-luna",
        "codex-cli-volc",
        "opencode-luna",
        "opencode-volc",
        "zcode-luna",
        "zcode-volc",
        "pi-luna",
        "pi-volc",
        "omp-luna",
        "omp-volc",
    ]
    assert all(case["outcome"] == "passed" for case in summary["cases"])
    assert len(summary["artifacts"]) == 12
    assert all((tmp_path / "output" / artifact).is_file() for artifact in summary["artifacts"])
    serialized = json.dumps(summary, sort_keys=True)
    assert "fixture-secret" not in serialized
    assert str(tmp_path) not in serialized


def test_windows_client_state_paths_are_isolated_per_case(tmp_path):
    result = _run(tmp_path, fake="fake-client-isolation.cmd")

    assert result.returncode == 0, result.stdout + result.stderr


def test_failure_matrix_is_bounded_sanitized_and_cleans_up_children(tmp_path):
    started = time.monotonic()
    result = _run(
        tmp_path,
        client_fakes={
            "CodexCliPath": "fake-client-timeout.cmd",
            "OpenCodePath": "fake-client-capacity.cmd",
            "PiPath": "fake-client-invalid-evidence.cmd",
            "OmpPath": "fake-client-malformed.cmd",
        },
        timeout_seconds=1,
    )

    assert result.returncode != 0
    assert time.monotonic() - started < 20
    summary_path = tmp_path / "output" / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    codex_cases = [case for case in summary["cases"] if case["case_id"].startswith("codex-cli")]
    assert [case["terminal_classification"] for case in codex_cases] == ["timeout", "nonzero_exit"]
    opencode_cases = [case for case in summary["cases"] if case["case_id"].startswith("opencode")]
    assert all(case["retry_classification"] == "capacity_429_pre_output_retried" for case in opencode_cases)
    pi_cases = [case for case in summary["cases"] if case["case_id"].startswith("pi")]
    assert all(case["fallback_count"] == 1 for case in pi_cases)
    assert all(case["duplicate_terminal_count"] == 1 for case in pi_cases)
    assert all(case["reconnect_classification"] == "unclassified" for case in pi_cases)
    omp_cases = [case for case in summary["cases"] if case["case_id"].startswith("omp")]
    assert all(case["outcome"] == "failed" for case in omp_cases)
    assert all(case["error_event_count"] == 1 for case in omp_cases)
    assert len(list((tmp_path / "output").rglob("child-started"))) == 1
    time.sleep(6)
    assert not list((tmp_path / "output").rglob("child-survived"))
    sanitized = result.stdout + result.stderr
    for path in (summary_path, *(tmp_path / "output" / "artifacts").rglob("*.json")):
        sanitized += path.read_text(encoding="utf-8-sig")
    assert "fixture-private-token" not in sanitized
    assert "C:\\Users\\private-account" not in sanitized
    assert len(sanitized) < 100_000


def _mutate_manual(output: Path, mutation) -> None:
    path = output / "manual-evidence.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    mutation(evidence)
    path.write_text(json.dumps(evidence), encoding="utf-8")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda evidence: evidence["cases"].pop(),
        lambda evidence: evidence["cases"].append(dict(evidence["cases"][0])),
        lambda evidence: evidence["cases"][0].update({"fallback_count": 1}),
        lambda evidence: evidence.update({"candidate_sha": "b" * 40}),
    ],
    ids=["missing", "duplicate", "contradictory", "stale-sha"],
)
def test_manual_evidence_rejects_invalid_merges_before_client_launch(tmp_path, mutation):
    result = _run(
        tmp_path,
        mutate=lambda output, _isolation, _debug: _mutate_manual(output, mutation),
    )

    assert result.returncode != 0
    assert not (tmp_path / "output" / "summary.json").exists()
    assert "fixture-private-token" not in result.stdout + result.stderr


def test_manual_evidence_merge_is_deterministic_for_reordered_input(tmp_path):
    result = _run(
        tmp_path,
        mutate=lambda output, _isolation, _debug: _mutate_manual(
            output, lambda evidence: evidence["cases"].reverse()
        ),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig"))
    manual_ids = [
        case["case_id"]
        for case in summary["cases"]
        if case["case_id"].startswith(("desktop", "zcode"))
    ]
    assert manual_ids == ["desktop-luna", "desktop-volc", "zcode-luna", "zcode-volc"]


def test_missing_credentials_fail_before_any_summary_or_launch(tmp_path):
    def remove_credentials(_output, isolation, _debug):
        (isolation / "credentials" / "volc.json").unlink()

    result = _run(tmp_path, mutate=remove_credentials)

    assert result.returncode != 0
    assert not (tmp_path / "output" / "summary.json").exists()


def test_version_manifest_and_debug_build_sidecar_are_sha_bound_preflight_gates(tmp_path):
    def invalidate_versions(_output, isolation, debug_build):
        versions_path = isolation / "config" / "client-versions.json"
        versions = json.loads(versions_path.read_text(encoding="utf-8"))
        versions["codex_cli"] = "0.144.6"
        versions_path.write_text(json.dumps(versions), encoding="utf-8")
        Path(f"{debug_build}.candidate-sha").write_text("b" * 40, encoding="ascii")

    result = _run(tmp_path, mutate=invalidate_versions)

    assert result.returncode != 0
    assert not (tmp_path / "output" / "summary.json").exists()
