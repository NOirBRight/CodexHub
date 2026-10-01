"""Current-account subscription discovery, without generation or login ownership.

Discovery is separate from Provider publication and exchange admission. An
available model list is not proof of subscription generation entitlement.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from claude_native_models import cli_command, concrete_executable

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+\[\]-]{0,255}\Z")
_CURSOR_ROW = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._:+\[\]-]*) - (.+)$")
_MAX_BYTES = 4 * 1024 * 1024
_PROVIDERS = {"claude-subscription", "cursor-subscription"}
_FAILURE_STATES = {"cli-timeout", "auth-required", "auth-expired", "not-eligible",
                   "discovery-failed", "account-changed"}


@dataclass(frozen=True)
class SubscriptionModel:
    id: str
    display_name: str


@dataclass(frozen=True)
class SubscriptionDiscovery:
    provider_id: str
    state: str
    cli_version: str | None = None
    models: tuple[SubscriptionModel, ...] = ()

    def public_status(self) -> dict[str, Any]:
        """Return only public catalog facts; never CLI output or account identity."""
        return {
            "provider_id": self.provider_id,
            "state": self.state,
            "cli_version": self.cli_version,
            "generation_qualified": False,
            "models": [{"id": row.id, "display_name": row.display_name} for row in self.models],
        }


class DiscoveryFailure(ValueError):
    """Bounded failure classification without upstream/CLI secret material."""

    def __init__(self, state: str = "discovery-failed"):
        if state not in _FAILURE_STATES:
            raise ValueError("unknown discovery failure state")
        self.state = state
        super().__init__(state)


def run_cli(binary: Path, arguments: list[str], *, env: Mapping[str, str], cwd: Path,
            timeout: float) -> subprocess.CompletedProcess[str]:
    """Run an inference-free CLI operation; reap its process tree on timeout."""
    command = cli_command(binary, arguments)
    windows = os.name == "nt"
    with subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="strict", env=dict(env), cwd=cwd,
        start_new_session=not windows,
        creationflags=subprocess.CREATE_NO_WINDOW if windows else 0,
    ) as child:
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if windows:
                try:
                    subprocess.run(
                        [str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"),
                         "/PID", str(child.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=5, check=False,
                    )
                finally:
                    child.kill()
            else:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.kill()
            child.communicate(timeout=5)
            raise DiscoveryFailure("cli-timeout") from None
        if len(stdout.encode("utf-8")) > _MAX_BYTES or len(stderr.encode("utf-8")) > _MAX_BYTES:
            raise DiscoveryFailure()
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def fetch_claude_model_page(token: str, after: str, cli_version: str) -> Mapping[str, Any]:
    """Read the account vendor catalog. Never redirect or inherit a Gateway proxy."""
    query = {"limit": "1000"}
    if after:
        query["after_id"] = after
    request = Request("https://api.anthropic.com/v1/models?" + urlencode(query), headers={
        "Authorization": "Bearer " + token,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": f"claude-cli/{cli_version} (external, cli)",
    })
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=15) as response:
            body = response.read(_MAX_BYTES + 1)
    except HTTPError as error:
        state = "auth-required" if error.code == 401 else "not-eligible" if error.code == 403 else "discovery-failed"
        error.close()
        raise DiscoveryFailure(state) from None
    if len(body) > _MAX_BYTES:
        raise DiscoveryFailure()
    result = json.loads(body)
    if not isinstance(result, Mapping):
        raise DiscoveryFailure()
    return result


def discover_subscription(
    provider_id: str, *, binary: Path | None = None,
    source_home: Path | None = None, environ: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = run_cli,
    claude_page: Callable[[str, str], Mapping[str, Any]] | None = None,
    now: Callable[[], float] = time.time,
) -> SubscriptionDiscovery:
    """Return one coherent discovery snapshot from the current CLI account.

    Ports are injectable for deterministic tests. Every command runs in a
    disposable configuration with only the official login snapshot, no project
    settings/hooks or ambient provider credentials. The official source is
    reread before returning; refresh/signout/account change invalidates rows.
    """
    if provider_id not in _PROVIDERS:
        raise ValueError("unknown CLI subscription provider")
    environment = dict(os.environ if environ is None else environ)
    home = source_home or Path.home()
    executable_name = "claude" if provider_id == "claude-subscription" else "cursor-agent"
    candidate = binary or shutil.which(executable_name, path=environment.get("PATH", ""))
    if not candidate or not Path(candidate).is_file():
        return SubscriptionDiscovery(provider_id, "cli-missing")
    version = None
    try:
        resolved = concrete_executable(Path(candidate).absolute(), executable_name)
        if provider_id == "claude-subscription":
            source = Path(environment.get("CLAUDE_CONFIG_DIR") or home / ".claude") / ".credentials.json"
        else:
            config = Path(environment.get("APPDATA") or home / "AppData" / "Roaming") if os.name == "nt" else Path(environment.get("XDG_CONFIG_HOME") or home / ".config")
            source = config / ("Cursor" if os.name == "nt" else "cursor") / "auth.json"
        original = _read_source(source)
        auth = json.loads(original)
        if not isinstance(auth, dict):
            raise DiscoveryFailure("auth-required")
        if provider_id == "claude-subscription":
            oauth = auth.get("claudeAiOauth")
            if not isinstance(oauth, dict):
                raise DiscoveryFailure("auth-required")
            token = _token(oauth.get("accessToken"))
            expiry = oauth.get("expiresAt")
            if not isinstance(expiry, (int, float)) or isinstance(expiry, bool) or not math.isfinite(expiry):
                raise DiscoveryFailure("auth-required")
            expiry = expiry / 1000 if expiry >= 1e12 else expiry
            if expiry <= now():
                raise DiscoveryFailure("auth-expired")
            scopes = oauth.get("scopes")
            if not isinstance(scopes, list) or "user:inference" not in scopes:
                raise DiscoveryFailure("not-eligible")
        else:
            token = _token(auth.get("accessToken"))
            expiry = _jwt_expiry(token)
            if expiry is not None and expiry <= now():
                raise DiscoveryFailure("auth-expired")
        with tempfile.TemporaryDirectory(prefix="codexhub-cli-discovery-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            env = _isolated_environment(environment, root, resolved)
            if provider_id == "claude-subscription":
                private_config = root / ".claude"
                private_config.mkdir(mode=0o700)
                _private_copy(private_config / ".credentials.json", json.dumps({"claudeAiOauth": oauth}).encode())
                env["CLAUDE_CONFIG_DIR"] = str(private_config)
                result = runner(resolved, ["auth", "status", "--json"], env=env, cwd=root, timeout=10)
                status = _json_object(result.stdout)
                if status.get("loggedIn") is False:
                    raise DiscoveryFailure("auth-required")
                if status.get("loggedIn") is not True or status.get("apiProvider") != "firstParty" or status.get("authMethod") not in {"claude.ai", "oauth_token"}:
                    raise DiscoveryFailure("not-eligible")
                if result.returncode:
                    raise DiscoveryFailure()
                version_result = runner(resolved, ["--version"], env=env, cwd=root, timeout=5)
                match = re.match(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?", version_result.stdout.strip())
                if version_result.returncode or not match:
                    raise DiscoveryFailure()
                version = match.group()
                page_reader = claude_page or (lambda token, after: fetch_claude_model_page(token, after, version))
                rows = _claude_models(token, page_reader)
            else:
                private_config = Path(env["APPDATA"] if os.name == "nt" else env["XDG_CONFIG_HOME"]) / ("Cursor" if os.name == "nt" else "cursor")
                private_config.mkdir(mode=0o700, parents=True)
                _private_copy(private_config / "auth.json", json.dumps({
                    key: auth[key] for key in ("accessToken", "refreshToken") if key in auth
                }).encode())
                result = runner(resolved, ["about", "--format", "json"], env=env, cwd=root, timeout=15)
                status = _json_object(result.stdout)
                if not isinstance(status.get("userEmail"), str) or not status["userEmail"].strip():
                    raise DiscoveryFailure("auth-required")
                if result.returncode:
                    raise DiscoveryFailure()
                raw_version = status.get("cliVersion")
                if not isinstance(raw_version, str) or not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}-[0-9a-f]+", raw_version):
                    raise DiscoveryFailure()
                version = raw_version
                result = runner(resolved, ["models"], env=env, cwd=root, timeout=30)
                if result.returncode:
                    raise DiscoveryFailure()
                rows = _cursor_models(result.stdout)
            try:
                unchanged = _read_source(source) == original
            except DiscoveryFailure:
                unchanged = False
            if not unchanged:
                raise DiscoveryFailure("account-changed")
            if expiry is not None and expiry <= now():
                raise DiscoveryFailure("auth-expired")
        return SubscriptionDiscovery(provider_id, "available", version, tuple(rows))
    except DiscoveryFailure as error:
        return SubscriptionDiscovery(provider_id, error.state, version)
    except subprocess.TimeoutExpired:
        return SubscriptionDiscovery(provider_id, "cli-timeout", version)
    except (OSError, ValueError, TypeError):
        return SubscriptionDiscovery(provider_id, "discovery-failed", version)


def _read_source(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            value = handle.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        raise DiscoveryFailure("auth-required") from None
    if len(value) > _MAX_BYTES:
        raise DiscoveryFailure("auth-required")
    return value


def _token(value: Any) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise DiscoveryFailure("auth-required")
    return value


def _jwt_expiry(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        expiry = payload.get("exp") if isinstance(payload, dict) else None
        if isinstance(expiry, (int, float)) and not isinstance(expiry, bool) and math.isfinite(expiry):
            return expiry
    except (ValueError, UnicodeError):
        pass
    raise DiscoveryFailure("auth-required")


def _private_copy(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)


def _isolated_environment(source: Mapping[str, str], root: Path, binary: Path) -> dict[str, str]:
    env = {key: value for key, value in source.items() if key in {
        "PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT",
        "CODEXHUB_E2E_PYTHON",
    }}
    # Keep installed CLI helpers on PATH even when the parent uses a mise shim.
    env["PATH"] = str(binary.parent) + os.pathsep + env.get("PATH", os.defpath)
    env.update({
        "HOME": str(root), "USERPROFILE": str(root),
        "APPDATA": str(root / "appdata"), "LOCALAPPDATA": str(root / "localappdata"),
        "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_DATA_HOME": str(root / "data"), "TMPDIR": str(root), "TMP": str(root), "TEMP": str(root),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1", "TERM": "dumb",
        "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    })
    return env


def _json_object(output: str) -> dict[str, Any]:
    # Some Cursor releases precede JSON with an update notice. JSON itself
    # must consume the remainder; embedded/trailing arbitrary logs are unsafe.
    cleaned = _ANSI.sub("", output).strip()
    start = cleaned.find("{")
    value = json.loads(cleaned[start:]) if start >= 0 else None
    if not isinstance(value, dict):
        raise DiscoveryFailure()
    return value


def _append_model(rows: dict[str, SubscriptionModel], model_id: Any, name: Any) -> None:
    if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
        raise DiscoveryFailure()
    if not isinstance(name, str) or not name.strip() or len(name) > 1024:
        raise DiscoveryFailure()
    display = " ".join(_ANSI.sub("", name).replace("\u200b", "").split())
    if not display:
        raise DiscoveryFailure()
    row = SubscriptionModel(model_id, display)
    if model_id in rows and rows[model_id] != row:
        raise DiscoveryFailure()
    rows[model_id] = row


def _cursor_models(output: str) -> list[SubscriptionModel]:
    rows: dict[str, SubscriptionModel] = {}
    for line in _ANSI.sub("", output).splitlines():
        match = _CURSOR_ROW.fullmatch(line.strip())
        if match:
            name = re.sub(r"\s*\((?:default|current)\)\s*$", "", match[2])
            _append_model(rows, match[1], name)
        elif re.fullmatch(r"\S+ - .+", line.strip()):
            # A vendor row with an unsupported identity cannot be quietly
            # skipped while claiming discovery retained every account model.
            raise DiscoveryFailure()
    if not rows:
        raise DiscoveryFailure()
    return list(rows.values())


def _claude_models(token: str, page_reader: Callable[[str, str], Mapping[str, Any]]) -> list[SubscriptionModel]:
    rows: dict[str, SubscriptionModel] = {}
    cursors: set[str] = set()
    after = ""
    for _ in range(32):
        page = page_reader(token, after)
        data = page.get("data")
        if not isinstance(data, list) or not isinstance(page.get("has_more"), bool):
            raise DiscoveryFailure()
        for model in data:
            if not isinstance(model, Mapping):
                raise DiscoveryFailure()
            _append_model(rows, model.get("id"), model.get("display_name") or model.get("id"))
        if page["has_more"] is False:
            if not rows:
                raise DiscoveryFailure()
            return list(rows.values())
        after = page.get("last_id")
        if not isinstance(after, str) or not _MODEL_ID.fullmatch(after) or after in cursors:
            raise DiscoveryFailure()
        cursors.add(after)
    raise DiscoveryFailure()
