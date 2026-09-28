"""Validate candidate provenance and the dedicated official DeepSeek route."""

# ruff: noqa: E402

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import hashlib
from pathlib import Path
import re
import subprocess
import tomllib
from urllib.parse import urlsplit


def deepseek_provider_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    providers = tomllib.loads(text).get("providers", [])
    selected = [p for p in providers if isinstance(p, dict) and p.get("id") == "deepseek"]
    if len(selected) != 1:
        raise ValueError("gate inputs require exactly one DeepSeek provider")
    provider = selected[0]
    endpoint = urlsplit(str(provider.get("base_url", "")))
    if (
        endpoint.scheme != "https" or endpoint.hostname != "api.deepseek.com"
        or endpoint.port not in {None, 443} or endpoint.username or endpoint.password
        or endpoint.path.rstrip("/") not in {"", "/v1"} or endpoint.query or endpoint.fragment
        or provider.get("api_key") != "{env:DEEPSEEK_API_KEY}"
        or provider.get("upstream_format") != "responses"
    ):
        raise ValueError("DeepSeek gate requires its official HTTPS Responses route and dedicated key binding")
    return text


def qualify_candidate_binary(
    binary: Path, source_root: Path, candidate_sha: str, *, self_built: bool = False,
) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
        raise ValueError("candidate SHA must be a full lowercase commit ID")
    head = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(source_root), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout.strip()
    if head != candidate_sha or dirty:
        raise ValueError("candidate source must be clean and match the requested SHA")
    if not self_built:
        sidecar = Path(str(binary) + ".candidate-sha")
        if not sidecar.is_file() or sidecar.read_text(encoding="utf-8").strip() != candidate_sha:
            raise ValueError("candidate binary requires a matching .candidate-sha build sidecar")
    digest = hashlib.sha256()
    with binary.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return {"candidate_sha": candidate_sha, "candidate_sha256": digest.hexdigest()}
