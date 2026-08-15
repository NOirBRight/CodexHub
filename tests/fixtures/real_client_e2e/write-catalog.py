from __future__ import annotations

import json
from pathlib import Path
import sys

from fixture_runtime_contract import require_python_313

require_python_313(__file__)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(1)
    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"candidate-managed": true}', encoding="utf-8")


if __name__ == "__main__":
    main()
