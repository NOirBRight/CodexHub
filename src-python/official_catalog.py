"""Read the complete Codex subscription catalog; publication belongs to Rust.

The parent process owns the total deadline and cancellation, including OAuth.
No credentials or remote error bodies cross the helper's stdout boundary.
"""
from __future__ import annotations

from python_runtime_contract import require_python_313
require_python_313(__file__)

import argparse
from datetime import datetime, timezone
import json
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import codex_auth

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
ENDPOINT = "https://chatgpt.com/backend-api/codex/models"


class CatalogError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Catalog redirects are not supported", headers, fp)


def fetch_catalog(client_version: str, timeout: float, *, opener=None) -> dict:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?", client_version):
        raise CatalogError("Cannot determine the installed Codex CLI version")
    token = codex_auth.access_token()
    account = codex_auth.account_id()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if account:
        headers["ChatGPT-Account-Id"] = account
    request = Request(ENDPOINT + "?" + urlencode({"client_version": client_version}), headers=headers)
    transport = opener or build_opener(NoRedirect()).open
    try:
        with transport(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            etag = response.headers.get("ETag")
    except HTTPError as exc:
        raise CatalogError(f"Official catalog request failed (HTTP {exc.code})") from None
    except (URLError, TimeoutError, OSError):
        raise CatalogError("Official catalog request failed or timed out") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CatalogError("Official catalog response exceeds the size limit")
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise CatalogError("Official catalog returned invalid JSON") from None
    if not isinstance(result, dict) or not isinstance(result.get("models"), list):
        raise CatalogError("Official catalog response has no model array")
    result.update(client_version=client_version, etag=etag,
                  fetched_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-version", required=True)
    parser.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(fetch_catalog(args.client_version, args.timeout), ensure_ascii=False))
        return 0
    except codex_auth.CodexAuthError:
        print(json.dumps({"error": "Codex subscription login is unavailable; sign in again with Codex"}))
    except CatalogError as exc:
        print(json.dumps({"error": str(exc)}))
    except Exception:
        print(json.dumps({"error": "Official catalog refresh failed"}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
