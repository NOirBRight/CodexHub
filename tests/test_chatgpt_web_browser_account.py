from __future__ import annotations

import json
from pathlib import Path

import pytest

import chatgpt_web_browser_account as accounts
import chatgpt_web_runtime as runtime


@pytest.fixture
def account_home(tmp_path, monkeypatch):
    home = tmp_path / "runtime"
    (home / "web-home" / "browser").mkdir(parents=True)
    original = home / "web-home" / "browser" / "storage-state.json"
    original.write_text('{"cookies": [], "origins": []}')
    (home / "web-home" / "config.json").write_text(json.dumps({"storageStatePath": str(original), "headed": True}))
    monkeypatch.setattr(runtime, "build_status", lambda home: {"component": {"compatible": True}})
    class Browser:
        def __init__(self, home, config):
            assert config["headed"] is True
            self.state = json.loads(Path(config["storageStatePath"]).read_text())
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def operation(self, *args, **kwargs):
            assert self.state["cookies"][0]["sameSite"] == "Lax"
            return {"value": {"authenticated": True, "temporary": True,
                              "capabilities": {"solAvailable": True}}}
    monkeypatch.setattr(accounts.checks, "_PinnedBrowserSession", Browser)
    return home


def cookie(**changes):
    return {"name": "__Secure-next-auth.session-token", "value": "synthetic-only",
            "domain": ".chatgpt.com", "path": "/", "secure": True,
            "httpOnly": True, "sameSite": "lax", **changes}


def test_import_selects_verified_generation_without_changing_active_account(account_home):
    home = account_home
    config_before = (home / "web-home" / "config.json").read_bytes()
    original = home / "web-home" / "browser" / "storage-state.json"
    before = original.read_bytes()
    result = accounts.import_session(home, [cookie()])
    assert result == {"ok": True, "authenticated": True, "restart_required": True}
    assert original.read_bytes() == before
    assert (home / "web-home" / "config.json").read_bytes() == config_before
    selected = json.loads((home / "account" / "selected.json").read_text())["generation"]
    state = home / "account" / selected / "storage-state.json"
    assert state.is_file()
    assert json.loads(state.with_name(state.name + ".verified.json").read_text())["solAvailable"] is True
    assert "synthetic-only" not in json.dumps(result)


@pytest.mark.parametrize("changes", [
    {"domain": ".google.com"}, {"domain": "chatgpt.com.attacker.test"},
    {"secure": False}, {"httpOnly": "true"}, {"path": "/other"},
    {"partitionKey": {}}, {"expirationDate": float("nan")},
    {"expirationDate": 1}, {"sameSite": "bad"}, {"name": "bad\nname"},
])
def test_invalid_cookie_never_reaches_browser_or_account_files(account_home, monkeypatch, changes):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid input reached browser")
    monkeypatch.setattr(accounts.checks, "_PinnedBrowserSession", unexpected)
    with pytest.raises(ValueError, match="invalid_browser_session"):
        accounts.import_session(account_home, [cookie(**changes)])
    assert not (account_home / "account").exists()


def test_failed_verification_preserves_selected_account_and_cleans_staging(account_home, monkeypatch):
    accounts.import_session(account_home, [cookie()])
    before = (account_home / "account" / "selected.json").read_bytes()
    entries = sorted(p.name for p in (account_home / "account").iterdir())
    def failed(*args, **kwargs):
        raise RuntimeError("synthetic-only-secret must not reach response")
    monkeypatch.setattr(accounts.checks, "_PinnedBrowserSession", failed)
    with pytest.raises(ValueError, match="^browser_session_verification_failed$"):
        accounts.import_session(account_home, [cookie()])
    assert (account_home / "account" / "selected.json").read_bytes() == before
    assert sorted(p.name for p in (account_home / "account").iterdir()) == entries
