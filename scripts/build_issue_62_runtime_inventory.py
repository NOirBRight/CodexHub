#!/usr/bin/env python3
"""Build the versioned Codex runtime/wire inventory for Issue #62.

The inventory records one per-scope disposition for every taxonomy item the
Codex CLI exposes over the core Responses contract: text, history, streaming
SSE, standard function tools, identity, terminal events, errors, hosted-only
declarations, unknown tagged sentinels, default runtime fields, and the
explicitly-deferred advanced capabilities (Code Mode, ``tool_search``,
Collaboration V2, Chat conversion).

Each item receives one of:

- ``preserved``           : observed and carried through unchanged
- ``reversibly_adapted``  : observed with a documented reversible adaptation
- ``local_consume``       : observed and consumed locally without upstream I/O
- ``Unsupported``         : explicitly out of scope for the beta.1 core contract
- ``Unqualified``        : observed but not qualified by accepted evidence
- ``live_control_required`` : the bounded artifacts do not prove the item; a
  separately authorized live control window must capture it before any
  ``Supported`` claim

The generator reads the existing sanitized Issue #62 evidence artifacts and
never fabricates a ``Supported`` disposition for a gate the artifacts mark
``live_control_required``. It feeds #249 (capability gate) and #66 (Chat
conversion matrix).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
ARTIFACT_KIND = "runtime_wire_inventory"
DEFAULT_CLI_FLOOR = "0.145.0"
DEFAULT_CANDIDATE_CLI_VERSION = None
DEFAULT_CANDIDATE_SOURCE_COMMIT = None

ALLOWED_DISPOSITIONS = (
    "preserved",
    "reversibly_adapted",
    "local_consume",
    "Unsupported",
    "Unqualified",
    "live_control_required",
)

# Scopes that must stay live-control-required until a coordinated live window
# captures real evidence. The bounded read-only audit (read-only-gate-audit)
# and the sanitized fixtures already classify these as live-control-required
# or contract sentinels; the inventory must not flip them.
LIVE_CONTROL_SCOPES = frozenset(
    {
        "core_text_non_streaming",
        "core_sse_terminal_events",
        "core_sse_errors",
        "terminal_events",
        "errors",
        "hosted_only_declarations",
        "unknown_tagged_sentinels",
        "default_runtime_fields",
    }
)

# Advanced capabilities explicitly deferred for beta.1 per #248/#258. They are
# not part of the core Responses contract and must not be advertised.
ADVANCED_UNSUPPORTED_SCOPES = frozenset(
    {
        "code_mode",
        "tool_search",
        "collaboration_v2",
        "chat_conversion",
    }
)

# Every core scope is required in the inventory, even when the current
# evidence only justifies ``live_control_required``.  Keeping this set
# separate from the disposition vocabulary prevents an incomplete fixture from
# being mistaken for a completed beta gate.
CORE_CONTRACT_SCOPES = frozenset(
    {
        "core_text_streaming",
        "core_text_non_streaming",
        "core_history_multiturn",
        "core_history_item_ids",
        "core_history_call_ids",
        "core_sse_streaming_events",
        "core_sse_terminal_events",
        "core_sse_errors",
        "core_function_declaration",
        "core_function_call",
        "core_function_result",
        "core_function_replay",
        "identity_item_call_ids",
        "identity_response_ids",
        "identity_request_ids",
    }
)

KNOWN_SCOPES = (
    CORE_CONTRACT_SCOPES
    | LIVE_CONTROL_SCOPES
    | ADVANCED_UNSUPPORTED_SCOPES
    | {"choice_controls"}
)

# Evidence references are part of the contract, not free-form annotations.
# This map lets reconciliation detect a stale or hand-edited inventory that
# silently points a core claim at an unrelated tool-surface check.
CORE_SCOPE_EVIDENCE = {
    "core_text_streaming": "codexhub-runtime-wire-fixture.json#response.streaming.captured",
    "core_text_non_streaming": "codexhub-runtime-wire-fixture.json#response.non_streaming.captured",
    "core_history_multiturn": "codexhub-runtime-wire-fixture.json#history.captured_source_counts.paired_calls",
    "core_history_item_ids": "codexhub-runtime-wire-fixture.json#history.call_links",
    "core_history_call_ids": "codexhub-runtime-wire-fixture.json#history.required_call_ids",
    "core_sse_streaming_events": "codexhub-runtime-wire-fixture.json#response.streaming.events",
    "core_sse_terminal_events": "read-only-gate-audit.json#gateway_identity_route.observed_sse_event_type_counts",
    "core_sse_errors": "read-only-gate-audit.json#gate_classification.full_pre_post_request_response",
    "core_function_declaration": "codexhub-runtime-wire-fixture.json#pre_gateway.tool_surface.namespaces",
    "core_function_call": "codexhub-runtime-wire-fixture.json#history.call_links",
    "core_function_result": "codexhub-runtime-wire-fixture.json#history.call_links",
    "core_function_replay": "codexhub-runtime-wire-fixture.json#history.call_links",
    "identity_item_call_ids": "codexhub-runtime-wire-fixture.json#history.call_links",
    "identity_response_ids": "codexhub-runtime-wire-fixture.json#response.streaming.response_id",
    "identity_request_ids": "codexhub-runtime-wire-fixture.json#pre_gateway.request_id",
}

REQUIRED_CANDIDATE_FIELDS = (
    "cli_version",
    "source_commit",
    "route_upstream",
    "inbound_format",
    "upstream_format",
    "configured_provider_id",
    "model",
    "catalog_binding",
    "catalog_snapshot_sha256",
    "catalog_model_entry_id",
    "route_behavior_profile",
    "evidence_manifest_sha256",
)


def _sha256_file(path: Path) -> str:
    # Evidence is JSON text; hash its canonical LF representation so a
    # Windows checkout (CRLF) and a Linux checkout (LF) bind to the same
    # artifact bytes.
    canonical = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(canonical).hexdigest()


def _evidence_manifest_sha256(evidence_binding: dict[str, Any]) -> str:
    manifest = "\n".join(
        f"{name}:{evidence_binding[name]['file']}:{evidence_binding[name]['sha256']}"
        for name in sorted(evidence_binding)
    )
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest()


def _version_key(value: str) -> tuple[int, int, int, int, str]:
    """Return a comparable key for Codex's semver-like CLI versions.

    Stable releases sort after prereleases of the same core version.  Build
    metadata is ignored for eligibility, while malformed values fail closed.
    """

    match = re.fullmatch(
        r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
        r"(?P<pre>-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
        value,
    )
    if not match:
        raise ValueError(f"invalid Codex CLI version: {value!r}")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        0 if match.group("pre") else 1,
        match.group("pre") or "",
    )


def _candidate_version_status(candidate: str, floor: str) -> str:
    return "eligible" if _version_key(candidate) >= _version_key(floor) else "legacy_below_floor"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _item(
    scope: str,
    disposition: str,
    evidence_source: str,
    notes: str = "",
) -> dict[str, Any]:
    if disposition not in ALLOWED_DISPOSITIONS:
        raise ValueError(f"disposition {disposition!r} not allowed for {scope}")
    item: dict[str, Any] = {
        "scope": scope,
        "disposition": disposition,
        "evidence_source": evidence_source,
    }
    if notes:
        item["notes"] = notes
    return item


def _gate_is_met(value: Any) -> bool:
    return value in {"observed", "met", "complete", "pass"}


def _gate_is_complete(value: Any) -> bool:
    return value in {"met", "complete"}


def _real_non_streaming_captured(wire: dict[str, Any]) -> bool:
    non_streaming = wire.get("response", {}).get("non_streaming", {})
    return (
        non_streaming.get("captured") is True
        and non_streaming.get("fixture_kind") != "contract_sentinel"
        and bool(non_streaming.get("response_items"))
        and non_streaming.get("request_stream") is False
    )


def _count_unknown_tags(value: Any) -> int:
    if isinstance(value, dict):
        own = 1 if value.get("tag") == "unknown" else 0
        return own + sum(_count_unknown_tags(child) for child in value.values())
    if isinstance(value, list):
        return sum(_count_unknown_tags(child) for child in value)
    return 0


def _unknown_tag_mode_counts(wire: dict[str, Any]) -> tuple[int, int]:
    streaming = _streaming_events(wire)
    non_streaming = wire.get("response", {}).get("non_streaming", {}).get(
        "response_items", []
    )
    return (
        sum(1 for item in streaming if item.get("tag") == "unknown"),
        sum(
            1
            for item in non_streaming
            if isinstance(item, dict) and item.get("tag") == "unknown"
        ),
    )


def _streaming_events(wire: dict[str, Any]) -> list[dict[str, Any]]:
    events = wire.get("response", {}).get("streaming", {}).get("events", [])
    return [event for event in events if isinstance(event, dict)]


def _has_terminal_event(wire: dict[str, Any]) -> bool:
    return any(event.get("event") == "response.completed" for event in _streaming_events(wire))


def _has_error_event(wire: dict[str, Any]) -> bool:
    return any(
        "error" in str(event.get("event", "")).lower()
        or event.get("tag") == "error"
        for event in _streaming_events(wire)
    )


def _wire_identity_replay_status(
    audit: dict[str, Any], *, expected_wire_sha256: str | None = None
) -> str:
    replay = audit.get("wire_identity_replay")
    if not isinstance(replay, dict):
        return "not_captured"
    status = replay.get("status", "not_captured")
    if status not in {"complete", "met"}:
        return status
    cases = replay.get("cases")
    source_hash = replay.get("wire_fixture_sha256")
    if (
        replay.get("fail_closed") is True
        and isinstance(source_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", source_hash)
        and (expected_wire_sha256 is None or source_hash == expected_wire_sha256)
        and isinstance(cases, dict)
        and all(
            isinstance(cases.get(case), dict)
            and cases[case].get("status") in {"complete", "met"}
            and cases[case].get("observed") is True
            and re.fullmatch(
                r"[0-9a-f]{64}", str(cases[case].get("output_sha256", ""))
            )
            for case in ("identity", "mutation", "deletion", "loss")
        )
    ):
        return status
    return "not_captured"


def _sse_identity_status(
    audit: dict[str, Any], *, expected_wire_sha256: str | None = None
) -> str:
    evidence = audit.get("sse_identity")
    if not isinstance(evidence, dict):
        return "not_captured"
    status = evidence.get("status", "not_captured")
    if status not in {"complete", "met"}:
        return status
    source_hash = evidence.get("wire_fixture_sha256")
    pre_hash = evidence.get("pre_stream_sequence_sha256")
    post_hash = evidence.get("post_stream_sequence_sha256")
    if (
        evidence.get("fail_closed") is True
        and isinstance(source_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", source_hash)
        and (expected_wire_sha256 is None or source_hash == expected_wire_sha256)
        and isinstance(pre_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", pre_hash)
        and post_hash == pre_hash
        and evidence.get("event_count", 0) > 0
    ):
        return status
    return "not_captured"


def _classify_core_items(
    wire: dict[str, Any],
    audit: dict[str, Any],
    *,
    wire_fixture_sha256: str | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    streaming_captured = wire.get("response", {}).get("streaming", {}).get("captured") is True
    non_streaming_captured = _real_non_streaming_captured(wire)
    full_wire_gate = audit.get("gate_classification", {}).get(
        "full_pre_post_request_response"
    )
    identity_gate = audit.get("gate_classification", {}).get(
        "zero_unclassified_identity"
    )
    wire_replay_gate = _wire_identity_replay_status(
        audit, expected_wire_sha256=wire_fixture_sha256
    )
    items.append(
        _item(
            "core_text_streaming",
            "preserved" if streaming_captured else "live_control_required",
            "codexhub-runtime-wire-fixture.json#response.streaming.captured",
            "streaming response text observed and preserved across the official Responses route",
        )
    )

    items.append(
        _item(
            "core_text_non_streaming",
            "preserved" if non_streaming_captured else "live_control_required",
            "codexhub-runtime-wire-fixture.json#response.non_streaming.captured",
            "non-streaming response text is only qualified when a real captured fixture is present",
        )
    )

    history_counts = wire.get("history", {}).get("captured_source_counts", {})
    paired = history_counts.get("paired_calls", 0)
    items.append(
        _item(
            "core_history_multiturn",
            "preserved" if paired > 0 else "live_control_required",
            "codexhub-runtime-wire-fixture.json#history.captured_source_counts.paired_calls",
            "multi-turn history with paired call/output rows observed",
        )
    )

    call_links = wire.get("history", {}).get("call_links", [])
    items.append(
        _item(
            "core_history_item_ids",
            "preserved" if call_links else "live_control_required",
            "codexhub-runtime-wire-fixture.json#history.call_links",
            "call_item_id and output_item_id aliases preserved per link",
        )
    )
    items.append(
        _item(
            "core_history_call_ids",
            "preserved" if call_links else "live_control_required",
            "codexhub-runtime-wire-fixture.json#history.required_call_ids",
            "call_id aliases preserved and reconciled with call links",
        )
    )

    sse_events = wire.get("response", {}).get("streaming", {}).get("events", [])
    items.append(
        _item(
            "core_sse_streaming_events",
            "reversibly_adapted" if sse_events else "live_control_required",
            "codexhub-runtime-wire-fixture.json#response.streaming.events",
            "observed SSE event kinds sanitized to redacted payloads with stable event sequence",
        )
    )

    items.append(
        _item(
            "core_sse_terminal_events",
            "preserved"
            if _gate_is_complete(full_wire_gate) and _has_terminal_event(wire)
            else "live_control_required",
            "read-only-gate-audit.json#gateway_identity_route.observed_sse_event_type_counts",
            "terminal event classification requires independently captured full response-body evidence",
        )
    )
    items.append(
        _item(
            "core_sse_errors",
            "preserved"
            if _gate_is_complete(full_wire_gate) and _has_error_event(wire)
            else "live_control_required",
            "read-only-gate-audit.json#gate_classification.full_pre_post_request_response",
            "error classification requires independently fingerprinted pre/post response bodies",
        )
    )

    namespaces = (
        wire.get("pre_gateway", {}).get("tool_surface", {}).get("namespaces", [])
    )
    codex_app_ns = next((ns for ns in namespaces if ns.get("name") == "codex_app"), None)
    items.append(
        _item(
            "core_function_declaration",
            "preserved" if codex_app_ns else "live_control_required",
            "codexhub-runtime-wire-fixture.json#pre_gateway.tool_surface.namespaces",
            "Direct and Deferred function declarations observed for the codex_app namespace",
        )
    )

    call_links_have_function = any(
        link.get("call_type") == "function_call" for link in call_links
    )
    items.append(
        _item(
            "core_function_call",
            "preserved" if call_links_have_function else "live_control_required",
            "codexhub-runtime-wire-fixture.json#history.call_links",
            "function_call items with call_id and arguments aliases observed",
        )
    )
    items.append(
        _item(
            "core_function_result",
            "preserved" if call_links_have_function else "live_control_required",
            "codexhub-runtime-wire-fixture.json#history.call_links",
            "function_call_output items with call_id and output aliases observed",
        )
    )
    items.append(
        _item(
            "core_function_replay",
            "preserved"
            if _gate_is_complete(full_wire_gate)
            and _gate_is_complete(identity_gate)
            and _gate_is_complete(wire_replay_gate)
            else "live_control_required",
            "codexhub-runtime-wire-fixture.json#history.call_links",
            "call/result links are present, but the bounded tool-membership replay does not prove a complete function replay across the real wire",
        )
    )

    items.append(
        _item(
            "identity_item_call_ids",
            "preserved" if call_links else "live_control_required",
            "codexhub-runtime-wire-fixture.json#history.call_links",
            "item/call id aliases preserved across pre/post-Gateway replay",
        )
    )
    items.append(
        _item(
            "identity_response_ids",
            "preserved" if streaming_captured else "live_control_required",
            "codexhub-runtime-wire-fixture.json#response.streaming.response_id",
            "response_id aliases preserved across pre/post-Gateway replay",
        )
    )
    pre_request_id = wire.get("pre_gateway", {}).get("request_id")
    post_request_id = wire.get("post_gateway", {}).get("request_id")
    items.append(
        _item(
            "identity_request_ids",
            "preserved"
            if pre_request_id and post_request_id and pre_request_id == post_request_id
            else "live_control_required",
            "codexhub-runtime-wire-fixture.json#pre_gateway.request_id",
            "request_id aliases preserved and equal across pre/post-Gateway replay",
        )
    )

    return items


def _classify_live_control_items(
    wire: dict[str, Any],
    audit: dict[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    gate_classification = audit.get("gate_classification", {})
    full_wire_gate = gate_classification.get("full_pre_post_request_response")
    non_direct_gate = gate_classification.get("non_direct_states")
    non_streaming_captured = _real_non_streaming_captured(wire)
    unknown_tag_count = _count_unknown_tags(wire)
    unknown_stream_count, unknown_non_stream_count = _unknown_tag_mode_counts(wire)

    choice_captured = (
        wire.get("pre_gateway", {}).get("choice_controls", {}).get("captured") is True
    )
    audit_choice = audit.get("gate_classification", {}).get("choice_controls")
    items.append(
        _item(
            "choice_controls",
            "live_control_required",
            "codexhub-runtime-wire-fixture.json#pre_gateway.choice_controls.captured=false",
            "choice controls are a contract sentinel in the wire fixture; the bounded audit observes tool_choice/parallel_tool_calls but full pre/post choice identity requires a live control",
        )
        if not choice_captured
        else _item(
            "choice_controls",
            "preserved",
            "codexhub-runtime-wire-fixture.json#pre_gateway.choice_controls.captured=true",
            f"choice controls captured and preserved across pre/post-Gateway replay (audit gate={audit_choice!r})",
        )
    )

    items.append(
        _item(
            "terminal_events",
            "preserved"
            if _gate_is_complete(full_wire_gate) and _has_terminal_event(wire)
            else "live_control_required",
            "read-only-gate-audit.json#gate_classification.full_pre_post_request_response=live_control_required",
            "terminal event classification requires a real captured response body fingerprint",
        )
    )
    items.append(
        _item(
            "errors",
            "preserved"
            if _gate_is_complete(full_wire_gate) and _has_error_event(wire)
            else "live_control_required",
            "read-only-gate-audit.json#gate_classification.full_pre_post_request_response=live_control_required",
            "error classification requires independently captured pre/post response evidence",
        )
    )

    items.append(
        _item(
            "hosted_only_declarations",
            "preserved" if _gate_is_met(non_direct_gate) else "live_control_required",
            "current-codexhub-thread-tool-surface.json#exposure_state_catalog",
            "hosted-only and host-unavailable are tagged reconciliation sentinels; no runtime-observed host binding",
        )
    )

    items.append(
        _item(
            "unknown_tagged_sentinels",
            "preserved"
            if unknown_tag_count > 0
            and unknown_stream_count > 0
            and unknown_non_stream_count > 0
            and _gate_is_complete(full_wire_gate)
            and non_streaming_captured
            else "live_control_required",
            "codexhub-runtime-wire-fixture.json#response.streaming.events.tag=unknown",
            "unknown tagged sentinels are preserved as opaque replay sentinels; live runtime dispositions require a real unknown item",
        )
    )

    items.append(
        _item(
            "default_runtime_fields",
            "preserved" if _gate_is_complete(full_wire_gate) else "live_control_required",
            "read-only-gate-audit.json#model_visible_request_plan.top_level_field_presence",
            "default runtime field classification requires an independently captured full request body",
        )
    )

    return items


def _classify_advanced_items(
    trace: dict[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    items.append(
        _item(
            "code_mode",
            "Unsupported",
            "issue-248#beta.1-scope",
            "Code Mode is beta.2 scope (#251/#279/#280); not advertised in beta.1",
        )
    )

    tool_search = (
        trace.get("planner_gates", {})
        .get("caller_request", {})
        .get("additional_tools_contains_tool_search")
    )
    items.append(
        _item(
            "tool_search",
            "Unqualified" if tool_search is True else "Unsupported",
            "current-codexhub-thread-tool-surface.json#planner_gates.caller_request",
            "client-executed tool_search is observed in the caller request but model non-selection is owned by #63; beta.2 scope",
        )
    )

    multi_agent = trace.get("source", {}).get("multi_agent_version")
    items.append(
        _item(
            "collaboration_v2",
            "Unsupported",
            "issue-248#beta.3-scope",
            f"Collaboration V2 is beta.3 scope (#64/#282/#283); observed multi_agent_version={multi_agent!r} is not advertised in beta.1",
        )
    )

    items.append(
        _item(
            "chat_conversion",
            "Unsupported",
            "issue-248#beta.4-scope",
            "Chat conversion is beta.4 scope (#66/#253/#254); not advertised in beta.1",
        )
    )

    return items


def _build_identity_control(
    items: list[dict[str, Any]], *, unknown_tagged_source_count: int
) -> dict[str, Any]:
    by_scope = {item.get("scope"): item for item in items}
    core_allowed = {"preserved", "reversibly_adapted", "live_control_required"}
    unclassified = []
    for scope in sorted(CORE_CONTRACT_SCOPES):
        item = by_scope.get(scope)
        if (
            item is None
            or item.get("disposition") not in core_allowed
            or item.get("evidence_source") != CORE_SCOPE_EVIDENCE[scope]
        ):
            unclassified.append(scope)
    return {
        "unclassified_core_items": len(unclassified),
        "unclassified_scopes": unclassified,
        "fail_closed": True,
        "replay_cases": ["identity", "mutation", "deletion", "loss"],
        "unknown_tagged_source_count": unknown_tagged_source_count,
    }


def _build_qualification(
    items: list[dict[str, Any]],
    candidate_version_status: str,
    *,
    trace: dict[str, Any],
    wire: dict[str, Any],
    audit: dict[str, Any],
    wire_fixture_sha256: str | None = None,
) -> dict[str, Any]:
    live_control_scopes = sorted(
        item["scope"]
        for item in items
        if item["disposition"] == "live_control_required"
    )
    unqualified_core_scopes = sorted(
        item["scope"]
        for item in items
        if item["scope"] in CORE_CONTRACT_SCOPES
        and item["disposition"] in {"Unqualified", "Unsupported"}
    )
    blocking_scopes = sorted(set(live_control_scopes) | set(unqualified_core_scopes))
    candidate_version_eligible = candidate_version_status == "eligible"
    evidence_gates = {
        "complete_model_visible_plan": trace.get("capture_coverage", {})
        .get("complete_model_visible_plan", {})
        .get("status", "unknown"),
        "clean_cold_start_current_binding": trace.get("capture_coverage", {})
        .get("clean_cold_start_current_binding", {})
        .get("status", "unknown"),
        "full_pre_post_request_response": audit.get("gate_classification", {}).get(
            "full_pre_post_request_response", "unknown"
        ),
        "full_request_fingerprint": trace.get("gateway_observability", {}).get(
            "full_request_body_fingerprint", "unknown"
        ),
        "full_response_fingerprint": trace.get("gateway_observability", {}).get(
            "full_response_body_fingerprint", "unknown"
        ),
        "sse_identity": (
            _sse_identity_status(audit, expected_wire_sha256=wire_fixture_sha256)
        ),
        "terminal_events": (
            "met"
            if _gate_is_complete(audit.get("gate_classification", {}).get(
                "full_pre_post_request_response"
            ))
            and _has_terminal_event(wire)
            else "not_captured"
        ),
        "error_events": (
            "met"
            if _gate_is_complete(audit.get("gate_classification", {}).get(
                "full_pre_post_request_response"
            ))
            and _has_error_event(wire)
            else "not_captured"
        ),
        "non_streaming": audit.get("gate_classification", {}).get(
            "non_streaming", "unknown"
        ),
        "non_streaming_fixture": (
            "met"
            if _real_non_streaming_captured(wire)
            else "not_captured"
        ),
        "identity_replay": audit.get("gate_classification", {}).get(
            "zero_unclassified_identity", "unknown"
        ),
        "wire_identity_replay": _wire_identity_replay_status(
            audit, expected_wire_sha256=wire_fixture_sha256
        ),
    }
    accepted_gate_statuses = {
        "complete_model_visible_plan": {"complete"},
        "clean_cold_start_current_binding": {"complete", "pass"},
        "full_pre_post_request_response": {"complete", "met"},
        "full_request_fingerprint": {"captured", "complete", "met"},
        "full_response_fingerprint": {"captured", "complete", "met"},
        "sse_identity": {"captured", "complete", "met"},
        "terminal_events": {"complete", "met"},
        "error_events": {"complete", "met"},
        "non_streaming": {"complete", "met"},
        "non_streaming_fixture": {"captured", "complete", "met"},
        "identity_replay": {"complete", "met"},
        "wire_identity_replay": {"complete", "met"},
    }
    blocking_gates = sorted(
        gate
        for gate, status in evidence_gates.items()
        if status not in accepted_gate_statuses[gate]
    )
    return {
        "candidate_version_status": candidate_version_status,
        "candidate_version_eligible": candidate_version_eligible,
        "blocking_scopes": blocking_scopes,
        "evidence_gates": evidence_gates,
        "blocking_gates": blocking_gates,
        "ready_for_beta1": candidate_version_eligible
        and not blocking_scopes
        and not blocking_gates,
    }


def _validate_candidate_binding(
    *,
    trace_data: dict[str, Any],
    wire_data: dict[str, Any],
    cli_version_floor: str,
    candidate_cli_version: str | None,
    candidate_source_commit: str | None,
) -> tuple[dict[str, Any], str]:
    source = trace_data.get("source", {})
    planner_gates = trace_data.get("planner_gates", {})
    trace_cli_version = source.get("cli_version")
    trace_source_commit = planner_gates.get("source_commit")
    if not trace_cli_version or not trace_source_commit:
        raise ValueError("trace evidence is missing source.cli_version or planner_gates.source_commit")

    if candidate_cli_version is None:
        candidate_cli_version = trace_cli_version
    elif candidate_cli_version != trace_cli_version:
        raise ValueError(
            "candidate CLI version does not match trace evidence: "
            f"requested={candidate_cli_version!r} observed={trace_cli_version!r}"
        )

    if candidate_source_commit is None:
        candidate_source_commit = trace_source_commit
    elif candidate_source_commit != trace_source_commit:
        raise ValueError(
            "candidate source commit does not match trace evidence: "
            f"requested={candidate_source_commit!r} observed={trace_source_commit!r}"
        )

    if not re.fullmatch(r"[0-9a-f]{40}", candidate_source_commit):
        raise ValueError(f"candidate source commit is not a 40-character SHA-1: {candidate_source_commit!r}")

    trace_route = trace_data.get("gateway_route", {})
    wire_route = wire_data.get("route", {})
    route_fields = (
        ("route_upstream", wire_route.get("upstream_route"), trace_route.get("upstream")),
        ("inbound_format", wire_route.get("inbound_format"), trace_route.get("inbound_format")),
        ("upstream_format", wire_route.get("upstream_format"), trace_route.get("upstream_format")),
    )
    for name, wire_value, trace_value in route_fields:
        if not wire_value or wire_value != trace_value:
            raise ValueError(
                f"candidate route field {name} is not consistently bound: "
                f"wire={wire_value!r} trace={trace_value!r}"
            )

    profile_fields = (
        "catalog_binding",
        "behavior_profile",
        "route_mode",
        "wire_format_adapter",
        "codex_semantic_adapter",
        "repair_policy",
    )
    for field in profile_fields:
        wire_value = wire_route.get(field)
        trace_value = trace_route.get(field)
        if not wire_value or wire_value != trace_value:
            raise ValueError(
                f"candidate route profile field {field} is not consistently bound: "
                f"wire={wire_value!r} trace={trace_value!r}"
            )

    trace_provider = source.get("configured_provider_id")
    wire_provider = wire_route.get("configured_provider_id")
    if not trace_provider or wire_provider != trace_provider:
        raise ValueError(
            "candidate provider binding is inconsistent: "
            f"wire={wire_provider!r} trace={trace_provider!r}"
        )
    wire_provenance = wire_data.get("provenance", {})
    if (
        wire_provenance.get("cli_version") != trace_cli_version
        or wire_provenance.get("source_commit") != trace_source_commit
        or wire_provenance.get("capture_id") != source.get("capture_id")
    ):
        raise ValueError("wire provenance is not bound to the trace candidate")
    catalog_snapshot = (
        trace_data.get("planner_gates", {})
        .get("catalog_source", {})
        .get("read_only_snapshot_validation", {})
    )
    if (
        wire_route.get("catalog_snapshot_sha256") != catalog_snapshot.get("sha256")
        or wire_route.get("catalog_model_entry_id") != catalog_snapshot.get("model_entry_id")
        or wire_route.get("catalog_model_supports_search_tool")
        is not catalog_snapshot.get("model_entry_supports_search_tool")
    ):
        raise ValueError("wire catalog snapshot is not bound to the trace catalog evidence")
    trace_model = source.get("model")
    pre_wire_model = wire_data.get("pre_gateway", {}).get("model")
    post_wire_model = wire_data.get("post_gateway", {}).get("model")
    if not trace_model or pre_wire_model != trace_model or post_wire_model != trace_model:
        raise ValueError(
            "candidate model binding is inconsistent: "
            f"pre_wire={pre_wire_model!r} post_wire={post_wire_model!r} trace={trace_model!r}"
        )

    status = _candidate_version_status(candidate_cli_version, cli_version_floor)
    identity = {
        "cli_version": candidate_cli_version,
        "source_commit": candidate_source_commit,
        "codex_source_commit": candidate_source_commit,
        "route_upstream": wire_route["upstream_route"],
        "inbound_format": wire_route["inbound_format"],
        "upstream_format": wire_route["upstream_format"],
        "configured_provider_id": trace_provider,
        "model": trace_model,
        "catalog_binding": wire_route["catalog_binding"],
        "catalog_snapshot_sha256": wire_route["catalog_snapshot_sha256"],
        "catalog_model_entry_id": wire_route["catalog_model_entry_id"],
        "route_behavior_profile": trace_route.get("behavior_profile"),
    }
    return identity, status


def build_inventory(
    *,
    trace: Path,
    wire_fixture: Path,
    audit: Path,
    cli_version_floor: str = DEFAULT_CLI_FLOOR,
    candidate_cli_version: str | None = DEFAULT_CANDIDATE_CLI_VERSION,
    candidate_source_commit: str | None = DEFAULT_CANDIDATE_SOURCE_COMMIT,
) -> dict[str, Any]:
    trace_data = _load_json(trace)
    wire_data = _load_json(wire_fixture)
    audit_data = _load_json(audit)
    wire_fixture_sha256 = _sha256_file(wire_fixture)
    if (
        trace_data.get("schema_version") != 4
        or wire_data.get("schema_version") != 1
        or wire_data.get("fixture_kind") != "sanitized_artifact_backed_replay"
        or audit_data.get("schema_version") != 1
        or audit_data.get("capture_kind") != "sanitized_bounded_read_only_audit"
    ):
        raise ValueError("Issue #62 evidence schema identity is invalid")

    items: list[dict[str, Any]] = []
    items.extend(
        _classify_core_items(
            wire_data, audit_data, wire_fixture_sha256=wire_fixture_sha256
        )
    )
    items.extend(_classify_live_control_items(wire_data, audit_data))
    items.extend(_classify_advanced_items(trace_data))

    seen: set[str] = set()
    for item in items:
        if item["scope"] in seen:
            raise ValueError(f"duplicate scope: {item['scope']}")
        seen.add(item["scope"])

    unknown_tagged_source_count = _count_unknown_tags(wire_data)
    unknown_stream_count, unknown_non_stream_count = _unknown_tag_mode_counts(wire_data)
    if unknown_tagged_source_count == 0:
        raise ValueError("wire evidence contains no unknown-tag sentinel to classify")
    if unknown_stream_count == 0 or unknown_non_stream_count == 0:
        raise ValueError(
            "wire evidence must contain unknown-tag sentinels in both response modes"
        )
    identity_control = _build_identity_control(
        items, unknown_tagged_source_count=unknown_tagged_source_count
    )
    candidate_identity, candidate_version_status = _validate_candidate_binding(
        trace_data=trace_data,
        wire_data=wire_data,
        cli_version_floor=cli_version_floor,
        candidate_cli_version=candidate_cli_version,
        candidate_source_commit=candidate_source_commit,
    )
    evidence_binding = {
        "trace": {"file": trace.name, "sha256": _sha256_file(trace)},
        "wire_fixture": {
            "file": wire_fixture.name,
            "sha256": wire_fixture_sha256,
        },
        "audit": {"file": audit.name, "sha256": _sha256_file(audit)},
    }
    candidate_identity["evidence_manifest_sha256"] = _evidence_manifest_sha256(
        evidence_binding
    )
    qualification = _build_qualification(
        items,
        candidate_version_status,
        trace=trace_data,
        wire=wire_data,
        audit=audit_data,
        wire_fixture_sha256=wire_fixture_sha256,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": ARTIFACT_KIND,
        "cli_version_floor": cli_version_floor,
        "candidate_identity": candidate_identity,
        "qualification": qualification,
        "disposition_vocabulary": list(ALLOWED_DISPOSITIONS),
        "items": items,
        "identity_control": identity_control,
        "feeds": {
            "capability_gate": "#249",
            "chat_conversion_matrix": "#66",
        },
        "evidence_sources": {
            "trace": trace.name,
            "wire_fixture": wire_fixture.name,
            "audit": audit.name,
        },
        "evidence_binding": evidence_binding,
    }


def replay_inventory(inventory: dict[str, Any], case: str) -> dict[str, Any]:
    """Return a mutated copy of the inventory for a negative replay case.

    - ``identity``     : unchanged
    - ``mutation``     : change a core item's disposition to a disallowed value
    - ``deletion``     : remove a core item
    - ``loss``         : drop a candidate_identity field
    """
    clone = copy.deepcopy(inventory)
    if case == "identity":
        return clone

    if case == "mutation":
        for item in clone["items"]:
            if item["scope"] == "core_history_call_ids":
                item["disposition"] = "Supported"
                clone["identity_control"]["unclassified_core_items"] = (
                    clone["identity_control"].get("unclassified_core_items", 0) + 1
                )
                clone["identity_control"].setdefault("unclassified_scopes", []).append(
                    "core_history_call_ids"
                )
                return clone
        return clone

    if case == "deletion":
        clone["items"] = [
            item for item in clone["items"] if item["scope"] != "core_text_streaming"
        ]
        return clone

    if case == "loss":
        clone["candidate_identity"].pop("route_upstream", None)
        return clone

    raise ValueError(f"unknown replay case: {case!r}")


def reconcile_inventory(
    inventory: dict[str, Any], *, evidence_root: Path | None = None
) -> dict[str, Any]:
    """Reconcile an inventory (possibly mutated) against the identity contract."""
    mismatches: list[str] = []

    if inventory.get("schema_version") != SCHEMA_VERSION:
        mismatches.append("inventory schema_version is invalid")
    if inventory.get("artifact_kind") != ARTIFACT_KIND:
        mismatches.append("inventory artifact_kind is invalid")
    if inventory.get("disposition_vocabulary") != list(ALLOWED_DISPOSITIONS):
        mismatches.append("inventory disposition_vocabulary is invalid")

    items = inventory.get("items", [])
    seen_scopes: set[str] = set()
    for item in items:
        scope = item.get("scope")
        if not scope:
            mismatches.append("item missing scope")
            continue
        if scope in seen_scopes:
            mismatches.append(f"duplicate scope: {scope}")
            continue
        seen_scopes.add(scope)
        if scope not in KNOWN_SCOPES:
            mismatches.append(f"mutation: unknown scope {scope}")
        disposition = item.get("disposition")
        if disposition not in ALLOWED_DISPOSITIONS:
            mismatches.append(
                f"mutation: {scope}: disposition {disposition!r} not in allowed vocabulary"
            )
        expected_evidence = CORE_SCOPE_EVIDENCE.get(scope)
        if expected_evidence and item.get("evidence_source") != expected_evidence:
            mismatches.append(
                f"mutation: {scope}: evidence_source {item.get('evidence_source')!r} "
                f"does not match required {expected_evidence!r}"
            )
        if scope in CORE_CONTRACT_SCOPES and disposition in {"Unsupported", "Unqualified"}:
            mismatches.append(
                f"mutation: {scope}: core disposition {disposition!r} cannot replace "
                "preserved, reversibly_adapted, or live_control_required"
            )

    required_scopes = (
        CORE_CONTRACT_SCOPES
        | LIVE_CONTROL_SCOPES
        | ADVANCED_UNSUPPORTED_SCOPES
        | {"choice_controls"}
    )
    missing = sorted(required_scopes - seen_scopes)
    if missing:
        mismatches.append(f"deletion: missing scopes {missing}")

    candidate_identity = inventory.get("candidate_identity", {})
    for field in REQUIRED_CANDIDATE_FIELDS:
        if field not in candidate_identity:
            mismatches.append(f"loss: candidate_identity.{field} is missing")

    qualification = inventory.get("qualification", {})
    candidate_status = qualification.get("candidate_version_status")
    if candidate_status not in {"eligible", "legacy_below_floor"}:
        mismatches.append("qualification.candidate_version_status is invalid")
    candidate_eligible = qualification.get("candidate_version_eligible")
    if candidate_eligible is not (candidate_status == "eligible"):
        mismatches.append(
            "qualification.candidate_version_eligible does not match candidate version status"
        )
    actual_blocking_scopes = sorted(
        set(
            item.get("scope")
            for item in items
            if item.get("disposition") == "live_control_required"
        )
        | {
            item.get("scope")
            for item in items
            if item.get("scope") in CORE_CONTRACT_SCOPES
            and item.get("disposition") in {"Unqualified", "Unsupported"}
        }
    )
    if qualification.get("blocking_scopes") != actual_blocking_scopes:
        mismatches.append(
            "qualification.blocking_scopes does not match observed blocking dispositions"
        )
    expected_status = None
    try:
        expected_status = _candidate_version_status(
            str(candidate_identity.get("cli_version", "")),
            str(inventory.get("cli_version_floor", "")),
        )
    except ValueError:
        mismatches.append("candidate_identity.cli_version or cli_version_floor is invalid")
    if expected_status and candidate_status != expected_status:
        mismatches.append(
            "qualification.candidate_version_status does not match the CLI floor"
        )

    evidence_gates = qualification.get("evidence_gates", {})
    expected_gate_keys = {
        "complete_model_visible_plan",
        "clean_cold_start_current_binding",
        "full_pre_post_request_response",
        "full_request_fingerprint",
        "full_response_fingerprint",
        "sse_identity",
        "terminal_events",
        "error_events",
        "non_streaming",
        "non_streaming_fixture",
        "identity_replay",
        "wire_identity_replay",
    }
    if set(evidence_gates) != expected_gate_keys:
        mismatches.append("qualification.evidence_gates has an unexpected key set")
    accepted_gate_statuses = {
        "complete_model_visible_plan": {"complete"},
        "clean_cold_start_current_binding": {"complete", "pass"},
        "full_pre_post_request_response": {"complete", "met"},
        "full_request_fingerprint": {"captured", "complete", "met"},
        "full_response_fingerprint": {"captured", "complete", "met"},
        "sse_identity": {"captured", "complete", "met"},
        "terminal_events": {"complete", "met"},
        "error_events": {"complete", "met"},
        "non_streaming": {"complete", "met"},
        "non_streaming_fixture": {"captured", "complete", "met"},
        "identity_replay": {"complete", "met"},
        "wire_identity_replay": {"complete", "met"},
    }
    actual_blocking_gates = sorted(
        gate
        for gate, allowed in accepted_gate_statuses.items()
        if evidence_gates.get(gate) not in allowed
    )
    if qualification.get("blocking_gates") != actual_blocking_gates:
        mismatches.append(
            "qualification.blocking_gates does not match evidence gate statuses"
        )
    expected_ready = (
        candidate_eligible is True
        and not actual_blocking_scopes
        and not actual_blocking_gates
    )
    if qualification.get("ready_for_beta1") is not expected_ready:
        mismatches.append(
            "qualification.ready_for_beta1 is inconsistent with candidate eligibility and blockers"
        )

    evidence_binding = inventory.get("evidence_binding", {})
    for name in ("trace", "wire_fixture", "audit"):
        entry = evidence_binding.get(name, {})
        if not isinstance(entry, dict) or not entry.get("file") or not re.fullmatch(
            r"[0-9a-f]{64}", str(entry.get("sha256", ""))
        ):
            mismatches.append(f"loss: evidence_binding.{name} is missing or malformed")

    if evidence_binding and all(
        isinstance(evidence_binding.get(name), dict)
        for name in ("trace", "wire_fixture", "audit")
    ):
        manifest = _evidence_manifest_sha256(evidence_binding)
        if candidate_identity.get("evidence_manifest_sha256") != manifest:
            mismatches.append("loss: candidate_identity.evidence_manifest_sha256 is stale")

    if evidence_root is not None and not mismatches:
        bound_paths: dict[str, Path] = {}
        for name in ("trace", "wire_fixture", "audit"):
            entry = evidence_binding[name]
            relative_name = str(entry["file"])
            relative_path = Path(relative_name)
            if relative_path.is_absolute() or relative_path.name != relative_name:
                mismatches.append(
                    f"loss: evidence binding file must be a basename: {relative_name}"
                )
                continue
            path = evidence_root / relative_path
            bound_paths[name] = path
            if not path.is_file():
                mismatches.append(f"loss: evidence binding file does not exist: {path}")
                continue
            if _sha256_file(path) != entry["sha256"]:
                mismatches.append(f"mutation: evidence binding hash mismatch for {name}")
        if not mismatches:
            trace_data = _load_json(bound_paths["trace"])
            wire_data = _load_json(bound_paths["wire_fixture"])
            audit_data = _load_json(bound_paths["audit"])
            if (
                trace_data.get("schema_version") != 4
                or wire_data.get("schema_version") != 1
                or wire_data.get("fixture_kind") != "sanitized_artifact_backed_replay"
                or audit_data.get("schema_version") != 1
                or audit_data.get("capture_kind") != "sanitized_bounded_read_only_audit"
            ):
                mismatches.append("mutation: bound evidence schema identity is invalid")
                return {"reconciled": False, "mismatches": mismatches}
            try:
                expected_identity, expected_status_from_evidence = _validate_candidate_binding(
                    trace_data=trace_data,
                    wire_data=wire_data,
                    cli_version_floor=str(inventory.get("cli_version_floor", "")),
                    candidate_cli_version=candidate_identity.get("cli_version"),
                    candidate_source_commit=candidate_identity.get("source_commit"),
                )
            except (TypeError, ValueError) as exc:
                mismatches.append(f"mutation: candidate evidence binding failed: {exc}")
            else:
                for field, expected in expected_identity.items():
                    if candidate_identity.get(field) != expected:
                        mismatches.append(
                            f"mutation: candidate_identity.{field} does not match evidence"
                        )
                if candidate_status != expected_status_from_evidence:
                    mismatches.append(
                        "qualification candidate status does not match bound evidence"
                    )
                expected_qualification = _build_qualification(
                    items,
                    expected_status_from_evidence,
                    trace=trace_data,
                    wire=wire_data,
                    audit=audit_data,
                    wire_fixture_sha256=evidence_binding["wire_fixture"]["sha256"],
                )
                for field in (
                    "evidence_gates",
                    "blocking_gates",
                    "blocking_scopes",
                    "ready_for_beta1",
                ):
                    if qualification.get(field) != expected_qualification[field]:
                        mismatches.append(
                            f"mutation: qualification.{field} does not match bound evidence"
                        )

    identity_control = inventory.get("identity_control", {})
    if identity_control.get("fail_closed") is not True:
        mismatches.append("identity_control.fail_closed must be true")
    if identity_control.get("replay_cases") != [
        "identity",
        "mutation",
        "deletion",
        "loss",
    ]:
        mismatches.append("identity_control.replay_cases are invalid")

    unclassified = identity_control.get("unclassified_core_items")
    if not isinstance(unclassified, int) or unclassified < 0:
        mismatches.append("identity_control.unclassified_core_items is invalid")
    else:
        actual_unclassified = sum(
            1
            for item in items
            if item.get("disposition") not in ALLOWED_DISPOSITIONS
        )
        if unclassified != actual_unclassified:
            mismatches.append(
                f"identity_control.unclassified_core_items={unclassified} "
                f"does not match observed unclassified items={actual_unclassified}"
            )

    unknown_tagged_source_count = identity_control.get(
        "unknown_tagged_source_count"
    )
    if not isinstance(unknown_tagged_source_count, int) or unknown_tagged_source_count <= 0:
        mismatches.append("identity_control.unknown_tagged_source_count is invalid")
    if evidence_root is not None and not mismatches:
        wire_entry = evidence_binding.get("wire_fixture", {})
        wire_path = evidence_root / str(wire_entry.get("file", ""))
        if wire_path.is_file():
            actual_unknown_tagged_source_count = _count_unknown_tags(
                _load_json(wire_path)
            )
            if unknown_tagged_source_count != actual_unknown_tagged_source_count:
                mismatches.append(
                    "mutation: identity_control.unknown_tagged_source_count does not "
                    "match bound wire evidence"
                )

    return {"reconciled": not mismatches, "mismatches": mismatches}


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trace",
        type=Path,
        default=Path("docs/evidence/issue-62/current-codexhub-thread-tool-surface.json"),
    )
    parser.add_argument(
        "--wire-fixture",
        type=Path,
        default=Path("docs/evidence/issue-62/codexhub-runtime-wire-fixture.json"),
    )
    parser.add_argument(
        "--audit",
        type=Path,
        default=Path("docs/evidence/issue-62/read-only-gate-audit.json"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("docs/evidence/issue-62/runtime-wire-inventory.json"),
    )
    parser.add_argument("--cli-version-floor", default=DEFAULT_CLI_FLOOR)
    parser.add_argument("--candidate-cli-version", default=DEFAULT_CANDIDATE_CLI_VERSION)
    parser.add_argument(
        "--candidate-source-commit", default=DEFAULT_CANDIDATE_SOURCE_COMMIT
    )
    parser.add_argument(
        "--replay-case",
        default="identity",
        choices=["identity", "mutation", "deletion", "loss"],
    )
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()
    inventory = build_inventory(
        trace=args.trace,
        wire_fixture=args.wire_fixture,
        audit=args.audit,
        cli_version_floor=args.cli_version_floor,
        candidate_cli_version=args.candidate_cli_version,
        candidate_source_commit=args.candidate_source_commit,
    )

    if args.replay_case != "identity":
        replayed = replay_inventory(inventory, args.replay_case)
        report = reconcile_inventory(replayed, evidence_root=args.trace.parent)
        print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
        return 0 if report["reconciled"] else 1

    report = reconcile_inventory(inventory, evidence_root=args.trace.parent)
    if not report["reconciled"]:
        raise SystemExit(f"identity reconciliation failed: {report['mismatches']}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(inventory, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
