import json
from pathlib import Path
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_beta_candidate_version_is_consistent_across_manifests():
    expected = "0.1.4-beta.7"
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


def test_portable_uses_flavor_executable_base_name():
    flavors = json.loads((ROOT / "config" / "build-flavors.json").read_text(encoding="utf-8"))
    script = (ROOT / "scripts" / "build-windows-portable.ps1").read_text(encoding="utf-8-sig")

    assert flavors["stable"]["executableBaseName"] == "CodexHub"
    assert flavors["beta"]["executableBaseName"] == "CodexHubBeta"
    assert '$portableExecutable = "{0}.exe" -f ([string]$flavorConfig.executableBaseName)' in script
    assert 'Join-Path $portableDir $portableExecutable' in script
    assert 'Join-Path $tauriDir "target\\release\\codexhub.exe"' in script


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


def _portable(repo: Path, flavor: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "build-windows-portable.ps1"),
            "-Flavor",
            flavor,
            "-RepoRoot",
            str(repo),
            "-DryRun",
        ],
        text=True,
        capture_output=True,
    )


def test_portable_dry_run_validates_generated_beta_config_and_output_name(tmp_path):
    repo = _portable_fixture(tmp_path, "0.1.5-beta.1")

    result = _portable(repo, "beta")

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["flavor"] == "beta"
    assert plan["version"] == "0.1.5-beta.1"
    assert plan["executable"] == "CodexHubBeta.exe"
    assert plan["portable_name"].startswith("CodexHubBeta_0.1.5-beta.1_portable_")
    assert plan["generated_config"] == {
        "productName": "CodexHub Beta",
        "identifier": "com.codexhub.beta",
        "title": "CodexHub Beta",
        "updaterEndpoint": "https://github.com/NOirBRight/CodexHub/releases/download/beta/latest-beta.json",
    }


def test_portable_dry_run_rejects_beta_version_for_stable(tmp_path):
    repo = _portable_fixture(tmp_path, "0.1.5-beta.1")

    result = _portable(repo, "stable")

    assert result.returncode != 0
    assert "Stable" in result.stderr
    assert "prerelease" in result.stderr


def test_portable_dry_run_validates_stable_executable_name(tmp_path):
    repo = _portable_fixture(tmp_path, "0.1.5")

    result = _portable(repo, "stable")

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["executable"] == "CodexHub.exe"
    assert plan["portable_name"].startswith("CodexHub_0.1.5_portable_")
    assert plan["generated_config"]["productName"] == "CodexHub"


def test_portable_dry_run_rejects_stable_version_for_beta(tmp_path):
    repo = _portable_fixture(tmp_path, "0.1.5")

    result = _portable(repo, "beta")

    assert result.returncode != 0
    assert "Beta" in result.stderr
    assert "prerelease" in result.stderr


def test_portable_requires_explicit_flavor(tmp_path):
    repo = _portable_fixture(tmp_path, "0.1.5")

    result = subprocess.run(
        [
            "powershell",
            "-NonInteractive",
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "build-windows-portable.ps1"),
            "-RepoRoot",
            str(repo),
            "-DryRun",
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "Flavor" in result.stderr


def test_beta_portable_asset_prefix_is_distinct():
    flavors = json.loads((ROOT / "config" / "build-flavors.json").read_text(encoding="utf-8"))

    assert flavors["stable"]["releaseAssetPrefix"] == "CodexHub"
    assert flavors["beta"]["releaseAssetPrefix"] == "CodexHubBeta"


def test_flavor_manifest_separates_beta_runtime_from_codex_target():
    flavors = json.loads((ROOT / "config" / "build-flavors.json").read_text(encoding="utf-8"))
    assert flavors["stable"]["defaultCodexHome"] == ".codex"
    assert flavors["stable"]["codexTargetHome"] == ".codex"
    assert flavors["beta"]["defaultCodexHome"] == ".codexhub-beta"
    assert flavors["beta"]["codexTargetHome"] == ".codex"


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


def test_beta_dry_run_plans_immutable_assets_and_pointer_manifest(tmp_path):
    repo, _, dev_commit = _release_repo(tmp_path)

    result = _plan(repo, "beta", "0.1.4-beta.1", dev_commit)

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["immutable_release"]["tag"] == "v0.1.4-beta.1"
    assert plan["immutable_release"]["prerelease"] is True
    assert plan["immutable_release"]["assets"] == [
        "CodexHubBeta_0.1.4-beta.1_x64-setup.exe",
        "CodexHubBeta_0.1.4-beta.1_x64-setup.exe.sig",
    ]
    assert plan["channel_release"] == {
        "tag": "beta",
        "prerelease": True,
        "assets": ["latest-beta.json"],
    }
    assert "latest.json" not in json.dumps(plan)


def test_beta_gate_rejects_stable_version(tmp_path):
    repo, _, dev_commit = _release_repo(tmp_path)
    result = _plan(repo, "beta", "0.1.4", dev_commit)
    assert result.returncode != 0
    assert "prerelease version" in result.stderr


def test_beta_gate_accepts_next_version_prerelease_via_shared_semver_validator(tmp_path):
    repo, _, dev_commit = _release_repo(tmp_path)

    result = _plan(repo, "beta", "0.1.5-beta.1", dev_commit)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["version"] == "0.1.5-beta.1"


def test_semver_accepts_alphanumeric_prerelease_starting_with_zero(tmp_path):
    repo, _, dev_commit = _release_repo(tmp_path)

    result = _plan(repo, "beta", "1.2.3-0alpha", dev_commit)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["version"] == "1.2.3-0alpha"


def test_semver_rejects_numeric_prerelease_with_leading_zero(tmp_path):
    repo, _, dev_commit = _release_repo(tmp_path)

    result = _plan(repo, "beta", "1.2.3-01", dev_commit)

    assert result.returncode != 0
    assert "valid SemVer" in result.stderr


def test_semver_rejects_trailing_line_feed(tmp_path):
    repo, _, dev_commit = _release_repo(tmp_path)

    result = _plan(repo, "beta", "1.2.3-beta.1\n", dev_commit)

    assert result.returncode != 0
    assert "valid SemVer" in result.stderr


def test_stable_gate_requires_exact_main_and_stable_version(tmp_path):
    repo, main_commit, dev_commit = _release_repo(tmp_path)

    accepted = _plan(repo, "stable", "0.1.4", main_commit)
    rejected = _plan(repo, "stable", "0.1.4", dev_commit)

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0
    assert "exact main commit" in rejected.stderr


def test_release_builder_points_beta_manifest_to_immutable_tag():
    script = (ROOT / "scripts" / "build-windows-release.ps1").read_text(encoding="utf-8-sig")
    assert 'releases/download/v$version' in script
    assert 'releases/download/beta"' not in script


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


def test_beta_manifest_validator_requires_immutable_asset_url_and_pair(tmp_path):
    version = "0.1.4-beta.4"
    installer = tmp_path / f"CodexHubBeta_{version}_x64-setup.exe"
    signature = Path(f"{installer}.sig")
    manifest = tmp_path / "latest-beta.json"
    installer.write_bytes(b"installer")
    signature.write_text("signed-value", encoding="utf-8")
    manifest.write_text(
        json.dumps(
            {
                "version": version,
                "platforms": {
                    "windows-x86_64": {
                        "signature": "signed-value",
                        "url": f"https://github.com/NOirBRight/CodexHub/releases/download/v{version}/{installer.name}",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    accepted = _validate_manifest("beta", version, manifest, installer, signature)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["platforms"]["windows-x86_64"]["url"] = (
        f"https://github.com/NOirBRight/CodexHub/releases/download/beta/{installer.name}"
    )
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    rejected = _validate_manifest("beta", version, manifest, installer, signature)

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0
    assert "immutable version tag" in rejected.stderr


def test_release_scripts_share_generic_semver_channel_validation():
    plan_script = (ROOT / "scripts" / "New-ReleaseChannelPlan.ps1").read_text(encoding="utf-8-sig")
    manifest_script = (ROOT / "scripts" / "Test-ReleaseManifest.ps1").read_text(encoding="utf-8-sig")

    for script in (plan_script, manifest_script):
        assert '. (Join-Path $PSScriptRoot "ReleaseChannel.ps1")' in script
        assert "Assert-ReleaseChannelVersion" in script
        assert "0\\.1\\.4" not in script


def test_stable_manifest_gate_rejects_prerelease_version(tmp_path):
    version = "0.1.5-beta.1"
    installer = tmp_path / f"CodexHub_{version}_x64-setup.exe"
    signature = Path(f"{installer}.sig")
    manifest = tmp_path / "latest.json"
    installer.write_bytes(b"installer")
    signature.write_text("signed-value", encoding="utf-8")
    manifest.write_text(
        json.dumps(
            {
                "version": version,
                "platforms": {
                    "windows-x86_64": {
                        "signature": "signed-value",
                        "url": f"https://github.com/NOirBRight/CodexHub/releases/download/v{version}/{installer.name}",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = _validate_manifest("stable", version, manifest, installer, signature)

    assert result.returncode != 0
    assert "Stable" in result.stderr
    assert "prerelease" in result.stderr
