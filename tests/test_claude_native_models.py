"""Native picker discovery uses the CLI protocol without running inference."""
import json
import os
from pathlib import Path
import subprocess
import sys
import shlex


def test_native_discovery_preserves_exact_ids_and_isolates_configuration(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    settings = source / 'settings.json'
    settings.write_text(json.dumps({'env': {
        'ANTHROPIC_BASE_URL': 'http://production.invalid',
        'ANTHROPIC_DEFAULT_OPUS_MODEL': 'claude-codexhub-external',
        'CODEXHUB_MANAGED_CLIENT': 'claude',
    }}))
    before = settings.read_bytes()
    program = tmp_path / 'fixture.py'
    program.write_text('''
import json,os,sys
from pathlib import Path
assert sys.version_info >= (3,13)
assert sys.executable == os.environ["CODEXHUB_E2E_PYTHON"]
if "--version" in sys.argv:
    print("2.1.282 (Claude Code)")
    raise SystemExit(0)
assert "ANTHROPIC_API_KEY" not in os.environ
assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in json.loads((Path(os.environ["CLAUDE_CONFIG_DIR"])/"settings.json").read_text()).get("env", {})
assert json.loads(sys.stdin.readline())["request"]["subtype"] == "initialize"
print(json.dumps({"response":{"response":{"models":[{"value":"opus[1m]","resolvedModel":"claude-opus-5-5[1m]","displayName":"Opus"},{"value":"haiku","resolvedModel":"claude-haiku-4-5-20251001","displayName":"Haiku"},{"value":"custom","resolvedModel":"claude-codexhub-external","displayName":"External"}],"account":{"secret":"MUST_NOT_RETURN"}}}}))
''')
    fixture = tmp_path / ('claude & fixture.cmd' if os.name == 'nt' else 'claude')
    if os.name == 'nt':
        fixture.write_text(f'@echo off\n"%CODEXHUB_E2E_PYTHON%" "{program}" %*\n')
    else:
        fixture.write_text(f'#!/bin/sh\nexec "$CODEXHUB_E2E_PYTHON" {shlex.quote(str(program))} "$@"\n')
    fixture.chmod(0o700)
    script = Path(__file__).resolve().parents[1] / 'src-python' / 'claude_native_models.py'
    result = subprocess.run([sys.executable, str(script), '--claude-bin', str(fixture), '--config-dir', str(source)],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, 'CODEXHUB_E2E_PYTHON': sys.executable, 'ANTHROPIC_API_KEY': 'MUST_NOT_INHERIT'})
    assert result.returncode == 0, result.stderr
    discovered = json.loads(result.stdout)
    rows = discovered['models']
    assert discovered['source']['cli_version'] == '2.1.282'
    assert len(discovered['source']['fingerprint']) == 64
    assert rows[0]['label'] == 'Opus'
    assert [row['model'] for row in rows] == ['claude-opus-5-5[1m]', 'claude-haiku-4-5-20251001']
    assert 'MUST_NOT_RETURN' not in result.stdout
    assert settings.read_bytes() == before


def test_native_discovery_keeps_a_symlink_shim_name(tmp_path):
    if os.name == 'nt':
        return
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'settings.json').write_text('{}')
    program = tmp_path / 'fixture.py'
    program.write_text('''
import json,os,sys
from pathlib import Path
if "--version" in sys.argv:
    print("2.1.283 (Claude Code)")
    raise SystemExit(0)
assert json.loads(sys.stdin.readline())["request"]["subtype"] == "initialize"
print(json.dumps({"response":{"response":{"models":[{"value":"opus","resolvedModel":"claude-opus-5-5","displayName":"Opus"}]}}}))
''')
    target = tmp_path / 'real-tool'
    target.write_text(f'#!/bin/sh\nname=$(basename "$0")\nif [ "$name" != claude ]; then echo "shim name $name" >&2; exit 9; fi\nexec "$CODEXHUB_E2E_PYTHON" {shlex.quote(str(program))} "$@"\n')
    target.chmod(0o700)
    shim_dir = tmp_path / 'shims'
    shim_dir.mkdir()
    shim = shim_dir / 'claude'
    shim.symlink_to(target)
    script = Path(__file__).resolve().parents[1] / 'src-python' / 'claude_native_models.py'
    result = subprocess.run(
        [sys.executable, str(script), '--claude-bin', str(shim), '--config-dir', str(source)],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, 'CODEXHUB_E2E_PYTHON': sys.executable},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['models'][0]['model'] == 'claude-opus-5-5'


def test_native_discovery_resolves_a_mise_shim_before_isolation(tmp_path):
    if os.name == 'nt':
        return
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'settings.json').write_text('{}')
    program = tmp_path / 'fixture.py'
    program.write_text('''
import json,sys
if "--version" in sys.argv:
    print("2.1.283 (Claude Code)")
    raise SystemExit(0)
assert json.loads(sys.stdin.readline())["request"]["subtype"] == "initialize"
print(json.dumps({"response":{"response":{"models":[{"value":"opus","resolvedModel":"claude-opus-5-5","displayName":"Opus"}]}}}))
''')
    installed = tmp_path / 'installs' / 'claude'
    installed.parent.mkdir()
    installed.write_text(f'#!/bin/sh\nexec "$CODEXHUB_E2E_PYTHON" {shlex.quote(str(program))} "$@"\n')
    installed.chmod(0o700)
    mise = tmp_path / 'mise'
    mise.write_text(f'#!/bin/sh\nif [ "$1" = which ] && [ "$2" = claude ]; then echo {shlex.quote(str(installed))}; exit 0; fi\necho "mise network" >&2; exit 9\n')
    mise.chmod(0o700)
    shim = tmp_path / 'shims' / 'claude'
    shim.parent.mkdir()
    shim.symlink_to(mise)
    script = Path(__file__).resolve().parents[1] / 'src-python' / 'claude_native_models.py'
    result = subprocess.run(
        [sys.executable, str(script), '--claude-bin', str(shim), '--config-dir', str(source)],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, 'CODEXHUB_E2E_PYTHON': sys.executable},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['models'][0]['model'] == 'claude-opus-5-5'
    assert 'mise network' not in result.stderr


def test_native_discovery_failure_does_not_publish_an_empty_catalog(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'src-python' / 'claude_native_models.py'
    result = subprocess.run([sys.executable, str(script), '--claude-bin', str(tmp_path / 'missing'),
                             '--config-dir', str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    assert result.stdout == ''
    assert 'existing configuration was preserved' in result.stderr
