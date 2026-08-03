from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from run_issue_62_live_control import (  # noqa: E402
    CONTROL_NAMES,
    LiveControlValidationError,
    load_live_control_plan,
    main,
    run_live_control,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _binding_files(tmp_path: Path) -> dict[str, str]:
    files = {
        "candidate_sha_file": tmp_path / "candidate-sha",
        "cli_package_file": tmp_path / "cli-package.tgz",
        "catalog_file": tmp_path / "catalog.json",
        "route_file": tmp_path / "route.json",
    }
    files["candidate_sha_file"].write_text("a" * 40 + "\n", encoding="ascii")
    files["cli_package_file"].write_bytes(b"package")
    files["catalog_file"].write_bytes(b"catalog")
    files["route_file"].write_bytes(b"route")
    return {key: str(value) for key, value in files.items()}


def _plan(tmp_path: Path) -> dict[str, object]:
    binding = _binding_files(tmp_path)
    return {
        "schema": "codexhub.issue62.live-control-plan.v1",
        "verification_scope": "authorized_live_control",
        "candidate_identity": {
            "candidate_sha": "a" * 40,
            "cli_version": "0.146.0",
            "cli_package_sha256": _sha("package"),
            "catalog_digest": _sha("catalog"),
            "route_digest": _sha("route"),
        },
        "binding": binding,
        "catalog_model_entry_id": "gpt-5.6-sol",
        "cli": {"argv": [sys.executable, "-c", "pass"]},
        "sidecars": {
            "pre": {"argv": [sys.executable, "-c", "pass"], "output_dir": str(tmp_path / "pre")},
            "post": {"argv": [sys.executable, "-c", "pass"], "output_dir": str(tmp_path / "post")},
        },
        "controls": [
            {"name": name, "args": [], "capture": {}}
            for name in CONTROL_NAMES
        ],
        "replays": {
            "identity": {"argv": [sys.executable, "-c", "pass"]},
            "mutation": {"argv": [sys.executable, "-c", "pass"]},
            "deletion": {"argv": [sys.executable, "-c", "pass"]},
            "loss": {"argv": [sys.executable, "-c", "pass"]},
        },
    }


def test_missing_live_plan_uses_fixed_fail_closed_code(tmp_path: Path) -> None:
    with pytest.raises(LiveControlValidationError, match="live_control_plan_missing"):
        load_live_control_plan(tmp_path / "missing.json")


def test_cli_live_mode_reports_missing_plan_without_starting_children(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--run-root", str(tmp_path / "run"), "--enable-live-control"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status_code"] == "live_control_plan_missing"
    assert output["ready_for_issue62"] is False


def test_live_plan_binds_candidate_and_route_catalog_files(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    loaded = load_live_control_plan(plan)
    assert loaded["candidate_identity"]["candidate_sha"] == "a" * 40

    (tmp_path / "route.json").write_bytes(b"changed")
    with pytest.raises(LiveControlValidationError, match="route_binding_mismatch"):
        load_live_control_plan(plan)


def test_live_plan_requires_exactly_eight_control_labels(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan["controls"] = list(plan["controls"])[1:]
    with pytest.raises(LiveControlValidationError, match="control_labels_incomplete"):
        load_live_control_plan(plan)


def test_live_execution_requires_complete_pre_and_post_sidecar_records(tmp_path: Path) -> None:
    result = run_live_control(
        _plan(tmp_path),
        run_root=tmp_path / "run",
        timeout_seconds=30,
    )
    assert result["ready_for_issue62"] is False
    assert result["status_code"] == "sidecar_capture_missing"
    receipt = json.loads((tmp_path / "run" / "cleanup-receipt.json").read_text(encoding="utf-8"))
    assert receipt["cleanup_attempted"] is True
    assert receipt["cleanup_completed"] is True


def test_live_execution_binds_all_controls_and_keeps_qualification_closed(tmp_path: Path) -> None:
    source = importlib.import_module("test_issue_62_control_manifest")
    plan = _plan(tmp_path)
    controls = source._controls()
    pre_dir = Path(plan["sidecars"]["pre"]["output_dir"])
    post_dir = Path(plan["sidecars"]["post"]["output_dir"])
    pre_dir.mkdir()
    post_dir.mkdir()
    payloads: dict[str, Path] = {}
    for index, control in enumerate(controls):
        pre = tmp_path / f"pre-{index}.json"
        post = tmp_path / f"post-{index}.json"
        pre.write_text(json.dumps(control["pre"]), encoding="utf-8")
        post.write_text(json.dumps(control["post"]), encoding="utf-8")
        payloads[f"pre-{index}"] = pre
        payloads[f"post-{index}"] = post
        semantic = {
            key: value
            for key, value in control.items()
            if key not in {"name", "pre", "post"}
        }
        plan["controls"][index] = {
            "name": control["name"],
            "args": [],
            "capture": semantic,
            "pre_record": str(pre_dir / f"pre-c{index}.json"),
            "post_record": str(post_dir / f"post-c{index}.json"),
        }
    sidecar_script = tmp_path / "write-sidecar.py"
    sidecar_script.write_text(
        "import pathlib, shutil, sys, time\n"
        "out = pathlib.Path(sys.argv[1]); src = pathlib.Path(sys.argv[2])\n"
        "shutil.copyfile(src, out / pathlib.Path(sys.argv[3]).name)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    for hop in ("pre", "post"):
        specs = plan["sidecars"][hop]
        source_paths = [payloads[f"{hop}-{index}"] for index in range(8)]
        # One sidecar per control gives deterministic pairing without relying
        # on opaque capture-id ordering.
        plan["sidecars"][hop] = [
            {
                "argv": [
                    sys.executable,
                    str(sidecar_script),
                    str(Path(specs["output_dir"]).parent / f"{hop}-{index}"),
                    str(source_paths[index]),
                    f"{hop}-c{index}.json",
                ],
                "output_dir": str(Path(specs["output_dir"]).parent / f"{hop}-{index}"),
            }
            for index in range(8)
        ]
        for index in range(8):
            plan["controls"][index][f"{hop}_record"] = str(
                Path(plan["sidecars"][hop][index]["output_dir"]) / f"{hop}-c{index}.json"
            )
    # Re-run plan validation after replacing the sidecar specs.
    result = run_live_control(plan, run_root=tmp_path / "run", timeout_seconds=30)
    assert result["completed"] is True
    assert result["ready_for_issue62"] is False
    assert result["replay"] == {
        "identity": "pass",
        "mutation": "pass",
        "deletion": "pass",
        "loss": "pass",
    }
