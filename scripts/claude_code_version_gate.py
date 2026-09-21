"""Compare a Claude Code --version string to the campaign pin.

No network, no credentials. Exit 0 on match, 2 on drift.
"""

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import json
import re
import sys

PINNED_VERSION = "2.1.278"
VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


def parse_version(text: str) -> str | None:
    match = VERSION_RE.search(text or "")
    return match.group(1) if match else None


def report(text: str, *, pin: str = PINNED_VERSION) -> dict[str, str | bool]:
    found = parse_version(text)
    return {
        "pin": pin,
        "found": found or "",
        "match": found == pin,
        "drift": bool(found) and found != pin,
        "unreadable": not found,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version-text", required=True, help="Output of `claude --version`")
    args = parser.parse_args(argv)
    payload = report(args.version_text)
    json.dump(payload, sys.stdout, ensure_ascii=True)
    sys.stdout.write("\n")
    if payload["unreadable"]:
        return 2
    return 0 if payload["match"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
