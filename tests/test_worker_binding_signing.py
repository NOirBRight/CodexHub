import os

import worker_binding_signing


def test_worker_binding_signing_secret_is_reused_across_restart(tmp_path):
    payload = b"call-id\0canonical-binding"

    first = worker_binding_signing.sign(tmp_path, payload)
    secret_path = worker_binding_signing.signing_secret_path(tmp_path)
    persisted = secret_path.read_text(encoding="ascii")
    second = worker_binding_signing.sign(tmp_path, payload)

    assert first == second
    assert secret_path.read_text(encoding="ascii") == persisted
    assert worker_binding_signing.verify(tmp_path, payload, first) is True
    if os.name != "nt":
        assert secret_path.stat().st_mode & 0o777 == 0o600


def test_worker_binding_signing_secret_rotation_invalidates_existing_history(tmp_path):
    payload = b"call-id\0canonical-binding"
    original = worker_binding_signing.sign(tmp_path, payload)

    worker_binding_signing.signing_secret_path(tmp_path).unlink()
    rotated = worker_binding_signing.sign(tmp_path, payload)

    assert rotated != original
    assert worker_binding_signing.verify(tmp_path, payload, original) is False
    assert worker_binding_signing.verify(tmp_path, payload, rotated) is True
