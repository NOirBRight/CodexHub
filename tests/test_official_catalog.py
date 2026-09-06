"""Direct subscription discovery must preserve the complete server document."""
import io
import json
from unittest.mock import patch

import pytest
import official_catalog
import codex_auth


class Response(io.BytesIO):
    headers = {"ETag": '"revision-1"'}


def test_catalog_preserves_full_metadata_and_uses_subscription_auth():
    row = {"slug": "example", "visibility": "list", "context_window": 272000,
           "future_metadata": {"nested": True}, "display_name": "Example", "shell_type": "shell_command",
           "supported_in_api": True, "priority": 1, "supported_reasoning_levels": [],
           "support_verbosity": False, "truncation_policy": {"mode": "bytes", "limit": 10000},
           "experimental_supported_tools": []}
    def open_request(request, timeout):
        assert request.full_url.endswith("models?client_version=0.153.4")
        assert request.get_header("Authorization") == "Bearer test-secret"
        assert request.get_header("Chatgpt-account-id") == "account"
        assert timeout == 30
        return Response(json.dumps({"models": [row]}).encode())
    with patch.object(codex_auth, "access_token", return_value="test-secret"), \
         patch.object(codex_auth, "account_id", return_value="account"):
        result = official_catalog.fetch_catalog("0.153.4", 30, opener=open_request)
    assert result["models"] == [row]
    assert result["etag"] == '"revision-1"'
    assert result["client_version"] == "0.153.4"
    assert result["fetched_at"].endswith("Z")


@pytest.mark.parametrize("body", [b'{}', b'{"models":null}', b'invalid'])
def test_rejects_invalid_document(body):
    with patch.object(codex_auth, "access_token", return_value="secret"), \
         patch.object(codex_auth, "account_id", return_value=None):
        with pytest.raises(official_catalog.CatalogError):
            official_catalog.fetch_catalog("0.153.4", 30, opener=lambda *a, **k: Response(body))


def test_redirects_are_not_followed_with_credentials():
    from urllib.request import Request
    from urllib.error import HTTPError
    with pytest.raises(HTTPError):
        official_catalog.NoRedirect().redirect_request(
            Request("https://chatgpt.com", headers={"Authorization": "Bearer secret"}),
            None, 302, "redirect", {}, "https://elsewhere.example")


def test_response_slower_than_cli_five_second_limit_is_accepted():
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.request import build_opener
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(5.1)
            self.send_response(200)
            self.send_header("ETag", "slow-catalog")
            self.end_headers()
            self.wfile.write(b'{"models":[]}')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.object(official_catalog, "ENDPOINT", f"http://127.0.0.1:{server.server_port}/models"), \
             patch.object(codex_auth, "access_token", return_value="fake"), \
             patch.object(codex_auth, "account_id", return_value=None):
            result = official_catalog.fetch_catalog("0.153.4", 30, opener=build_opener().open)
        assert result["models"] == []
        assert result["etag"] == "slow-catalog"
    finally:
        server.shutdown()
        server.server_close()


def test_remote_errors_do_not_include_response_body():
    from urllib.error import HTTPError
    def fail(*args, **kwargs):
        raise HTTPError("https://chatgpt.com", 401, "secret", {}, io.BytesIO(b"private response"))
    with patch.object(codex_auth, "access_token", return_value="secret"), \
         patch.object(codex_auth, "account_id", return_value=None):
        with pytest.raises(official_catalog.CatalogError, match=r"^Official catalog request failed \(HTTP 401\)$"):
            official_catalog.fetch_catalog("0.153.4", 30, opener=fail)


def test_identity_only_catalog_is_rejected():
    with pytest.raises(official_catalog.CatalogError, match="incomplete"):
        official_catalog.validate_models([{"slug": "gpt-new", "visibility": "list"}])
