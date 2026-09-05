"""Explicit inputs for optional live-provider checks; never discover user state."""
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest


@dataclass(frozen=True)
class LiveGateway:
    base_url: str
    client_key: str = field(repr=False)


def configured_live_gateway() -> LiveGateway:
    config_path = os.environ.get('CODEXHUB_LIVE_GATEWAY_CONFIG')
    if not config_path:
        pytest.skip('live Gateway not configured: set CODEXHUB_LIVE_GATEWAY_CONFIG')
    try:
        data = json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
        url = data['base_url']
        key = data['gateway_client_key']
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or not ipaddress.ip_address(parsed.hostname).is_loopback
                or parsed.port is None or parsed.username is not None or parsed.password is not None
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or not isinstance(key, str) or not key.strip()):
            raise ValueError('invalid live Gateway configuration')
        return LiveGateway(url.rstrip('/'), key)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pytest.fail('invalid CODEXHUB_LIVE_GATEWAY_CONFIG: expected loopback base_url and gateway_client_key', pytrace=False)
