"""Read the installed Claude picker in a disposable, inference-free CLI session."""
from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def concrete_executable(binary: Path) -> Path:
    """Return a Claude binary that can start without a version-manager lookup.

    A mise shim's resolved file is ``mise``. Running that shim after ``HOME`` is
    replaced makes mise try to download Claude, which the offline discovery
    proxy then refuses.
    """
    if not binary.is_file():
        raise ValueError('Claude executable was not found')
    resolved = binary.resolve()
    if resolved.name != 'mise':
        return binary
    which = subprocess.run(
        [str(resolved), 'which', 'claude'],
        capture_output=True, text=True, timeout=5,
    )
    lines = [line.strip() for line in which.stdout.splitlines() if line.strip()]
    found = Path(lines[-1]) if which.returncode == 0 and lines else None
    if found is None or not found.is_file() or found.resolve().name == 'mise':
        raise ValueError('Claude executable was not found')
    return found


def cli_command(binary: Path, arguments: list[str]) -> list[str] | str:
    command = [str(binary), *arguments]
    if os.name == 'nt' and binary.suffix.lower() in ('.cmd', '.bat'):
        if any(any(char in arg for char in '\"%!\r\n') for arg in command):
            raise ValueError('Unsafe Claude batch shim path')
        shell = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'cmd.exe')
        payload = '"' + ' '.join('"' + arg + '"' for arg in command) + '"'
        # cmd parses this quoting itself; list2cmdline would escape it again.
        return f'"{shell}" /d /s /c {payload}'
    return command


def discover(binary: Path, config: Path) -> dict:
    source = config / 'settings.json'
    current = json.loads(source.read_text(encoding='utf-8')) if source.exists() else {}
    if not isinstance(current, dict):
        raise ValueError('Claude settings must be an object')
    # Carry selection policy, never hooks, plugins, credentials or arbitrary env.
    snapshot = {key: current[key] for key in ('availableModels', 'modelOverrides') if key in current}
    source_env = current.get('env', {})
    if not isinstance(source_env, dict):
        raise ValueError('Claude settings env must be an object')
    snapshot['env'] = {
        key: value for key, value in source_env.items()
        if key.startswith('ANTHROPIC_DEFAULT_') and isinstance(value, str)
        and not value.startswith('claude-codexhub-')
    }
    # Ask for the gateway-mode lineup: some genuine native 1M variants are only
    # listed separately when a base URL is configured. No request is submitted.
    snapshot['env']['ANTHROPIC_BASE_URL'] = 'http://127.0.0.1:1'
    with tempfile.TemporaryDirectory(prefix='codexhub-native-picker-') as directory:
        root = Path(directory)
        isolated = root / '.claude'
        isolated.mkdir(mode=0o700)
        (isolated / 'settings.json').write_text(json.dumps(snapshot))
        # Retain account classification, not login credentials or refresh tokens.
        metadata = {}
        for account_path in (config.parent / '.claude.json', config / '.claude.json'):
            if account_path.is_file():
                account = json.loads(account_path.read_text(encoding='utf-8'))
                if isinstance(account, dict) and isinstance(account.get('oauthAccount'), dict):
                    metadata = {'oauthAccount': {key: value for key, value in account['oauthAccount'].items()
                                                if key in ('accountUuid', 'organizationUuid', 'billingType', 'hasExtraUsageEnabled')}}
                    for target in (root / '.claude.json', isolated / '.claude.json'):
                        target.write_text(json.dumps(metadata))
                        target.chmod(0o600)
                    break
        env = {key: value for key, value in os.environ.items()
               if key in ('PATH', 'SYSTEMROOT', 'SystemRoot', 'WINDIR', 'ProgramData', 'CODEXHUB_E2E_PYTHON')}
        env.update({
            'HOME': str(root), 'USERPROFILE': str(root), 'CLAUDE_CONFIG_DIR': str(isolated),
            'APPDATA': str(root / 'appdata'), 'LOCALAPPDATA': str(root / 'localappdata'),
            'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_CACHE_HOME': str(root / 'cache'),
            'XDG_DATA_HOME': str(root / 'data'), 'TMPDIR': str(root), 'TMP': str(root), 'TEMP': str(root),
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1',
            'DISABLE_TELEMETRY': '1', 'DISABLE_ERROR_REPORTING': '1',
            'CLAUDE_CODE_DISABLE_CLAUDE_MDS': '1',
            'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1',
            'http_proxy': 'http://127.0.0.1:1', 'https_proxy': 'http://127.0.0.1:1',
        })
        request = {'type': 'control_request', 'request_id': 'native-picker', 'request': {'subtype': 'initialize'}}
        command = cli_command(binary, [
            '-p', '--bare', '--tools', '', '--strict-mcp-config',
            '--setting-sources', 'user', '--input-format', 'stream-json',
            '--output-format', 'stream-json', '--verbose', '--no-session-persistence',
        ])
        result = subprocess.run(command, input=json.dumps(request) + '\n', capture_output=True, text=True,
            encoding='utf-8', errors='strict',
            cwd=root, env=env, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise ValueError('Claude native model initialization failed')
        rows = {}
        for line in result.stdout.splitlines():
            try:
                packet = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(packet, dict):
                continue
            models = packet.get('response', {}).get('response', {}).get('models', [])
            for model in models:
                resolved = model.get('resolvedModel', '')
                if not isinstance(resolved, str) or not re.fullmatch(r'claude-[A-Za-z0-9._-]+(?:\[1m\])?', resolved):
                    continue
                if resolved.startswith('claude-codexhub-'):
                    continue
                label = model.get('displayName') if model.get('value') != 'default' else None
                rows[resolved] = {'model': resolved, 'label': label or resolved,
                                  'description': f'Claude subscription via Gateway · {resolved}'}
        if not rows:
            raise ValueError('Claude returned no native models; existing configuration was preserved')
        version = subprocess.run(cli_command(binary, ['--version']), capture_output=True, text=True,
                                 encoding='utf-8', errors='strict',
                                 cwd=root, env=env, timeout=3,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        match = re.match(r'\d+\.\d+\.\d+', version.stdout.strip())
        if version.returncode or not match:
            raise ValueError('Cannot identify Claude version')
        source = json.dumps({'settings': snapshot, 'account': metadata, 'models': list(rows.values())}, sort_keys=True)
        return {'models': list(rows.values()), 'source': {
            'cli_version': match.group(), 'fingerprint': hashlib.sha256(source.encode()).hexdigest(),
        }}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claude-bin', type=Path, required=True)
    parser.add_argument('--config-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        binary = concrete_executable(args.claude_bin.expanduser())
        print(json.dumps(discover(binary, args.config_dir.expanduser().resolve())))
        return 0
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        print('Cannot obtain the native Claude model list. Update Claude Code and retry; existing configuration was preserved.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
