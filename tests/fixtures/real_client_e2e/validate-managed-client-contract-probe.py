import json
from pathlib import Path
import sys


EXPECTED = {
    (client, model)
    for client in ("codex", "opencode", "zcode", "pi", "omp")
    for model in (
        ("gpt-5.6-luna", "volc/glm-5.2")
        if client == "codex"
        else ("openai/gpt-5.6-luna", "volc/glm-5.2")
    )
}


def main(path: Path) -> None:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    observed = {(row["client"], row["model"]) for row in rows}
    assert EXPECTED <= observed
    for pair in EXPECTED:
        assert {
            row["verb"]
            for row in rows
            if (row["client"], row["model"]) == pair
        } == {"preview", "apply", "readback"}
    for row in rows:
        if row["client"] in ("opencode", "zcode", "pi", "omp") and row["model"] == "openai/gpt-5.6-luna":
            assert "--catalog-path" in row["flags"], row


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]))
    except Exception:
        raise SystemExit(1)
