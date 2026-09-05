import json

import pytest

from live_gateway_support import configured_live_gateway


def test_unconfigured_live_checks_do_not_discover_user_gateway(monkeypatch):
    monkeypatch.delenv('CODEXHUB_LIVE_GATEWAY_CONFIG', raising=False)
    with pytest.raises(pytest.skip.Exception):
        configured_live_gateway()


def test_explicit_live_gateway_uses_supplied_endpoint_and_key(tmp_path, monkeypatch):
    config = tmp_path / 'gateway.json'
    config.write_text(json.dumps({'base_url': 'http://127.0.0.1:19099/', 'gateway_client_key': 'isolated-test-key'}))
    monkeypatch.setenv('CODEXHUB_LIVE_GATEWAY_CONFIG', str(config))
    gateway = configured_live_gateway()
    assert gateway.base_url == 'http://127.0.0.1:19099'
    assert gateway.client_key == 'isolated-test-key'
    assert gateway.client_key not in repr(gateway)


@pytest.mark.parametrize('url', ['http://example.com:9099', 'http://127.0.0.1', 'http://127.0.0.1:9099/path', 'http://user:secret@127.0.0.1:9099', 'http://127.0.0.1:9099?secret'])
def test_invalid_explicit_live_input_fails_instead_of_skipping(tmp_path, monkeypatch, url):
    config = tmp_path / 'gateway.json'
    config.write_text(json.dumps({'base_url': url, 'gateway_client_key': 'isolated-test-key'}))
    monkeypatch.setenv('CODEXHUB_LIVE_GATEWAY_CONFIG', str(config))
    with pytest.raises(pytest.fail.Exception):
        configured_live_gateway()


@pytest.mark.parametrize('status', [200, 401])
def test_live_checks_use_explicit_http_target_and_do_not_hide_auth_failure(tmp_path, monkeypatch, status):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import test_live_opencode_commandcode_e2e as provider_checks
    import test_third_party_reasoning_request as xai_checks

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers.get('Authorization')))
            self.rfile.read(int(self.headers['Content-Length']))
            body = json.dumps({'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'TEXT_OK'}]}]}).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = tmp_path / 'gateway.json'
        config.write_text(json.dumps({'base_url': f'http://127.0.0.1:{server.server_port}', 'gateway_client_key': 'isolated-test-key'}))
        monkeypatch.setenv('CODEXHUB_LIVE_GATEWAY_CONFIG', str(config))
        monkeypatch.delenv('CODEXHUB_SKIP_LIVE_XAI_E2E', raising=False)
        try:
            for check in (lambda: provider_checks.test_text_generation(provider_checks.MUSE), xai_checks.test_live_gateway_accepts_sanitized_xai_codex_app_history):
                if status == 200:
                    check()
                else:
                    with pytest.raises(AssertionError):
                        check()
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert received == [('/v1/responses', 'Bearer isolated-test-key')] * 2
