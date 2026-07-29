import json
from pathlib import Path
import sys


def main(root: Path, client: str) -> None:
    opencode = json.loads(
        (root / ".config" / "opencode" / "opencode.json").read_text(encoding="utf-8-sig")
    )["provider"]
    assert opencode["codexhub-openai"]["npm"] == "@ai-sdk/openai"
    assert opencode["codexhub-volc"]["npm"] == "@ai-sdk/openai-compatible"
    assert opencode["codexhub-openai"]["options"]["baseURL"].endswith("/providers/openai")
    assert opencode["codexhub-volc"]["options"]["baseURL"].endswith("/providers/volc")

    pi = json.loads(
        (root / ".pi" / "agent" / "models.json").read_text(encoding="utf-8-sig")
    )["providers"]
    assert pi["codexhub-openai"]["api"] == "openai-responses"
    assert pi["codexhub-volc"]["api"] == "openai-completions"

    omp = (root / ".omp" / "agent" / "models.yml").read_text(encoding="utf-8-sig")
    openai_block, volc_block = omp.split("  codexhub-volc:", 1)
    assert "api: openai-responses" in openai_block
    assert "api: openai-completions" in volc_block
    assert "responses" not in volc_block


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]), sys.argv[2])
    except Exception:
        raise SystemExit(1)
