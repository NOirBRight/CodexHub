#!/usr/bin/env python3
"""CLI wrapper for the xAI SuperGrok device-code adapter."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src-python"))

from python_runtime_contract import require_python_313

require_python_313(__file__)

from subscription_credential import SubscriptionAuthError
import xai_auth


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="xAI SuperGrok device-code login")
    parser.add_argument(
        "command",
        choices=("status", "start-device", "poll-device", "logout", "access-token", "usage"),
    )
    parser.add_argument("--device-json", default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps({"signed_in": xai_auth.has_session()}))
        elif args.command == "start-device":
            print(json.dumps(xai_auth.start_device_login()))
        elif args.command == "poll-device":
            if not args.device_json:
                raise SubscriptionAuthError(
                    "poll-device requires --device-json",
                    classification="auth-required",
                )
            tokens = xai_auth.poll_device_login(json.loads(args.device_json))
            print(json.dumps({"ok": True, "token_type": tokens.get("token_type")}))
        elif args.command == "access-token":
            print(json.dumps({"access_token": xai_auth.access_token()}))
        elif args.command == "logout":
            xai_auth.logout()
            print(json.dumps({"ok": True}))
        elif args.command == "usage":
            print(json.dumps(xai_auth.fetch_usage()))
    except SubscriptionAuthError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "classification": exc.classification}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
