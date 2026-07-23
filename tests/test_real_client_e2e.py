import json
import hashlib
import ctypes
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "Run-RealClientE2E.ps1"
FIXTURES = ROOT / "tests" / "fixtures" / "real_client_e2e"
CANDIDATE_SHA = "a" * 40
LUNA_MODEL = "codexhub-openai/gpt-5.6-luna"
VOLC_MODEL = "codexhub-volc/glm-5.2"
MINIMUM_VERSIONS = {
    "desktop": "26.715.8383.0",
    "codex_cli": "0.144.5",
    "zcode": "3.3.6",
    "opencode": "1.18.4",
    "pi": "0.80.6",
    "omp": "17.0.3",
}
SUMMARY_KEYS = {
    "schema",
    "candidate_sha",
    "managed_client_config_sha",
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
STARTUP_DIAGNOSTIC_KEYS = {
    "schema",
    "outcome",
    "failure_classification",
    "duration_ms",
    "portable_resources_ready",
    "candidate_running",
    "python_child_seen",
    "listener_seen",
    "health_ready",
    "diagnostics_ready",
}
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
    "streaming_request_count",
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
        "streaming_request_count": 2,
        "fallback_count": 0,
        "duplicate_terminal_count": 0,
    }


def _prepare_run(
    tmp_path: Path,
    candidate_sha: str = CANDIDATE_SHA,
    materializer_sha: str = CANDIDATE_SHA,
) -> tuple[Path, Path, Path, Path, Path]:
    output = tmp_path / "output"
    isolation = output / "isolated"
    for relative in ("account", "credentials", "config"):
        (isolation / relative).mkdir(parents=True, exist_ok=True)
    (isolation / "account" / "profile.json").write_text(
        json.dumps(
            {
                "schema": "codexhub.real-client-account.v1",
                "dedicated_account": True,
                "codex_login_ready": True,
                "gui_ready": True,
                "host_session_reused": False,
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
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
    ) as key:
        machine_guid = str(winreg.QueryValueEx(key, "MachineGuid")[0]).lower()
    machine_hash = "sha256:" + hashlib.sha256(
        f"windows-machine-guid-v1:{machine_guid}".encode()
    ).hexdigest()
    host_manifest = isolation / "config" / "host-environment.json"
    host_manifest_template = json.loads(
        (FIXTURES / "host-environment.template.json").read_text()
    )
    host_manifest_template["machine_binding_sha256"] = machine_hash
    host_manifest.write_text(json.dumps(host_manifest_template), encoding="utf-8")
    install_metadata = json.loads(
        (FIXTURES / "windows-install-metadata.template.json").read_text()
    )
    install_metadata["desktop"]["install_location"] = str(FIXTURES.resolve())
    install_metadata["zcode"]["DisplayIcon"] = str(
        (FIXTURES / "fake-client-zcode-appdata.cmd").resolve()
    )
    install_metadata["zcode"]["UninstallString"] = (
        f'"{(FIXTURES / "fake-client-real-contract.cmd").resolve()}" /allusers'
    )
    (isolation / "config" / "windows-install-metadata.json").write_text(
        json.dumps(install_metadata), encoding="utf-8"
    )

    debug_build = tmp_path / "CodexHub-debug.cmd"
    shutil.copyfile(FIXTURES / "fake-debug-build.cmd", debug_build)
    materializer_build = tmp_path / "CodexHub-materializer.cmd"
    shutil.copyfile(FIXTURES / "fake-managed-client-config.cmd", materializer_build)
    shutil.copyfile(
        FIXTURES / "fake-managed-client-config.py",
        tmp_path / "fake-managed-client-config.py",
    )
    shutil.copyfile(FIXTURES / "fake-debug-gateway.py", tmp_path / "fake-debug-gateway.py")
    shutil.copyfile(
        FIXTURES / "validate-managed-client-contract-probe.py",
        tmp_path / "validate-managed-client-contract-probe.py",
    )
    shutil.copyfile(FIXTURES / "write-catalog.py", tmp_path / "write-catalog.py")
    portable_files = (
        "config/providers.toml",
        "src-python/codex_proxy.py",
        "src-python/diagnostic_recorder.py",
        "python/python.exe",
        "python/codexhub-python-runtime.json",
    )
    for relative in portable_files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    Path(f"{debug_build}.candidate-sha").write_text(candidate_sha, encoding="ascii")
    Path(f"{materializer_build}.candidate-sha").write_text(
        materializer_sha, encoding="ascii"
    )

    return output, isolation, debug_build, materializer_build, host_manifest


def _finalize_manual_evidence(
    output: Path, mutation=None, stop_event: threading.Event | None = None
) -> None:
    template_path = output / "manual-evidence.template.json"
    work = output / "isolated" / "work"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and not (stop_event and stop_event.is_set()):
        if (
            template_path.is_file()
            and all(
                (work / f"gui-{case_id}.launched").is_file()
                for case_id in (
                    "desktop-luna",
                    "desktop-volc",
                    "zcode-luna",
                    "zcode-volc",
                )
            )
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
    materializer_fake: str | None = None,
    candidate_sha: str = CANDIDATE_SHA,
    materializer_sha: str = CANDIDATE_SHA,
    mutate=None,
    manual_mutation=None,
    finalize_manual: bool = True,
    timeout_seconds: int = 10,
    manual_timeout_seconds: int = 10,
    overall_timeout_seconds: int = 180,
    authoritative_paths_with_spaces: bool = False,
) -> subprocess.CompletedProcess[str]:
    output, isolation, debug_build, materializer_build, host_manifest = _prepare_run(
        tmp_path, candidate_sha, materializer_sha
    )
    if debug_fake is not None:
        shutil.copyfile(FIXTURES / debug_fake, debug_build)
    if materializer_fake is not None:
        shutil.copyfile(FIXTURES / materializer_fake, materializer_build)
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
    if authoritative_paths_with_spaces:
        candidate_root = tmp_path / "Program Files" / "CodexHub Candidate"
        candidate_root.mkdir(parents=True)
        spaced_debug_build = candidate_root / "CodexHub Debug.cmd"
        spaced_materializer_build = candidate_root / "CodexHub Materializer.cmd"
        shutil.copyfile(debug_build, spaced_debug_build)
        shutil.copyfile(materializer_build, spaced_materializer_build)
        for support in (
            "fake-debug-gateway.py",
            "fake-managed-client-config.py",
            "validate-managed-client-contract-probe.py",
        ):
            shutil.copyfile(tmp_path / support, candidate_root / support)
        for relative in (
            "config/providers.toml",
            "src-python/codex_proxy.py",
            "src-python/diagnostic_recorder.py",
            "python/python.exe",
            "python/codexhub-python-runtime.json",
        ):
            path = candidate_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture", encoding="utf-8")
        Path(f"{spaced_debug_build}.candidate-sha").write_text(
            candidate_sha, encoding="ascii"
        )
        Path(f"{spaced_materializer_build}.candidate-sha").write_text(
            materializer_sha, encoding="ascii"
        )
        debug_build = spaced_debug_build
        materializer_build = spaced_materializer_build

        desktop_root = tmp_path / "Program Files" / "OpenAI Codex"
        zcode_root = tmp_path / "Program Files" / "ZCode"
        desktop_root.mkdir(parents=True)
        zcode_root.mkdir(parents=True)
        desktop_path = desktop_root / "Codex Desktop.cmd"
        zcode_path = zcode_root / "ZCode.cmd"
        shutil.copyfile(FIXTURES / fake, desktop_path)
        shutil.copyfile(FIXTURES / fake, zcode_path)
        zcode_icon = zcode_root / "uninstallerIcon.ico"
        zcode_uninstaller = zcode_root / "Uninstall ZCode.exe"
        zcode_icon.write_bytes(b"fixture")
        zcode_uninstaller.write_bytes(b"fixture")
        metadata_path = isolation / "config" / "windows-install-metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["desktop"]["install_location"] = str(desktop_root.resolve())
        metadata["zcode"]["DisplayIcon"] = str(zcode_icon.resolve())
        metadata["zcode"]["UninstallString"] = (
            f'"{zcode_uninstaller.resolve()}" /allusers'
        )
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        executable_arguments["CodexDesktopPath"] = desktop_path
        executable_arguments["ZCodePath"] = zcode_path
    metadata_path = isolation / "config" / "windows-install-metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["desktop"]["manifest_executable"] = str(
        Path(executable_arguments["CodexDesktopPath"]).resolve()
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    if mutate is not None:
        mutate(output, isolation, debug_build)
    command = [
        _powershell(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-CandidateSha",
        candidate_sha,
        "-DebugBuild",
        str(debug_build),
        "-ManagedClientConfigBuild",
        str(materializer_build),
        "-ManagedClientConfigSha",
        materializer_sha,
        "-LunaModel",
        LUNA_MODEL,
        "-VolcModel",
        VOLC_MODEL,
        "-OutputDirectory",
        str(output),
        "-HostEnvironmentManifest",
        str(host_manifest),
        "-TestWindowsInstallMetadataFixture",
        str(isolation / "config" / "windows-install-metadata.json"),
    ]
    for name, executable in executable_arguments.items():
        command.extend((f"-{name}", str(executable)))
    command.extend(("-TimeoutSeconds", str(timeout_seconds)))
    command.extend(("-ManualEvidenceTimeoutSeconds", str(manual_timeout_seconds)))
    command.extend(("-OverallTimeoutSeconds", str(overall_timeout_seconds)))
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
            timeout=240,
        )
    finally:
        if finalizer is not None and finalizer_stop is not None:
            finalizer_stop.set()
            finalizer.join(timeout=1)
    return result


def _pid_is_running(process_id: int) -> bool:
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, process_id)
    if not process:
        return False
    try:
        exit_code = ctypes.c_ulong()
        return bool(
            ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))
        ) and exit_code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(process)


def _run_watchdog_fixture(tmp_path: Path, mode: str) -> tuple[subprocess.CompletedProcess[str], list[int]]:
    pid_log = tmp_path / f"{mode}.pids"
    command = [
        sys.executable,
        str(FIXTURES / "run-with-windows-watchdog.py"),
        "--timeout-seconds",
        "5",
        "--",
        sys.executable,
        str(FIXTURES / "fake-watchdog-child.py"),
        mode,
        str(pid_log),
    ]
    started = time.monotonic()
    result = subprocess.run(command, text=True, capture_output=True, timeout=15)
    assert time.monotonic() - started < 12
    pids = [int(value) for value in pid_log.read_text().splitlines()]
    return result, pids


def test_baseline_gateway_is_bound_to_exact_current_candidate_materializer(tmp_path):
    baseline_sha = "cc9df197a709fb4c7548021819ecb8fa716ed664"
    materializer_sha = "b" * 40

    result = _run(
        tmp_path,
        candidate_sha=baseline_sha,
        materializer_sha=materializer_sha,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(
        (tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig")
    )
    assert summary["candidate_sha"] == baseline_sha
    assert summary["managed_client_config_sha"] == materializer_sha
    assert set(summary["hashes"]) == {
        "debug_build",
        "managed_client_config_build",
    }
    manual_template = json.loads(
        (tmp_path / "output" / "manual-evidence.template.json").read_text(
            encoding="utf-8-sig"
        )
    )
    assert manual_template["candidate_sha"] == baseline_sha
    assert manual_template["managed_client_config_sha"] == materializer_sha


def test_runner_invokes_candidate_materializer_for_every_managed_client(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    invocations = [
        json.loads(line)
        for line in (
            tmp_path
            / "output"
            / "isolated"
            / "work"
            / "managed-client-config-invocations.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert {item["client"] for item in invocations} == {
        "codex",
        "opencode",
        "zcode",
        "pi",
        "omp",
    }
    for client in {item["client"] for item in invocations}:
        assert {item["verb"] for item in invocations if item["client"] == client} == {
            "preview",
            "apply",
            "readback",
        }
    official_models = {
        ("codex", "gpt-5.6-luna"),
        ("opencode", "openai/gpt-5.6-luna"),
        ("zcode", "openai/gpt-5.6-luna"),
        ("pi", "openai/gpt-5.6-luna"),
        ("omp", "openai/gpt-5.6-luna"),
    }
    assert all(
        "--catalog-path" in item["flags"]
        for item in invocations
        if (item["client"], item["model"]) in official_models
    )
    assert all(
        "--catalog-path" not in item["flags"]
        for item in invocations
        if (item["client"], item["model"]) not in official_models
    )
    assert all(
        item["root_role"]
        == ("managed-preview" if item["verb"] == "preview" else "managed-apply")
        for item in invocations
    )
    applied_models = {
        client: {
            item["model"]
            for item in invocations
            if item["client"] == client and item["verb"] == "apply"
        }
        for client in {"codex", "opencode", "zcode", "pi", "omp"}
    }
    assert applied_models == {
        "codex": {"gpt-5.6-luna", "volc/glm-5.2"},
        "opencode": {"openai/gpt-5.6-luna", "volc/glm-5.2"},
        "zcode": {"openai/gpt-5.6-luna", "volc/glm-5.2"},
        "pi": {"openai/gpt-5.6-luna", "volc/glm-5.2"},
        "omp": {"openai/gpt-5.6-luna", "volc/glm-5.2"},
    }
    assert "managed-client-config" in SCRIPT.read_text(encoding="utf-8")
    assert "Get-ClientProviderMap" not in SCRIPT.read_text(encoding="utf-8")


def test_all_managed_client_route_contracts_are_probed_before_candidate_launch(
    tmp_path,
):
    result = _run(
        tmp_path,
        debug_fake="fake-debug-build-contract-probe.cmd",
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_candidate_publishes_catalog_at_production_runtime_root(tmp_path):
    result = _run(
        tmp_path,
        debug_fake="fake-debug-build-official-bootstrap.cmd",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    candidate_runtime = (
        tmp_path / "output" / "isolated" / "work" / "candidate" / "runtime"
    )
    assert (
        candidate_runtime / "model-catalogs" / "codexhub-model-catalog.json"
    ).is_file()
    assert not (
        candidate_runtime / "proxy" / "model-catalogs" / "codexhub-model-catalog.json"
    ).exists()


def test_candidate_waits_for_atomic_catalog_publication_within_startup_budget(
    tmp_path,
):
    catalog_path = (
        tmp_path
        / "output"
        / "isolated"
        / "work"
        / "candidate"
        / "runtime"
        / "model-catalogs"
        / "codexhub-model-catalog.json"
    )
    published = threading.Event()

    def publish_after_refresh_returns() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not catalog_path.parent.is_dir():
            time.sleep(0.01)
        time.sleep(0.25)
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text('{"candidate-managed": true}', encoding="utf-8")
        published.set()

    publisher = threading.Thread(target=publish_after_refresh_returns, daemon=True)
    publisher.start()
    try:
        result = _run(
            tmp_path,
            debug_fake="fake-debug-build-official-bootstrap-no-catalog.cmd",
        )
    finally:
        publisher.join(timeout=5)

    assert published.is_set()
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(
        (tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig")
    )
    assert summary["outcome"] == "passed"


def test_obsolete_proxy_subdir_catalog_fails_closed_before_clients_or_requests(
    tmp_path,
):
    def replace_debug_build_with_obsolete_publisher(output, isolation, debug_build):
        # Use a special debug-build fixture that writes the catalog to the
        # obsolete proxy/model-catalogs location instead of the production
        # runtime root. The runner must reject the missing production path.
        shutil.copyfile(
            FIXTURES / "fake-debug-build-official-bootstrap-obsolete.cmd",
            debug_build,
        )

    result = _run(
        tmp_path,
        debug_fake="fake-debug-build-official-bootstrap.cmd",
        mutate=replace_debug_build_with_obsolete_publisher,
        finalize_manual=False,
    )

    assert result.returncode != 0
    summary = json.loads(
        (tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig")
    )
    _assert_exact_summary_schema(summary)
    assert (
        summary["failure_classification"]
        == "candidate_gateway_bootstrap_failed_context_budget"
    )
    assert summary["counts"]["case_count"] == 0
    assert not list((tmp_path / "output" / "isolated" / "work").rglob("gui-*.launched"))


def test_official_managed_client_probes_receive_explicit_runtime_catalog_path(
    tmp_path,
):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    invocations = [
        json.loads(line)
        for line in (
            tmp_path
            / "output"
            / "isolated"
            / "work"
            / "managed-client-config-invocations.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    for item in invocations:
        if "--catalog-path" in item["flags"]:
            catalog = item["catalog_path"]
            assert catalog.endswith(
                "runtime\\model-catalogs\\codexhub-model-catalog.json"
            )
            assert "proxy\\model-catalogs" not in catalog


def test_codex_apply_accepts_production_omitted_optional_history_fields(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(
        (tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig")
    )
    assert summary["outcome"] == "passed"


def test_materializer_resolves_unique_nested_codex_target_from_opaque_basename(
    tmp_path,
):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    published = list(
        (tmp_path / "output" / "isolated" / "work").glob(
            "*/.codex/config.toml"
        )
    )
    assert published
    assert all("127.0.0.1:19190" in path.read_text() for path in published)


@pytest.mark.parametrize(
    "materializer_fake",
    [
        "fake-managed-client-config-target-missing.cmd",
        "fake-managed-client-config-target-ambiguous.cmd",
        "fake-managed-client-config-target-reparse.cmd",
        "fake-managed-client-config-target-hardlink.cmd",
        "fake-managed-client-config-target-escape.cmd",
        "fake-managed-client-config-target-over-bound.cmd",
    ],
)
def test_materializer_target_resolution_fails_closed(
    tmp_path, materializer_fake
):
    result = _run(
        tmp_path,
        materializer_fake=materializer_fake,
        finalize_manual=False,
    )

    assert result.returncode != 0
    summaries = list(tmp_path.rglob("summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    _assert_exact_summary_schema(summary)
    assert summary["failure_classification"] == (
        "client_configuration_materializer_output_invalid"
    )


def test_opencodex_appdata_shim_fails_under_case_local_isolation(tmp_path):
    result = _run(
        tmp_path,
        client_fakes={
            "CodexCliPath": "fake-client-opencodex-appdata-shim.cmd"
        },
    )

    assert result.returncode != 0
    summary = json.loads(
        (tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig")
    )
    assert summary["failure_classification"] == "case_failure"
    assert {
        case["case_id"]
        for case in summary["cases"]
        if case["outcome"] == "failed"
    } == {"codex-cli-luna", "codex-cli-volc"}
    markers = list(
        (tmp_path / "output" / "isolated" / "work").glob(
            "codex-cli-*/opencodex-appdata-isolated.marker"
        )
    )
    assert len(markers) == 2
    documentation = (ROOT / "docs" / "agents" / "real-client-e2e.md").read_text(
        encoding="utf-8"
    )
    assert "Pass the real Codex CLI executable" in documentation
    assert "OpenCodex-style shim" in documentation
    assert "replaces `%APPDATA%`" in documentation


def test_codex_apply_accepts_bounded_present_optional_history_fields(tmp_path):
    result = _run(
        tmp_path,
        materializer_fake="fake-managed-client-config-present-optionals.cmd",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "materializer_fake",
    [
        "fake-managed-client-config-missing-required.cmd",
        "fake-managed-client-config-unknown-key.cmd",
    ],
)
def test_codex_apply_rejects_missing_required_and_unknown_keys(
    tmp_path, materializer_fake
):
    result = _run(
        tmp_path,
        materializer_fake=materializer_fake,
        finalize_manual=False,
    )

    assert result.returncode != 0
    summary = json.loads(
        (tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig")
    )
    assert summary["failure_classification"] == (
        "client_configuration_materializer_output_invalid"
    )


def test_runner_has_no_second_managed_client_schema_or_protocol_generator():
    source = SCRIPT.read_text(encoding="utf-8")
    documentation = (ROOT / "docs" / "agents" / "real-client-e2e.md").read_text(
        encoding="utf-8"
    )

    for removed in (
        "Get-CodexProviderConfigText",
        "Get-ClientProviderMap",
        "Get-ProviderEndpointContract",
        "@ai-sdk/openai",
        "openai-chat-completions",
        "openai-completions",
        "/chat/completions",
        "codex-target",
    ):
        assert removed not in source
    assert "managed-client-config" in source
    assert "preview" in source and "apply" in source and "readback" in source
    assert "cc9df197a709fb4c7548021819ecb8fa716ed664" in documentation
    assert "never falls back to handwritten configuration" in documentation


@pytest.mark.parametrize(
    ("materializer_fake", "classification"),
    [
        (
            "fake-managed-client-config-contradiction.cmd",
            "client_configuration_materializer_contradiction",
        ),
        (
            "fake-managed-client-config-failure.cmd",
            "client_configuration_materializer_failed",
        ),
        (
            "fake-managed-client-config-unsafe-output.cmd",
            "client_configuration_materializer_output_invalid",
        ),
    ],
)
def test_materializer_contradiction_and_failure_fail_closed_without_secrets(
    tmp_path, materializer_fake, classification
):
    result = _run(
        tmp_path,
        materializer_fake=materializer_fake,
        finalize_manual=False,
    )

    assert result.returncode != 0
    summaries = list(tmp_path.rglob("summary.json"))
    assert len(summaries) == 1
    serialized = summaries[0].read_text(encoding="utf-8-sig")
    summary = json.loads(serialized)
    _assert_exact_summary_schema(summary)
    assert summary["failure_classification"] == classification
    assert "fixture-gateway-private-key" not in serialized


def test_materializer_build_sidecar_must_match_explicit_current_candidate_sha(tmp_path):
    materializer_sha = "b" * 40

    def stale_materializer_sidecar(output, isolation, debug_build):
        materializer_build = debug_build.with_name("CodexHub-materializer.cmd")
        Path(f"{materializer_build}.candidate-sha").write_text(
            CANDIDATE_SHA, encoding="ascii"
        )

    result = _run(
        tmp_path,
        materializer_sha=materializer_sha,
        mutate=stale_materializer_sidecar,
        finalize_manual=False,
    )

    assert result.returncode != 0
    summary = json.loads(
        (tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig")
    )
    _assert_exact_summary_schema(summary)
    assert summary["failure_classification"] == "preflight_materializer_build_sha_mismatch"


def _assert_exact_summary_schema(summary: dict) -> None:
    assert set(summary) == (SUMMARY_KEYS if summary["cases"] else FAILURE_SUMMARY_KEYS)
    assert set(summary["pinned_versions"]) == set(MINIMUM_VERSIONS)
    assert set(summary["counts"]) == COUNT_KEYS
    if summary["cases"]:
        assert set(summary["hashes"]) == {
            "debug_build",
            "managed_client_config_build",
        }
        for case in summary["cases"]:
            expected = (
                AUTOMATED_CASE_KEYS
                if case["case_id"].startswith(("codex-cli", "opencode", "pi", "omp"))
                else CASE_KEYS
            )
            assert set(case) == expected


def test_operator_workflow_requires_release_optimized_debug_portable_build():
    documentation = (ROOT / "docs" / "agents" / "real-client-e2e.md").read_text()

    assert "build-windows-portable.ps1" in documentation
    assert "-Flavor debug" in documentation
    assert "-RepoRoot <absolute-repo-root>" in documentation
    assert "_debug_portable_<sha8>/CodexHub.exe" in documentation
    assert "plain Cargo Debug executable" in documentation


def test_operator_workflow_uses_authoritative_machine_bound_local_host():
    documentation = (ROOT / "docs" / "agents" / "real-client-e2e.md").read_text()

    assert "machine-bound local dedicated Windows host" in documentation
    assert "A VM or named snapshot is not required" in documentation
    assert "dedicated VM" not in documentation
    assert "VM snapshot" not in documentation


def test_exact_compatibility_floors_pass_and_emit_one_sanitized_sha_bound_summary(
    tmp_path,
):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    summaries = list(tmp_path.rglob("summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    _assert_exact_summary_schema(summary)
    assert summary["schema"] == "codexhub.real-client-e2e-summary.v1"
    assert summary["candidate_sha"] == CANDIDATE_SHA
    assert summary["pinned_versions"] == MINIMUM_VERSIONS
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
        "isolated/config/host-environment.json",
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
    assert all(case["streaming_request_count"] == 2 for case in automated)
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


def test_volc_managed_client_probes_do_not_receive_catalog_path(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    invocations = [
        json.loads(line)
        for line in (
            tmp_path
            / "output"
            / "isolated"
            / "work"
            / "managed-client-config-invocations.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert all(
        "--catalog-path" not in item["flags"]
        for item in invocations
        if item["model"] == "volc/glm-5.2"
    )


def test_codex_cli_read_tool_requires_explicit_completed_zero_exit(tmp_path):
    result = _run(
        tmp_path,
        client_fakes={"CodexCliPath": "fake-client-codex-tool-invalid.cmd"},
    )

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    codex_cases = [
        case for case in summary["cases"] if case["case_id"].startswith("codex-cli-")
    ]
    assert [case["outcome"] for case in codex_cases] == ["failed", "failed"]
    assert [case["read_only_tool_call_count"] for case in codex_cases] == [0, 0]
    assert not list((tmp_path / "output" / "isolated" / "work").rglob("sentinel.txt"))


def test_final_client_message_with_nonstream_gateway_completions_fails(tmp_path):
    result = _run(
        tmp_path,
        client_fakes={"PiPath": "fake-client-nonstreaming.cmd"},
    )

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    pi_cases = [case for case in summary["cases"] if case["case_id"].startswith("pi-")]
    assert [case["outcome"] for case in pi_cases] == ["failed", "failed"]
    assert [case["sentinel_chunk_count"] for case in pi_cases] == [1, 1]
    assert [case["streaming_request_count"] for case in pi_cases] == [0, 0]
    assert all(case["error_event_count"] >= 1 for case in pi_cases)


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
            lambda _output, isolation, _debug: (isolation / "config" / "host-environment.json").write_text(
                json.dumps(
                    {
                        "schema": "codexhub.real-client-host-environment.v1",
                        "environment": "codexhub-real-client-e2e",
                        "machine_binding_sha256": "sha256:" + "0" * 64,
                    }
                ),
                encoding="utf-8",
            ),
            "preflight_host_environment_identity_mismatch",
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
    ids=["host-environment", "codex-login", "gateway-config"],
)
def test_host_login_and_gateway_inputs_are_fail_closed(tmp_path, mutation, failure_classification):
    result = _run(tmp_path, mutate=mutation, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text(encoding="utf-8-sig"))
    assert summary["failure_classification"] == failure_classification


def test_host_environment_manifest_is_machine_bound_and_credential_free(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(
        (tmp_path / "output" / "isolated" / "config" / "host-environment.json").read_text()
    )
    assert set(manifest) == {"schema", "environment", "machine_binding_sha256"}
    assert manifest["schema"] == "codexhub.real-client-host-environment.v1"
    assert manifest["environment"] == "codexhub-real-client-e2e"
    assert manifest["machine_binding_sha256"].startswith("sha256:")
    serialized = json.dumps(manifest).lower()
    assert not any(name in serialized for name in ("username", "credential", "token", "auth"))


def test_malformed_host_environment_manifest_fails_before_launch(tmp_path):
    def add_host_identity(_output, isolation, _debug):
        path = isolation / "config" / "host-environment.json"
        manifest = json.loads(path.read_text())
        manifest["username"] = "must-not-be-recorded"
        path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run(tmp_path, mutate=add_host_identity, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_host_environment_manifest_invalid"
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()
    assert "must-not-be-recorded" not in json.dumps(summary)


def test_hard_linked_host_auth_input_is_rejected_before_launch(tmp_path):
    host_auth = tmp_path / "host-auth.json"
    host_auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "host-access", "refresh_token": "host-refresh"},
            }
        ),
        encoding="utf-8",
    )

    def reuse_host_auth(_output, isolation, _debug):
        isolated_auth = isolation / "account" / "auth.json"
        isolated_auth.unlink()
        os.link(host_auth, isolated_auth)

    result = _run(tmp_path, mutate=reuse_host_auth, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_host_session_reuse_detected"
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


def test_sparse_hklm_zcode_metadata_is_normalized_under_strict_mode(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["pinned_versions"]["desktop"] == "26.715.8383.0"
    assert summary["pinned_versions"]["zcode"] == "3.3.6"
    install_metadata = json.loads(
        (
            tmp_path
            / "output"
            / "isolated"
            / "config"
            / "windows-install-metadata.json"
        ).read_text()
    )
    desktop_metadata = install_metadata["desktop"]
    assert desktop_metadata["package_version"] == "26.715.8383.0"
    assert desktop_metadata["executable_product_version"] == "1.2026.1704.0"
    zcode_metadata = install_metadata["zcode"]
    assert zcode_metadata["DisplayName"] == "ZCode 3.3.6"
    assert zcode_metadata["DisplayVersion"] == "3.3.6"
    assert zcode_metadata["Publisher"] == "ZCode"
    assert "InstallLocation" not in zcode_metadata


def test_zcode_valid_install_location_agrees_with_authoritative_fallbacks(tmp_path):
    def add_install_location(_output, isolation, _debug):
        path = isolation / "config" / "windows-install-metadata.json"
        metadata = json.loads(path.read_text())
        metadata["zcode"]["InstallLocation"] = str(FIXTURES.resolve())
        path.write_text(json.dumps(metadata), encoding="utf-8")

    result = _run(tmp_path, mutate=add_install_location)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("argument", "fixture", "failure"),
    [
        ("CodexCliPath", "fake-client-version-suffix.cmd", "preflight_codex_cli_version_mismatch"),
        ("OpenCodePath", "fake-client-version-suffix.cmd", "preflight_opencode_version_mismatch"),
        ("OpenCodePath", "fake-client-version-multiple.cmd", "preflight_opencode_version_mismatch"),
        ("PiPath", "fake-client-version-suffix.cmd", "preflight_pi_version_mismatch"),
        ("OmpPath", "fake-client-version-multiple.cmd", "preflight_omp_version_mismatch"),
    ],
)
def test_non_zcode_client_versions_reject_suffixes_and_multiple_versions(
    tmp_path, argument, fixture, failure
):
    result = _run(
        tmp_path,
        client_fakes={argument: fixture},
        finalize_manual=False,
    )

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == failure
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


def test_codex_cli_0_145_0_is_accepted_and_recorded_as_actual_version(tmp_path):
    result = _run(
        tmp_path,
        client_fakes={"CodexCliPath": "fake-client-codex-0.145.0.cmd"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["pinned_versions"]["codex_cli"] == "0.145.0"


def test_all_newer_stable_authoritative_versions_are_recorded_as_actual(tmp_path):
    def install_newer_desktop_and_zcode(_output, isolation, _debug):
        path = isolation / "config" / "windows-install-metadata.json"
        metadata = json.loads(path.read_text())
        metadata["desktop"]["package_version"] = "26.716.0.0"
        metadata["zcode"].update(
            {
                "DisplayName": "ZCode 3.4.0",
                "DisplayVersion": "3.4.0",
                "ExecutableProductVersion": "3.4.0.4000",
            }
        )
        path.write_text(json.dumps(metadata), encoding="utf-8")

    result = _run(
        tmp_path,
        fake="fake-client-newer-stable.cmd",
        mutate=install_newer_desktop_and_zcode,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["pinned_versions"] == {
        "desktop": "26.716.0.0",
        "codex_cli": "0.145.0",
        "zcode": "3.4.0",
        "opencode": "1.19.0",
        "pi": "0.81.0",
        "omp": "17.1.0",
    }


@pytest.mark.parametrize(
    ("argument", "failure"),
    [
        ("CodexCliPath", "preflight_codex_cli_version_mismatch"),
        ("OpenCodePath", "preflight_opencode_version_mismatch"),
        ("PiPath", "preflight_pi_version_mismatch"),
        ("OmpPath", "preflight_omp_version_mismatch"),
    ],
)
def test_non_windows_clients_reject_every_below_floor_release(
    tmp_path, argument, failure
):
    result = _run(
        tmp_path,
        client_fakes={argument: "fake-client-below-floor.cmd"},
        finalize_manual=False,
    )

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == failure


@pytest.mark.parametrize("client", ["desktop", "zcode"])
def test_windows_authorities_reject_every_below_floor_release(tmp_path, client):
    def install_below_floor(_output, isolation, _debug):
        path = isolation / "config" / "windows-install-metadata.json"
        metadata = json.loads(path.read_text())
        if client == "desktop":
            metadata["desktop"]["package_version"] = "26.715.8382.9999"
        else:
            metadata["zcode"].update(
                {
                    "DisplayName": "ZCode 3.3.5",
                    "DisplayVersion": "3.3.5",
                    "ExecutableProductVersion": "3.3.5.3198",
                }
            )
        path.write_text(json.dumps(metadata), encoding="utf-8")

    result = _run(tmp_path, mutate=install_below_floor, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == f"preflight_{client}_version_mismatch"


@pytest.mark.parametrize(
    ("argument", "failure"),
    [
        ("CodexCliPath", "preflight_codex_cli_version_mismatch"),
        ("OpenCodePath", "preflight_opencode_version_mismatch"),
        ("PiPath", "preflight_pi_version_mismatch"),
        ("OmpPath", "preflight_omp_version_mismatch"),
    ],
)
def test_non_windows_clients_reject_unparseable_versions(tmp_path, argument, failure):
    result = _run(
        tmp_path,
        client_fakes={argument: "fake-client-version-unparseable.cmd"},
        finalize_manual=False,
    )

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == failure


def test_opencode_1_18_3_is_rejected_as_missing_header_timeout_fix(tmp_path):
    result = _run(
        tmp_path,
        client_fakes={"OpenCodePath": "fake-client-opencode-1.18.3.cmd"},
        finalize_manual=False,
    )

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_opencode_version_mismatch"
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


def test_opencode_compatibility_floor_records_upstream_header_timeout_fix():
    documentation = (ROOT / "docs" / "agents" / "real-client-e2e.md").read_text()
    runner = SCRIPT.read_text()

    assert "opencode = '1.18.4'" in runner
    assert "OpenCode | `1.18.4`" in documentation
    assert "response-header-timeout" in documentation
    assert "67caf894e0843ee370e72839e8265e483233479b" in documentation


def test_operator_docs_define_compatibility_floors_and_actual_version_evidence():
    documentation = (ROOT / "docs" / "agents" / "real-client-e2e.md").read_text()

    assert "Minimum stable version" in documentation
    assert "Codex CLI `0.145.0` is accepted" in documentation
    assert "actual normalized versions" in documentation
    assert "pinned exactly" not in documentation
    assert "equal to the pin" not in documentation


@pytest.mark.parametrize(
    ("client", "field", "value", "failure"),
    [
        ("desktop", "package_version", "26.715.7063.0", "preflight_desktop_version_mismatch"),
        (
            "desktop",
            "package_version",
            "26.715.8383.0-beta",
            "preflight_desktop_version_mismatch",
        ),
        ("zcode", "DisplayVersion", "3.3.7", "preflight_zcode_version_mismatch"),
        (
            "zcode",
            "DisplayVersion",
            "3.3.6-beta",
            "preflight_zcode_version_mismatch",
        ),
        (
            "zcode",
            "ExecutableProductVersion",
            "3.3.7.1",
            "preflight_zcode_version_mismatch",
        ),
    ],
)
def test_windows_install_metadata_mismatch_fails_closed(
    tmp_path, client, field, value, failure
):
    def invalidate_metadata(_output, isolation, _debug):
        path = isolation / "config" / "windows-install-metadata.json"
        metadata = json.loads(path.read_text())
        metadata[client][field] = value
        path.write_text(json.dumps(metadata), encoding="utf-8")

    result = _run(tmp_path, mutate=invalidate_metadata, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == failure
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("DisplayName", "ZCode 3.3.7"),
        ("DisplayName", "ZCode Enterprise 3.3.6"),
        ("Publisher", "Not ZCode"),
    ],
)
def test_zcode_authoritative_identity_rejects_spoofed_name_or_publisher(
    tmp_path, field, value
):
    def spoof_identity(_output, isolation, _debug):
        path = isolation / "config" / "windows-install-metadata.json"
        metadata = json.loads(path.read_text())
        metadata["zcode"][field] = value
        path.write_text(json.dumps(metadata), encoding="utf-8")

    result = _run(tmp_path, mutate=spoof_identity, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_zcode_version_mismatch"
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


def test_zcode_authoritative_roots_must_not_conflict(tmp_path):
    conflicting_root = tmp_path / "conflicting-zcode-install"
    conflicting_root.mkdir()
    conflicting_uninstaller = conflicting_root / "Uninstall ZCode.exe"
    conflicting_uninstaller.write_bytes(b"fixture")

    def conflict_roots(_output, isolation, _debug):
        path = isolation / "config" / "windows-install-metadata.json"
        metadata = json.loads(path.read_text())
        metadata["zcode"]["UninstallString"] = (
            f'"{conflicting_uninstaller.resolve()}" /allusers'
        )
        path.write_text(json.dumps(metadata), encoding="utf-8")

    result = _run(tmp_path, mutate=conflict_roots, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_zcode_install_metadata_conflict"
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("DisplayIcon", r"relative\uninstallerIcon.ico"),
        ("UninstallString", r'"relative\Uninstall ZCode.exe" /allusers'),
    ],
)
def test_zcode_authoritative_paths_must_be_absolute(tmp_path, field, value):
    def use_relative_path(_output, isolation, _debug):
        path = isolation / "config" / "windows-install-metadata.json"
        metadata = json.loads(path.read_text())
        metadata["zcode"][field] = value
        path.write_text(json.dumps(metadata), encoding="utf-8")

    result = _run(tmp_path, mutate=use_relative_path, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_zcode_install_metadata_invalid"
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


def test_zcode_install_root_metadata_is_required(tmp_path):
    def remove_roots(_output, isolation, _debug):
        path = isolation / "config" / "windows-install-metadata.json"
        metadata = json.loads(path.read_text())
        metadata["zcode"]["DisplayIcon"] = ""
        metadata["zcode"]["UninstallString"] = ""
        metadata["zcode"].pop("InstallLocation", None)
        path.write_text(json.dumps(metadata), encoding="utf-8")

    result = _run(tmp_path, mutate=remove_roots, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_zcode_install_metadata_invalid"
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


def test_zcode_authoritative_root_must_bind_passed_executable(tmp_path):
    unrelated_root = tmp_path / "unrelated-zcode-install"
    unrelated_root.mkdir()
    icon = unrelated_root / "uninstallerIcon.ico"
    uninstaller = unrelated_root / "Uninstall ZCode.exe"
    icon.write_bytes(b"fixture")
    uninstaller.write_bytes(b"fixture")

    def unbind_executable(_output, isolation, _debug):
        path = isolation / "config" / "windows-install-metadata.json"
        metadata = json.loads(path.read_text())
        metadata["zcode"]["DisplayIcon"] = str(icon.resolve())
        metadata["zcode"]["UninstallString"] = f'"{uninstaller.resolve()}" /allusers'
        path.write_text(json.dumps(metadata), encoding="utf-8")

    result = _run(tmp_path, mutate=unbind_executable, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_zcode_executable_unbound"
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


def test_passed_executables_must_be_bound_to_authoritative_install_locations(tmp_path):
    unrelated_install = tmp_path / "unrelated-install"
    unrelated_install.mkdir()

    def unbind_executables(_output, isolation, _debug):
        path = isolation / "config" / "windows-install-metadata.json"
        metadata = json.loads(path.read_text())
        metadata["desktop"]["install_location"] = str(unrelated_install)
        metadata["zcode"]["InstallLocation"] = str(unrelated_install)
        path.write_text(json.dumps(metadata), encoding="utf-8")

    result = _run(tmp_path, mutate=unbind_executables, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_desktop_executable_unbound"
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


def test_desktop_executable_must_match_appx_manifest_entry(tmp_path):
    def bind_manifest_to_different_executable(_output, isolation, _debug):
        path = isolation / "config" / "windows-install-metadata.json"
        metadata = json.loads(path.read_text())
        metadata["desktop"]["manifest_executable"] = str(
            (FIXTURES / "fake-client-isolation.cmd").resolve()
        )
        path.write_text(json.dumps(metadata), encoding="utf-8")

    result = _run(
        tmp_path,
        mutate=bind_manifest_to_different_executable,
        finalize_manual=False,
    )

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert (
        summary["failure_classification"]
        == "preflight_desktop_executable_unbound"
    )
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


def test_desktop_gui_cases_use_distinct_case_local_user_data_directories(tmp_path):
    result = _run(
        tmp_path,
        client_fakes={"CodexDesktopPath": "fake-client-desktop-argv.cmd"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    work = tmp_path / "output" / "isolated" / "work"
    observed = {}
    for case_id in ("desktop-luna", "desktop-volc"):
        argument_log = work / f"gui-{case_id}.launched.argv"
        arguments = argument_log.read_text(encoding="ascii").strip()
        expected_profile = work / "gui-desktop" / case_id / "browser-profile"
        assert arguments.count("--user-data-dir=") == 1
        assert str(expected_profile).casefold() in arguments.casefold()
        assert "--no-first-run" in arguments
        expected_workspace = work / "gui-desktop" / case_id
        assert arguments.rstrip('"').casefold().endswith(
            str(expected_workspace).casefold()
        )
        observed[case_id] = arguments
    assert observed["desktop-luna"] != observed["desktop-volc"]


def test_zcode_gui_cases_open_their_case_local_workspaces(tmp_path):
    result = _run(
        tmp_path,
        client_fakes={"ZCodePath": "fake-client-desktop-argv.cmd"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    work = tmp_path / "output" / "isolated" / "work"
    for case_id in ("zcode-luna", "zcode-volc"):
        arguments = (
            work / f"gui-{case_id}.launched.argv"
        ).read_text(encoding="ascii").strip()
        expected_workspace = work / "gui-zcode" / case_id
        assert arguments.rstrip('"').casefold().endswith(
            str(expected_workspace).casefold()
        )


def test_candidate_runtime_declares_volc_native_responses_route(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    providers = (
        tmp_path
        / "output"
        / "isolated"
        / "work"
        / "candidate"
        / "runtime"
        / "proxy"
        / "config"
        / "providers.toml"
    ).read_text(encoding="utf-8")
    assert 'upstream_format = "responses"' in providers
    assert 'available_upstream_formats = ["responses"]' in providers


def test_gui_cases_copy_reusable_state_into_fresh_isolated_roots(tmp_path):
    seeded_files = {
        "desktop-luna/browser-profile/Preferences": "desktop-luna-state",
        "desktop-volc/browser-profile/Preferences": "desktop-volc-state",
        "zcode-luna/.zcode/v2/setting.json": "zcode-luna-state",
        "zcode-volc/.zcode/v2/setting.json": "zcode-volc-state",
    }

    def seed_gui_state(_output, isolation, _debug):
        seed_root = isolation / "gui-seed"
        for relative, value in seeded_files.items():
            path = seed_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")

    result = _run(tmp_path, mutate=seed_gui_state)

    assert result.returncode == 0, result.stdout + result.stderr
    work = tmp_path / "output" / "isolated" / "work"
    for relative, value in seeded_files.items():
        case_id, case_relative = relative.split("/", 1)
        client = "desktop" if case_id.startswith("desktop-") else "zcode"
        source = tmp_path / "output" / "isolated" / "gui-seed" / relative
        copied = work / f"gui-{client}" / case_id / case_relative
        assert copied.read_text(encoding="utf-8") == value
        assert copied.stat().st_ino != source.stat().st_ino


def test_gui_state_seed_rejects_hardlinked_files(tmp_path):
    external = tmp_path / "external-gui-state"
    external.write_text("must not be imported", encoding="utf-8")

    def seed_hardlink(_output, isolation, _debug):
        seed = (
            isolation
            / "gui-seed"
            / "desktop-luna"
            / "browser-profile"
            / "Preferences"
        )
        seed.parent.mkdir(parents=True)
        os.link(external, seed)

    result = _run(
        tmp_path,
        mutate=seed_hardlink,
        finalize_manual=False,
    )

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_gui_seed_invalid"


def test_desktop_gui_preserves_windows_identity_for_sandbox_acl_setup(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("USERNAME", "e2e-current-user")
    monkeypatch.setenv("USERDOMAIN", "e2e-current-domain")

    result = _run(
        tmp_path,
        client_fakes={"CodexDesktopPath": "fake-client-desktop-argv.cmd"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    work = tmp_path / "output" / "isolated" / "work"
    for case_id in ("desktop-luna", "desktop-volc"):
        identity = (work / f"gui-{case_id}.launched.identity").read_text(
            encoding="ascii"
        )
        assert identity == "e2e-current-user|e2e-current-domain"


def test_desktop_gui_launches_from_invocation_local_manifest_payload(tmp_path):
    source_executable = FIXTURES / "fake-client-desktop-argv.cmd"

    result = _run(
        tmp_path,
        client_fakes={"CodexDesktopPath": source_executable.name},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    work = tmp_path / "output" / "isolated" / "work"
    staged_root = work / "desktop-app"
    staged_executable = staged_root / source_executable.name
    assert staged_executable.read_bytes() == source_executable.read_bytes()
    assert staged_executable.stat().st_ino != source_executable.stat().st_ino
    for case_id in ("desktop-luna", "desktop-volc"):
        launch_path = (
            work / f"gui-{case_id}.launched.executable"
        ).read_text(encoding="ascii")
        assert Path(launch_path).resolve() == staged_executable.resolve()
        assert Path(launch_path).resolve().is_relative_to(staged_root.resolve())


def test_desktop_payload_hardlinks_are_copied_as_independent_files(tmp_path):
    payload = b"appx deployment hardlink payload"
    desktop_root = tmp_path / "desktop-install"
    desktop_root.mkdir()
    desktop_executable = desktop_root / "CodexDesktop.cmd"
    shutil.copyfile(FIXTURES / "fake-client-desktop-argv.cmd", desktop_executable)
    shutil.copyfile(
        FIXTURES / "fake-client-real-contract.cmd",
        desktop_root / "fake-client-real-contract.cmd",
    )
    source = desktop_root / "shared-runtime.bin"
    linked = desktop_root / "shared-runtime-copy.bin"
    source.write_bytes(payload)
    os.link(source, linked)
    assert source.stat().st_ino == linked.stat().st_ino

    def bind_payload_root(_output, isolation, _debug):
        metadata_path = isolation / "config" / "windows-install-metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["desktop"]["install_location"] = str(desktop_root.resolve())
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    result = _run(
        tmp_path,
        client_fakes={"CodexDesktopPath": desktop_executable},
        mutate=bind_payload_root,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    staged_root = tmp_path / "output" / "isolated" / "work" / "desktop-app"
    staged_source = staged_root / "shared-runtime.bin"
    staged_link = staged_root / "shared-runtime-copy.bin"
    assert staged_source.read_bytes() == payload
    assert staged_link.read_bytes() == payload
    assert staged_source.stat().st_ino != source.stat().st_ino
    assert staged_link.stat().st_ino != linked.stat().st_ino
    assert staged_source.stat().st_ino != staged_link.stat().st_ino


def test_desktop_payload_reparse_points_fail_before_gui_launch(tmp_path):
    external = tmp_path / "external-desktop-state"
    external.mkdir()
    (external / "must-not-be-copied.txt").write_text("host state", encoding="utf-8")

    def add_payload_junction(_output, _isolation, _debug):
        desktop_root = tmp_path / "Program Files" / "OpenAI Codex"
        result = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "mklink",
                "/J",
                str(desktop_root / "linked-host-state"),
                str(external),
            ],
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    result = _run(
        tmp_path,
        authoritative_paths_with_spaces=True,
        mutate=add_payload_junction,
        finalize_manual=False,
        manual_timeout_seconds=1,
    )

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_desktop_payload_invalid"
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()
    assert not (
        tmp_path
        / "output"
        / "isolated"
        / "work"
        / "desktop-app"
        / "linked-host-state"
        / "must-not-be-copied.txt"
    ).exists()


def test_ambient_host_session_environment_never_reaches_candidate_or_clients(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CODEXHUB_HOST_SESSION", "host-session-must-not-reach-child")
    monkeypatch.setenv("OPENAI_API_KEY", "host-openai-key-must-not-reach-child")

    result = _run(tmp_path, fake="fake-client-isolation.cmd")

    assert result.returncode == 0, result.stdout + result.stderr


def test_prepopulated_invocation_work_root_is_rejected_before_launch(tmp_path):
    def add_stale_client_state(_output, isolation, _debug):
        work = isolation / "work"
        work.mkdir()
        (work / "stale-session.json").write_text("stale-host-session", encoding="utf-8")

    result = _run(tmp_path, mutate=add_stale_client_state, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_work_root_not_fresh"
    assert (tmp_path / "output" / "isolated" / "work" / "stale-session.json").is_file()
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


def test_junction_invocation_work_root_is_rejected_before_launch(tmp_path):
    redirected = tmp_path / "host-session-state"
    redirected.mkdir()

    def junction_work_root(_output, isolation, _debug):
        result = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "mklink",
                "/J",
                str(isolation / "work"),
                str(redirected),
            ],
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    result = _run(tmp_path, mutate=junction_work_root, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_work_root_reparse"
    assert list(redirected.iterdir()) == []
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


def test_junction_isolation_ancestor_is_rejected_before_work_root_creation(tmp_path):
    redirected = tmp_path / "redirected-isolation"

    def junction_isolation_ancestor(_output, isolation, _debug):
        isolation.rename(redirected)
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(isolation), str(redirected)],
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    result = _run(tmp_path, mutate=junction_isolation_ancestor, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_work_root_reparse"
    assert not (redirected / "work").exists()
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


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


def test_supervisor_preserves_space_containing_authoritative_path_arguments(tmp_path):
    def remove_credentials(_output, isolation, _debug):
        (isolation / "credentials" / "volc.json").unlink()

    run_root = tmp_path / "Authoritative Host Run"
    result = _run(
        run_root,
        mutate=remove_credentials,
        finalize_manual=False,
        authoritative_paths_with_spaces=True,
    )

    assert result.returncode != 0
    summaries = list(run_root.rglob("summary.json"))
    assert len(summaries) == 1, result.stdout + result.stderr
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    assert summary["failure_classification"] == "preflight_required_file_missing"
    assert "PositionalParameterNotFound" not in result.stdout + result.stderr
    assert not (run_root / "output" / "manual-evidence.template.json").exists()


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
        timeout_seconds=3,
    )

    assert result.returncode != 0
    assert time.monotonic() - started < 120
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
        (lambda evidence: evidence.update({"managed_client_config_sha": "b" * 40}), "manual_evidence_materializer_sha_stale"),
        (lambda evidence: evidence.update({"run_binding_sha256": "sha256:" + "0" * 64}), "manual_evidence_run_binding_stale"),
        (lambda evidence: evidence.update({"login_confirmed": False}), "manual_evidence_login_missing"),
        (lambda evidence: evidence.update({"gui_confirmed": False}), "manual_evidence_gui_missing"),
    ],
    ids=[
        "missing",
        "duplicate",
        "contradictory",
        "stale-sha",
        "stale-materializer-sha",
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
    assert summary["artifacts"] == ["candidate-startup.json"]
    startup = json.loads((tmp_path / "output" / "candidate-startup.json").read_text())
    assert set(startup) == STARTUP_DIAGNOSTIC_KEYS
    assert startup["failure_classification"] == summary["failure_classification"]
    assert startup["candidate_running"] is False
    assert not (tmp_path / "output" / "artifacts").exists()


def test_resource_incomplete_debug_build_is_rejected_before_gui_launch(tmp_path):
    def remove_portable_python(_output, _isolation, debug_build):
        (debug_build.parent / "python" / "python.exe").unlink()

    result = _run(tmp_path, mutate=remove_portable_python, finalize_manual=False)

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_debug_build_not_portable"
    assert summary["artifacts"] == ["candidate-startup.json"]
    startup = json.loads((tmp_path / "output" / "candidate-startup.json").read_text())
    assert set(startup) == STARTUP_DIAGNOSTIC_KEYS
    assert startup["portable_resources_ready"] is False
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()
    assert not list((tmp_path / "output" / "isolated" / "work").glob("gui-*.launched"))


def test_preexisting_gateway_listener_is_rejected_before_candidate_or_gui(tmp_path):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 19190))
    listener.listen()
    try:
        result = _run(tmp_path, finalize_manual=False)
    finally:
        listener.close()

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == "preflight_gateway_port_in_use"
    assert not (tmp_path / "output" / "manual-evidence.template.json").exists()


def test_candidate_bootstraps_official_context_budget_before_gateway_start(tmp_path):
    result = _run(
        tmp_path,
        debug_fake="fake-debug-build-official-bootstrap.cmd",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    isolated_auth = tmp_path / "output" / "isolated" / "account" / "auth.json"
    candidate_auth = (
        tmp_path / "output" / "isolated" / "work" / "candidate" / ".codex" / "auth.json"
    )
    assert candidate_auth.read_bytes() == isolated_auth.read_bytes()
    assert candidate_auth.stat().st_ino != isolated_auth.stat().st_ino
    candidate_proxy = (
        tmp_path / "output" / "isolated" / "work" / "candidate" / "runtime" / "proxy"
    )
    assert (candidate_proxy / "official-bootstrap-invocations.txt").read_text().splitlines() == [
        "refresh-models",
        "start",
    ]
    assert (candidate_proxy / "official-context-budget.ready").is_file()


def test_candidate_bootstrap_does_not_discover_or_reuse_ambient_host_state(
    tmp_path, monkeypatch
):
    host_runtime = tmp_path / "host-runtime"
    host_codex = tmp_path / "host-codex"
    host_runtime.mkdir()
    host_codex.mkdir()
    host_catalog = host_runtime / "host-official-catalog.json"
    host_catalog.write_text("host state must remain unused", encoding="utf-8")
    monkeypatch.setenv("CODEXHUB_RUNTIME_HOME", str(host_runtime))
    monkeypatch.setenv("CODEXHUB_CODEX_TARGET_HOME", str(host_codex))
    monkeypatch.setenv("CODEX_HOME", str(host_codex))
    monkeypatch.setenv("CODEXHUB_CODEX_PATH", str(tmp_path / "missing-host-codex.exe"))
    monkeypatch.setenv("CODEXHUB_HOST_SESSION", "must-not-reach-bootstrap")

    result = _run(
        tmp_path,
        debug_fake="fake-debug-build-official-bootstrap.cmd",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert host_catalog.read_text() == "host state must remain unused"
    assert sorted(path.name for path in host_runtime.iterdir()) == [
        "host-official-catalog.json"
    ]
    assert list(host_codex.iterdir()) == []
    candidate_proxy = (
        tmp_path / "output" / "isolated" / "work" / "candidate" / "runtime" / "proxy"
    )
    assert (candidate_proxy / "official-context-budget.ready").is_file()


def test_candidate_context_budget_bootstrap_failure_is_bounded_and_sanitized(tmp_path):
    def force_context_budget_failure(_output, _isolation, debug_build):
        Path(f"{debug_build}.bootstrap-fail").write_text("fail", encoding="ascii")

    started = time.monotonic()
    result = _run(
        tmp_path,
        debug_fake="fake-debug-build-official-bootstrap.cmd",
        client_fakes={"CodexCliPath": "fake-client-codex-0.145.0.cmd"},
        mutate=force_context_budget_failure,
        finalize_manual=False,
        timeout_seconds=1,
    )
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    assert elapsed < 90
    summaries = list(tmp_path.rglob("summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text())
    assert summary["failure_classification"] == (
        "candidate_gateway_bootstrap_failed_context_budget"
    )
    assert summary["counts"]["case_count"] == 0
    assert summary["artifacts"] == ["candidate-startup.json"]
    assert summary["pinned_versions"]["codex_cli"] == "0.145.0"
    startup_path = tmp_path / "output" / "candidate-startup.json"
    startup = json.loads(startup_path.read_text())
    assert set(startup) == STARTUP_DIAGNOSTIC_KEYS
    assert startup["failure_classification"] == (
        "candidate_gateway_bootstrap_failed_context_budget"
    )
    assert startup["candidate_running"] is False
    assert startup["listener_seen"] is False
    assert startup["health_ready"] is False
    serialized = startup_path.read_text() + summaries[0].read_text()
    assert str(tmp_path) not in serialized
    assert "fixture-codex-access-token" not in serialized
    assert "fixture-volc-private-token" not in serialized
    assert not list((tmp_path / "output" / "isolated" / "work").glob("gui-*.launched"))


def test_candidate_retries_transient_native_model_cache_timeout_within_shared_budget(
    tmp_path,
):
    def fail_first_native_cache_publication(_output, _isolation, debug_build):
        Path(f"{debug_build}.bootstrap-fail-once").write_text(
            "fail once", encoding="ascii"
        )

    started = time.monotonic()
    result = _run(
        tmp_path,
        debug_fake="fake-debug-build-official-bootstrap.cmd",
        mutate=fail_first_native_cache_publication,
        timeout_seconds=30,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed < 30
    candidate_proxy = (
        tmp_path / "output" / "isolated" / "work" / "candidate" / "runtime" / "proxy"
    )
    assert (
        candidate_proxy / "official-bootstrap-invocations.txt"
    ).read_text().splitlines() == [
        "refresh-models",
        "refresh-models",
        "start",
    ]


def test_candidate_bootstrap_and_listener_share_one_startup_budget(tmp_path):
    def no_listener(_output, _isolation, debug_build):
        Path(f"{debug_build}.no-listener").write_text("fail", encoding="ascii")

    def slow_bootstrap_no_listener(_output, _isolation, debug_build):
        Path(f"{debug_build}.bootstrap-slow").write_text("slow", encoding="ascii")
        Path(f"{debug_build}.no-listener").write_text("fail", encoding="ascii")

    fast = _run(
        tmp_path / "fast",
        debug_fake="fake-debug-build-official-bootstrap.cmd",
        mutate=no_listener,
        finalize_manual=False,
        timeout_seconds=5,
    )
    slow = _run(
        tmp_path / "slow",
        debug_fake="fake-debug-build-official-bootstrap.cmd",
        mutate=slow_bootstrap_no_listener,
        finalize_manual=False,
        timeout_seconds=5,
    )
    assert fast.returncode != 0
    assert slow.returncode != 0
    fast_startup = json.loads(
        (tmp_path / "fast" / "output" / "candidate-startup.json").read_text()
    )
    slow_startup = json.loads(
        (tmp_path / "slow" / "output" / "candidate-startup.json").read_text()
    )
    assert slow_startup["duration_ms"] - fast_startup["duration_ms"] < 2_000
    assert slow_startup["duration_ms"] <= 5000
    assert slow_startup["failure_classification"] == (
        "candidate_gateway_startup_failed_python"
    )


@pytest.mark.parametrize(
    ("debug_fake", "failure", "python_seen", "listener_seen"),
    [
        (
            "fake-debug-build-no-listener.cmd",
            "candidate_gateway_startup_failed_python",
            False,
            False,
        ),
        (
            "fake-debug-build-bad-health.cmd",
            "candidate_gateway_startup_failed_lifecycle",
            True,
            True,
        ),
    ],
)
def test_candidate_gateway_must_be_ready_before_any_gui_launch(
    tmp_path, debug_fake, failure, python_seen, listener_seen
):
    result = _run(
        tmp_path,
        debug_fake=debug_fake,
        finalize_manual=False,
        timeout_seconds=10,
    )

    assert result.returncode != 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    assert summary["failure_classification"] == failure
    assert summary["counts"]["case_count"] == 0
    assert summary["artifacts"] == ["candidate-startup.json"]
    startup_path = tmp_path / "output" / "candidate-startup.json"
    startup = json.loads(startup_path.read_text())
    assert set(startup) == STARTUP_DIAGNOSTIC_KEYS
    assert startup["failure_classification"] == failure
    assert startup["portable_resources_ready"] is True
    assert startup["python_child_seen"] is python_seen
    assert startup["listener_seen"] is listener_seen
    assert startup["health_ready"] is False
    serialized = startup_path.read_text()
    assert str(tmp_path) not in serialized
    assert "fixture-private" not in serialized
    assert not list((tmp_path / "output" / "isolated" / "work").glob("gui-*.launched"))


def test_manual_timeout_cleanup_is_bounded_and_still_writes_one_summary(tmp_path):
    started = time.monotonic()
    result = _run(
        tmp_path,
        client_fakes={
            "CodexDesktopPath": "fake-gui-expanding-tree.cmd",
            "ZCodePath": "fake-gui-expanding-tree.cmd",
        },
        finalize_manual=False,
        manual_timeout_seconds=1,
    )
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    assert elapsed < 90
    summaries = list(tmp_path.rglob("summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text())
    assert summary["failure_classification"] == "manual_evidence_timeout"


def test_outer_watchdog_reaps_orphans_after_intermediate_parent_exits(tmp_path):
    started = time.monotonic()
    result = _run(
        tmp_path,
        client_fakes={
            "CodexDesktopPath": "fake-gui-expanding-tree.cmd",
            "ZCodePath": "fake-gui-expanding-tree.cmd",
        },
        finalize_manual=False,
        manual_timeout_seconds=120,
        overall_timeout_seconds=75,
    )
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    assert elapsed < 90
    summaries = list(tmp_path.rglob("summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text())
    assert summary["failure_classification"] == "automated_outer_timeout"
    assert summary["artifacts"] == ["runner-timeout.json"]
    timeout_diagnostic = json.loads(
        (tmp_path / "output" / "runner-timeout.json").read_text()
    )
    assert set(timeout_diagnostic) == {
        "schema",
        "outcome",
        "failure_classification",
        "phase",
        "duration_ms",
        "total_process_count",
        "active_process_count",
    }
    assert timeout_diagnostic["phase"] == "manual_evidence"
    assert 1 <= timeout_diagnostic["active_process_count"] <= 1000
    serialized = summaries[0].read_text() + json.dumps(timeout_diagnostic)
    assert str(tmp_path) not in serialized
    assert "fixture-private" not in serialized
    pid_markers = list(tmp_path.rglob("*.orphan.*"))
    assert pid_markers
    orphan_pids = [int(marker.read_text()) for marker in pid_markers]
    assert not [pid for pid in orphan_pids if _pid_is_running(pid)]


def test_external_watchdog_reaps_descendant_after_intermediate_parent_is_missing(tmp_path):
    result, process_ids = _run_watchdog_fixture(tmp_path, "missing-parent")

    assert result.returncode == 124
    assert "watchdog_timeout phase=command" in result.stderr
    assert not [pid for pid in process_ids if _pid_is_running(pid)]


def test_external_watchdog_timeout_is_not_blocked_by_inherited_output_handles(tmp_path):
    result, process_ids = _run_watchdog_fixture(tmp_path, "inherited-pipe")

    assert result.returncode == 124
    assert "watchdog_timeout phase=command" in result.stderr
    assert not [pid for pid in process_ids if _pid_is_running(pid)]


def test_operator_commands_have_explicit_outer_and_manual_deadlines():
    documentation = (ROOT / "docs" / "agents" / "real-client-e2e.md").read_text()

    assert "run-with-windows-watchdog.py --timeout-seconds 3600 --" in documentation
    assert "-OverallTimeoutSeconds 5400" in documentation
    assert "-ManualEvidenceTimeoutSeconds 900" in documentation
    assert "manual window is finite" in documentation


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
