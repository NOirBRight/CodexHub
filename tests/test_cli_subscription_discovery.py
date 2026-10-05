"""Account discovery does not publish inferred entitlement or private CLI state."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import cli_subscription_discovery
from cli_subscription_discovery import discover_subscription, DiscoveryFailure, run_cli


def account(tmp_path, provider="cursor-subscription", auth=None):
    home = tmp_path / "source"
    home.mkdir(exist_ok=True)
    config = home / ".claude" if provider == "claude-subscription" else home / "config" / "cursor"
    if os.name == "nt" and provider == "cursor-subscription":
        config = home / "appdata" / "Cursor"
    config.mkdir(parents=True, exist_ok=True)
    if auth is None:
        auth = {"accessToken": "cursor-token", "refreshToken": "refresh-private"}
        if provider == "claude-subscription":
            auth = {"claudeAiOauth": {"accessToken": "claude-token", "refreshToken": "refresh-private",
                                     "expiresAt": 4_000_000_000_000, "scopes": ["user:inference"]},
                    "mcpOAuth": {"secret": "unrelated-login"}}
    path = config / (".credentials.json" if provider == "claude-subscription" else "auth.json")
    path.write_text(json.dumps(auth), encoding="utf-8")
    (config / "settings.json").write_text(json.dumps({"hooks": {"bad": "execute"}, "env": {
        "ANTHROPIC_BASE_URL": "http://running-gateway.invalid", "ANTHROPIC_API_KEY": "settings-secret"}}))
    binary = tmp_path / "installed-cli"
    binary.touch()
    return home, path, binary


def environment(home):
    return {
        "XDG_CONFIG_HOME": str(home / "config"), "APPDATA": str(home / "appdata"),
        "PATH": os.environ.get("PATH", ""), "ANTHROPIC_API_KEY": "ambient-private",
        "ANTHROPIC_BASE_URL": "http://running-gateway.invalid", "CODEX_HOME": "/normal/codex",
        "CODEXHUB_MANAGED_CLIENT": "claude", "HTTP_PROXY": "http://proxy.invalid",
        "CURSOR_API_KEY": "ambient-cursor", "NODE_OPTIONS": "--require=unsafe.js",
    }


def completed(stdout, code=0):
    return subprocess.CompletedProcess([], code, stdout, "private-token private-email@example.com")


def cursor_runner(roots, models=None):
    def runner(binary, args, *, env, cwd, timeout):
        roots.append(cwd)
        assert env["HOME"] == str(cwd)
        assert not any(key in env for key in ("CURSOR_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                                               "CODEX_HOME", "CODEXHUB_MANAGED_CLIENT", "HTTP_PROXY", "NODE_OPTIONS"))
        config = Path(env["APPDATA"] if os.name == "nt" else env["XDG_CONFIG_HOME"]) / ("Cursor" if os.name == "nt" else "cursor")
        assert not (config / "settings.json").exists()
        assert set(json.loads((config / "auth.json").read_text())) <= {"accessToken", "refreshToken"}
        if os.name != "nt":
            assert cwd.stat().st_mode & 0o777 == 0o700
            assert (config / "auth.json").stat().st_mode & 0o777 == 0o600
        if args == ["about", "--format", "json"]:
            return completed('Update notice\n' + json.dumps({"cliVersion": "2026.09.28-64d2043",
                                                           "userEmail": "private-email@example.com", "subscriptionTier": "Ultra"}))
        assert args == ["models"]
        return completed(models or "Available models:\n" + "\n".join(f"model-{i}-high - Model {i} High" for i in range(246)))
    return runner


def claude_runner(roots, status=None):
    def runner(binary, args, *, env, cwd, timeout):
        roots.append(cwd)
        config = Path(env["CLAUDE_CONFIG_DIR"])
        assert config.parent == cwd
        assert not (config / "settings.json").exists()
        assert set(json.loads((config / ".credentials.json").read_text())) == {"claudeAiOauth"}
        assert not any(key in env for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "NODE_OPTIONS"))
        if args == ["auth", "status", "--json"]:
            return completed(json.dumps(status or {"loggedIn": True, "authMethod": "claude.ai",
                                                  "apiProvider": "firstParty", "email": "private@example.com"}))
        assert args == ["--version"]
        return completed("2.1.282 (Claude Code)")
    return runner


def test_cursor_preserves_all_exact_variants_without_reference_export_cap(tmp_path):
    home, source, binary = account(tmp_path, auth={"accessToken": "cursor-token", "otherSecret": "not-copied"})
    before = source.read_bytes()
    roots = []
    result = discover_subscription("cursor-subscription", binary=binary, source_home=home,
                                   environ=environment(home), runner=cursor_runner(roots))
    assert result.state == "available"
    assert len(result.models) == 246
    assert result.models[-1].id == "model-245-high"
    assert result.cli_version == "2026.09.28-64d2043"
    assert source.read_bytes() == before
    assert roots and all(not root.exists() for root in roots)
    public = json.dumps(result.public_status())
    assert all(value not in public for value in ("private", "cursor-token", str(home), "Ultra"))
    assert result.public_status()["generation_qualified"] is False


def test_cursor_display_cleanup_does_not_collapse_variant_identity(tmp_path):
    home, _, binary = account(tmp_path)
    output = "\x1b[32mfamily-high-fast - Model \u200b High Fast (current)\x1b[0m\nfamily-low - Model Low (default)"
    result = discover_subscription("cursor-subscription", binary=binary, source_home=home,
                                   environ=environment(home), runner=cursor_runner([], output))
    assert [(m.id, m.display_name) for m in result.models] == [
        ("family-high-fast", "Model High Fast"), ("family-low", "Model Low")]


@pytest.mark.parametrize("change", ["switch", "logout"])
def test_account_change_during_discovery_discards_every_row(tmp_path, change):
    home, source, binary = account(tmp_path)
    delegate = cursor_runner([])
    def runner(*args, **kwargs):
        result = delegate(*args, **kwargs)
        if args[1] == ["models"]:
            if change == "switch":
                source.write_text('{"accessToken":"another-account"}')
            else:
                source.unlink()
        return result
    result = discover_subscription("cursor-subscription", binary=binary, source_home=home,
                                   environ=environment(home), runner=runner)
    assert result.state == "account-changed"
    assert not result.models


def test_claude_catalog_is_vendor_paginated_and_not_downstream_picker(tmp_path):
    home, source, binary = account(tmp_path, "claude-subscription")
    before = source.read_bytes()
    calls, roots = [], []
    def page(token, after):
        assert token == "claude-token"
        calls.append(after)
        if after == "":
            return {"data": [{"id": "claude-opus-5-5", "display_name": "Opus"}],
                    "has_more": True, "last_id": "claude-opus-5-5"}
        return {"data": [{"id": "claude-sonnet-5-5", "display_name": "Sonnet"}], "has_more": False}
    result = discover_subscription("claude-subscription", binary=binary, source_home=home,
                                   environ=environment(home), runner=claude_runner(roots), claude_page=page)
    assert result.state == "available"
    assert [m.id for m in result.models] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert calls == ["", "claude-opus-5-5"]
    assert source.read_bytes() == before
    assert roots and all(not root.exists() for root in roots)
    assert "private" not in json.dumps(result.public_status())


@pytest.mark.parametrize("status", [
    {"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"},
    {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"},
])
def test_claude_rejects_api_key_or_cloud_account_as_subscription(tmp_path, status):
    home, _, binary = account(tmp_path, "claude-subscription")
    def page(*_args):
        pytest.fail("unrelated credential must not reach model discovery")
    result = discover_subscription("claude-subscription", binary=binary, source_home=home,
                                   environ=environment(home), runner=claude_runner([], status), claude_page=page)
    assert result.state == "not-eligible"
    assert not result.models


@pytest.mark.parametrize("provider", ["claude-subscription", "cursor-subscription"])
def test_expired_account_never_refreshes_or_starts_generation(tmp_path, provider):
    if provider == "claude-subscription":
        auth = {"claudeAiOauth": {"accessToken": "expired", "expiresAt": 1, "scopes": ["user:inference"]}}
    else:
        payload = base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip("=")
        auth = {"accessToken": "header." + payload + ".signature"}
    home, _, binary = account(tmp_path, provider, auth)
    def runner(*_args, **_kwargs):
        pytest.fail("expired account must be handled by the official CLI owner")
    result = discover_subscription(provider, binary=binary, source_home=home,
                                   environ=environment(home), runner=runner)
    assert result.state == "auth-expired"


@pytest.mark.parametrize("failure,expected", [(DiscoveryFailure("cli-timeout"), "cli-timeout"),
                                             (OSError("secret-token"), "discovery-failed")])
def test_failures_clean_private_directory_and_never_return_raw_errors(tmp_path, failure, expected):
    home, _, binary = account(tmp_path)
    roots = []
    def runner(*_args, **kwargs):
        roots.append(kwargs["cwd"])
        raise failure
    result = discover_subscription("cursor-subscription", binary=binary, source_home=home,
                                   environ=environment(home), runner=runner)
    assert result.state == expected
    assert roots and not roots[0].exists()
    assert "secret" not in json.dumps(result.public_status())


@pytest.mark.parametrize("output", ["", "warning: could not list models", "same - One\nsame - Two",
                                    "good - Good\nunsupported id - Unsupported"])
def test_missing_or_ambiguous_cursor_catalog_is_not_available(tmp_path, output):
    home, _, binary = account(tmp_path)
    runner = cursor_runner([])
    def invoke(binary, args, **kwargs):
        return completed(output) if args == ["models"] else runner(binary, args, **kwargs)
    result = discover_subscription("cursor-subscription", binary=binary, source_home=home,
                                   environ=environment(home), runner=invoke)
    assert result.state == "discovery-failed"
    assert not result.models


def test_raw_vendor_slash_identity_survives_discovery(tmp_path):
    home, _, binary = account(tmp_path)
    result = discover_subscription("cursor-subscription", binary=binary, source_home=home,
                                   environ=environment(home), runner=cursor_runner([], "vendor/exact-high-fast - Vendor Model"))
    assert result.state == "available"
    assert result.models[0].id == "vendor/exact-high-fast"


@pytest.mark.parametrize("page", [
    {"data": [], "has_more": False},
    {"data": [{"id": "good", "display_name": "Good"}], "has_more": True},
    {"data": [{"id": "good"}], "has_more": "false"},
    {"data": [{"id": "bad id", "display_name": "Bad"}], "has_more": False},
])
def test_malformed_or_incomplete_vendor_catalog_discards_partial_models(tmp_path, page):
    home, _, binary = account(tmp_path, "claude-subscription")
    result = discover_subscription("claude-subscription", binary=binary, source_home=home,
                                   environ=environment(home), runner=claude_runner([]), claude_page=lambda *_: page)
    assert result.state == "discovery-failed"
    assert not result.models


def test_vendor_pagination_cycle_does_not_publish_truncated_catalog(tmp_path):
    home, _, binary = account(tmp_path, "claude-subscription")
    calls = []
    def page(token, after):
        calls.append(after)
        return {"data": [{"id": "good"}], "has_more": True, "last_id": "good"}
    result = discover_subscription("claude-subscription", binary=binary, source_home=home,
                                   environ=environment(home), runner=claude_runner([]), claude_page=page)
    assert result.state == "discovery-failed"
    assert calls == ["", "good"]


def test_missing_cli_and_missing_login_have_distinct_status(tmp_path):
    result = discover_subscription("cursor-subscription", binary=tmp_path / "missing", environ={})
    assert result.state == "cli-missing"
    binary = tmp_path / "cli"
    binary.touch()
    result = discover_subscription("cursor-subscription", binary=binary, source_home=tmp_path, environ={})
    assert result.state == "auth-required"


def test_account_expiring_during_discovery_does_not_publish_stale_availability(tmp_path):
    payload = base64.urlsafe_b64encode(b'{"exp":10}').decode().rstrip("=")
    home, _, binary = account(tmp_path, auth={"accessToken": "header." + payload + ".signature"})
    clock = iter([1, 11])
    result = discover_subscription("cursor-subscription", binary=binary, source_home=home,
                                   environ=environment(home), runner=cursor_runner([]), now=lambda: next(clock))
    assert result.state == "auth-expired"
    assert not result.models


def test_claude_vendor_request_is_pinned_and_never_redirects_credentials(monkeypatch):
    seen = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def read(self, size):
            assert size == 4 * 1024 * 1024 + 1
            return b'{"data":[],"has_more":false}'
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://api.anthropic.com/v1/models?limit=1000&after_id=claude-opus"
            assert request.get_header("Authorization") == "Bearer private-token"
            assert request.get_header("User-agent") == "claude-cli/2.1.285 (external, cli)"
            assert timeout == 15
            seen.append(request)
            return Response()
    def build(*handlers):
        assert handlers[0].proxies == {}
        assert handlers[1].redirect_request(None, None, None, None, None, None) is None
        return Opener()
    monkeypatch.setattr(cli_subscription_discovery, "build_opener", build)
    assert cli_subscription_discovery.fetch_claude_model_page("private-token", "claude-opus", "2.1.285")["has_more"] is False
    assert len(seen) == 1


@pytest.mark.parametrize("code,state", [(401, "auth-required"), (403, "not-eligible"), (502, "discovery-failed"), (302, "discovery-failed")])
def test_vendor_http_errors_have_bounded_classification_and_no_raw_message(monkeypatch, code, state):
    from urllib.error import HTTPError
    import io
    body = io.BytesIO(b'{"error":"private-token private-account"}')
    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, code, "private-token", {}, body)
    monkeypatch.setattr(cli_subscription_discovery, "build_opener", lambda *_: Opener())
    with pytest.raises(DiscoveryFailure) as captured:
        cli_subscription_discovery.fetch_claude_model_page("private-token", "", "2.1.285")
    assert captured.value.state == state
    assert str(captured.value) == state
    assert body.closed


def assert_descendant_not_running(state_path):
    # An orphan can briefly remain a zombie awaiting init; it cannot execute.
    try:
        stat = state_path.read_text()
    except FileNotFoundError:
        return
    assert stat.split()[2] == "Z"


def test_descendant_stat_disappearing_at_read_is_not_running(tmp_path, monkeypatch):
    state_path = tmp_path / "stat"
    state_path.write_text("123 (python) Z")
    read_text = Path.read_text
    def disappearing_read(path, *args, **kwargs):
        if path == state_path:
            path.unlink()
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", disappearing_read)
    assert_descendant_not_running(state_path)


def test_descendant_zombie_is_not_running(tmp_path):
    state_path = tmp_path / "stat"
    state_path.write_text("123 (python) Z")
    assert_descendant_not_running(state_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group observation")
def test_descendant_observation_rejects_a_living_process(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                             stdin=subprocess.PIPE, cwd=tmp_path)
    try:
        with pytest.raises(AssertionError):
            assert_descendant_not_running(Path(f"/proc/{child.pid}/stat"))
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdin.close()


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("read failed")])
def test_descendant_observation_does_not_hide_read_errors(tmp_path, monkeypatch, error):
    state_path = tmp_path / "stat"
    state_path.touch()
    def failed_read(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(Path, "read_text", failed_read)
    with pytest.raises(type(error), match=str(error)):
        assert_descendant_not_running(state_path)


def test_descendant_observation_does_not_hide_malformed_stat(tmp_path):
    state_path = tmp_path / "stat"
    state_path.write_text("malformed")
    with pytest.raises(IndexError):
        assert_descendant_not_running(state_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group observation")
def test_cli_timeout_kills_the_cli_and_its_descendant(tmp_path):
    script = "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
    pid_file = tmp_path / "child.pid"
    with pytest.raises(DiscoveryFailure, match="cli-timeout"):
        run_cli(Path(sys.executable), ["-c", script, str(pid_file)], env=os.environ, cwd=tmp_path, timeout=1)
    assert pid_file.exists()
    state_path = Path(f"/proc/{pid_file.read_text()}/stat")
    assert_descendant_not_running(state_path)
