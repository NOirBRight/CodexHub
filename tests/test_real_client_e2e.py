import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import threading
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
SUMMARY_KEYS = {
    "schema",
    "candidate_sha",
    "run_binding_sha256",
    "outcome",
    "failure_classification",
    "hashes",
    "pinned_versions",
    "canonical_models",
    "counts",
    "cases",
    "artifacts",
}
FAILURE_SUMMARY_KEYS = SUMMARY_KEYS - {"run_binding_sha256", "hashes"}
COUNT_KEYS = {
    "case_count",
    "passed_count",
    "failed_count",
    "manual_case_count",
    "automated_case_count",
}
CASE_KEYS = {
    "case_id",
    "canonical_model",
    "outcome",
    "duration_ms",
    "request_complete_count",
    "http_status",
    "read_only_tool_call_count",
    "sentinel_chunk_count",
    "fallback_count",
    "error_event_count",
    "duplicate_terminal_count",
    "terminal_classification",
    "reconnect_classification",
    "retry_classification",
    "artifact",
}
AUTOMATED_CASE_KEYS = CASE_KEYS | {
    "gateway_request_count",
    "gateway_complete_count",
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


def _prepare_run(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    output = tmp_path / "output"
    isolation = output / "isolated"
    for relative in ("account", "credentials", "config", "work"):
        (isolation / relative).mkdir(parents=True, exist_ok=True)
    (isolation / "account" / "profile.json").write_text(
        json.dumps(
            {
                "schema": "codexhub.real-client-account.v1",
                "dedicated_account": True,
                "codex_login_ready": True,
                "gui_ready": True,
            }
        ),
        encoding="utf-8",
    )
    (isolation / "account" / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "fixture-codex-access-token",
                    "refresh_token": "fixture-codex-refresh-token",
                },
            }
        ),
        encoding="utf-8",
    )
    (isolation / "credentials" / "volc.json").write_text(
        json.dumps(
            {
                "schema": "codexhub.real-client-volc.v1",
                "api_key": "fixture-volc-private-token",
            }
        ),
        encoding="utf-8",
    )
    (isolation / "config" / "gateway.json").write_text(
        json.dumps(
            {
                "schema": "codexhub.real-client-gateway.v1",
                "listen_port": 19190,
                "gateway_client_key": "fixture-gateway-private-key",
            }
        ),
        encoding="utf-8",
    )
    snapshot_manifest = isolation / "config" / "vm-snapshot.json"
    machine_hash = "sha256:" + hashlib.sha256(os.environ["COMPUTERNAME"].encode()).hexdigest()
    snapshot_manifest.write_text(
        json.dumps(
            {
                "schema": "codexhub.real-client-vm-snapshot.v1",
                "snapshot": "codexhub-real-client-e2e-v1",
                "machine_name_sha256": machine_hash,
            }
        ),
        encoding="utf-8",
    )

    debug_build = tmp_path / "CodexHub-debug.cmd"
    shutil.copyfile(FIXTURES / "fake-debug-build.cmd", debug_build)
    Path(f"{debug_build}.candidate-sha").write_text(CANDIDATE_SHA, encoding="ascii")

    return output, isolation, debug_build, snapshot_manifest


def _finalize_manual_evidence(
    output: Path, mutation=None, stop_event: threading.Event | None = None
) -> None:
    template_path = output / "manual-evidence.template.json"
    work = output / "isolated" / "work"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not (stop_event and stop_event.is_set()):
        if (
            template_path.is_file()
            and (work / "gui-desktop.launched").is_file()
            and (work / "gui-zcode.launched").is_file()
        ):
            evidence = json.loads(template_path.read_text(encoding="utf-8-sig"))
            evidence["login_confirmed"] = True
            evidence["gui_confirmed"] = True
            for case in evidence["cases"]:
                case.update(_manual_case(case["case_id"], case["client"], case["canonical_model"]))
            if mutation is not None:
                mutation(evidence)
            target = output / "manual-evidence.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(evidence), encoding="utf-8")
            temporary.replace(target)
            return
        if stop_event:
            stop_event.wait(0.05)
        else:
            time.sleep(0.05)


def _run(
    tmp_path: Path,
    fake: str = "fake-client-real-contract.cmd",
    *,
    client_fakes: dict[str, str] | None = None,
    debug_fake: str | None = None,
    mutate=None,
    manual_mutation=None,
    finalize_manual: bool = True,
    timeout_seconds: int = 3,
    manual_timeout_seconds: int = 10,
) -> subprocess.CompletedProcess[str]:
    output, isolation, debug_build, snapshot_manifest = _prepare_run(tmp_path)
    if debug_fake is not None:
        shutil.copyfile(FIXTURES / debug_fake, debug_build)
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
        "-SnapshotManifest",
        str(snapshot_manifest),
    ]
    for name, executable in executable_arguments.items():
        command.extend((f"-{name}", str(executable)))
    command.extend(("-TimeoutSeconds", str(timeout_seconds)))
    command.extend(("-ManualEvidenceTimeoutSeconds", str(manual_timeout_seconds)))
    finalizer = None
    finalizer_stop = None
    if finalize_manual:
        finalizer_stop = threading.Event()
        finalizer = threading.Thread(
            target=_finalize_manual_evidence,
            args=(output, manual_mutation, finalizer_stop),
            daemon=True,
        )
        finalizer.start()
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=90,
        )
    finally:
        if finalizer is not None and finalizer_stop is not None:
            finalizer_stop.set()
            finalizer.join(timeout=1)
    return result


def _assert_exact_summary_schema(summary: dict) -> None:
    assert set(summary) == (SUMMARY_KEYS if summary["cases"] else FAILURE_SUMMARY_KEYS)
    assert set(summary["pinned_versions"]) == set(PINNED_VERSIONS)
    assert set(summary["counts"]) == COUNT_KEYS
    if summary["cases"]:
        assert set(summary["hashes"]) == {"debug_build"}
        for case in summary["cases"]:
            expected = (
                AUTOMATED_CASE_KEYS
                if case["case_id"].startswith(("codex-cli", "opencode", "pi", "omp"))
                else CASE_KEYS
            )
            assert set(case) == expected


def test_successful_matrix_emits_one_sanitized_sha_bound_summary(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    summaries = list(tmp_path.rglob("summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    _assert_exact_summary_schema(summary)
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
    template = json.loads(
        (tmp_path / "output" / "manual-evidence.template.json").read_text(encoding="utf-8-sig")
    )
    assert template["run_binding_sha256"] == summary["run_binding_sha256"]
    assert not list((tmp_path / "output" / "isolated" / "work").rglob("sentinel.txt"))
    serialized = json.dumps(summary, sort_keys=True)
    for secret in (
        "fixture-codex-access-token",
        "fixture-codex-refresh-token",
        "fixture-volc-private-token",
        "fixture-gateway-private-key",
    ):
        assert secret not in serialized
    for relative in (
        "isolated/account/profile.json",
        "isolated/account/auth.json",
        "isolated/credentials/volc.json",
        "isolated/config/gateway.json",
        "isolated/config/vm-snapshot.json",
        "manual-evidence.json",
    ):
        payload = (tmp_path / "output" / relative).read_bytes()
        fingerprint = "sha256:" + hashlib.sha256(payload).hexdigest()
        assert fingerprint not in serialized
    assert str(tmp_path) not in serialized


def test_omp_17_0_3_uses_print_json_one_shot_arguments(tmp_path):
    result = _run(
        tmp_path,
        client_fakes={"OmpPath": "fake-client-omp-argv.cmd"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(
        (tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig")
    )
    omp_cases = [case for case in summary["cases"] if case["case_id"].startswith("omp-")]
    assert [case["outcome"] for case in omp_cases] == ["passed", "passed"]


def test_windows_client_state_paths_are_isolated_per_case(tmp_path):
    result = _run(tmp_path, fake="fake-client-isolation.cmd")

    assert result.returncode == 0, result.stdout + result.stderr


def test_real_versioned_client_events_are_correlated_with_gateway_diagnostics(tmp_path):
    result = _run(tmp_path, fake="fake-client-real-contract.cmd")

    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig"))
    automated = [case for case in summary["cases"] if not case["case_id"].startswith(("desktop", "zcode"))]
    assert all(case["request_complete_count"] == 1 for case in automated)
    assert all(case["http_status"] == 200 for case in automated)
    assert all(case["terminal_classification"] == "completed" for case in automated)
    assert all(case["gateway_request_count"] == 2 for case in automated)
    assert all(case["gateway_complete_count"] == 2 for case in automated)
    assert all(case["fallback_count"] == 0 for case in automated)
    assert all(case["duplicate_terminal_count"] == 0 for case in automated)
    assert all(case["reconnect_classification"] == "none" for case in automated)
    assert {
        case["canonical_model"]
        for case in automated
        if case["case_id"].startswith(("opencode", "pi", "omp"))
    } == {LUNA_MODEL, VOLC_MODEL}
    opencode = [case for case in automated if case["case_id"].startswith("opencode-")]
    assert [case["duplicate_terminal_count"] for case in opencode] == [0, 0]
    diagnostics = list((tmp_path / "output").rglob("codex-proxy-events.jsonl"))
    assert len(diagnostics) == 1
    native = [json.loads(line) for line in diagnostics[0].read_text().splitlines()]
    completes = [event for event in native if event["event"] == "request_complete"]
    assert len(completes) == 16
    assert {event["model_canonical"] for event in completes} == {
        "gpt-5.6-luna",
        "volc/glm-5.2",
        "openai/gpt-5.6-luna",
    }
    production_fields = {
        "event",
        "request_id",
        "method",
        "model",
        "model_requested",
        "model_canonical",
        "upstream",
        "provider_id",
        "provider_hint",
        "upstream_format",
        "behavior_profile",
        "inbound_format",
        "route_reason",
        "route_mode",
        "is_stream",
        "status",
        "duration_ms",
        "client_id",
    }
    assert all(set(event) == production_fields for event in completes)
    assert all("terminal_count" not in event for event in completes)
    assert all("sse_terminal_event_seen" not in event for event in completes)


def test_isolated_client_configs_follow_production_provider_endpoint_selection(tmp_path):
    result = _run(tmp_path, fake="fake-client-routing-config.cmd")

    assert result.returncode == 0, result.stdout + result.stderr


def test_zcode_gui_consumes_catalog_from_isolated_roaming_appdata(tmp_path):
    result = _run(
        tmp_path,
        client_fakes={"ZCodePath": "fake-client-zcode-appdata.cmd"},
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_wrong_model_fallback_cannot_be_filtered_into_a_false_pass(tmp_path):
    result = _run(
        tmp_path,
        client_fakes={"PiPath": "fake-client-wrong-model.cmd"},
    )

    assert result.returncode != 0
    summary_path = tmp_path / "output" / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    pi_cases = [case for case in summary["cases"] if case["case_id"].startswith("pi-")]
    assert [case["outcome"] for case in pi_cases] == ["failed", "failed"]
    assert [case["fallback_count"] for case in pi_cases] == [1, 1]
    assert all(case["error_event_count"] >= 1 for case in pi_cases)
    serialized = summary_path.read_text(encoding="utf-8-sig")
    assert "codexhub-openai/wrong-route" not in serialized
    assert "pi-luna-attempt-1-request-1" not in serialized


def test_post_tool_capacity_response_is_not_eligible_for_retry(tmp_path):
    result = _run(
        tmp_path,
        client_fakes={"PiPath": "fake-client-post-tool-capacity.cmd"},
    )

    assert result.returncode != 0
    summary = json.loads(
        (tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig")
    )
    pi_cases = [case for case in summary["cases"] if case["case_id"].startswith("pi-")]
    assert [case["outcome"] for case in pi_cases] == ["failed", "failed"]
    assert [case["retry_classification"] for case in pi_cases] == [
        "not_eligible",
        "not_eligible",
    ]
    assert [case["read_only_tool_call_count"] for case in pi_cases] == [1, 1]
    assert [case["gateway_complete_count"] for case in pi_cases] == [1, 1]


def test_pi_and_omp_reject_non_stop_or_missing_final_assistant_states(tmp_path):
    result = _run(
        tmp_path,
        client_fakes={
            "PiPath": "fake-client-terminal-states.cmd",
            "OmpPath": "fake-client-terminal-states.cmd",
        },
    )

    assert result.returncode != 0
    summary_path = tmp_path / "output" / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    cases = [
        case
        for case in summary["cases"]
        if case["case_id"].startswith(("pi-", "omp-"))
    ]
    assert [case["outcome"] for case in cases] == ["failed"] * 4
    assert [case["terminal_classification"] for case in cases] == [
        "error",
        "aborted",
        "length",
        "unclassified",
    ]
    assert all(case["error_event_count"] >= 1 for case in cases)
    assert "fixture-terminal-error" not in summary_path.read_text(encoding="utf-8-sig")


def test_pi_rejects_stop_with_contradictory_error_message(tmp_path):
    result = _run(
        tmp_path,
        client_fakes={"PiPath": "fake-client-terminal-contradiction.cmd"},
    )

    assert result.returncode != 0
    summary_path = tmp_path / "output" / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    pi_cases = [case for case in summary["cases"] if case["case_id"].startswith("pi-")]
    assert [case["terminal_classification"] for case in pi_cases] == ["error", "error"]
    assert all(case["error_event_count"] >= 1 for case in pi_cases)
    assert "fixture-contradictory-error" not in summary_path.read_text(encoding="utf-8-sig")


def test_empty_account_and_arbitrary_credential_cannot_pass_preflight(tmp_path):
    def invalidate_identity(_output, isolation, _debug):
        (isolation / "account" / "profile.json").write_text("{}", encoding="utf-8")
        (isolation / "credentials" / "volc.json").write_text(
            '{"api_key":"arbitrary"}', encoding="utf-8"
        )

    result = _run(tmp_path, fake="fake-client-real-contract.cmd", mutate=invalidate_identity)

    assert result.returncode != 0


def test_preflight_return_does_not_leave_a_manual_finalizer_thread(tmp_path):
    existing_threads = set(threading.enumerate())

    result = _run(
        tmp_path,
        mutate=lambda _output, isolation, _debug: (
            isolation / "credentials" / "volc.json"
        ).unlink(),
    )

    assert result.returncode != 0
    assert set(threading.enumerate()) <= existing_threads


@pytest.mark.parametrize(
    ("mutation", "failure_classification"),
    [
        (
            lambda _output, isolation, _debug: (isolation / "config" / "vm-snapshot.json").write_text(
                json.dumps(
                    {
                        "schema": "codexhub.real-client-vm-snapshot.v1",
                        "snapshot": "codexhub-real-client-e2e-v1",
                        "machine_name_sha256": "sha256:" + "0" * 64,
                    }
                ),
                encoding="utf-8",
            ),
            "preflight_snapshot_identity_mismatch",
        ),
        (
            lambda _output, isolation, _debug: (isolation / "account" / "auth.json").write_text(
                json.dumps({"auth_mode": "chatgpt", "tokens": {}}), encoding="utf-8"
            ),
            "preflight_codex_login_missing",
        ),
        (
            lambda _output, isolation, _debug: (isolation / "config" / "gateway.json").write_text(
                json.dumps(
                    {
                        "schema": "codexhub.real-client-gateway.v1",
                        "listen_port": 19190,
                        "gateway_client_key": "short",
                    }
                ),
                encoding="utf-8",
            ),
            "preflight_gateway_config_invalid",
        ),
    ],
    ids=["snapshot", "codex-login", "gateway-config"],
)
def test_vm_login_and_gateway_inputs_are_fail_closed(tmp_path, mutation, failure_classification):
    result = _run(tmp_path, mutate=mutation, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig"))
    assert summary["failure_classification"] == failure_classification


def test_manual_evidence_cannot_predate_template_and_gui_launch(tmp_path):
    def precreate_evidence(output, _isolation, _debug):
        (output / "manual-evidence.json").write_text("{}", encoding="utf-8")

    result = _run(
        tmp_path,
        fake="fake-client-real-contract.cmd",
        mutate=precreate_evidence,
        finalize_manual=False,
    )

    assert result.returncode != 0
    assert (tmp_path / "output" / "manual-evidence.template.json").is_file()


def test_preflight_failure_emits_one_bounded_sanitized_summary(tmp_path):
    def remove_credentials(_output, isolation, _debug):
        (isolation / "credentials" / "volc.json").unlink()

    result = _run(tmp_path, mutate=remove_credentials, finalize_manual=False)

    assert result.returncode != 0
    summaries = list(tmp_path.rglob("summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    _assert_exact_summary_schema(summary)
    assert summary["outcome"] == "failed"
    assert summary["failure_classification"] == "preflight_required_file_missing"
    assert summary["cases"] == []
    assert summary["artifacts"] == []
    serialized = json.dumps(summary, sort_keys=True)
    assert "fixture-volc-private-token" not in serialized
    assert str(tmp_path) not in serialized


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
    assert time.monotonic() - started < 60
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
    assert [case["error_event_count"] for case in omp_cases] == [2, 2]
    assert len(list((tmp_path / "output").rglob("child-started"))) == 1
    time.sleep(6)
    assert not list((tmp_path / "output").rglob("child-survived"))
    sanitized = result.stdout + result.stderr
    for path in (summary_path, *(tmp_path / "output" / "artifacts").rglob("*.json")):
        sanitized += path.read_text(encoding="utf-8-sig")
    assert "fixture-private-token" not in sanitized
    assert "C:\\Users\\private-account" not in sanitized
    assert len(sanitized) < 100_000


@pytest.mark.parametrize(
    ("mutation", "failure_classification"),
    [
        (lambda evidence: evidence["cases"].pop(), "manual_evidence_case_count_invalid"),
        (lambda evidence: evidence["cases"].__setitem__(1, dict(evidence["cases"][0])), "manual_evidence_case_missing_or_duplicate"),
        (lambda evidence: evidence["cases"][0].update({"fallback_count": 1}), "manual_evidence_contradictory"),
        (lambda evidence: evidence.update({"candidate_sha": "b" * 40}), "manual_evidence_candidate_sha_stale"),
        (lambda evidence: evidence.update({"run_binding_sha256": "sha256:" + "0" * 64}), "manual_evidence_run_binding_stale"),
        (lambda evidence: evidence.update({"login_confirmed": False}), "manual_evidence_login_missing"),
        (lambda evidence: evidence.update({"gui_confirmed": False}), "manual_evidence_gui_missing"),
    ],
    ids=[
        "missing",
        "duplicate",
        "contradictory",
        "stale-sha",
        "stale-run",
        "missing-login",
        "missing-gui",
    ],
)
def test_manual_evidence_rejects_invalid_post_launch_merges(
    tmp_path, mutation, failure_classification
):
    result = _run(
        tmp_path,
        manual_mutation=mutation,
    )

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig"))
    assert summary["failure_classification"] == failure_classification
    assert summary["artifacts"] == []
    assert "fixture-private-token" not in result.stdout + result.stderr


def test_manual_evidence_merge_is_deterministic_for_reordered_input(tmp_path):
    result = _run(
        tmp_path,
        manual_mutation=lambda evidence: evidence["cases"].reverse(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig"))
    manual_ids = [
        case["case_id"]
        for case in summary["cases"]
        if case["case_id"].startswith(("desktop", "zcode"))
    ]
    assert manual_ids == ["desktop-luna", "desktop-volc", "zcode-luna", "zcode-volc"]


def test_missing_manual_evidence_and_early_gui_exit_are_classified(tmp_path):
    missing = _run(
        tmp_path / "missing",
        finalize_manual=False,
        manual_timeout_seconds=1,
    )
    exited = _run(
        tmp_path / "exited",
        client_fakes={"CodexDesktopPath": "fake-gui-exit.cmd"},
        finalize_manual=False,
        manual_timeout_seconds=5,
    )

    missing_summary = json.loads(
        (tmp_path / "missing" / "output" / "summary.json").read_text(encoding="utf-8-sig")
    )
    exited_summary = json.loads(
        (tmp_path / "exited" / "output" / "summary.json").read_text(encoding="utf-8-sig")
    )
    assert missing_summary["failure_classification"] == "manual_evidence_timeout"
    assert exited_summary["failure_classification"] == "manual_gui_exited_before_finalization"


def test_candidate_startup_failure_emits_one_summary_without_partial_artifacts(tmp_path):
    result = _run(
        tmp_path,
        debug_fake="fake-debug-build-exit.cmd",
        finalize_manual=False,
    )

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig"))
    assert summary["failure_classification"] == "candidate_debug_build_exited_during_startup"
    assert summary["artifacts"] == []
    assert not (tmp_path / "output" / "artifacts").exists()


def test_missing_credentials_fail_with_sanitized_summary_before_launch(tmp_path):
    def remove_credentials(_output, isolation, _debug):
        (isolation / "credentials" / "volc.json").unlink()

    result = _run(tmp_path, mutate=remove_credentials)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig"))
    assert summary["failure_classification"] == "preflight_required_file_missing"


def test_native_version_and_debug_build_sidecar_are_sha_bound_preflight_gates(tmp_path):
    def invalidate_sha(_output, _isolation, debug_build):
        Path(f"{debug_build}.candidate-sha").write_text("b" * 40, encoding="ascii")

    wrong_version = _run(
        tmp_path / "version",
        client_fakes={"CodexCliPath": "fake-client-wrong-version.cmd"},
        finalize_manual=False,
    )
    stale_sha = _run(tmp_path / "sha", mutate=invalidate_sha, finalize_manual=False)

    assert wrong_version.returncode != 0
    assert stale_sha.returncode != 0
    wrong_summary = json.loads((tmp_path / "version" / "output" / "summary.json").read_text(encoding="utf-8-sig"))
    stale_summary = json.loads((tmp_path / "sha" / "output" / "summary.json").read_text(encoding="utf-8-sig"))
    assert wrong_summary["failure_classification"] == "preflight_codex_cli_version_mismatch"
    assert stale_summary["failure_classification"] == "preflight_debug_build_sha_mismatch"
