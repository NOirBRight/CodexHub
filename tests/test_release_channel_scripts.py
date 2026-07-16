import hashlib
import json
from pathlib import Path
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_official_transport_wheel_is_pinned_and_packaged():
    wheel = ROOT / "src-python" / "vendor" / "urllib3-2.7.0-py3-none-any.whl"
    tauri = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))

    assert wheel.is_file()
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == (
        "9fb4c81ebbb1ce9531cce37674bbc6f1360472bc18ca9a553ede278ef7276897"
    )
    assert tauri["bundle"]["resources"]["../src-python/vendor/*.whl"] == "src-python/vendor"


def test_release_version_is_consistent_across_manifests():
    expected = "0.1.5"
    tauri = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))
    cargo_lock = tomllib.loads((ROOT / "src-tauri" / "Cargo.lock").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    codexhub_lock = next(item for item in cargo_lock["package"] if item["name"] == "codexhub")

    assert tauri["version"] == expected
    assert cargo["package"]["version"] == expected
    assert codexhub_lock["version"] == expected
    assert package["version"] == expected
    assert package_lock["version"] == expected
    assert package_lock["packages"][""]["version"] == expected


def test_v014_audit_records_reconciliation_and_display_contract():
    audit = (ROOT / "docs" / "reviews" / "v0.1.3-human-audit.md").read_text(encoding="utf-8")

    for evidence in (
        "38e99408",
        "08d507af",
        "not exact cherry-picks",
        "009b58fe",
        "5baf8cc0",
        "No third intended v0.1.4 commit was missing",
        "patch-equivalent",
        "v0.2/TLS/FlClash/keepalive",
        "OpenAI 5.6 Sol",
        "`5.6 Sol`",
        "`gpt-5.6-sol`",
        "`codexhub-openai/gpt-5.6-sol`",
        "AI review is not a substitute for human maintainer approval",
    ):
        assert evidence in audit


def test_flavors_share_application_identity_and_split_only_update_artifacts():
    flavors = json.loads((ROOT / "config" / "build-flavors.json").read_text(encoding="utf-8"))

    assert sorted(flavors) == ["debug", "normal"]
    for key in (
        "productName",
        "executableBaseName",
        "identifier",
        "windowTitle",
        "frontendPort",
        "bridgePort",
        "gatewayPort",
        "routingOwner",
        "defaultCodexHome",
        "codexTargetHome",
        "autostartTaskName",
        "macosLabel",
        "macosPlistFile",
        "linuxServiceFile",
    ):
        assert flavors["normal"][key] == flavors["debug"][key]

    assert flavors["normal"]["updaterManifestName"] == "latest.json"
    assert flavors["debug"]["updaterManifestName"] == "latest-debug.json"
    assert flavors["normal"]["releaseAssetSuffix"] == ""
    assert flavors["debug"]["releaseAssetSuffix"] == "_debug"


def test_portable_uses_one_executable_name_and_flavor_specific_archives():
    script = (ROOT / "scripts" / "build-windows-portable.ps1").read_text(encoding="utf-8-sig")

    assert '[ValidateSet("normal", "debug")]' in script
    assert '[string]$Flavor = "normal"' in script
    assert '$portableExecutable = "{0}.exe" -f ([string]$flavorConfig.executableBaseName)' in script
    assert '$portableName = "{0}_{1}{2}_portable_{3}"' in script
    assert "debug-diagnostics" in script
    assert "CARGO_TARGET_DIR" in script
    assert 'Join-Path $targetRoot "release\\codexhub.exe"' in script


def _portable_fixture(tmp_path: Path, version: str) -> Path:
    repo = tmp_path / "portable-repo"
    (repo / "config").mkdir(parents=True)
    (repo / "src-tauri").mkdir()
    (repo / "config" / "build-flavors.json").write_bytes(
        (ROOT / "config" / "build-flavors.json").read_bytes()
    )
    base = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    base["version"] = version
    (repo / "src-tauri" / "tauri.conf.json").write_text(json.dumps(base), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "portable-tests@example.test")
    _git(repo, "config", "user.name", "Portable Tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    return repo


def _portable(repo: Path, flavor: str | None = None) -> subprocess.CompletedProcess[str]:
    args = [
        "powershell",
        "-NoProfile",
        "-File",
        str(ROOT / "scripts" / "build-windows-portable.ps1"),
    ]
    if flavor is not None:
        args.extend(["-Flavor", flavor])
    args.extend(["-RepoRoot", str(repo), "-DryRun"])
    return subprocess.run(args, text=True, capture_output=True)


def _replacement(repo: Path, version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "Test-BuildFlavorReplacement.ps1"),
            "-RepoRoot",
            str(repo),
            "-Version",
            version,
            "-DryRun",
        ],
        text=True,
        capture_output=True,
    )


def test_portable_dry_runs_default_to_normal_and_keep_source_version_parity(tmp_path):
    repo = _portable_fixture(tmp_path, "0.1.5")

    default_normal = _portable(repo)
    explicit_normal = _portable(repo, "normal")
    debug = _portable(repo, "debug")

    assert default_normal.returncode == 0, default_normal.stderr
    assert explicit_normal.returncode == 0, explicit_normal.stderr
    assert debug.returncode == 0, debug.stderr
    default_plan = json.loads(default_normal.stdout)
    normal_plan = json.loads(explicit_normal.stdout)
    debug_plan = json.loads(debug.stdout)
    assert default_plan["flavor"] == "normal"
    assert normal_plan["version"] == debug_plan["version"] == "0.1.5"
    assert normal_plan["source_revision"] == debug_plan["source_revision"]
    assert normal_plan["executable"] == debug_plan["executable"] == "CodexHub.exe"
    assert normal_plan["portable_name"].startswith("CodexHub_0.1.5_portable_")
    assert debug_plan["portable_name"].startswith("CodexHub_0.1.5_debug_portable_")
    assert normal_plan["installer_name"] == "CodexHub_0.1.5_x64-setup.exe"
    assert debug_plan["installer_name"] == "CodexHub_0.1.5_debug_x64-setup.exe"
    assert normal_plan["updater_manifest"] == "latest.json"
    assert debug_plan["updater_manifest"] == "latest-debug.json"
    assert normal_plan["release_optimized"] is True
    assert debug_plan["release_optimized"] is True
    assert normal_plan["debug_diagnostics_enabled"] is False
    assert debug_plan["debug_diagnostics_enabled"] is True
    assert normal_plan["generated_config"]["productName"] == debug_plan["generated_config"]["productName"]
    assert normal_plan["generated_config"]["identifier"] == debug_plan["generated_config"]["identifier"]
    assert normal_plan["generated_config"]["title"] == debug_plan["generated_config"]["title"]
    assert normal_plan["generated_config"]["bridgePort"] == debug_plan["generated_config"]["bridgePort"] == 1421
    assert normal_plan["generated_config"]["gatewayPort"] == debug_plan["generated_config"]["gatewayPort"] == 9099
    assert normal_plan["generated_config"]["updaterEndpoint"].endswith("/latest.json")
    assert debug_plan["generated_config"]["updaterEndpoint"].endswith("/latest-debug.json")


def test_replacement_smoke_contract_uses_one_identity_and_gateway_owner(tmp_path):
    repo = _portable_fixture(tmp_path, "0.1.5")

    result = _replacement(repo, "0.1.5")

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["version"] == "0.1.5"
    assert plan["sequence"] == ["normal", "debug", "normal"]
    assert plan["normal_installer"] == "CodexHub_0.1.5_x64-setup.exe"
    assert plan["debug_installer"] == "CodexHub_0.1.5_debug_x64-setup.exe"
    assert plan["application_identity"] == {
        "product_name": "CodexHub",
        "identifier": "com.codexhub.app",
        "executable": "CodexHub.exe",
    }
    assert plan["runtime"] == {
        "home": ".codex",
        "routing_owner": "release",
        "gateway_port": 9099,
        "expected_gateway_owner_count": 1,
    }


def test_replacement_smoke_requires_explicit_installer_inputs(tmp_path):
    repo = _portable_fixture(tmp_path, "0.1.5")

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "Test-BuildFlavorReplacement.ps1"),
            "-RepoRoot",
            str(repo),
            "-Version",
            "0.1.5",
            "-RunInstall",
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "requires both -NormalInstaller and -DebugInstaller" in result.stderr

    script = (ROOT / "scripts" / "Test-BuildFlavorReplacement.ps1").read_text(encoding="utf-8")
    assert "if ($owners.Count -ne 1)" in script
    assert "Expected exactly one Gateway owner" in script


def _update_e2e_app_build_plan(flavor: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "e2e-app-update.ps1"),
            "-Flavor",
            flavor,
            "-ShowAppBuildPlan",
        ],
        text=True,
        capture_output=True,
    )


def test_debug_update_e2e_build_plan_produces_its_default_flavor_path():
    result = _update_e2e_app_build_plan("debug")

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    target_root = ROOT / "src-tauri" / "target" / "build-flavors" / "debug"
    assert plan["flavor"] == "debug"
    assert Path(plan["target_root"]).resolve() == target_root.resolve()
    assert Path(plan["app_executable"]).resolve() == (target_root / "debug" / "codexhub.exe").resolve()
    assert Path(plan["prepare_python_runtime"]).resolve() == (ROOT / "scripts" / "Prepare-PythonRuntime.ps1").resolve()
    assert plan["environment"] == {
        "CODEXHUB_BUILD_FLAVOR": "debug",
        "CARGO_TARGET_DIR": str(target_root),
    }
    assert plan["cargo_args"] == ["build", "--locked", "--features", "debug-diagnostics"]
    assert plan["command"] == "cargo build --locked --features debug-diagnostics"

    script = (ROOT / "scripts" / "e2e-app-update.ps1").read_text(encoding="utf-8-sig")
    assert "& $Plan.prepare_python_runtime -RepoRoot $repoRoot" in script
    assert "Python runtime preparation failed with exit code $LASTEXITCODE." in script


def test_portable_rejects_invalid_flavor_before_building(tmp_path):
    repo = _portable_fixture(tmp_path, "0.1.5")

    result = _portable(repo, "invalid")

    assert result.returncode != 0
    assert "Flavor" in result.stderr


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _release_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-tests@example.test")
    _git(repo, "config", "user.name", "Release Tests")
    (repo / "seed.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-qm", "main")
    main_commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "main", main_commit)
    (repo / "seed.txt").write_text("dev\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "dev")
    dev_commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "dev", dev_commit)
    return repo, main_commit, dev_commit


def _plan(repo: Path, flavor: str, version: str, commit: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "New-ReleaseChannelPlan.ps1"),
            "-RepoRoot",
            str(repo),
            "-Flavor",
            flavor,
            "-Version",
            version,
            "-Commit",
            commit,
            "-DryRun",
        ],
        text=True,
        capture_output=True,
    )


def test_release_plan_places_both_flavors_in_one_immutable_release(tmp_path):
    repo, main_commit, _ = _release_repo(tmp_path)

    result = _plan(repo, "debug", "0.1.5", main_commit)

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["flavor"] == "debug"
    assert plan["manifest"] == {
        "name": "latest-debug.json",
        "asset_url": "https://github.com/NOirBRight/CodexHub/releases/download/v0.1.5/CodexHub_0.1.5_debug_x64-setup.exe",
    }
    assert plan["immutable_release"] == {
        "tag": "v0.1.5",
        "prerelease": False,
        "assets": [
            "CodexHub_0.1.5_x64-setup.exe",
            "CodexHub_0.1.5_x64-setup.exe.sig",
            "latest.json",
            "CodexHub_0.1.5_debug_x64-setup.exe",
            "CodexHub_0.1.5_debug_x64-setup.exe.sig",
            "latest-debug.json",
        ],
    }
    assert plan["channel_release"] is None


def test_release_plan_rejects_prerelease_versions_and_non_main_commits(tmp_path):
    repo, main_commit, dev_commit = _release_repo(tmp_path)

    prerelease = _plan(repo, "normal", "0.1.5-beta.1", main_commit)
    wrong_commit = _plan(repo, "debug", "0.1.5", dev_commit)

    assert prerelease.returncode != 0
    assert "prerelease suffix" in prerelease.stderr
    assert wrong_commit.returncode != 0
    assert "exact main commit" in wrong_commit.stderr


def test_release_builder_uses_release_optimized_flavor_features_and_immutable_urls():
    script = (ROOT / "scripts" / "build-windows-release.ps1").read_text(encoding="utf-8-sig")

    assert '[ValidateSet("normal", "debug")]' in script
    assert "Get-ReleaseArtifactName" in script
    assert "Get-FlavorTargetRoot" in script
    assert "debug-diagnostics" in script
    assert "CARGO_TARGET_DIR" in script
    assert '"--bundles", "nsis", "--ci"' in script
    assert "--debug" not in script
    assert 'releases/download/v$version' in script
    assert 'releases/download/beta' not in script
    assert "codexhub_flavor = $Flavor" in script
    assert "codexhub_source_revision = $sourceRevision" in script


def _validate_manifest(
    flavor: str,
    version: str,
    manifest: Path,
    installer: Path,
    signature: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "Test-ReleaseManifest.ps1"),
            "-Flavor",
            flavor,
            "-Version",
            version,
            "-ManifestPath",
            str(manifest),
            "-InstallerPath",
            str(installer),
            "-SignaturePath",
            str(signature),
        ],
        text=True,
        capture_output=True,
    )


def _manifest_payload(flavor: str, version: str, installer: Path) -> dict:
    return {
        "version": version,
        "codexhub_flavor": flavor,
        "platforms": {
            "windows-x86_64": {
                "signature": "signed-value",
                "url": f"https://github.com/NOirBRight/CodexHub/releases/download/v{version}/{installer.name}",
            }
        },
    }


def test_debug_manifest_validator_requires_matching_flavor_and_artifact_pair(tmp_path):
    version = "0.1.5"
    installer = tmp_path / f"CodexHub_{version}_debug_x64-setup.exe"
    signature = Path(f"{installer}.sig")
    manifest = tmp_path / "latest-debug.json"
    installer.write_bytes(b"installer")
    signature.write_text("signed-value", encoding="utf-8")
    payload = _manifest_payload("debug", version, installer)
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    accepted = _validate_manifest("debug", version, manifest, installer, signature)
    payload["codexhub_flavor"] = "normal"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    rejected = _validate_manifest("debug", version, manifest, installer, signature)

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0
    assert "Manifest flavor" in rejected.stderr


def test_normal_manifest_validator_preserves_latest_json_name_and_rejects_debug_artifact(tmp_path):
    version = "0.1.5"
    installer = tmp_path / f"CodexHub_{version}_x64-setup.exe"
    signature = Path(f"{installer}.sig")
    manifest = tmp_path / "latest.json"
    installer.write_bytes(b"installer")
    signature.write_text("signed-value", encoding="utf-8")
    payload = _manifest_payload("normal", version, installer)
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    accepted = _validate_manifest("normal", version, manifest, installer, signature)
    debug_installer = tmp_path / f"CodexHub_{version}_debug_x64-setup.exe"
    debug_installer.write_bytes(b"installer")
    rejected = _validate_manifest("normal", version, manifest, debug_installer, signature)

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0
    assert "normal installer" in rejected.stderr.lower()


def test_release_scripts_share_flavor_validation_helpers():
    plan_script = (ROOT / "scripts" / "New-ReleaseChannelPlan.ps1").read_text(encoding="utf-8-sig")
    manifest_script = (ROOT / "scripts" / "Test-ReleaseManifest.ps1").read_text(encoding="utf-8-sig")
    helpers = (ROOT / "scripts" / "ReleaseChannel.ps1").read_text(encoding="utf-8-sig")

    for script in (plan_script, manifest_script):
        assert '. (Join-Path $PSScriptRoot "ReleaseChannel.ps1")' in script
        assert "Assert-ReleaseFlavorVersion" in script
    assert "Get-ReleaseArtifactName" in helpers
    assert "Get-ReleaseManifestName" in helpers
    assert "Get-FlavorTargetRoot" in helpers
