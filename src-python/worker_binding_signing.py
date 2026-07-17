"""Dedicated signing-key lifecycle for Worker requested-binding history.

The Gateway owns this key independently from telemetry. It is atomically
created once and reused across restarts. Deleting or rotating the file is an
intentional fail-closed operation: signatures carried by existing histories
will no longer verify and those histories must be restarted.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import secrets

from atomic_io import atomic_read_or_create_text


SIGNING_SECRET_FILENAME = "worker-binding-signing-secret-v1"
PRIVATE_SIGNING_SECRET_MODE = 0o600
_SIGNATURE_DOMAIN = b"codexhub-worker-requested-binding-v1"


def signing_secret_path(root: Path) -> Path:
    return root / SIGNING_SECRET_FILENAME


def _load_or_create_worker_binding_secret(root: Path) -> bytes:
    encoded = atomic_read_or_create_text(
        signing_secret_path(root),
        lambda: secrets.token_hex(32),
        encoding="ascii",
        mode=PRIVATE_SIGNING_SECRET_MODE,
    ).strip()
    try:
        secret = bytes.fromhex(encoded)
    except ValueError as exc:
        raise RuntimeError("invalid Worker binding signing secret") from exc
    if len(secret) != 32:
        raise RuntimeError("invalid Worker binding signing secret")
    return secret


def sign(root: Path, payload: bytes) -> str:
    secret = _load_or_create_worker_binding_secret(root)
    return hmac.new(secret, _SIGNATURE_DOMAIN + b"\0" + payload, hashlib.sha256).hexdigest()


def verify(root: Path, payload: bytes, signature: str) -> bool:
    if not isinstance(signature, str):
        return False
    return hmac.compare_digest(sign(root, payload), signature)
