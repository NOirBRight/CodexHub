import json
from pathlib import Path
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_beta_candidate_version_is_consistent_across_manifests():
    expected = "0.1.4-beta.1"
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
    version = "0.1.4-beta.2"
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
