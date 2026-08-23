"""HTTP client for the in-process Gateway characterization harness."""

from __future__ import annotations

import http.client
from dataclasses import dataclass


@dataclass(frozen=True)
class GatewayHttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def request_gateway(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> GatewayHttpResponse:
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return GatewayHttpResponse(
            status=response.status,
            headers={key.lower(): value for key, value in response.getheaders()},
            body=response.read(),
        )
    finally:
        connection.close()
