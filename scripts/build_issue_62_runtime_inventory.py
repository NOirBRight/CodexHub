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
- ``Unqualified``      : not qualified by accepted evidence; a separately
  authorized live control window must capture it before any capability claim

The generator reads the existing sanitized Issue #62 evidence artifacts and
never fabricates a ``Supported`` disposition for a gate the artifacts leave
unqualified. It feeds #249 (capability gate) and #66 (Chat
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
DEFAULT_CLI_FLOOR = "0.146.0"
SUPPORTED_CLI_FLOOR = DEFAULT_CLI_FLOOR
DEFAULT_CANDIDATE_CLI_VERSION = None
DEFAULT_CANDIDATE_SOURCE_COMMIT = None
CLI_SOURCE_TAG = "rust-v0.146.0"
CLI_SOURCE_COMMIT_STATUS = "published_attested"
CLI_BINARY_SHA256 = "bc343ba420dc2e2e9f59e6fc5e5bf0aae1cd8c771fc319665241fc9c0271fddb"
CLI_SOURCE_COMMIT = "e363b08c9175ac1cbe5893615dd2cb9ddf95043b"
CANDIDATE_REVISION = "accab8ff6eb4d6ebd93cda84585fb5f6cb89da82"
HISTORICAL_CLI_VERSION = "0.144.0-alpha.4"
HISTORICAL_SOURCE_COMMIT = "9e552e9d15ba52bed7077d5357f3e18e330f8f38"
HISTORICAL_CAPTURED_AT = "2026-07-12T14:57:55+08:00"
DEFAULT_SOURCE_CONTRACT = Path(
    "docs/evidence/issue-62/codex-0.146-source-contract.json"
)
STRUCTURAL_FAMILIES = (
    "plain_function",
    "custom_freeform",
    "namespace",
    "client_executed_tool_discovery",
    "selected_provider_hosted",
    "unknown_future_kind",
)

ALLOWED_DISPOSITIONS = (
    "preserved",
    "reversibly_adapted",
    "local_consume",
    "Unsupported",
    "Unqualified",
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
# evidence only justifies ``Unqualified``.  Keeping this set
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

# An unqualified core/live-control item blocks beta qualification.  Advanced
# capabilities remain explicitly deferred and therefore do not block the
# beta.1 core gate merely because they are unqualified.
BLOCKING_UNQUALIFIED_SCOPES = frozenset(
    CORE_CONTRACT_SCOPES | LIVE_CONTROL_SCOPES | {"choice_controls"}
)

# Evidence references are part of the contract, not free-form annotations.
# This map lets reconciliation detect a stale or hand-edited inventory that
# silently points a core claim at an unrelated tool-surface check.
IDENTITY_RESPONSE_EVIDENCE = (
    "codexhub-runtime-wire-fixture.json#"
    "pre_gateway.response.streaming.response_id|"
    "post_gateway.response.streaming.response_id"
)

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
    "identity_response_ids": IDENTITY_RESPONSE_EVIDENCE,
    "identity_request_ids": "codexhub-runtime-wire-fixture.json#pre_gateway.request_id",
}

# Every emitted inventory scope has a fixed evidence reference.  Keeping this
# separate from the classifier implementation lets reconciliation reject a
# hand-edited source pointer even when the disposition itself is valid.  The
# choice-control reference has two historical forms because a future captured
# fixture may legitimately flip the ``captured`` sentinel to true.
SCOPE_EVIDENCE_SOURCES = {
    **CORE_SCOPE_EVIDENCE,
    "choice_controls": frozenset(
        {
            "codexhub-runtime-wire-fixture.json#pre_gateway.choice_controls.captured=false",
            "codexhub-runtime-wire-fixture.json#pre_gateway.choice_controls.captured=true",
        }
    ),
    "terminal_events": "read-only-gate-audit.json#gate_classification.full_pre_post_request_response=live_control_required",
    "errors": "read-only-gate-audit.json#gate_classification.full_pre_post_request_response=live_control_required",
    "hosted_only_declarations": "current-codexhub-thread-tool-surface.json#exposure_state_catalog",
    "unknown_tagged_sentinels": "codexhub-runtime-wire-fixture.json#response.streaming.events.tag=unknown",
    "default_runtime_fields": "read-only-gate-audit.json#model_visible_request_plan.top_level_field_presence",
    "code_mode": "issue-248#beta.1-scope",
    "tool_search": "current-codexhub-thread-tool-surface.json#planner_gates.caller_request",
    "collaboration_v2": "issue-248#beta.3-scope",
    "chat_conversion": "issue-248#beta.4-scope",
}

# These are the core contract claims for which the retained bounded evidence
# already proves a final preserved/adapted disposition.  They may not be
# downgraded to ``Unqualified`` or ``local_consume`` by a hand-edited artifact.
CORE_REQUIRED_PRESERVED_SCOPES = frozenset(
    {
        "core_text_streaming",
        "core_history_multiturn",
        "core_history_item_ids",
        "core_history_call_ids",
        "core_sse_streaming_events",
        "core_function_declaration",
        "core_function_call",
        "core_function_result",
        "identity_item_call_ids",
        "identity_response_ids",
        "identity_request_ids",
    }
)

CORE_FINAL_DISPOSITIONS = frozenset({"preserved", "reversibly_adapted", "Unqualified"})

# JSON paths behind the core evidence references.  Most scopes point at one
# value; the response identity claim deliberately points at a pre/post pair
# and additionally requires those two aliases to agree.
CORE_EVIDENCE_POINTERS = {
    "core_text_streaming": (
        "codexhub-runtime-wire-fixture.json",
        (("response", "streaming", "captured"),),
    ),
    "core_text_non_streaming": (
        "codexhub-runtime-wire-fixture.json",
        (("response", "non_streaming", "captured"),),
    ),
    "core_history_multiturn": (
        "codexhub-runtime-wire-fixture.json",
        (("history", "captured_source_counts", "paired_calls"),),
    ),
    "core_history_item_ids": (
        "codexhub-runtime-wire-fixture.json",
        (("history", "call_links"),),
    ),
    "core_history_call_ids": (
        "codexhub-runtime-wire-fixture.json",
        (("history", "required_call_ids"),),
    ),
    "core_sse_streaming_events": (
        "codexhub-runtime-wire-fixture.json",
        (("response", "streaming", "events"),),
    ),
    "core_sse_terminal_events": (
        "read-only-gate-audit.json",
        (("gateway_identity_route", "observed_sse_event_type_counts"),),
    ),
    "core_sse_errors": (
        "read-only-gate-audit.json",
        (("gate_classification", "full_pre_post_request_response"),),
    ),
    "core_function_declaration": (
        "codexhub-runtime-wire-fixture.json",
        (("pre_gateway", "tool_surface", "namespaces"),),
    ),
    "core_function_call": (
        "codexhub-runtime-wire-fixture.json",
        (("history", "call_links"),),
    ),
    "core_function_result": (
        "codexhub-runtime-wire-fixture.json",
        (("history", "call_links"),),
    ),
    "core_function_replay": (
        "codexhub-runtime-wire-fixture.json",
        (("history", "call_links"),),
    ),
    "identity_item_call_ids": (
        "codexhub-runtime-wire-fixture.json",
        (("history", "call_links"),),
    ),
    "identity_response_ids": (
        "codexhub-runtime-wire-fixture.json",
        (
            ("pre_gateway", "response", "streaming", "response_id"),
            ("post_gateway", "response", "streaming", "response_id"),
        ),
    ),
    "identity_request_ids": (
        "codexhub-runtime-wire-fixture.json",
        (("pre_gateway", "request_id"),),
    ),
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
    "candidate_revision",
    "cli_binary_sha256",
    "cli_source_commit_status",
    "cli_source_tag",
)

# This artifact is deliberately bound to the current Issue #62 evidence
# package.  A standalone reconciliation has no evidence root to reopen, so
# it must still reject a hand-edited candidate or pointer that merely has the
# right type/shape but no longer names the retained package.
EXPECTED_CANDIDATE_VALUES = {
    "cli_version": DEFAULT_CLI_FLOOR,
    "source_commit": CLI_SOURCE_COMMIT,
    "codex_source_commit": CLI_SOURCE_COMMIT,
    "route_upstream": "official",
    "inbound_format": "responses",
    "upstream_format": "responses",
    "configured_provider_id": "custom",
    "model": "gpt-5.6-sol",
    "catalog_binding": "official Codex catalog entry for openai/gpt-5.6-sol",
    "catalog_model_entry_id": "gpt-5.6-sol",
    "route_behavior_profile": "official_codex_app_http_passthrough",
    "candidate_revision": CANDIDATE_REVISION,
    "cli_binary_sha256": CLI_BINARY_SHA256,
    "cli_source_commit_status": CLI_SOURCE_COMMIT_STATUS,
    "cli_source_tag": CLI_SOURCE_TAG,
}

EXPECTED_EVIDENCE_BINDING_FILES = {
    "source_contract": "codex-0.146-source-contract.json",
    "trace": "current-codexhub-thread-tool-surface.json",
    "wire_fixture": "codexhub-runtime-wire-fixture.json",
    "audit": "read-only-gate-audit.json",
}


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


def _validate_supported_floor(value: str) -> str:
    """Require the inventory to bind to the repository's supported CLI floor."""

    if not isinstance(value, str):
        raise ValueError(f"supported CLI floor must be a string: {value!r}")
    try:
        _version_key(value)
    except ValueError as exc:
        raise ValueError(f"supported CLI floor is malformed: {value!r}") from exc
    if value != SUPPORTED_CLI_FLOOR:
        raise ValueError(
            "inventory CLI floor must bind to the supported CLI floor "
            f"{SUPPORTED_CLI_FLOOR}, got {value!r}"
        )
    return value


def _candidate_identity_mismatches(
    candidate_identity: Any, *, enforce_retained: bool = False
) -> list[str]:
    """Validate candidate metadata that is self-contained in the inventory.

    Reconciliation is also used for in-memory replay controls where no evidence
    root is available.  These checks therefore cover contradictions that can be
    detected without reopening the source fixtures; the evidence-root path
    below performs the stronger exact binding against the trace and wire data.
    """

    mismatches: list[str] = []
    if not isinstance(candidate_identity, dict):
        return ["candidate_identity must be an object"]

    candidate_cli_version = candidate_identity.get("cli_version")
    if not isinstance(candidate_cli_version, str):
        mismatches.append("candidate_identity.cli_version must be a string")
    else:
        try:
            _version_key(candidate_cli_version)
        except ValueError:
            mismatches.append("candidate_identity.cli_version is malformed")

    source_commit = candidate_identity.get("source_commit")
    codex_source_commit = candidate_identity.get("codex_source_commit")
    for field, value in (
        ("source_commit", source_commit),
        ("codex_source_commit", codex_source_commit),
    ):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            mismatches.append(
                f"candidate_identity.{field} is not a lowercase 40-character SHA-1"
            )
    if (
        isinstance(source_commit, str)
        and isinstance(codex_source_commit, str)
        and source_commit != codex_source_commit
    ):
        mismatches.append(
            "candidate_identity.source_commit and codex_source_commit contradict each other"
        )

    for field in (
        "route_upstream",
        "inbound_format",
        "upstream_format",
        "configured_provider_id",
        "model",
        "catalog_binding",
        "catalog_model_entry_id",
        "route_behavior_profile",
    ):
        value = candidate_identity.get(field)
        if not isinstance(value, str) or not value.strip():
            mismatches.append(f"candidate_identity.{field} is missing or blank")

    candidate_revision = candidate_identity.get("candidate_revision")
    if not isinstance(candidate_revision, str) or not re.fullmatch(
        r"[0-9a-f]{40}", candidate_revision
    ):
        mismatches.append(
            "candidate_identity.candidate_revision is not a lowercase 40-character SHA-1"
        )
    source_status = candidate_identity.get("cli_source_commit_status")
    if source_status not in {"published_attested", "not_published_by_registry"}:
        mismatches.append("candidate_identity.cli_source_commit_status is invalid")
    source_tag = candidate_identity.get("cli_source_tag")
    if not isinstance(source_tag, str) or not source_tag.strip():
        mismatches.append("candidate_identity.cli_source_tag is missing or blank")

    if enforce_retained:
        for field, expected in EXPECTED_CANDIDATE_VALUES.items():
            if candidate_identity.get(field) != expected:
                mismatches.append(
                    f"candidate_identity.{field} does not match the retained Issue #62 candidate"
                )

    for field in (
        "catalog_snapshot_sha256",
        "evidence_manifest_sha256",
        "cli_binary_sha256",
    ):
        value = candidate_identity.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            mismatches.append(
                f"candidate_identity.{field} is not a lowercase 64-character SHA-256"
            )
    return mismatches


def _evidence_source_allowed(scope: str, evidence_source: Any) -> bool:
    expected = SCOPE_EVIDENCE_SOURCES.get(scope)
    if expected is None:
        return False
    if isinstance(expected, frozenset):
        return evidence_source in expected
    return evidence_source == expected


def _resolve_fixture_pointer(document: Any, path: tuple[str, ...]) -> Any:
    current = document
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            raise KeyError(".".join(path))
        current = current[segment]
    return current


def _validate_core_evidence_pointers(
    *, wire: dict[str, Any], audit: dict[str, Any]
) -> None:
    """Require every core evidence reference to resolve in its bound fixture."""

    documents = {
        "codexhub-runtime-wire-fixture.json": wire,
        "read-only-gate-audit.json": audit,
    }
    for scope, (filename, pointers) in CORE_EVIDENCE_POINTERS.items():
        document = documents[filename]
        values: list[Any] = []
        for pointer in pointers:
            try:
                value = _resolve_fixture_pointer(document, pointer)
            except KeyError as exc:
                raise ValueError(
                    f"evidence pointer for {scope} is missing: {filename}#{exc.args[0]}"
                ) from exc
            values.append(value)
        if scope == "identity_response_ids":
            if (
                len(values) != 2
                or not all(isinstance(value, str) and value for value in values)
                or values[0] != values[1]
            ):
                raise ValueError(
                    "identity_response_ids evidence pointer must bind equal, non-empty "
                    "pre_gateway and post_gateway response_id values"
                )


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


def _count_response_unknown_tags(wire: dict[str, Any]) -> int:
    """Count unknown-tag sentinels in the two response modes only.

    Structural family examples intentionally carry their own opaque unknown
    sentinel.  That declaration inventory is separate from the response-tag
    evidence used by the identity control, so it must not inflate this count.
    """

    response = wire.get("response", {})
    return _count_unknown_tags(
        {
            "streaming": response.get("streaming", {}),
            "non_streaming": response.get("non_streaming", {}),
        }
    )


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


_STRUCTURAL_RULES = {
    "plain_function": {
        "selected_protocol_disposition": "native",
        "optional_rule": "preserve_when_supported_else_omit",
        "required_rule": "preserve_when_supported_else_required-but-unavailable",
        "executor": "codex_client",
    },
    "custom_freeform": {
        "selected_protocol_disposition": "native",
        "optional_rule": "preserve_when_supported_else_omit",
        "required_rule": "preserve_when_supported_else_required-but-unavailable",
        "executor": "codex_client",
    },
    "namespace": {
        "selected_protocol_disposition": "native",
        "optional_rule": "preserve_when_supported_else_omit",
        "required_rule": "preserve_when_supported_else_required-but-unavailable",
        "executor": "codex_client",
    },
    "client_executed_tool_discovery": {
        "selected_protocol_disposition": "native",
        "optional_rule": "preserve_client_execution_else_omit",
        "required_rule": "preserve_client_execution_else_required-but-unavailable",
        "executor": "codex_client",
    },
    "selected_provider_hosted": {
        "selected_protocol_disposition": "native_if_selected_provider_supports",
        "optional_rule": "omit_if_selected_provider_unsupported",
        "required_rule": "required-but-unavailable_if_selected_provider_unsupported",
        "executor": "selected_provider",
    },
    "unknown_future_kind": {
        "selected_protocol_disposition": "omit",
        "optional_rule": "omit_and_emit_sanitized_diagnostic",
        "required_rule": "required-but-unavailable",
        "executor": "unknown",
    },
}

# The source contract is intentionally closed over the six declaration
# families.  Keep the wire/runtime names and identity-bearing sections
# canonical so a regenerated inventory cannot turn an arbitrary fixture value
# into a qualified family claim.
STRUCTURAL_FAMILY_SCHEMAS = {
    "plain_function": {
        "runtime_type": "function",
        "wire_declaration_type": "function",
        "executor": "codex_client",
        "observation": "not_observed_source_contract_only",
        "loss_boundary": "preserve declaration and inverse call/result/history IDs",
        "declaration": {"type": "function", "required": ("name", "parameters")},
        "call": {
            "type": "function_call",
            "required": ("item_id", "call_id", "arguments"),
        },
        "result": {
            "type": "function_call_output",
            "required": ("item_id", "call_id", "output"),
        },
        "history": {"required": ("call_id", "call_item_id", "output_item_id")},
    },
    "custom_freeform": {
        "runtime_type": "custom",
        "wire_declaration_type": "custom",
        "executor": "codex_client",
        "observation": "not_observed_source_contract_only",
        "loss_boundary": "preserve declaration and inverse call/result/history IDs",
        "declaration": {"type": "custom", "required": ("name", "format")},
        "call": {
            "type": "custom_tool_call",
            "required": ("item_id", "call_id", "input"),
        },
        "result": {
            "type": "custom_tool_call_output",
            "required": ("item_id", "call_id", "output"),
        },
        "history": {"required": ("call_id", "call_item_id", "output_item_id")},
    },
    "namespace": {
        "runtime_type": "namespace",
        "wire_declaration_type": "namespace",
        "executor": "codex_client",
        "observation": "not_observed_source_contract_only",
        "loss_boundary": "preserve declaration and inverse call/result/history IDs",
        "declaration": {"type": "namespace", "required": ("name", "tools")},
        "call": {
            "type": "function_call",
            "required": ("item_id", "call_id", "namespace", "arguments"),
        },
        "result": {
            "type": "function_call_output",
            "required": ("item_id", "call_id", "output"),
        },
        "history": {
            "required": ("call_id", "call_item_id", "output_item_id", "namespace")
        },
    },
    "client_executed_tool_discovery": {
        "runtime_type": "tool_search",
        "wire_declaration_type": "tool_search",
        "executor": "codex_client",
        "observation": "not_observed_source_contract_only",
        "loss_boundary": "discovery request/result stays client-executed",
        "declaration": {
            "type": "tool_search",
            "required": ("execution", "parameters"),
            "equals": {"execution": "client"},
        },
        "call": {
            "type": "tool_search_call",
            "required": ("item_id", "call_id", "execution", "arguments"),
            "equals": {"execution": "client"},
        },
        "result": {
            "type": "tool_search_output",
            "required": ("item_id", "call_id", "execution", "tools"),
            "equals": {"execution": "client"},
        },
        "history": {
            "required": ("call_id", "call_item_id", "output_item_id", "executor"),
            "equals": {"executor": "codex_client"},
        },
    },
    "selected_provider_hosted": {
        "runtime_type": "web_search",
        "wire_declaration_type": "web_search",
        "executor": "selected_provider",
        "observation": "not_observed_selected_provider_control_required",
        "loss_boundary": "optional unsupported hosted capability is omitted; required capability fails visibly",
        "declaration": {
            "type": "web_search",
            "required": ("executor", "provider_scope"),
            "equals": {
                "executor": "selected_provider",
                "provider_scope": "selected_provider_only",
            },
        },
        "call": {
            "type": "web_search_call",
            "required": ("item_id", "status", "action"),
        },
        "result": {
            "type": "web_search_call",
            "required": ("item_id", "status", "provider_scope"),
            "equals": {"provider_scope": "selected_provider_only"},
        },
        "history": {
            "required": (
                "call_id",
                "call_item_id",
                "output_item_id",
                "executor",
                "cross_provider_proxy",
            ),
            "equals": {
                "executor": "selected_provider",
                "cross_provider_proxy": "forbidden",
            },
            "nullable": ("call_id",),
        },
    },
    "unknown_future_kind": {
        "runtime_type": "unknown",
        "wire_declaration_type": "<unknown>",
        "executor": "unknown",
        "observation": "opaque_sentinel_only",
        "loss_boundary": "retain tag and opaque payload; do not normalize",
        "declaration": {
            "type": "unknown",
            "required": ("tag", "opaque_payload"),
            "equals": {"tag": "unknown"},
        },
        "call": {
            "type": "unknown",
            "required": ("tag", "opaque_payload"),
            "equals": {"tag": "unknown"},
        },
        "result": {
            "type": "unknown",
            "required": ("tag", "opaque_payload"),
            "equals": {"tag": "unknown"},
        },
        "history": {
            "required": ("call_id", "call_item_id", "output_item_id", "loss_rule"),
            "nullable": ("call_id", "call_item_id", "output_item_id"),
            "equals": {
                "call_id": None,
                "call_item_id": None,
                "output_item_id": None,
                "loss_rule": "retain opaque sentinel",
            },
        },
    },
}
STRUCTURAL_NONEMPTY_STRING_FIELDS = frozenset(
    {
        "name",
        "namespace",
        "executor",
        "execution",
        "provider_scope",
        "cross_provider_proxy",
        "tag",
        "loss_rule",
        "item_id",
        "call_id",
        "call_item_id",
        "output_item_id",
    }
)


def _validate_structural_family_schema(
    family_name: str,
    family: dict[str, Any],
    example: dict[str, Any],
    *,
    context: str,
) -> None:
    schema = STRUCTURAL_FAMILY_SCHEMAS.get(family_name)
    if schema is None:
        raise ValueError(f"{context} has an unknown family schema")
    for field in ("runtime_type", "wire_declaration_type", "executor", "observation", "loss_boundary"):
        if family.get(field) != schema[field]:
            raise ValueError(f"{context}.{field} does not match the canonical family schema")
    for section_name in ("declaration", "call", "result", "history"):
        section = example.get(section_name)
        section_schema = schema[section_name]
        if not isinstance(section, dict):
            raise ValueError(f"{context}.{section_name} must be an object")
        expected_type = section_schema.get("type")
        if expected_type is not None and section.get("type") != expected_type:
            raise ValueError(f"{context}.{section_name}.type does not match the canonical family schema")
        nullable = set(section_schema.get("nullable", ()))
        for key in section_schema.get("required", ()):
            if key not in section:
                raise ValueError(f"{context}.{section_name}.{key} is required")
            if key in STRUCTURAL_NONEMPTY_STRING_FIELDS and section[key] is not None and (
                not isinstance(section[key], str) or not section[key]
            ):
                raise ValueError(
                    f"{context}.{section_name}.{key} must be a non-empty string"
                )
            if key not in nullable and section[key] is None:
                raise ValueError(f"{context}.{section_name}.{key} cannot be null")
        for key, expected in section_schema.get("equals", {}).items():
            if section.get(key) != expected:
                raise ValueError(f"{context}.{section_name}.{key} does not match the canonical family schema")
    if family_name == "namespace":
        tools = example["declaration"].get("tools")
        if not isinstance(tools, list) or not tools:
            raise ValueError(f"{context}.declaration.tools must contain a function")
        for nested in tools:
            if (
                not isinstance(nested, dict)
                or nested.get("type") != "function"
                or not isinstance(nested.get("name"), str)
                or not nested.get("name")
                or "parameters" not in nested
            ):
                raise ValueError(f"{context}.declaration.tools contains an invalid function")
        namespace = example["declaration"].get("name")
        if (
            example["call"].get("namespace") != namespace
            or example["history"].get("namespace") != namespace
        ):
            raise ValueError(f"{context} namespace ownership does not reconcile")
    if family_name in {
        "plain_function",
        "custom_freeform",
        "namespace",
        "client_executed_tool_discovery",
        "selected_provider_hosted",
    }:
        if (
            example["call"].get("item_id") != example["history"].get("call_item_id")
            or example["result"].get("item_id")
            != example["history"].get("output_item_id")
        ):
            raise ValueError(f"{context} call/result item IDs do not reconcile")
    _validate_structural_stream_example(
        family_name,
        example.get("streaming"),
        context=context,
    )

STRUCTURAL_EVIDENCE_SOURCES = {
    "plain_function": "codex-0.146-source-contract.json#runtime_wire_surface.declaration_family_examples.plain_function",
    "custom_freeform": "codex-0.146-source-contract.json#runtime_wire_surface.declaration_family_examples.custom_freeform",
    "namespace": "codex-0.146-source-contract.json#runtime_wire_surface.declaration_family_examples.namespace",
    "client_executed_tool_discovery": "codex-0.146-source-contract.json#runtime_wire_surface.declaration_family_examples.client_executed_tool_discovery",
    "selected_provider_hosted": "codex-0.146-source-contract.json#runtime_wire_surface.declaration_family_examples.selected_provider_hosted",
    "unknown_future_kind": "codex-0.146-source-contract.json#runtime_wire_surface.declaration_family_examples.unknown_future_kind",
}

STRUCTURAL_STREAM_DONE_EVENTS = {
    "plain_function": ("arguments_done", "response.function_call_arguments.done"),
    "custom_freeform": ("input_done", "response.custom_tool_call_input.done"),
    "namespace": ("arguments_done", "response.function_call_arguments.done"),
    "unknown_future_kind": ("done", "unknown.future_done"),
}

STRUCTURAL_STREAM_SCHEMAS = {
    "plain_function": {
        "added": "response.output_item.added",
        "delta": "response.function_call_arguments.delta",
        "done_field": "arguments_done",
        "done": "response.function_call_arguments.done",
        "item_done": "response.output_item.done",
        "terminal": "response.completed",
        "event_order": [
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
            "response.output_item.done",
            "response.completed",
        ],
    },
    "custom_freeform": {
        "added": "response.output_item.added",
        "delta": "response.custom_tool_call_input.delta",
        "done_field": "input_done",
        "done": "response.custom_tool_call_input.done",
        "item_done": "response.output_item.done",
        "terminal": "response.completed",
        "event_order": [
            "response.output_item.added",
            "response.custom_tool_call_input.delta",
            "response.custom_tool_call_input.done",
            "response.output_item.done",
            "response.completed",
        ],
    },
    "namespace": {
        "added": "response.output_item.added",
        "delta": "response.function_call_arguments.delta",
        "done_field": "arguments_done",
        "done": "response.function_call_arguments.done",
        "item_done": "response.output_item.done",
        "terminal": "response.completed",
        "event_order": [
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
            "response.output_item.done",
            "response.completed",
        ],
    },
    "client_executed_tool_discovery": {
        "added": None,
        "delta": None,
        "done_field": "done",
        "done": "response.output_item.done",
        "item_done": "response.output_item.done",
        "terminal": "response.completed",
        "event_order": ["response.output_item.done", "response.completed"],
    },
    "selected_provider_hosted": {
        "added": "response.output_item.added",
        "delta": "<provider-defined>",
        "done_field": "done",
        "done": "response.output_item.done",
        "item_done": "response.output_item.done",
        "terminal": "response.completed",
        "event_order": [
            "response.output_item.added",
            "<provider-defined>",
            "response.output_item.done",
            "response.completed",
        ],
    },
    "unknown_future_kind": {
        "added": "unknown.future_event",
        "delta": "unknown.future_delta",
        "done_field": "done",
        "done": "unknown.future_done",
        "item_done": "unknown.future_done",
        "terminal": "response.completed",
        "event_order": [
            "unknown.future_event",
            "unknown.future_delta",
            "unknown.future_done",
            "response.completed",
        ],
    },
}


def _validate_structural_stream_example(
    family_name: str,
    streaming: Any,
    *,
    context: str,
) -> None:
    schema = STRUCTURAL_STREAM_SCHEMAS.get(family_name)
    if schema is None:
        raise ValueError(f"{context} has an unknown stream schema")
    if not isinstance(streaming, dict):
        raise ValueError(f"{context}.streaming must be an object")
    for field in ("added", "delta", "terminal"):
        if streaming.get(field) != schema[field]:
            raise ValueError(f"{context}.streaming.{field} does not match the canonical SSE schema")
    done_field = schema["done_field"]
    if streaming.get(done_field) != schema["done"]:
        raise ValueError(f"{context}.streaming.{done_field} does not match the canonical SSE schema")
    if streaming.get("done") != schema["item_done"]:
        raise ValueError(f"{context}.streaming.done does not match the canonical SSE schema")
    if streaming.get("event_order") != schema["event_order"]:
        raise ValueError(f"{context}.streaming.event_order does not match the canonical SSE schema")


def _validate_structural_stream_contract(
    *, wire: dict[str, Any], examples: dict[str, Any]
) -> None:
    """Require family SSE examples to retain done-before-terminal ordering."""

    response_shape = wire.get("runtime_wire_surface", {}).get("response_shape", {})
    stream_event_order = response_shape.get("stream_event_order")
    if not isinstance(stream_event_order, list) or not all(
        isinstance(event, str) and event for event in stream_event_order
    ):
        raise ValueError("wire response_shape.stream_event_order must be a non-empty string array")
    if "response.completed" not in stream_event_order:
        raise ValueError("wire response_shape.stream_event_order is missing response.completed")
    completed_index = stream_event_order.index("response.completed")
    output_done_index = (
        stream_event_order.index("response.output_item.done")
        if "response.output_item.done" in stream_event_order
        else None
    )
    for family_name, (_, event) in STRUCTURAL_STREAM_DONE_EVENTS.items():
        if event.startswith("unknown."):
            continue
        if event not in stream_event_order:
            raise ValueError(
                f"wire response_shape.stream_event_order is missing {event} for {family_name}"
            )
        event_index = stream_event_order.index(event)
        if event_index >= completed_index or (
            output_done_index is not None and event_index >= output_done_index
        ):
            raise ValueError(f"wire stream event {event} must precede terminal/item completion")

    for family_name in STRUCTURAL_FAMILIES:
        example = examples[family_name]
        streaming = example.get("streaming")
        _validate_structural_stream_example(
            family_name,
            streaming,
            context=f"wire runtime family {family_name}",
        )
        if not isinstance(streaming, dict):
            raise ValueError(f"wire runtime streaming example is malformed for {family_name}")
        event_order = streaming.get("event_order")
        if not isinstance(event_order, list) or not all(
            isinstance(event, str) and event for event in event_order
        ):
            raise ValueError(f"wire runtime streaming event_order is invalid for {family_name}")
        if streaming.get("terminal") != "response.completed" or event_order[-1] != "response.completed":
            raise ValueError(f"wire runtime streaming terminal ordering is invalid for {family_name}")
        if family_name != "unknown_future_kind" and (
            "response.output_item.done" not in event_order
            or event_order.index("response.output_item.done") >= len(event_order) - 1
        ):
            raise ValueError(f"wire runtime streaming item completion is invalid for {family_name}")
        done_spec = STRUCTURAL_STREAM_DONE_EVENTS.get(family_name)
        if done_spec is not None:
            field, event = done_spec
            if streaming.get(field) != event or event not in event_order:
                raise ValueError(
                    f"wire runtime streaming family done event is missing for {family_name}"
                )
            if event_order.index(event) >= event_order.index("response.completed"):
                raise ValueError(f"wire runtime streaming family done event follows terminal for {family_name}")
        if family_name == "client_executed_tool_discovery":
            if event_order != ["response.output_item.done", "response.completed"]:
                raise ValueError("tool_search_call SSE must contain only item completion and terminal events")
            if streaming.get("added") is not None or streaming.get("delta") is not None:
                raise ValueError("tool_search_call SSE must not claim a text delta or item-added event")


def _validate_structural_evidence_pointers(
    *,
    source_contract: dict[str, Any],
    wire: dict[str, Any],
    audit: dict[str, Any],
    declaration_families: list[dict[str, Any]],
) -> None:
    """Require every declaration-family evidence source to resolve in its fixture."""

    documents = {
        "codex-0.146-source-contract.json": source_contract,
        "codexhub-runtime-wire-fixture.json": wire,
        "read-only-gate-audit.json": audit,
    }
    for entry in declaration_families:
        family = entry.get("family")
        source = entry.get("evidence_source")
        expected = STRUCTURAL_EVIDENCE_SOURCES.get(family)
        if expected is None or source != expected:
            raise ValueError(
                f"declaration-family evidence source is invalid for {family!r}: {source!r}"
            )
        filename, pointer_text = source.split("#", 1)
        document = documents.get(filename)
        if document is None:
            raise ValueError(f"declaration-family evidence fixture is unknown: {filename}")
        try:
            _resolve_fixture_pointer(document, tuple(pointer_text.split(".")))
        except KeyError as exc:
            raise ValueError(
                f"declaration-family evidence pointer for {family} is missing: "
                f"{filename}#{exc.args[0]}"
            ) from exc


def _build_structural_inventory(
    *,
    source_contract: dict[str, Any],
    trace: dict[str, Any],
    wire: dict[str, Any],
    audit: dict[str, Any],
) -> list[dict[str, Any]]:
    """Join the unobserved 0.146 source contract with sanitized examples."""

    families = source_contract.get("runtime_wire_surface", {}).get(
        "declaration_families", []
    )
    if not isinstance(families, list):
        raise ValueError("source contract declaration_families must be an array")
    by_family: dict[str, dict[str, Any]] = {}
    for family in families:
        if not isinstance(family, dict) or not isinstance(family.get("family"), str):
            raise ValueError("runtime planner declaration family is malformed")
        name = family["family"]
        if name in by_family:
            raise ValueError(f"duplicate runtime planner declaration family: {name}")
        by_family[name] = family
    if set(by_family) != set(STRUCTURAL_FAMILIES):
        missing = sorted(set(STRUCTURAL_FAMILIES) - set(by_family))
        extra = sorted(set(by_family) - set(STRUCTURAL_FAMILIES))
        raise ValueError(
            "runtime planner declaration families are incomplete: "
            f"missing={missing!r} extra={extra!r}"
        )

    examples = source_contract.get("runtime_wire_surface", {}).get(
        "declaration_family_examples", {}
    )
    if not isinstance(examples, dict):
        raise ValueError("wire runtime declaration_family_examples must be an object")
    output: list[dict[str, Any]] = []
    for family_name in STRUCTURAL_FAMILIES:
        family = by_family[family_name]
        example = examples.get(family_name)
        if not isinstance(example, dict):
            raise ValueError(f"wire runtime example missing for {family_name}")
        required_example_parts = {
            "declaration",
            "call",
            "result",
            "history",
            "streaming",
            "terminal",
            "error",
            "loss_boundary",
        }
        if not required_example_parts.issubset(example):
            missing = sorted(required_example_parts - set(example))
            raise ValueError(
                f"wire runtime example for {family_name} is missing {missing!r}"
            )
        _validate_structural_family_schema(
            family_name,
            family,
            example,
            context=f"runtime planner family {family_name}",
        )
        if not isinstance(example.get("terminal"), dict) or example["terminal"].get(
            "classification"
        ) not in {"not_observed", "unqualified"}:
            raise ValueError(f"runtime terminal classification is invalid for {family_name}")
        if not isinstance(example.get("error"), dict) or example["error"].get(
            "classification"
        ) not in {"not_observed", "unqualified"}:
            raise ValueError(f"runtime error classification is invalid for {family_name}")
        if not isinstance(example.get("loss_boundary"), str) or not example["loss_boundary"]:
            raise ValueError(f"runtime loss boundary is missing for {family_name}")
        rule = _STRUCTURAL_RULES[family_name]
        if family.get("executor") != rule["executor"]:
            raise ValueError(
                f"runtime planner executor for {family_name} contradicts the CLI contract"
            )
        if family_name == "selected_provider_hosted":
            if example.get("provider_scope") != "selected_provider_only":
                raise ValueError("hosted declaration is not bound to the selected Provider")
            if example.get("cross_provider_proxy") != "forbidden":
                raise ValueError("hosted declaration permits a cross-Provider proxy")
        if family_name in {
            "plain_function",
            "custom_freeform",
            "namespace",
            "client_executed_tool_discovery",
        }:
            call = example["call"]
            result = example["result"]
            history = example["history"]
            if not all(isinstance(part, dict) for part in (call, result, history)):
                raise ValueError(f"runtime identity sections are malformed for {family_name}")
            if (
                call.get("item_id") != history.get("call_item_id")
                or result.get("item_id") != history.get("output_item_id")
                or call.get("call_id") != result.get("call_id")
                or call.get("call_id") != history.get("call_id")
            ):
                raise ValueError(f"runtime call/result/history IDs do not reconcile for {family_name}")
        output.append(
            {
                "family": family_name,
                "runtime_type": family.get("runtime_type"),
                "wire_declaration_type": family.get("wire_declaration_type"),
                "observed": bool(family.get("observed")),
                "observation": family.get("observation"),
                "executor": rule["executor"],
                "selected_protocol_disposition": rule["selected_protocol_disposition"],
                "optional_rule": rule["optional_rule"],
                "required_rule": rule["required_rule"],
                "loss_boundary": family.get("loss_boundary"),
                "evidence_source": STRUCTURAL_EVIDENCE_SOURCES[family_name],
                "representative": example,
            }
        )
    _validate_structural_stream_contract(wire=source_contract, examples=examples)
    _validate_structural_evidence_pointers(
        source_contract=source_contract,
        wire=wire,
        audit=audit,
        declaration_families=output,
    )
    return output


def _structural_inventory_mismatches(
    value: Any, *, require_unobserved: bool = False
) -> list[str]:
    """Validate the stable shape of the emitted declaration-family inventory."""

    if not isinstance(value, list):
        return ["declaration_families must be an array"]
    mismatches: list[str] = []
    if len(value) != len(STRUCTURAL_FAMILIES):
        mismatches.append(
            "declaration_families must contain exactly the six known families"
        )
    required_example_parts = {
        "declaration",
        "call",
        "result",
        "history",
        "streaming",
        "terminal",
        "error",
        "loss_boundary",
    }
    for index, family_name in enumerate(STRUCTURAL_FAMILIES):
        if index >= len(value):
            break
        entry = value[index]
        prefix = f"declaration_families[{index}]"
        if not isinstance(entry, dict):
            mismatches.append(f"{prefix} must be an object")
            continue
        if entry.get("family") != family_name:
            mismatches.append(
                f"{prefix}.family must be {family_name!r} in the canonical order"
            )
            continue
        rule = _STRUCTURAL_RULES[family_name]
        if entry.get("executor") != rule["executor"]:
            mismatches.append(f"{prefix}.executor does not match the family rule")
        for field in ("runtime_type", "wire_declaration_type", "observation", "loss_boundary"):
            if not isinstance(entry.get(field), str) or not entry[field]:
                mismatches.append(f"{prefix}.{field} is missing or blank")
        if not isinstance(entry.get("observed"), bool):
            mismatches.append(f"{prefix}.observed must be boolean")
        elif require_unobserved and entry.get("observed") is not False:
            mismatches.append(
                f"{prefix}.observed must remain false for the unobserved 0.146 source contract"
            )
        for field in ("selected_protocol_disposition", "optional_rule", "required_rule"):
            if entry.get(field) != rule[field]:
                mismatches.append(f"{prefix}.{field} does not match the family rule")
        expected_source = STRUCTURAL_EVIDENCE_SOURCES[family_name]
        if entry.get("evidence_source") != expected_source:
            mismatches.append(f"{prefix}.evidence_source does not match the family source")
        representative = entry.get("representative")
        if not isinstance(representative, dict):
            mismatches.append(f"{prefix}.representative must be an object")
            continue
        if not required_example_parts.issubset(representative):
            mismatches.append(f"{prefix}.representative is missing a wire example section")
        else:
            try:
                _validate_structural_family_schema(
                    family_name,
                    entry,
                    representative,
                    context=prefix,
                )
            except ValueError as exc:
                mismatches.append(str(exc))
        terminal = representative.get("terminal")
        if not isinstance(terminal, dict):
            mismatches.append(f"{prefix}.representative.terminal must be an object")
        else:
            if terminal.get("event") != "response.completed":
                mismatches.append(
                    f"{prefix}.representative.terminal.event must be response.completed"
                )
            if terminal.get("classification") not in {"not_observed", "unqualified"}:
                mismatches.append(
                    f"{prefix}.representative.terminal.classification is invalid"
                )
        error = representative.get("error")
        if not isinstance(error, dict):
            mismatches.append(f"{prefix}.representative.error must be an object")
        else:
            if error.get("event") != "response.failed":
                mismatches.append(
                    f"{prefix}.representative.error.event must be response.failed"
                )
            if error.get("classification") not in {"not_observed", "unqualified"}:
                mismatches.append(
                    f"{prefix}.representative.error.classification is invalid"
                )
        if not isinstance(representative.get("loss_boundary"), str) or not representative[
            "loss_boundary"
        ]:
            mismatches.append(f"{prefix}.representative.loss_boundary is missing or blank")
        elif representative["loss_boundary"] != entry.get("loss_boundary"):
            mismatches.append(
                f"{prefix}.representative.loss_boundary does not match the family boundary"
            )
        streaming = representative.get("streaming")
        if not isinstance(streaming, dict):
            mismatches.append(f"{prefix}.representative.streaming must be an object")
        else:
            event_order = streaming.get("event_order")
            if not isinstance(event_order, list) or not event_order:
                mismatches.append(f"{prefix}.representative.streaming.event_order is invalid")
            elif streaming.get("terminal") != "response.completed" or event_order[-1] != "response.completed":
                mismatches.append(
                    f"{prefix}.representative.streaming terminal ordering is invalid"
                )
            else:
                done_spec = STRUCTURAL_STREAM_DONE_EVENTS.get(family_name)
                if family_name != "unknown_future_kind" and (
                    "response.output_item.done" not in event_order
                    or event_order.index("response.output_item.done") >= len(event_order) - 1
                ):
                    mismatches.append(
                        f"{prefix}.representative.streaming item completion is invalid"
                    )
                if done_spec is not None:
                    field, event = done_spec
                    if streaming.get(field) != event or event not in event_order:
                        mismatches.append(
                            f"{prefix}.representative.streaming family done event is missing"
                        )
                    elif event_order.index(event) >= event_order.index("response.completed"):
                        mismatches.append(
                            f"{prefix}.representative.streaming family done event follows terminal"
                        )
                if family_name == "client_executed_tool_discovery":
                    if event_order != ["response.output_item.done", "response.completed"]:
                        mismatches.append(
                            f"{prefix}.representative.streaming tool-search order is invalid"
                        )
                    if streaming.get("added") is not None or streaming.get("delta") is not None:
                        mismatches.append(
                            f"{prefix}.representative.streaming tool-search claims text events"
                        )
        if family_name in {
            "plain_function",
            "custom_freeform",
            "namespace",
            "client_executed_tool_discovery",
        }:
            call = representative.get("call")
            result = representative.get("result")
            history = representative.get("history")
            if not all(isinstance(part, dict) for part in (call, result, history)):
                mismatches.append(f"{prefix}.representative identity sections are malformed")
            elif (
                call.get("item_id") != history.get("call_item_id")
                or result.get("item_id") != history.get("output_item_id")
                or call.get("call_id") != result.get("call_id")
                or call.get("call_id") != history.get("call_id")
            ):
                mismatches.append(f"{prefix}.representative call/result/history IDs do not reconcile")
        if family_name == "selected_provider_hosted":
            if representative.get("provider_scope") != "selected_provider_only":
                mismatches.append(f"{prefix}.representative is not selected-provider scoped")
            if representative.get("cross_provider_proxy") != "forbidden":
                mismatches.append(f"{prefix}.representative permits a cross-provider proxy")
        if family_name == "unknown_future_kind":
            declaration = representative.get("declaration", {})
            if not isinstance(declaration, dict) or declaration.get("tag") != "unknown":
                mismatches.append(f"{prefix}.representative does not retain the unknown sentinel")
    return mismatches


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
            "preserved" if streaming_captured else "Unqualified",
            "codexhub-runtime-wire-fixture.json#response.streaming.captured",
            "streaming response text observed and preserved across the official Responses route",
        )
    )

    items.append(
        _item(
            "core_text_non_streaming",
            "preserved" if non_streaming_captured else "Unqualified",
            "codexhub-runtime-wire-fixture.json#response.non_streaming.captured",
            "non-streaming response text is only qualified when a real captured fixture is present",
        )
    )

    history_counts = wire.get("history", {}).get("captured_source_counts", {})
    paired = history_counts.get("paired_calls", 0)
    items.append(
        _item(
            "core_history_multiturn",
            "preserved" if paired > 0 else "Unqualified",
            "codexhub-runtime-wire-fixture.json#history.captured_source_counts.paired_calls",
            "multi-turn history with paired call/output rows observed",
        )
    )

    call_links = wire.get("history", {}).get("call_links", [])
    items.append(
        _item(
            "core_history_item_ids",
            "preserved" if call_links else "Unqualified",
            "codexhub-runtime-wire-fixture.json#history.call_links",
            "call_item_id and output_item_id aliases preserved per link",
        )
    )
    items.append(
        _item(
            "core_history_call_ids",
            "preserved" if call_links else "Unqualified",
            "codexhub-runtime-wire-fixture.json#history.required_call_ids",
            "call_id aliases preserved and reconciled with call links",
        )
    )

    sse_events = wire.get("response", {}).get("streaming", {}).get("events", [])
    items.append(
        _item(
            "core_sse_streaming_events",
            "reversibly_adapted" if sse_events else "Unqualified",
            "codexhub-runtime-wire-fixture.json#response.streaming.events",
            "observed SSE event kinds sanitized to redacted payloads with stable event sequence",
        )
    )

    items.append(
        _item(
            "core_sse_terminal_events",
            "preserved"
            if _gate_is_complete(full_wire_gate) and _has_terminal_event(wire)
            else "Unqualified",
            "read-only-gate-audit.json#gateway_identity_route.observed_sse_event_type_counts",
            "terminal event classification requires independently captured full response-body evidence",
        )
    )
    items.append(
        _item(
            "core_sse_errors",
            "preserved"
            if _gate_is_complete(full_wire_gate) and _has_error_event(wire)
            else "Unqualified",
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
            "preserved" if codex_app_ns else "Unqualified",
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
            "preserved" if call_links_have_function else "Unqualified",
            "codexhub-runtime-wire-fixture.json#history.call_links",
            "function_call items with call_id and arguments aliases observed",
        )
    )
    items.append(
        _item(
            "core_function_result",
            "preserved" if call_links_have_function else "Unqualified",
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
            else "Unqualified",
            "codexhub-runtime-wire-fixture.json#history.call_links",
            "call/result links are present, but the bounded tool-membership replay does not prove a complete function replay across the real wire",
        )
    )

    items.append(
        _item(
            "identity_item_call_ids",
            "preserved" if call_links else "Unqualified",
            "codexhub-runtime-wire-fixture.json#history.call_links",
            "item/call id aliases preserved across pre/post-Gateway replay",
        )
    )
    items.append(
        _item(
            "identity_response_ids",
            "preserved"
            if streaming_captured
            and wire.get("pre_gateway", {})
            .get("response", {})
            .get("streaming", {})
            .get("response_id")
            == wire.get("post_gateway", {})
            .get("response", {})
            .get("streaming", {})
            .get("response_id")
            else "Unqualified",
            IDENTITY_RESPONSE_EVIDENCE,
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
            else "Unqualified",
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
    unknown_tag_count = _count_response_unknown_tags(wire)
    unknown_stream_count, unknown_non_stream_count = _unknown_tag_mode_counts(wire)

    choice_captured = (
        wire.get("pre_gateway", {}).get("choice_controls", {}).get("captured") is True
    )
    audit_choice = audit.get("gate_classification", {}).get("choice_controls")
    items.append(
        _item(
            "choice_controls",
            "Unqualified",
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
            else "Unqualified",
            "read-only-gate-audit.json#gate_classification.full_pre_post_request_response=live_control_required",
            "terminal event classification requires a real captured response body fingerprint",
        )
    )
    items.append(
        _item(
            "errors",
            "preserved"
            if _gate_is_complete(full_wire_gate) and _has_error_event(wire)
            else "Unqualified",
            "read-only-gate-audit.json#gate_classification.full_pre_post_request_response=live_control_required",
            "error classification requires independently captured pre/post response evidence",
        )
    )

    items.append(
        _item(
            "hosted_only_declarations",
            "preserved" if _gate_is_met(non_direct_gate) else "Unqualified",
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
            else "Unqualified",
            "codexhub-runtime-wire-fixture.json#response.streaming.events.tag=unknown",
            "unknown tagged sentinels are preserved as opaque replay sentinels; live runtime dispositions require a real unknown item",
        )
    )

    items.append(
        _item(
            "default_runtime_fields",
            "preserved" if _gate_is_complete(full_wire_gate) else "Unqualified",
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
    core_allowed = {
        "preserved",
        "reversibly_adapted",
        "local_consume",
        "Unqualified",
    }
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
    unqualified_scopes = sorted(
        item["scope"]
        for item in items
        if item["scope"] in BLOCKING_UNQUALIFIED_SCOPES
        and item["disposition"] == "Unqualified"
    )
    blocking_scopes = unqualified_scopes
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
        "sse_identity": {"complete", "met"},
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
        "ready_for_beta2": candidate_version_eligible
        and not blocking_scopes
        and not blocking_gates,
    }


def _validate_source_contract(source_contract: dict[str, Any]) -> dict[str, Any]:
    if (
        source_contract.get("schema_version") != 1
        or source_contract.get("fixture_kind") != "codex_cli_source_contract"
        or source_contract.get("capture_status") != "not_observed"
        or source_contract.get("qualification_status") != "unqualified"
        or "captured_at" not in source_contract
        or source_contract.get("captured_at") is not None
    ):
        raise ValueError("Codex 0.146 source contract must remain not_observed and unqualified")
    provenance = source_contract.get("provenance", {})
    expected = {
        "cli_version": DEFAULT_CLI_FLOOR,
        "source_commit": CLI_SOURCE_COMMIT,
        "cli_source_tag": CLI_SOURCE_TAG,
        "cli_source_commit_status": CLI_SOURCE_COMMIT_STATUS,
        "cli_binary_sha256": CLI_BINARY_SHA256,
        "candidate_revision": CANDIDATE_REVISION,
    }
    if any(provenance.get(field) != value for field, value in expected.items()):
        raise ValueError("Codex 0.146 source contract provenance is invalid")
    runtime_surface = source_contract.get("runtime_wire_surface")
    if not isinstance(runtime_surface, dict):
        raise ValueError("Codex 0.146 source contract runtime surface is missing")
    if runtime_surface.get("declaration_family_order") != list(STRUCTURAL_FAMILIES):
        raise ValueError("Codex 0.146 source contract declaration family order is invalid")
    declaration_families = runtime_surface.get("declaration_families")
    if not isinstance(declaration_families, list) or len(declaration_families) != len(
        STRUCTURAL_FAMILIES
    ):
        raise ValueError("Codex 0.146 source contract declaration families are invalid")
    declaration_families_by_name: dict[str, dict[str, Any]] = {}
    expected_observations = {
        "plain_function": "not_observed_source_contract_only",
        "custom_freeform": "not_observed_source_contract_only",
        "namespace": "not_observed_source_contract_only",
        "client_executed_tool_discovery": "not_observed_source_contract_only",
        "selected_provider_hosted": "not_observed_selected_provider_control_required",
        "unknown_future_kind": "opaque_sentinel_only",
    }
    for family, expected_family in zip(STRUCTURAL_FAMILIES, declaration_families):
        if not isinstance(expected_family, dict) or expected_family.get("family") != family:
            raise ValueError("Codex 0.146 source contract declaration family identity is invalid")
        if set(expected_family) != {
            "family",
            "runtime_type",
            "wire_declaration_type",
            "observed",
            "observation",
            "executor",
            "loss_boundary",
        }:
            raise ValueError(
                "Codex 0.146 source contract declaration family has unknown fields"
            )
        declaration_families_by_name[family] = expected_family
        if expected_family.get("observed") is not False:
            raise ValueError(
                "Codex 0.146 source contract cannot claim an observed declaration family"
            )
        if expected_family.get("observation") != expected_observations[family]:
            raise ValueError(
                "Codex 0.146 source contract declaration observation is invalid"
            )
    request_shape = runtime_surface.get("request_shape")
    non_streaming_control = (
        request_shape.get("non_streaming_control")
        if isinstance(request_shape, dict)
        else None
    )
    if not isinstance(non_streaming_control, dict) or non_streaming_control.get(
        "captured"
    ) is not False or non_streaming_control.get("status") != "unqualified":
        raise ValueError(
            "Codex 0.146 source contract cannot claim a captured non-streaming response"
        )
    examples = runtime_surface.get("declaration_family_examples")
    if not isinstance(examples, dict):
        raise ValueError("Codex 0.146 source contract declaration examples are missing")
    for family in STRUCTURAL_FAMILIES:
        example = examples.get(family)
        if not isinstance(example, dict):
            raise ValueError(f"Codex 0.146 source contract example is missing for {family}")
        expected_status_fields = {
            "selected_provider_hosted": {
                "observed": False,
                "status": "selected_provider_control_required",
                "provider_scope": "selected_provider_only",
                "cross_provider_proxy": "forbidden",
            },
            "unknown_future_kind": {
                "observed": False,
                "status": "opaque_sentinel_only",
            },
            }.get(family, {})
        expected_example_fields = {
            "declaration",
            "call",
            "result",
            "history",
            "streaming",
            "terminal",
            "error",
            "loss_boundary",
            *expected_status_fields,
        }
        if set(example) != expected_example_fields:
            raise ValueError(
                f"Codex 0.146 source contract {family} has unknown example fields"
            )
        for field, expected_value in expected_status_fields.items():
            if example.get(field) != expected_value:
                raise ValueError(
                    f"Codex 0.146 source contract {family} {field} status is invalid"
                )
        if not expected_status_fields and any(
            field in example for field in ("observed", "status")
        ):
            raise ValueError(
                f"Codex 0.146 source contract {family} has an unknown status field"
            )
        _validate_structural_family_schema(
            family,
            declaration_families_by_name[family],
            example,
            context=f"Codex 0.146 source contract family {family}",
        )
        terminal = example.get("terminal")
        error = example.get("error")
        if (
            not isinstance(terminal, dict)
            or terminal.get("event") != "response.completed"
            or terminal.get("classification") not in {"not_observed", "unqualified"}
        ):
            raise ValueError(f"Codex 0.146 source contract terminal status is invalid for {family}")
        if (
            not isinstance(error, dict)
            or error.get("event") != "response.failed"
            or error.get("classification") not in {"not_observed", "unqualified"}
        ):
            raise ValueError(f"Codex 0.146 source contract error status is invalid for {family}")
        if not isinstance(example.get("loss_boundary"), str) or not example["loss_boundary"]:
            raise ValueError(f"Codex 0.146 source contract loss boundary is invalid for {family}")
    response_shape = runtime_surface.get("response_shape")
    error_shape = response_shape.get("error_shape") if isinstance(response_shape, dict) else None
    if not isinstance(error_shape, dict) or error_shape.get("classification") != "unqualified":
        raise ValueError("Codex 0.146 source contract response error status is invalid")

    def _captured_true(value: Any) -> bool:
        if isinstance(value, dict):
            return any(
                (key in {"captured", "observed"} and child is True)
                or _captured_true(child)
                for key, child in value.items()
            )
        if isinstance(value, list):
            return any(_captured_true(child) for child in value)
        return False

    if _captured_true(runtime_surface):
        raise ValueError("Codex 0.146 source contract contains a captured response claim")
    return provenance


def _validate_candidate_binding(
    *,
    source_contract_data: dict[str, Any],
    trace_data: dict[str, Any],
    wire_data: dict[str, Any],
    audit_data: dict[str, Any],
    cli_version_floor: str,
    candidate_cli_version: str | None,
    candidate_source_commit: str | None,
) -> tuple[dict[str, Any], str]:
    _validate_supported_floor(cli_version_floor)
    contract_provenance = _validate_source_contract(source_contract_data)
    source = trace_data.get("source", {})
    planner_gates = trace_data.get("planner_gates", {})
    trace_cli_version = source.get("cli_version")
    trace_source_commit = planner_gates.get("source_commit")
    trace_capture_id = source.get("capture_id")
    trace_captured_at = trace_data.get("captured_at")
    if (
        not trace_cli_version
        or not trace_source_commit
        or not trace_capture_id
        or not trace_captured_at
    ):
        raise ValueError(
            "historical trace evidence is missing source.cli_version, "
            "source.capture_id, planner_gates.source_commit, or captured_at"
        )
    if not isinstance(trace_cli_version, str):
        raise ValueError("trace source.cli_version must be a string")
    try:
        _version_key(trace_cli_version)
    except ValueError as exc:
        raise ValueError("trace source.cli_version is malformed") from exc
    if (
        trace_cli_version != HISTORICAL_CLI_VERSION
        or trace_source_commit != HISTORICAL_SOURCE_COMMIT
        or trace_captured_at != HISTORICAL_CAPTURED_AT
    ):
        raise ValueError(
            "trace evidence must retain the historical Codex 0.144.0-alpha.4 provenance"
        )

    if candidate_cli_version is None:
        candidate_cli_version = contract_provenance["cli_version"]
    elif candidate_cli_version != contract_provenance["cli_version"]:
        raise ValueError(
            "candidate CLI version does not match the source contract: "
            f"requested={candidate_cli_version!r} observed={contract_provenance['cli_version']!r}"
        )
    if not isinstance(candidate_cli_version, str):
        raise ValueError("candidate CLI version must be a string")

    if candidate_source_commit is None:
        candidate_source_commit = contract_provenance["source_commit"]
    elif candidate_source_commit != contract_provenance["source_commit"]:
        raise ValueError(
            "candidate source commit does not match the source contract: "
            f"requested={candidate_source_commit!r} observed={contract_provenance['source_commit']!r}"
        )

    if not isinstance(candidate_source_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}", candidate_source_commit
    ):
        raise ValueError(f"candidate source commit is not a 40-character SHA-1: {candidate_source_commit!r}")
    try:
        _version_key(candidate_cli_version)
    except ValueError as exc:
        raise ValueError(
            f"candidate CLI version is malformed: {candidate_cli_version!r}"
        ) from exc

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
        or wire_provenance.get("capture_id") != trace_capture_id
        or wire_provenance.get("captured_at") != trace_captured_at
    ):
        raise ValueError("historical wire provenance is not bound to the historical trace")
    audit_provenance = audit_data.get("provenance", {})
    if (
        audit_provenance.get("capture_status") != "not_observed"
        or any(audit_provenance.get(field) != value for field, value in contract_provenance.items())
    ):
        raise ValueError("read-only audit provenance is not bound to the 0.146 source contract")
    historical_capture = audit_provenance.get("historical_capture")
    if (
        not isinstance(historical_capture, dict)
        or historical_capture.get("captured_at") != trace_captured_at
        or historical_capture.get("cli_version") != trace_cli_version
        or historical_capture.get("source_commit") != trace_source_commit
        or historical_capture.get("captured_at") != wire_provenance.get("captured_at")
        or historical_capture.get("cli_version") != wire_provenance.get("cli_version")
        or historical_capture.get("source_commit") != wire_provenance.get("source_commit")
    ):
        raise ValueError("read-only audit historical capture provenance is not bound")
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
        "candidate_revision": contract_provenance["candidate_revision"],
        "cli_binary_sha256": contract_provenance["cli_binary_sha256"],
        "cli_source_commit_status": contract_provenance["cli_source_commit_status"],
        "cli_source_tag": contract_provenance["cli_source_tag"],
    }
    return identity, status


def build_inventory(
    *,
    source_contract: Path = DEFAULT_SOURCE_CONTRACT,
    trace: Path,
    wire_fixture: Path,
    audit: Path,
    cli_version_floor: str = DEFAULT_CLI_FLOOR,
    candidate_cli_version: str | None = DEFAULT_CANDIDATE_CLI_VERSION,
    candidate_source_commit: str | None = DEFAULT_CANDIDATE_SOURCE_COMMIT,
) -> dict[str, Any]:
    cli_version_floor = _validate_supported_floor(cli_version_floor)
    trace_data = _load_json(trace)
    wire_data = _load_json(wire_fixture)
    audit_data = _load_json(audit)
    source_contract_data = _load_json(source_contract)
    wire_fixture_sha256 = _sha256_file(wire_fixture)
    if (
        trace_data.get("schema_version") != 4
        or wire_data.get("schema_version") != 1
        or wire_data.get("fixture_kind") != "sanitized_artifact_backed_replay"
        or audit_data.get("schema_version") != 1
        or audit_data.get("capture_kind") != "sanitized_bounded_read_only_audit"
    ):
        raise ValueError("Issue #62 evidence schema identity is invalid")
    _validate_source_contract(source_contract_data)
    _validate_core_evidence_pointers(wire=wire_data, audit=audit_data)

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

    unknown_tagged_source_count = _count_response_unknown_tags(wire_data)
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
    structural_families = _build_structural_inventory(
        source_contract=source_contract_data,
        trace=trace_data,
        wire=wire_data,
        audit=audit_data,
    )
    candidate_identity, candidate_version_status = _validate_candidate_binding(
        source_contract_data=source_contract_data,
        trace_data=trace_data,
        wire_data=wire_data,
        audit_data=audit_data,
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
        "source_contract": {
            "file": source_contract.name,
            "sha256": _sha256_file(source_contract),
        },
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
        "declaration_families": structural_families,
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
    mismatches.extend(
        f"mutation: {message}"
        for message in _structural_inventory_mismatches(
            inventory.get("declaration_families"),
            require_unobserved=evidence_root is None,
        )
    )

    items = inventory.get("items", [])
    if not isinstance(items, list):
        mismatches.append("inventory items must be an array")
        items = []
    seen_scopes: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            mismatches.append("item must be an object")
            continue
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
        evidence_source = item.get("evidence_source")
        if not isinstance(evidence_source, str) or not evidence_source:
            mismatches.append(f"mutation: {scope}: evidence_source is missing")
        elif not _evidence_source_allowed(scope, evidence_source):
            mismatches.append(
                f"mutation: {scope}: evidence_source {evidence_source!r} "
                "does not match the expected fixture path/scope"
            )
        if scope in CORE_CONTRACT_SCOPES and disposition not in CORE_FINAL_DISPOSITIONS:
            mismatches.append(
                f"mutation: {scope}: core disposition {disposition!r} is not a final "
                "preserved, reversibly_adapted, or Unqualified disposition"
            )
        if (
            scope in CORE_REQUIRED_PRESERVED_SCOPES
            and disposition not in {"preserved", "reversibly_adapted"}
        ):
            mismatches.append(
                f"mutation: {scope}: bounded contract evidence requires a preserved "
                "or reversibly_adapted disposition"
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
    mismatches.extend(
        _candidate_identity_mismatches(
            candidate_identity, enforce_retained=evidence_root is None
        )
    )
    if not isinstance(candidate_identity, dict):
        candidate_identity = {}
    for field in REQUIRED_CANDIDATE_FIELDS:
        if field not in candidate_identity:
            mismatches.append(f"loss: candidate_identity.{field} is missing")

    qualification = inventory.get("qualification", {})
    if not isinstance(qualification, dict):
        mismatches.append("qualification must be an object")
        qualification = {}
    try:
        _validate_supported_floor(str(inventory.get("cli_version_floor", "")))
    except ValueError as exc:
        mismatches.append(str(exc))
    candidate_status = qualification.get("candidate_version_status")
    if candidate_status not in {"eligible", "legacy_below_floor"}:
        mismatches.append("qualification.candidate_version_status is invalid")
    candidate_eligible = qualification.get("candidate_version_eligible")
    if candidate_eligible is not (candidate_status == "eligible"):
        mismatches.append(
            "qualification.candidate_version_eligible does not match candidate version status"
        )
    actual_blocking_scopes = sorted(
        {
            item.get("scope")
            for item in items
            if item.get("scope") in BLOCKING_UNQUALIFIED_SCOPES
            and item.get("disposition") == "Unqualified"
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
    if "ready_for_beta1" in qualification:
        mismatches.append(
            "qualification.ready_for_beta1 is stale; use ready_for_beta2"
        )
    accepted_gate_statuses = {
        "complete_model_visible_plan": {"complete"},
        "clean_cold_start_current_binding": {"complete", "pass"},
        "full_pre_post_request_response": {"complete", "met"},
        "full_request_fingerprint": {"captured", "complete", "met"},
        "full_response_fingerprint": {"captured", "complete", "met"},
        "sse_identity": {"complete", "met"},
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
    if qualification.get("ready_for_beta2") is not expected_ready:
        mismatches.append(
            "qualification.ready_for_beta2 is inconsistent with candidate eligibility and blockers"
        )
    if evidence_root is None and qualification.get("ready_for_beta2") is True:
        mismatches.append(
            "qualification.ready_for_beta2 cannot be asserted without bound evidence"
        )
    if evidence_root is None and not actual_blocking_gates:
        mismatches.append(
            "qualification.blocking_gates cannot be empty without bound evidence"
        )

    evidence_binding = inventory.get("evidence_binding", {})
    if not isinstance(evidence_binding, dict):
        mismatches.append("evidence_binding must be an object")
        evidence_binding = {}
    elif set(evidence_binding) != set(EXPECTED_EVIDENCE_BINDING_FILES):
        mismatches.append("evidence_binding has an unexpected key set")
    for name in ("source_contract", "trace", "wire_fixture", "audit"):
        entry = evidence_binding.get(name, {})
        if not isinstance(entry, dict) or not entry.get("file") or not re.fullmatch(
            r"[0-9a-f]{64}", str(entry.get("sha256", ""))
        ):
            mismatches.append(f"loss: evidence_binding.{name} is missing or malformed")
        elif entry.get("file") != EXPECTED_EVIDENCE_BINDING_FILES[name]:
            mismatches.append(
                f"mutation: evidence_binding.{name}.file does not name the retained fixture"
            )

    if evidence_binding and all(
        isinstance(evidence_binding.get(name), dict)
        for name in ("source_contract", "trace", "wire_fixture", "audit")
    ):
        manifest = _evidence_manifest_sha256(evidence_binding)
        if candidate_identity.get("evidence_manifest_sha256") != manifest:
            mismatches.append("loss: candidate_identity.evidence_manifest_sha256 is stale")

    if evidence_root is not None and not mismatches:
        bound_paths: dict[str, Path] = {}
        for name in ("source_contract", "trace", "wire_fixture", "audit"):
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
            source_contract_data = _load_json(bound_paths["source_contract"])
            if (
                source_contract_data.get("schema_version") != 1
                or source_contract_data.get("fixture_kind") != "codex_cli_source_contract"
                or
                trace_data.get("schema_version") != 4
                or wire_data.get("schema_version") != 1
                or wire_data.get("fixture_kind") != "sanitized_artifact_backed_replay"
                or audit_data.get("schema_version") != 1
                or audit_data.get("capture_kind") != "sanitized_bounded_read_only_audit"
            ):
                mismatches.append("mutation: bound evidence schema identity is invalid")
                return {"reconciled": False, "mismatches": mismatches}
            try:
                _validate_source_contract(source_contract_data)
                _validate_structural_evidence_pointers(
                    source_contract=source_contract_data,
                    wire=wire_data,
                    audit=audit_data,
                    declaration_families=inventory.get("declaration_families", []),
                )
                _validate_core_evidence_pointers(wire=wire_data, audit=audit_data)
            except ValueError as exc:
                mismatches.append(f"mutation: bound evidence pointer validation failed: {exc}")
                return {"reconciled": False, "mismatches": mismatches}
            try:
                expected_identity, expected_status_from_evidence = _validate_candidate_binding(
                    source_contract_data=source_contract_data,
                    trace_data=trace_data,
                    wire_data=wire_data,
                    audit_data=audit_data,
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
                    "ready_for_beta2",
                ):
                    if qualification.get(field) != expected_qualification[field]:
                        mismatches.append(
                            f"mutation: qualification.{field} does not match bound evidence"
                        )
                try:
                    generated_inventory = build_inventory(
                        source_contract=bound_paths["source_contract"],
                        trace=bound_paths["trace"],
                        wire_fixture=bound_paths["wire_fixture"],
                        audit=bound_paths["audit"],
                        cli_version_floor=str(inventory.get("cli_version_floor", "")),
                        candidate_cli_version=candidate_identity.get("cli_version"),
                        candidate_source_commit=candidate_identity.get("source_commit"),
                    )
                except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
                    mismatches.append(f"generated inventory check failed: {exc}")
                else:
                    if json.dumps(generated_inventory, sort_keys=True) != json.dumps(
                        inventory, sort_keys=True
                    ):
                        mismatches.append(
                            "generated inventory differs from committed artifact"
                        )

    identity_control = inventory.get("identity_control", {})
    if not isinstance(identity_control, dict):
        mismatches.append("identity_control must be an object")
        identity_control = {}
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
    if evidence_root is not None:
        wire_entry = evidence_binding.get("wire_fixture", {})
        wire_path = evidence_root / str(wire_entry.get("file", ""))
        if wire_path.is_file():
            actual_unknown_tagged_source_count = _count_response_unknown_tags(
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
        "--source-contract",
        type=Path,
        default=DEFAULT_SOURCE_CONTRACT,
    )
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
    parser.add_argument(
        "--check-drift",
        action="store_true",
        help="validate that --out exactly matches freshly generated inventory without writing it",
    )
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()
    inventory = build_inventory(
        source_contract=args.source_contract,
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

    if args.check_drift:
        if not args.out.is_file():
            raise SystemExit(f"committed inventory artifact not found: {args.out}")
        committed = _load_json(args.out)
        if json.dumps(committed, sort_keys=True) != json.dumps(
            inventory, sort_keys=True
        ):
            raise SystemExit(
                "generated inventory differs from committed artifact: "
                f"{args.out}"
            )
        print(f"generated inventory matches {args.out}")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(inventory, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
