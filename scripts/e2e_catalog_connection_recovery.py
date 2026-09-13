"""Exercise catalog connection/restore through the real CLI and Codex app-server."""
from python_runtime_contract import require_python_313
require_python_313(__file__)

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

from e2e_codex_catalog_roundtrip import official_seed_models, request_model_list, resolve_app_codex
from catalog_sync import MINIMAL_OFFICIAL_MODEL

ROOT = Path(__file__).resolve().parents[1]


def run(codex: Path) -> dict:
    cases = []
    with tempfile.TemporaryDirectory(prefix="codexhub-catalog-recovery-") as temp:
        root = Path(temp)
        seed_home = root / "seed"
        seed_home.mkdir()
        seeds = official_seed_models(request_model_list(codex, seed_home))
        if not seeds:
            raise RuntimeError("isolated Codex model/list returned no seed model")
        for scenario in ("restore", "replace", "delete", "empty", "missing_config"):
            for reconnect in (False, True):
                home = root / f"{scenario}-{reconnect}"
                home.mkdir()
                config, backup = home / "config.toml", home / "backup.toml"
                old, managed = home / "user.json", home / "team's-catalog.json"
                for path, slug in ((old, "catalog-original"), (managed, "catalog-managed")):
                    model = copy.deepcopy(MINIMAL_OFFICIAL_MODEL)
                    model.update(copy.deepcopy(seeds[0]))
                    model.update(slug=slug, id=slug, model=slug, display_name=slug)
                    path.write_text(json.dumps({"models": [model]}), encoding="utf-8")
                original_catalog = old.read_bytes()
                original = "model_catalog_json = " + json.dumps(str(old)) + "\n"
                config.write_text(original, encoding="utf-8")
                env = os.environ.copy()
                env.update(CODEX_HOME=str(home), CODEXHUB_RUNTIME_HOME=str(home / "runtime"),
                           CODEXHUB_ROLLBACK_PROVENANCE_DIR=str(home / "provenance"))

                def overlay(verb):
                    command = [sys.executable, str(ROOT / "src-python/config_overlay.py"), verb,
                               "--config", str(config), "--backup", str(backup)]
                    if verb == "apply":
                        command += ["--catalog", str(managed), "--use-managed-catalog",
                                    "--base-url", "http://127.0.0.1:65534"]
                    completed = subprocess.run(command, env=env, capture_output=True, timeout=30)
                    if completed.returncode:
                        raise RuntimeError(f"catalog {verb} failed in {scenario}")

                overlay("apply")
                models = request_model_list(codex, home)
                ids = {item.get("model") or item.get("id") for item in models}
                if "catalog-managed" not in ids or "catalog-original" in ids:
                    raise AssertionError("explicit connection did not expose the managed catalog")
                expected = str(old)
                if scenario == "missing_config":
                    config.unlink()
                elif scenario != "restore":
                    expected = {"replace": str(home / "edited.json"), "delete": None, "empty": ""}[scenario]
                    lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
                    config.write_text("".join(
                        (("model_catalog_json = " + json.dumps(expected) + "\n") if expected is not None else "")
                        if line.startswith("model_catalog_json =") else line for line in lines
                    ), encoding="utf-8")
                if reconnect:
                    overlay("apply")
                overlay("restore")
                restored = tomllib.loads(config.read_text(encoding="utf-8"))
                if restored.get("model_catalog_json") != expected:
                    raise AssertionError(f"catalog recovery lost user intent in {scenario}")
                if old.read_bytes() != original_catalog:
                    raise AssertionError("user catalog file was modified")
                if scenario in ("restore", "missing_config"):
                    ids = {item.get("model") or item.get("id") for item in request_model_list(codex, home)}
                    if "catalog-original" not in ids or "catalog-managed" in ids:
                        raise AssertionError("Codex did not read the restored catalog")
                cases.append({"scenario": scenario, "reconnect": reconnect, "passed": True})
    return {"schema": "codexhub.catalog-recovery-e2e.v1", "platform": sys.platform,
            "candidate_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "cases": cases, "passed": len(cases) == 10}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-command")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(resolve_app_codex(args.codex_command))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "case_count": len(report["cases"])}))
