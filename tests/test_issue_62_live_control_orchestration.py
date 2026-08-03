from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import shutil
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
    (tmp_path / "inputs").mkdir()
    files = {
        "candidate_sha_file": tmp_path / "inputs" / "candidate-sha",
        "cli_package_file": tmp_path / "inputs" / "cli-package.tgz",
        "catalog_file": tmp_path / "inputs" / "catalog.json",
        "route_file": tmp_path / "inputs" / "route.json",
    }
    files["candidate_sha_file"].write_text("a" * 40 + "\n", encoding="ascii")
    files["cli_package_file"].write_bytes(b"package")
    files["catalog_file"].write_bytes(b"catalog")
    files["route_file"].write_bytes(b"route")
    return {key: str(value.relative_to(tmp_path)) for key, value in files.items()}


def _plan(tmp_path: Path) -> dict[str, object]:
    binding = _binding_files(tmp_path)
    (tmp_path / "helpers").mkdir()
    (tmp_path / "run").mkdir()
    python_copy = tmp_path / "tools" / "python.exe"
    python_copy.parent.mkdir()
    shutil.copy2(sys.executable, python_copy)
    # A copied Windows interpreter needs its private runtime DLLs beside the
    # executable.  Keep them under the isolated root and expose only that
    # case-local directory through PATH; never rely on the host PATH.
    for runtime_dll in Path(sys.executable).parent.glob("*.dll"):
        shutil.copy2(runtime_dll, python_copy.parent / runtime_dll.name)
    (tmp_path / "helpers" / "cli.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (tmp_path / "helpers" / "sidecar-empty.py").write_text(
        "import time\n"
        # Keep the fixture alive longer than the full Hosted startup window;
        # the harness still bounds and terminates it during cleanup.
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    (tmp_path / "helpers" / "replay.py").write_text(
        "import json, pathlib, sys\n"
        "case, candidate, manifest, output = sys.argv[1:5]\n"
        "path = pathlib.Path(output)\n"
        "path.parent.mkdir(parents=True, exist_ok=True)\n"
        "path.write_text(json.dumps({'schema':'codexhub.issue62.identity-replay.v1', 'case':case, 'candidate_sha':candidate, 'capture_manifest_sha256':manifest, 'wire_replay':True, 'outcome':'accepted' if case == 'identity' else 'rejected'}, sort_keys=True, separators=(',', ':')) + '\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    executable_digest = hashlib.sha256(python_copy.read_bytes()).hexdigest()
    executable_file = str(python_copy.relative_to(tmp_path))
    executable = {
        "executable_file": executable_file,
        "executable_sha256": executable_digest,
    }
    file_digest = lambda name: hashlib.sha256(
        (tmp_path / "helpers" / name).read_bytes()
    ).hexdigest()
    return {
        "schema": "codexhub.issue62.live-control-plan.v1",
        "verification_scope": "authorized_live_control",
        "isolation_root": ".",
        "candidate_identity": {
            "candidate_sha": "a" * 40,
            "cli_version": "0.146.0",
            "cli_package_sha256": _sha("package"),
            "catalog_digest": _sha("catalog"),
            "route_digest": _sha("route"),
        },
        "binding": binding,
        "catalog_model_entry_id": "gpt-5.6-sol",
        "environment": {"PATH": str(python_copy.parent)},
        "planner": {
            "model_visible_plan": "complete",
            "hosted_only_disposition": "Unqualified",
            "unknown_tag_disposition": "Unqualified",
        },
        "cli": {
            "argv": [executable_file, "helpers/cli.py"],
            **executable,
            "argv_file_digests": {"helpers/cli.py": file_digest("cli.py")},
            "cli_version": "0.146.0",
        },
        "sidecars": {
            "pre": {
                "argv": [executable_file, "helpers/sidecar-empty.py"],
                "output_dir": "run/pre",
                **executable,
                "argv_file_digests": {
                    "helpers/sidecar-empty.py": file_digest("sidecar-empty.py")
                },
            },
            "post": {
                "argv": [executable_file, "helpers/sidecar-empty.py"],
                "output_dir": "run/post",
                **executable,
                "argv_file_digests": {
                    "helpers/sidecar-empty.py": file_digest("sidecar-empty.py")
                },
            },
        },
        "controls": [
            {"name": name, "args": [], "capture": {}}
            for name in CONTROL_NAMES
        ],
        "replays": {
            case: {
                "argv": [executable_file, "helpers/replay.py", case, "a" * 40, "0" * 64, f"run/replay-{case}.json"],
                **executable,
                "argv_file_digests": {"helpers/replay.py": file_digest("replay.py")},
                "artifact_file": f"run/replay-{case}.json",
                "case": case,
            }
            for case in ("identity", "mutation", "deletion", "loss")
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
    loaded = load_live_control_plan(plan, isolated_root=tmp_path)
    assert loaded["candidate_identity"]["candidate_sha"] == "a" * 40

    (tmp_path / "inputs" / "route.json").write_bytes(b"changed")
    with pytest.raises(LiveControlValidationError, match="route_binding_mismatch"):
        load_live_control_plan(plan, isolated_root=tmp_path)


def test_live_plan_requires_exactly_eight_control_labels(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan["controls"] = list(plan["controls"])[1:]
    with pytest.raises(LiveControlValidationError, match="control_labels_incomplete"):
        load_live_control_plan(plan, isolated_root=tmp_path)


def test_live_execution_requires_complete_pre_and_post_sidecar_records(tmp_path: Path) -> None:
    result = run_live_control(
        _plan(tmp_path),
        run_root=tmp_path / "run",
        timeout_seconds=30,
        isolated_root=tmp_path,
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
    pre_dir = tmp_path / "inputs" / "pre-records"
    post_dir = tmp_path / "inputs" / "post-records"
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
            "pre_record": f"run/pre-{index}/pre-c{index}.json",
            "post_record": f"run/post-{index}/post-c{index}.json",
        }
    sidecar_script = tmp_path / "helpers" / "sidecar.py"
    sidecar_script.write_text(
        "import pathlib, shutil, sys, time\n"
            "out = pathlib.Path(sys.argv[1]); src = pathlib.Path(sys.argv[2])\n"
            "shutil.copyfile(src, out / pathlib.Path(sys.argv[3]).name)\n"
            "time.sleep(300)\n",
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
                    plan["cli"]["executable_file"],
                    "helpers/sidecar.py",
                    f"run/{hop}-{index}",
                    str(source_paths[index].relative_to(tmp_path)),
                    f"{hop}-c{index}.json",
                ],
                "output_dir": f"run/{hop}-{index}",
                "executable_file": plan["cli"]["executable_file"],
                "executable_sha256": plan["cli"]["executable_sha256"],
                "argv_file_digests": {
                    "helpers/sidecar.py": hashlib.sha256(
                        sidecar_script.read_bytes()
                    ).hexdigest()
                },
            }
            for index in range(8)
        ]
        for index in range(8):
            plan["controls"][index][f"{hop}_record"] = (
                f"run/{hop}-{index}/{hop}-c{index}.json"
            )
    # Re-run plan validation after replacing the sidecar specs.
    result = run_live_control(
        plan,
        run_root=tmp_path / "run",
        timeout_seconds=30,
        isolated_root=tmp_path,
    )
    assert result["completed"] is False
    assert result["ready_for_issue62"] is False
    assert result["status_code"] == "identity_replay_artifact_invalid"
    assert result["cleanup_completed"] is True
    assert result["manifest_reconciled"] is True
    assert not (tmp_path / "run" / "pre-0").exists()
    assert not (tmp_path / "run" / "post-0").exists()
    assert not (tmp_path / "run" / "replay-identity.json").exists()


def test_live_plan_rejects_host_credentials_and_duplicate_capture_paths(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan["environment"] = {"OPENAI_API_KEY": "must-not-pass"}
    with pytest.raises(LiveControlValidationError, match="environment_invalid"):
        load_live_control_plan(plan, isolated_root=tmp_path)

    duplicate_root = tmp_path / "duplicate"
    duplicate_root.mkdir()
    plan = _plan(duplicate_root)
    plan["controls"][1]["pre_record"] = "run/shared.json"
    plan["controls"][0]["pre_record"] = "run/shared.json"
    with pytest.raises(LiveControlValidationError, match="sidecar_capture_incomplete"):
        load_live_control_plan(plan, isolated_root=duplicate_root)
