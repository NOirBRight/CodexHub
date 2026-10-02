"""Packaged, read-only official CLI status/model discovery command."""
from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import json

from cli_subscription_discovery import discover_subscription


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("cursor-subscription", "claude-subscription"))
    args = parser.parse_args()
    print(json.dumps(discover_subscription(args.provider).public_status(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
