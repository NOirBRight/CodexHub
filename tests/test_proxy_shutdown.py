from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from codex_proxy import CodexProxyHandler


def test_shutdown_endpoint_stops_server() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), CodexProxyHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=2)
        connection.request("POST", "/shutdown")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()

        assert response.status == 200
        assert payload["ok"] is True

        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)
