import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
