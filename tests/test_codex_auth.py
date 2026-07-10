import base64
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_auth
from codex_auth import CodexAuthError


def _make_jwt(payload: dict) -> str:
    """Build a dummy JWT (header.payload.signature) with a real payload."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    sig = b"sig"
    return f"{header.decode()}.{body.decode()}.{sig.decode()}"


def _make_auth_json(
    tmpdir: Path,
    *,
    access_token: str,
    refresh_token: str = "rt.test",
    auth_mode: str = "chatgpt",
) -> Path:
    path = tmpdir / "auth.json"
    path.write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "auth_mode": auth_mode,
                "last_refresh": "2026-06-27T16:05:34Z",
                "tokens": {
                    "access_token": access_token,
                    "account_id": "d3544438-0ced-45ba-ab07-e1ff393a0ad2",
                    "id_token": "id",
                    "refresh_token": refresh_token,
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


class _FakeResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class DecodeJwtPayloadTests(unittest.TestCase):
    def test_decode_jwt_payload_extracts_exp(self):
        token = _make_jwt({"exp": 1783440333, "client_id": "app_x", "scp": ["openid"]})
        payload = codex_auth.decode_jwt_payload(token)
        self.assertEqual(payload["exp"], 1783440333)
        self.assertEqual(payload["client_id"], "app_x")
        self.assertEqual(payload["scp"], ["openid"])

    def test_decode_jwt_payload_rejects_malformed(self):
        with self.assertRaises(CodexAuthError):
            codex_auth.decode_jwt_payload("not-a-jwt")


class LoadAuthJsonTests(unittest.TestCase):
    def test_target_home_override_wins_over_runtime_codex_home(self):
        with patch.dict(
            os.environ,
            {
                "CODEX_HOME": str(Path("runtime-home")),
                "CODEXHUB_CODEX_TARGET_HOME": str(Path("real-codex-home")),
            },
            clear=False,
        ):
            self.assertEqual(codex_auth.codex_home(), Path("real-codex-home"))

    def test_load_auth_json_rejects_non_chatgpt_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_auth_json(Path(tmp), access_token=_make_jwt({"exp": 9999999999}), auth_mode="apikey")
            with self.assertRaises(CodexAuthError) as ctx:
                codex_auth.load_auth_json(path)
            self.assertIn("apikey", str(ctx.exception))

    def test_load_auth_json_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CodexAuthError):
                codex_auth.load_auth_json(Path(tmp) / "auth.json")

    def test_load_auth_json_rejects_missing_access_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {}}), encoding="utf-8")
            with self.assertRaises(CodexAuthError):
                codex_auth.load_auth_json(path)


class AccessTokenTests(unittest.TestCase):
    def setUp(self):
        codex_auth.reset_cache()

    def tearDown(self):
        codex_auth.reset_cache()

    def test_access_token_fresh_does_not_refresh(self):
        future_exp = int(time.time()) + 3600
        token = _make_jwt({"exp": future_exp, "client_id": "app_x"})
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_auth_json(Path(tmp), access_token=token)
            result = codex_auth.access_token(path)
            self.assertEqual(result, token)
            # cache should now be populated
            self.assertIsNotNone(codex_auth._cache)

    def test_access_token_expired_triggers_refresh(self):
        past_exp = int(time.time()) - 100
        old_token = _make_jwt({"exp": past_exp, "client_id": "app_x", "scp": ["openid", "email"]})
        new_token = _make_jwt({"exp": int(time.time()) + 3600, "client_id": "app_x"})
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_auth_json(Path(tmp), access_token=old_token)
            fake = _FakeResponse({
                "access_token": new_token,
                "refresh_token": "rt.new",
                "id_token": "id2",
            })
            with patch("codex_auth.urlopen", return_value=fake) as mock_open:
                result = codex_auth.access_token(path)
            self.assertEqual(result, new_token)
            mock_open.assert_called_once()
            # auth.json should be written back with the new token
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["tokens"]["access_token"], new_token)
            self.assertEqual(written["tokens"]["refresh_token"], "rt.new")

    def test_access_token_no_exp_refreshes_defensively(self):
        token = _make_jwt({"client_id": "app_x"})  # no exp
        new_token = _make_jwt({"exp": int(time.time()) + 3600, "client_id": "app_x"})
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_auth_json(Path(tmp), access_token=token)
            fake = _FakeResponse({"access_token": new_token, "refresh_token": "rt.new"})
            with patch("codex_auth.urlopen", return_value=fake):
                result = codex_auth.access_token(path)
            self.assertEqual(result, new_token)


class RefreshTests(unittest.TestCase):
    def test_refresh_writes_back_new_tokens_preserving_structure(self):
        old_token = _make_jwt({"exp": int(time.time()) - 100, "client_id": "app_x", "scp": ["openid"]})
        new_token = _make_jwt({"exp": int(time.time()) + 3600, "client_id": "app_x"})
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_auth_json(Path(tmp), access_token=old_token, refresh_token="rt.orig")
            auth_data = json.loads(path.read_text(encoding="utf-8"))
            fake = _FakeResponse({"access_token": new_token, "refresh_token": "rt.fresh"})
            with patch("codex_auth.urlopen", return_value=fake):
                result = codex_auth.refresh(auth_data, path)
            self.assertEqual(result, new_token)
            written = json.loads(path.read_text(encoding="utf-8"))
            # structure preserved
            self.assertEqual(written["auth_mode"], "chatgpt")
            self.assertEqual(written["tokens"]["account_id"], "d3544438-0ced-45ba-ab07-e1ff393a0ad2")
            self.assertEqual(written["tokens"]["access_token"], new_token)
            self.assertEqual(written["tokens"]["refresh_token"], "rt.fresh")
            self.assertIn("last_refresh", written)

    def test_refresh_writes_back_with_atomic_lock_recovery(self):
        old_token = _make_jwt({"exp": int(time.time()) - 100, "client_id": "app_x", "scp": ["openid"]})
        new_token = _make_jwt({"exp": int(time.time()) + 3600, "client_id": "app_x"})
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_auth_json(Path(tmp), access_token=old_token, refresh_token="rt.orig")
            lock_path = path.with_name("auth.json.lock")
            lock_path.write_text("pid=0\nacquired_at_millis=0\n", encoding="utf-8")
            auth_data = json.loads(path.read_text(encoding="utf-8"))
            fake = _FakeResponse({"access_token": new_token, "refresh_token": "rt.fresh"})

            with patch("codex_auth.urlopen", return_value=fake):
                codex_auth.refresh(auth_data, path)

            self.assertFalse(lock_path.exists())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["tokens"]["access_token"], new_token)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_refresh_without_refresh_token_raises(self):
        token = _make_jwt({"exp": int(time.time()) + 3600, "client_id": "app_x"})
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_auth_json(Path(tmp), access_token=token, refresh_token="")
            auth_data = json.loads(path.read_text(encoding="utf-8"))
            with self.assertRaises(CodexAuthError) as ctx:
                codex_auth.refresh(auth_data, path)
            self.assertIn("refresh_token", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
