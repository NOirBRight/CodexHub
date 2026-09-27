from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "prepare_chatgpt_web_runtime.py"


@pytest.mark.skipif(os.name == "nt", reason="uses executable Python-script fixtures")
def test_builder_cli_keeps_selected_bun_on_child_path(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "config", repo / "config")
    shutil.copytree(ROOT / "docs" / "evidence" / "issue-590", repo / "docs" / "evidence" / "issue-590")

    tools = tmp_path / "git-only-path"
    tools.mkdir()
    git = tools / "git"
    git.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"if 'rev-parse' in sys.argv: print('a13cd09950969f43e3b7e25c71fa43efaf5446c5')\n"
        f"elif 'write-tree' in sys.argv: print('641caaf875fcf908dfaa919c242f24fdede26a69')\n"
        "elif 'checkout' in sys.argv:\n"
        "    (Path(sys.argv[sys.argv.index('-C') + 1]) / 'launcher').mkdir()\n",
        encoding="utf-8",
    )
    git.chmod(0o755)

    selected_bun_dir = tmp_path / "selected-bun"
    selected_bun_dir.mkdir()
    bun = selected_bun_dir / "bun"
    bun_log = tmp_path / "resolved-bun-children.log"
    bun.write_text(
        f"#!{sys.executable}\n"
        "import json, shutil, subprocess, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('1.4.0')\n"
        "elif args == ['--nested']:\n"
        "    resolved = shutil.which('bun')\n"
        f"    Path({str(bun_log)!r}).open('a', encoding='utf-8').write(str(resolved) + '\\n')\n"
        "    raise SystemExit(0 if resolved and Path(resolved).resolve() == Path(sys.argv[0]).resolve() else 91)\n"
        "else:\n"
        "    result = subprocess.run(['bun', '--nested'], check=False)\n"
        "    if result.returncode:\n"
        "        raise SystemExit(result.returncode)\n"
        "    if args[:2] == ['run', 'scripts/build-runtime-bundle.ts']:\n"
        "        runtime = Path(args[2])\n"
        "        runtime.mkdir(parents=True, exist_ok=True)\n"
        "        (runtime / 'fixture.js').write_text('fixture', encoding='utf-8')\n"
        "        (runtime / 'manifest.json').write_text(json.dumps({"
        "'appVersion': '6.1.1', 'bunVersion': '1.4.0', 'platform': 'linux', "
        "'arch': 'x64', 'files': ['fixture.js']}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    bun.chmod(0o755)

    environment = os.environ.copy()
    environment["PATH"] = str(tools)
    environment["CODEXHUB_BUN_EXECUTABLE"] = str(bun)
    resource_dir = tmp_path / "resources"
    result = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--repo-root",
            str(repo),
            "--resource-dir",
            str(resource_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    child_paths = bun_log.read_text(encoding="utf-8").splitlines()
    assert child_paths
    assert set(child_paths) == {str(bun)}
    assert (resource_dir / "codexhub-chatgpt-web-runtime-linux-x64.tar.gz").is_file()
    packaged_pin = json.loads((resource_dir / "chatgpt_web_runtime_pin.json").read_text(encoding="utf-8"))
    assert packaged_pin["artifacts"]["linux-x64"]["bundled_only"] is True
