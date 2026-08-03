from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_issue_62_control_manifest.py"
SPEC = importlib.util.spec_from_file_location("issue_62_control_manifest", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
manifest = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manifest
SPEC.loader.exec_module(manifest)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("ascii")).hexdigest()


def _body(seed: str, *, complete: bool = True) -> dict[str, object]:
    return {
        "bytes": len(seed) + 20,
        "sha256": _digest("body:" + seed) if complete else None,
        "hmac_sha256": _digest("hmac:" + seed) if complete else None,
        "complete": complete,
    }


def _sse(seed: str, terminal: str) -> dict[str, object]:
    return {
        "complete": True,
        "frame_count": 3,
        "frame_bytes": 100 + len(seed),
        "sequence_sha256": _digest("sse:" + seed),
        "sequence_hmac_sha256": _digest("sse-hmac:" + seed),
        "terminal_classes": [terminal],
    }


def _sidecar(name: str, hop: str, *, streaming: bool, status: int, terminal: str) -> dict[str, object]:
    content_type = "event-stream" if streaming else "json"
    record: dict[str, object] = {
        "schema": "codexhub.issue62.live-evidence-lane.v1",
        "verification_scope": "capture_only_not_qualification",
        "capture_id": "c" + ("a" if hop == "pre" else "b") * 32,
        "hop": hop,
        "outcome": "complete",
        "failure": None,
        "status": status,
        "content_type_class": content_type,
        "request": _body(name + ":request"),
        "response": _body(name + ":response"),
        "sse": _sse(name, terminal) if streaming else None,
    }
    return record


def _shape(
    name: str,
    *,
    streaming: bool,
    choice: str | None,
    parallel: bool | None,
    status: str,
    terminal: str,
    events: list[str],
    items: list[str],
    tools: list[str],
    input_types: list[dict[str, object]],
    item_ids: int = 1,
    links: int = 0,
) -> dict[str, object]:
    return {
        "request": {
            "model": "gpt-5.6-sol",
            "stream": streaming,
            "tool_choice": choice,
            "parallel_tool_calls": parallel,
            "input_item_types": input_types,
            "tool_names": tools,
            "additional_tools_present": False,
        },
        "response": {
            "content_type_class": "event-stream" if streaming else "json",
            "status_class": status,
            "terminal": terminal,
            "event_types": events,
            "item_types": sorted(items),
            "unknown_tag_count": 0,
            "response_ref_present": True,
            "item_ref_count": item_ids,
            "call_link_count": links,
        },
    }


def _control(
    name: str,
    *,
    streaming: bool,
    choice: str | None,
    parallel: bool | None,
    status_code: int,
    status_class: str,
    terminal: str,
    events: list[str],
    items: list[str],
    tools: list[str],
    input_types: list[dict[str, object]],
    item_ids: int = 1,
    links: int = 0,
) -> dict[str, object]:
    shapes = _shape(
        name,
        streaming=streaming,
        choice=choice,
        parallel=parallel,
        status=status_class,
        terminal=terminal,
        events=events,
        items=items,
        tools=tools,
        input_types=input_types,
        item_ids=item_ids,
        links=links,
    )
    return {
        "name": name,
        "pre": _sidecar(name, "pre", streaming=streaming, status=status_code, terminal=terminal),
        "post": _sidecar(name, "post", streaming=streaming, status=status_code, terminal=terminal),
        "request_shape": shapes["request"],
        "response_shape": shapes["response"],
        "identity": {
            "request_pair_preserved": True,
            "response_ref_preserved": True,
            "item_refs_preserved": True,
            "call_links_preserved": True,
            "unclassified_core_items": 0,
        },
        "route_identity": {
            "model": "gpt-5.6-sol",
            "upstream": "official",
            "route_mode": "official",
            "behavior_profile": "official_codex_app_http_passthrough",
            "inbound_format": "responses",
            "upstream_format": "responses",
        },
    }


def _controls() -> list[dict[str, object]]:
    text_events = [
        "response.in_progress",
        "response.output_text.delta",
        "response.completed",
    ]
    function_events = [
        "response.in_progress",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_text.done",
        "response.completed",
    ]
    message = [{"type": "message", "count": 1}]
    function_history = [
        {"type": "function_call", "count": 1},
        {"type": "function_call_output", "count": 1},
        {"type": "message", "count": 1},
    ]
    return [
        _control(
            "streaming_text",
            streaming=True,
            choice="auto",
            parallel=False,
            status_code=200,
            status_class="2xx",
            terminal="response.completed",
            events=text_events,
            items=["message"],
            tools=[],
            input_types=message,
        ),
        _control(
            "streaming_function_history",
            streaming=True,
            choice="auto",
            parallel=False,
            status_code=200,
            status_class="2xx",
            terminal="response.completed",
            events=function_events,
            items=["function_call", "function_call_output", "message"],
            tools=["shell_command"],
            input_types=function_history,
            item_ids=3,
            links=1,
        ),
        _control(
            "non_streaming_text",
            streaming=False,
            choice="none",
            parallel=False,
            status_code=200,
            status_class="2xx",
            terminal="json_response",
            events=[],
            items=["message"],
            tools=[],
            input_types=message,
        ),
        _control(
            "choice_auto",
            streaming=True,
            choice="auto",
            parallel=True,
            status_code=200,
            status_class="2xx",
            terminal="response.completed",
            events=text_events,
            items=["message"],
            tools=["shell_command"],
            input_types=message,
        ),
        _control(
            "choice_none",
            streaming=False,
            choice="none",
            parallel=False,
            status_code=200,
            status_class="2xx",
            terminal="json_response",
            events=[],
            items=["message"],
            tools=["shell_command"],
            input_types=message,
        ),
        _control(
            "terminal_success",
            streaming=True,
            choice="auto",
            parallel=False,
            status_code=200,
            status_class="2xx",
            terminal="response.completed",
            events=["response.completed"],
            items=["message"],
            tools=[],
            input_types=message,
        ),
        _control(
            "terminal_error",
            streaming=True,
            choice="auto",
            parallel=False,
            status_code=400,
            status_class="4xx",
            terminal="response.failed",
            events=["response.failed"],
            items=["message"],
            tools=[],
            input_types=message,
        ),
        _control(
            "error_json",
            streaming=False,
            choice="auto",
            parallel=False,
            status_code=400,
            status_class="4xx",
            terminal="json_response",
            events=[],
            items=["message"],
            tools=[],
            input_types=message,
        ),
    ]


def _candidate() -> dict[str, str]:
    return {
        "codexhub_candidate_sha": "d6957f408b63a4cd8ef600e223f8c1ee1084da74",
        "cli_version": "0.146.0",
        "cli_source_commit": None,
        "cli_source_commit_status": "not_published_by_registry",
        "cli_package_sha256": "b" * 64,
        "catalog_snapshot_sha256": "c" * 64,
        "catalog_model_entry_id": "gpt-5.6-sol",
    }


def test_manifest_covers_non_streaming_choice_terminal_and_error_controls() -> None:
    built = manifest.build_manifest(_controls(), candidate_identity=_candidate())

    assert built["schema"] == manifest.SCHEMA
    assert built["verification_scope"] == manifest.SYNTHETIC_SCOPE
    assert built["qualification"]["ready_for_issue62"] is False
    assert {entry["name"] for entry in built["controls"]} == set(manifest.CONTROL_NAMES)

    by_name = {entry["name"]: entry for entry in built["controls"]}
    assert by_name["non_streaming_text"]["request_shape"]["stream"] is False
    assert by_name["choice_auto"]["request_shape"]["parallel_tool_calls"] is True
    assert by_name["choice_none"]["request_shape"]["tool_choice"] == "none"
    assert by_name["terminal_success"]["response_shape"]["terminal"] == "response.completed"
    assert by_name["terminal_error"]["response_shape"]["terminal"] == "response.failed"
    assert by_name["error_json"]["response_shape"]["status_class"] == "4xx"
    assert all(entry["body_equality"]["request"] for entry in built["controls"])
    assert all(entry["body_equality"]["response"] for entry in built["controls"])


def test_manifest_reconcile_and_negative_replays_fail_closed() -> None:
    built = manifest.build_manifest(_controls(), candidate_identity=_candidate())
    assert manifest.reconcile_manifest(built) == {"reconciled": True, "mismatches": []}

    for case in ("mutation", "deletion", "loss"):
        replay = manifest.replay_manifest(built, case)
        report = manifest.reconcile_manifest(replay)
        assert report["reconciled"] is False, case
        assert report["mismatches"], case


def test_manifest_rejects_pre_post_digest_mismatch() -> None:
    controls = _controls()
    controls[0]["post"]["response"]["sha256"] = "f" * 64  # type: ignore[index]

    with pytest.raises(manifest.ManifestValidationError, match="control_body_fingerprint_mismatch"):
        manifest.build_manifest(controls, candidate_identity=_candidate())


def test_manifest_rejects_incomplete_body_and_sse_fingerprints() -> None:
    controls = _controls()
    for side in (controls[0]["pre"], controls[0]["post"]):  # type: ignore[index]
        side["request"]["complete"] = False  # type: ignore[index]
        side["request"]["sha256"] = None  # type: ignore[index]
        side["request"]["hmac_sha256"] = None  # type: ignore[index]
    with pytest.raises(manifest.ManifestValidationError, match="control_request_fingerprint_incomplete"):
        manifest.build_manifest(controls, candidate_identity=_candidate())

    controls = _controls()
    for side in (controls[0]["pre"], controls[0]["post"]):  # type: ignore[index]
        side["sse"]["complete"] = False  # type: ignore[index]
        side["sse"]["sequence_sha256"] = None  # type: ignore[index]
        side["sse"]["sequence_hmac_sha256"] = None  # type: ignore[index]
    with pytest.raises(manifest.ManifestValidationError, match="control_sse_fingerprint_incomplete"):
        manifest.build_manifest(controls, candidate_identity=_candidate())


def test_sidecar_accepts_done_as_a_sanitized_sse_terminal_class() -> None:
    record = _sidecar("done-terminal", "pre", streaming=True, status=200, terminal="done")
    sanitized = manifest.sanitize_sidecar_record(record, expected_hop="pre")
    assert sanitized["sse"]["terminal_classes"] == ["done"]  # type: ignore[index]


def test_candidate_source_provenance_never_accepts_placeholder_hash() -> None:
    unavailable = _candidate()
    unavailable["cli_source_commit"] = "a" * 40
    with pytest.raises(manifest.ManifestValidationError, match="candidate_cli_source_commit_unexpected"):
        manifest.build_manifest(_controls(), candidate_identity=unavailable)

    published = _candidate()
    published["cli_source_commit_status"] = "published"
    with pytest.raises(manifest.ManifestValidationError, match="candidate_cli_source_commit_invalid"):
        manifest.build_manifest(_controls(), candidate_identity=published)

    published["cli_source_commit"] = "a" * 40
    assert manifest.build_manifest(_controls(), candidate_identity=published)["candidate_identity"][
        "cli_source_commit_status"
    ] == "published"


@pytest.mark.parametrize(
    "field",
    [
        ("request_shape", "prompt"),
        ("response_shape", "call_id"),
        ("identity", "item_id"),
    ],
)
def test_manifest_rejects_raw_content_or_wire_identifiers(field: tuple[str, str]) -> None:
    controls = _controls()
    section, key = field
    controls[0][section][key] = "must-not-be-retained"  # type: ignore[index]

    with pytest.raises(manifest.ManifestValidationError):
        manifest.build_manifest(controls, candidate_identity=_candidate())


def test_manifest_cli_emits_sanitized_fixture_and_replay_report(tmp_path: Path) -> None:
    capture_path = tmp_path / "capture.json"
    output_path = tmp_path / "manifest.json"
    capture_path.write_text(
        json.dumps({"catalog_model_entry_id": "gpt-5.6-sol", "controls": _controls()}),
        encoding="utf-8",
    )
    args = [
        "--capture",
        str(capture_path),
        "--out",
        str(output_path),
        "--codexhub-candidate-sha",
        _candidate()["codexhub_candidate_sha"],
        "--cli-version",
        "0.146.0",
        "--cli-source-commit-status",
        "not_published_by_registry",
        "--cli-package-sha256",
        "b" * 64,
        "--catalog-snapshot-sha256",
        "c" * 64,
    ]
    assert manifest.main(args) == 0
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert manifest.reconcile_manifest(written)["reconciled"] is True
    assert manifest.main([*args, "--replay-case", "mutation"]) == 0
    serialized = output_path.read_text(encoding="utf-8")
    for forbidden in ("must-not-be-retained", "capture_id", "call_id", "item_id", "prompt"):
        assert forbidden not in serialized


def test_manifest_reconcile_rejects_top_level_drift_and_qualification_loss() -> None:
    built = manifest.build_manifest(_controls(), candidate_identity=_candidate())

    with_extra = copy.deepcopy(built)
    with_extra["prompt"] = "must-not-be-retained"
    report = manifest.reconcile_manifest(with_extra)
    assert report["reconciled"] is False
    assert "manifest_fields_invalid" in report["mismatches"]

    without_qualification = copy.deepcopy(built)
    without_qualification.pop("qualification")
    report = manifest.reconcile_manifest(without_qualification)
    assert report["reconciled"] is False
    assert any("manifest_fields_invalid" in mismatch for mismatch in report["mismatches"])

    synthetic_ready = copy.deepcopy(built)
    synthetic_ready["qualification"]["ready_for_issue62"] = True
    report = manifest.reconcile_manifest(synthetic_ready)
    assert report["reconciled"] is False
    assert "synthetic_scope_cannot_qualify" in report["mismatches"]

    raw_reason = copy.deepcopy(built)
    raw_reason["qualification"]["reason"] = "https://example.invalid/prompt"
    report = manifest.reconcile_manifest(raw_reason)
    assert report["reconciled"] is False
    assert any("qualification_reason_invalid" in mismatch for mismatch in report["mismatches"])

    raw_identity = copy.deepcopy(built)
    raw_identity["identity_control"]["prompt"] = "must-not-be-retained"
    report = manifest.reconcile_manifest(raw_identity)
    assert report["reconciled"] is False
    assert any("identity_control_fields_invalid" in mismatch for mismatch in report["mismatches"])


def test_manifest_reconcile_recomputes_identity_control_from_controls() -> None:
    forged = copy.deepcopy(manifest.build_manifest(_controls(), candidate_identity=_candidate()))
    forged["controls"][0]["identity"]["response_ref_preserved"] = False
    forged["capture_manifest_sha256"] = manifest._canonical_digest(manifest._manifest_core(forged))
    report = manifest.reconcile_manifest(forged)
    assert report["reconciled"] is False
    assert "identity_control_consistency" in report["mismatches"]
    assert "identity_control_count_consistency" in report["mismatches"]


@pytest.mark.parametrize(
    ("control_name", "mutate", "expected"),
    [
        (
            "choice_auto",
            lambda control: control["request_shape"].update({"tool_choice": "none"}),
            "control_label_choice_auto_invalid",
        ),
        (
            "terminal_success",
            lambda control: control["response_shape"].update({"terminal": "response.failed"}),
            "control_label_terminal_success_invalid",
        ),
        (
            "streaming_function_history",
            lambda control: control["request_shape"].update(
                {"input_item_types": [{"type": "message", "count": 1}]}
            ),
            "control_label_function_history_items_invalid",
        ),
    ],
)
def test_manifest_binds_control_labels_to_contract(
    control_name: str,
    mutate: object,
    expected: str,
) -> None:
    forged = copy.deepcopy(manifest.build_manifest(_controls(), candidate_identity=_candidate()))
    control = next(item for item in forged["controls"] if item["name"] == control_name)
    mutate(control)  # type: ignore[operator]
    forged["capture_manifest_sha256"] = manifest._canonical_digest(manifest._manifest_core(forged))
    report = manifest.reconcile_manifest(forged)
    assert report["reconciled"] is False
    assert any(expected in mismatch for mismatch in report["mismatches"])
