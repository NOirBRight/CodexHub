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

# Evidence references are part of the contract, not free-form annotations.
# This map lets reconciliation detect a stale or hand-edited inventory that
# silently points a core claim at an unrelated tool-surface check.
CORE_SCOPE_EVIDENCE = {
    "core_text_streaming": "codexhub-runtime-wire-fixture.json#response.streaming.captured",
    "core_text_non_streaming": "codexhub-runtime-wire-fixture.json#response.non_streaming.captured=false",
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
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _classify_core_items(
    wire: dict[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    streaming_captured = wire.get("response", {}).get("streaming", {}).get("captured") is True
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
            "live_control_required",
            "codexhub-runtime-wire-fixture.json#response.non_streaming.captured=false",
            "no real non-streaming request exists; contract sentinel only",
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
            "live_control_required",
            "read-only-gate-audit.json#gateway_identity_route.observed_sse_event_type_counts",
            "terminal event classification requires independently captured full response-body evidence",
        )
    )
    items.append(
        _item(
            "core_sse_errors",
            "live_control_required",
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
            "live_control_required",
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
            "live_control_required",
            "read-only-gate-audit.json#gate_classification.full_pre_post_request_response=live_control_required",
            "terminal event classification requires a real captured response body fingerprint",
        )
    )
    items.append(
        _item(
            "errors",
            "live_control_required",
            "read-only-gate-audit.json#gate_classification.full_pre_post_request_response=live_control_required",
            "error classification requires independently captured pre/post response evidence",
        )
    )

    items.append(
        _item(
            "hosted_only_declarations",
            "live_control_required",
            "current-codexhub-thread-tool-surface.json#exposure_state_catalog",
            "hosted-only and host-unavailable are tagged reconciliation sentinels; no runtime-observed host binding",
        )
    )

    items.append(
        _item(
            "unknown_tagged_sentinels",
            "live_control_required",
            "codexhub-runtime-wire-fixture.json#response.streaming.events.tag=unknown",
            "unknown tagged sentinels are preserved as opaque replay sentinels; live runtime dispositions require a real unknown item",
        )
    )

    items.append(
        _item(
            "default_runtime_fields",
            "live_control_required",
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


def _build_identity_control(items: list[dict[str, Any]]) -> dict[str, Any]:
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
    }


def _build_qualification(
    items: list[dict[str, Any]], candidate_version_status: str
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
    return {
        "candidate_version_status": candidate_version_status,
        "candidate_version_eligible": candidate_version_eligible,
        "blocking_scopes": blocking_scopes,
        "ready_for_beta1": candidate_version_eligible and not blocking_scopes,
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

    trace_provider = source.get("configured_provider_id")
    wire_provider = wire_route.get("configured_provider_id")
    if not trace_provider or wire_provider != trace_provider:
        raise ValueError(
            "candidate provider binding is inconsistent: "
            f"wire={wire_provider!r} trace={trace_provider!r}"
        )
    trace_model = source.get("model")
    wire_model = wire_data.get("pre_gateway", {}).get("model")
    if not trace_model or wire_model != trace_model:
        raise ValueError(
            "candidate model binding is inconsistent: "
            f"wire={wire_model!r} trace={trace_model!r}"
        )

    status = _candidate_version_status(candidate_cli_version, cli_version_floor)
    identity = {
        "cli_version": candidate_cli_version,
        "source_commit": candidate_source_commit,
        "route_upstream": wire_route["upstream_route"],
        "inbound_format": wire_route["inbound_format"],
        "upstream_format": wire_route["upstream_format"],
        "configured_provider_id": trace_provider,
        "model": trace_model,
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

    items: list[dict[str, Any]] = []
    items.extend(_classify_core_items(wire_data))
    items.extend(_classify_live_control_items(wire_data, audit_data))
    items.extend(_classify_advanced_items(trace_data))

    seen: set[str] = set()
    for item in items:
        if item["scope"] in seen:
            raise ValueError(f"duplicate scope: {item['scope']}")
        seen.add(item["scope"])

    identity_control = _build_identity_control(items)
    candidate_identity, candidate_version_status = _validate_candidate_binding(
        trace_data=trace_data,
        wire_data=wire_data,
        cli_version_floor=cli_version_floor,
        candidate_cli_version=candidate_cli_version,
        candidate_source_commit=candidate_source_commit,
    )
    qualification = _build_qualification(items, candidate_version_status)

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
        "evidence_binding": {
            "trace": {"file": trace.name, "sha256": _sha256_file(trace)},
            "wire_fixture": {
                "file": wire_fixture.name,
                "sha256": _sha256_file(wire_fixture),
            },
            "audit": {"file": audit.name, "sha256": _sha256_file(audit)},
        },
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


def reconcile_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    """Reconcile an inventory (possibly mutated) against the identity contract."""
    mismatches: list[str] = []

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
    expected_ready = candidate_eligible is True and not actual_blocking_scopes
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

    unclassified = inventory.get("identity_control", {}).get("unclassified_core_items")
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
        report = reconcile_inventory(replayed)
        print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
        return 0 if report["reconciled"] else 1

    report = reconcile_inventory(inventory)
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
