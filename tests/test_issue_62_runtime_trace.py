import json
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "docs" / "evidence" / "issue-62" / "current-codexhub-thread-tool-surface.json"
WIRE_FIXTURE = ROOT / "docs" / "evidence" / "issue-62" / "codexhub-runtime-wire-fixture.json"
AUDIT = ROOT / "docs" / "evidence" / "issue-62" / "read-only-gate-audit.json"
REPLAY_SCRIPT = ROOT / "scripts" / "check-codex-thread-tool-surface.ps1"


def run_replay(case: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPLAY_SCRIPT),
            "-ReplayCase",
            case,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def run_inventory_replay(case: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPLAY_SCRIPT),
            "-InventoryReplayCase",
            case,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_trace_covers_dynamic_tool_exposure_and_sanitizes_session_identity() -> None:
    trace = json.loads(TRACE.read_text(encoding="utf-8"))

    assert trace["schema_version"] == 4
    assert "session_id" not in trace["source"]
    assert len(trace["registered_codex_app_tools"]) == 15
    required = set(trace["required_thread_tools"])
    assert required <= set(trace["registered_codex_app_tools"])
    assert required <= set(trace["dynamic_tool_exposure"]["deferred"])
    assert required <= set(
        trace["planner_gates"]["model_visible_plan"][
            "codex_app_deferred_tools_discoverable_through_tool_search"
        ]
    )

    contributor = trace["dynamic_tool_contributors"][0]
    assert contributor["namespace"] == "codex_app"
    assert contributor["registered_tool_count"] == 15

    direct = {
        tool["name"]
        for tool in contributor["tools"]
        if tool["planner_exposure"] == "Direct"
    }
    deferred = {
        tool["name"]
        for tool in contributor["tools"]
        if tool["planner_exposure"] == "Deferred"
    }
    assert direct == set(trace["dynamic_tool_exposure"]["direct"])
    assert deferred == set(trace["dynamic_tool_exposure"]["deferred"])
    assert all(
        tool["deferLoading"] is True
        for tool in contributor["tools"]
        if tool["planner_exposure"] == "Deferred"
    )

    states = {entry["state"] for entry in trace["exposure_state_catalog"]}
    assert states == {
        "Direct",
        "DirectModelOnly",
        "Deferred",
        "Hidden",
        "hosted-only",
        "host-unavailable",
    }
    snapshot = trace["planner_gates"]["catalog_source"]["read_only_snapshot_validation"]
    assert snapshot["model_entry_id"] == "gpt-5.6-sol"
    assert snapshot["model_entry_supports_search_tool"] is True
    assert len(snapshot["sha256"]) == 64


def test_wire_fixture_keeps_identity_and_unknown_sentinels() -> None:
    wire = json.loads(WIRE_FIXTURE.read_text(encoding="utf-8"))

    assert wire["route"]["upstream_route"] == "official"
    assert "no full request or response body fingerprint" in wire["evidence_limit"][
        "transport_observation"
    ]
    assert "not independent full-wire identity" in wire["evidence_limit"][
        "replay_fixture"
    ]
    assert wire["pre_gateway"]["tool_surface"] == wire["post_gateway"]["tool_surface"]
    assert wire["pre_gateway"]["response"] == wire["post_gateway"]["response"]
    assert wire["pre_gateway"]["choice_controls"] == wire["post_gateway"]["choice_controls"]
    assert wire["pre_gateway"]["choice_controls"]["fixture_kind"] == "contract_sentinel"
    assert wire["exposure_state_tags"] == [
        "Direct",
        "DirectModelOnly",
        "Deferred",
        "Hidden",
        "hosted-only",
        "host-unavailable",
    ]
    assert wire["pre_gateway"]["tool_surface"]["tool_search"]["execution"] == "client"
    assert wire["history"]["captured_source_counts"]["paired_calls"] == 533
    assert wire["history"]["captured_source_counts"]["unpaired_calls"] == 0
    assert wire["history"]["captured_source_counts"]["unpaired_outputs"] == 0
    assert wire["response"]["streaming"]["captured"] is True
    assert wire["response"]["non_streaming"]["captured"] is False
    assert any(
        event.get("tag") == "unknown"
        for event in wire["response"]["streaming"]["events"]
    )
    assert any(
        item.get("tag") == "unknown"
        for item in wire["response"]["non_streaming"]["response_items"]
    )


def test_identity_replay_passes() -> None:
    result = run_replay("identity")

    assert result.returncode == 0, result.stderr
    assert "THREAD_TOOL_SURFACE_COMPLETE" in result.stdout


@pytest.mark.parametrize(
    "case",
    [
        "mutation",
        "deletion",
        "loss",
        "required-set-deletion",
        "required-membership-mutation",
    ],
)
def test_negative_replays_fail_visibly(case: str) -> None:
    result = run_replay(case)

    assert result.returncode == 1
    assert "RECONCILIATION_MISMATCH:" in result.stderr


def test_inventory_identity_replay_passes() -> None:
    result = run_inventory_replay("identity")

    assert result.returncode == 0, result.stderr
    assert "THREAD_TOOL_SURFACE_COMPLETE" in result.stdout
    assert "Inventory replay case: identity" in result.stdout


def test_inventory_check_rejects_generated_artifact_drift(tmp_path: Path) -> None:
    inventory = json.loads(
        (ROOT / "docs/evidence/issue-62/runtime-wire-inventory.json").read_text(
            encoding="utf-8"
        )
    )
    inventory["items"][0]["notes"] = "hand-edited stale note"
    inventory_path = tmp_path / "runtime-wire-inventory.json"
    inventory_path.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPLAY_SCRIPT),
            "-InventoryPath",
            str(inventory_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "generated inventory drift check failed" in result.stderr


def test_inventory_check_rejects_stale_response_identity_pointer(tmp_path: Path) -> None:
    inventory = json.loads(
        (ROOT / "docs/evidence/issue-62/runtime-wire-inventory.json").read_text(
            encoding="utf-8"
        )
    )
    for item in inventory["items"]:
        if item["scope"] == "identity_response_ids":
            item["evidence_source"] = (
                "codexhub-runtime-wire-fixture.json#response.streaming.response_id"
            )
            break
    inventory_path = tmp_path / "runtime-wire-inventory.json"
    inventory_path.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPLAY_SCRIPT),
            "-InventoryPath",
            str(inventory_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "generated inventory drift check failed" in result.stderr


def test_powershell_accepts_pass_non_direct_state_like_python(tmp_path: Path) -> None:
    module_spec = importlib.util.spec_from_file_location(
        "issue_62_runtime_inventory", ROOT / "scripts/build_issue_62_runtime_inventory.py"
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    audit["gate_classification"]["non_direct_states"] = "pass"
    trace_path = tmp_path / TRACE.name
    wire_path = tmp_path / WIRE_FIXTURE.name
    shutil.copyfile(TRACE, trace_path)
    shutil.copyfile(WIRE_FIXTURE, wire_path)
    audit_path = tmp_path / "read-only-gate-audit.json"
    audit_path.write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    inventory = module.build_inventory(
        trace=trace_path,
        wire_fixture=wire_path,
        audit=audit_path,
    )
    inventory_path = tmp_path / "runtime-wire-inventory.json"
    inventory_path.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPLAY_SCRIPT),
            "-TracePath",
            str(trace_path),
            "-WireFixturePath",
            str(wire_path),
            "-AuditPath",
            str(audit_path),
            "-InventoryPath",
            str(inventory_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("case", ["mutation", "deletion", "loss"])
def test_negative_inventory_replays_fail_visibly(case: str) -> None:
    result = run_inventory_replay(case)

    assert result.returncode == 1
    assert "RECONCILIATION_MISMATCH:" in result.stderr
    assert "INVENTORY_IDENTITY_MISMATCH:" in result.stderr
    assert case in result.stderr
