import hashlib
import subprocess

import pytest

from scripts.e2e_gate_inputs import deepseek_provider_text, qualify_candidate_binary


def test_candidate_binding_rejects_wrong_sha_sidecar_and_dirty_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(source), *args], check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("init")
    tracked = source / "source.txt"
    tracked.write_text("reviewed")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    sha = git("rev-parse", "HEAD")
    binary = tmp_path / "candidate"
    binary.write_bytes(b"built candidate")
    with pytest.raises(ValueError, match="sidecar"):
        qualify_candidate_binary(binary, source, sha)
    sidecar = tmp_path / "candidate.candidate-sha"
    sidecar.write_text("0" * 40)
    with pytest.raises(ValueError, match="sidecar"):
        qualify_candidate_binary(binary, source, sha)
    sidecar.write_text(sha)
    assert qualify_candidate_binary(binary, source, sha) == {
        "candidate_sha": sha, "candidate_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    with pytest.raises(ValueError, match="match"):
        qualify_candidate_binary(binary, source, "0" * 40)
    tracked.write_text("unreviewed change")
    with pytest.raises(ValueError, match="clean"):
        qualify_candidate_binary(binary, source, sha)


@pytest.mark.parametrize("endpoint", [
    "https://api.deepseek.com", "https://api.deepseek.com/v1/",
    "http://api.deepseek.com", "https://api.deepseek.com.evil.invalid",
    "https://user:pass@api.deepseek.com", "https://api.deepseek.com/redirect",
])
def test_deepseek_route_rejects_non_official_endpoints(tmp_path, endpoint):
    path = tmp_path / "providers.toml"
    text = '\n'.join([
        '[[providers]]', 'id = "deepseek"', f'base_url = "{endpoint}"',
        'api_key = "{env:DEEPSEEK_API_KEY}"', 'upstream_format = "responses"',
    ])
    path.write_text(text)
    if endpoint in {"https://api.deepseek.com", "https://api.deepseek.com/v1/"}:
        assert deepseek_provider_text(path) == text
    else:
        with pytest.raises(ValueError, match="official"):
            deepseek_provider_text(path)
