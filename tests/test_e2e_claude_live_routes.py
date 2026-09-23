from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest

from scripts.e2e_claude_live_routes import deepseek_key, snapshot_claude_subscription


def test_deepseek_provider_source_extracts_only_official_key_in_memory(tmp_path: Path) -> None:
    provider_source = tmp_path / "providers.toml"
    provider_source.write_text(
        '[[providers]]\n'
        'id = "deepseek"\n'
        'base_url = "https://api.deepseek.com"\n'
        'api_key = "synthetic-deepseek-test-token"\n',
        encoding="utf-8",
    )

    assert deepseek_key(None, provider_source) == "synthetic-deepseek-test-token"
    assert provider_source.read_text(encoding="utf-8").endswith(
        'api_key = "synthetic-deepseek-test-token"\n'
    )


def test_deepseek_provider_source_rejects_nonofficial_origin_without_secret_detail(tmp_path: Path) -> None:
    provider_source = tmp_path / "providers.toml"
    provider_source.write_text(
        '[[providers]]\n'
        'id = "deepseek"\n'
        'base_url = "https://attacker.example"\n'
        'api_key = "synthetic-deepseek-test-token"\n',
        encoding="utf-8",
    )

    with pytest.raises(AssertionError) as error:
        deepseek_key(None, provider_source)
    assert "synthetic-deepseek-test-token" not in str(error.value)


def test_claude_subscription_snapshot_omits_refresh_and_private_metadata(tmp_path: Path) -> None:
    source = tmp_path / "host-credentials.json"
    destination = tmp_path / "isolated" / ".claude" / ".credentials.json"
    source.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "synthetic-access-token",
                    "refreshToken": "synthetic-refresh-token",
                    "expiresAt": int((time.time() + 1800) * 1000),
                    "scopes": ["user:inference"],
                    "subscriptionType": "synthetic",
                    "rateLimitTier": "synthetic",
                    "userId": "private-account-id",
                }
            }
        ),
        encoding="utf-8",
    )

    remaining = snapshot_claude_subscription(
        source,
        destination,
        minimum_remaining_seconds=900,
    )
    copied = json.loads(destination.read_text(encoding="utf-8"))
    oauth = copied["claudeAiOauth"]

    assert remaining > 900
    assert oauth == {
        "accessToken": "synthetic-access-token",
        "expiresAt": json.loads(source.read_text(encoding="utf-8"))["claudeAiOauth"]["expiresAt"],
        "scopes": ["user:inference"],
        "subscriptionType": "synthetic",
        "rateLimitTier": "synthetic",
    }
    assert "refreshToken" not in destination.read_text(encoding="utf-8")
    assert "private-account-id" not in destination.read_text(encoding="utf-8")
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert source.is_file()


def test_claude_subscription_snapshot_fails_before_copy_when_expiring(tmp_path: Path) -> None:
    source = tmp_path / "credentials.json"
    destination = tmp_path / "isolated" / ".credentials.json"
    source.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "synthetic-access-token",
                    "refreshToken": "synthetic-refresh-token",
                    "expiresAt": int((time.time() + 60) * 1000),
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="expires before"):
        snapshot_claude_subscription(source, destination, minimum_remaining_seconds=900)
    assert not destination.exists()
