"""Print sanitized current-account model discovery; never enable a Provider."""

from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import hashlib
import json
from pathlib import Path

from cli_subscription_discovery import discover_subscription


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("claude-subscription", "cursor-subscription"))
    parser.add_argument("--cli", type=Path, help="Installed official CLI executable")
    parser.add_argument("--source-home", type=Path, help="Official CLI user's home directory")
    parser.add_argument("--summary", action="store_true", help="Print counts/hash instead of model rows")
    args = parser.parse_args()
    result = discover_subscription(args.provider, binary=args.cli, source_home=args.source_home)
    payload = result.public_status()
    if args.summary:
        rows = payload.pop("models")
        payload["model_count"] = len(rows)
        payload["model_ids_sha256"] = hashlib.sha256(
            json.dumps(sorted(row["id"] for row in rows), ensure_ascii=False).encode()
        ).hexdigest()
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if result.state == "available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
