#!/usr/bin/env python3
"""Build a sanitized, candidate-bound control manifest for Issue #62.

The live sidecar deliberately records only byte/SSE fingerprints.  This
evidence-only helper joins one pre-hop and one post-hop sidecar record with a
small, already-sanitized semantic summary produced by the control harness.
It never opens a network connection and never reads or writes a raw request,
response, header, URL, credential, prompt, tool argument, or wire identifier.

The resulting manifest is a binding artifact, not a qualification by itself.
``synthetic_fixture_only`` is permanently ineligible for the Issue #62 gate;
an operator must explicitly use ``authorized_live_control`` after an
authorized isolated run and independently review the artifact.

Candidate provenance is explicit: the npm package version and SHA-256 are
bound, while the registry's missing Codex source commit is represented as
``cli_source_commit: null`` with
``cli_source_commit_status: not_published_by_registry``.  A placeholder hash
must never be used in place of an unavailable source commit.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


SCHEMA = "codexhub.issue62.control-manifest.v1"
SYNTHETIC_SCOPE = "synthetic_fixture_only"
LIVE_SCOPE = "authorized_live_control"
VERIFICATION_SCOPES = frozenset({SYNTHETIC_SCOPE, LIVE_SCOPE})
QUALIFICATION_REASONS = {
    SYNTHETIC_SCOPE: "synthetic_fixture_only",
    LIVE_SCOPE: "wire replay and final inventory reconciliation required",
}
MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "verification_scope",
        "candidate_identity",
        "controls",
        "identity_control",
        "planner",
        "capture_manifest_sha256",
        "qualification",
    }
)

# These are deliberately semantic labels, not arbitrary response/event text.
CONTROL_NAMES = (
    "streaming_text",
    "streaming_function_history",
    "non_streaming_text",
    "choice_auto",
    "choice_none",
    "terminal_success",
    "terminal_error",
    "error_json",
)
CONTROL_NAME_SET = frozenset(CONTROL_NAMES)
IDENTITY_BOOLEAN_FIELDS = (
    "request_pair_preserved",
    "response_ref_preserved",
    "item_refs_preserved",
    "call_links_preserved",
)
TERMINAL_CLASSES = frozenset(
    {"response.completed", "response.failed", "error", "json_response"}
)
SSE_TERMINAL_CLASSES = frozenset(
    {"response.completed", "response.failed", "error", "done"}
)
CONTENT_TYPE_CLASSES = frozenset({"event-stream", "json"})
TOOL_CHOICES = frozenset({"auto", "none", "required", "function", None})
STATUS_CLASSES = frozenset({"2xx", "4xx", "5xx"})
EVENT_TYPES = frozenset(
    {
        "response.in_progress",
        "response.output_text.delta",
        "response.output_text.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.completed",
        "response.failed",
        "error",
        "unknown",
    }
)
ITEM_TYPES = frozenset(
    {
        "message",
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "unknown",
    }
)

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SHA1_OR_HEX64 = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SEMVER = re.compile(r"0\.(?:\d+)\.(?:\d+)(?:-[0-9A-Za-z.-]+)?\Z")

PLANNER_FIELDS = frozenset(
    {"model_visible_plan", "hosted_only_disposition", "unknown_tag_disposition"}
)
PLANNER_PLAN_STATES = frozenset({"complete", "partial", "not_captured"})
PLANNER_DISPOSITIONS = frozenset(
    {
        "preserved",
        "reversibly_adapted",
        "local_consume",
        "Unsupported",
        "Unqualified",
    }
)
DEFAULT_PLANNER = {
    "model_visible_plan": "not_captured",
    "hosted_only_disposition": "Unqualified",
    "unknown_tag_disposition": "Unqualified",
}

_SIDEcar_ROOT_FIELDS = frozenset(
    {
        "schema",
        "verification_scope",
        "capture_id",
        "hop",
        "outcome",
        "failure",
        "status",
        "content_type_class",
        "request",
        "response",
        "sse",
    }
)
_BODY_FIELDS = frozenset({"bytes", "sha256", "hmac_sha256", "complete"})
_SSE_FIELDS = frozenset(
    {
        "complete",
        "frame_count",
        "frame_bytes",
        "sequence_sha256",
        "sequence_hmac_sha256",
        "terminal_classes",
    }
)


class ManifestValidationError(ValueError):
    """A fixed, sanitized manifest validation failure."""


def _require_exact_fields(value: Mapping[str, Any], expected: frozenset[str], code: str) -> None:
    if frozenset(value) != expected:
        raise ManifestValidationError(code)


def _require_hex(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ManifestValidationError(code)
    return value


def _require_bool(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestValidationError(code)
    return value


def _require_nonnegative_int(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManifestValidationError(code)
    return value


def _digest_summary(value: Any, code: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ManifestValidationError(code)
    _require_exact_fields(value, _BODY_FIELDS, code)
    complete = _require_bool(value.get("complete"), code)
    bytes_count = _require_nonnegative_int(value.get("bytes"), code)
    sha = value.get("sha256")
    hmac_sha = value.get("hmac_sha256")
    if complete:
        _require_hex(sha, _HEX64, code)
        _require_hex(hmac_sha, _HEX64, code)
    elif sha is not None or hmac_sha is not None:
        raise ManifestValidationError(code)
    # Do not retain capture ids or any input mapping; these are the only body
    # facts needed to prove independent pre/post byte equality.
    return {
        "bytes": bytes_count,
        "sha256": sha,
        "hmac_sha256": hmac_sha,
        "complete": complete,
    }


def _sse_summary(value: Any, code: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ManifestValidationError(code)
    _require_exact_fields(value, _SSE_FIELDS, code)
    complete = _require_bool(value.get("complete"), code)
    frame_count = _require_nonnegative_int(value.get("frame_count"), code)
    frame_bytes = _require_nonnegative_int(value.get("frame_bytes"), code)
    sequence_sha = value.get("sequence_sha256")
    sequence_hmac = value.get("sequence_hmac_sha256")
    if complete:
        _require_hex(sequence_sha, _HEX64, code)
        _require_hex(sequence_hmac, _HEX64, code)
    elif sequence_sha is not None or sequence_hmac is not None:
        raise ManifestValidationError(code)
    terminal_classes = value.get("terminal_classes")
    if (
        not isinstance(terminal_classes, list)
        or any(item not in SSE_TERMINAL_CLASSES for item in terminal_classes)
        or terminal_classes != sorted(set(terminal_classes))
    ):
        raise ManifestValidationError(code)
    return {
        "complete": complete,
        "frame_count": frame_count,
        "frame_bytes": frame_bytes,
        "sequence_sha256": sequence_sha,
        "sequence_hmac_sha256": sequence_hmac,
        "terminal_classes": list(terminal_classes),
    }


def sanitize_sidecar_record(record: Mapping[str, Any], *, expected_hop: str) -> dict[str, Any]:
    """Return the sidecar facts safe for a control manifest.

    ``capture_id`` is validated but intentionally discarded.  The caller
    binds the two hops by control ordinal/label in a separate manifest rather
    than persisting opaque transport identifiers.
    """

    if not isinstance(record, Mapping):
        raise ManifestValidationError("sidecar_record_invalid")
    _require_exact_fields(record, _SIDEcar_ROOT_FIELDS, "sidecar_record_fields_invalid")
    if record.get("schema") != "codexhub.issue62.live-evidence-lane.v1":
        raise ManifestValidationError("sidecar_record_schema_invalid")
    if record.get("verification_scope") != "capture_only_not_qualification":
        raise ManifestValidationError("sidecar_record_scope_invalid")
    if record.get("hop") != expected_hop:
        raise ManifestValidationError("sidecar_record_hop_invalid")
    capture_id = record.get("capture_id")
    if not isinstance(capture_id, str) or not re.fullmatch(r"c[0-9a-f]{32}\Z", capture_id):
        raise ManifestValidationError("sidecar_record_capture_id_invalid")
    outcome = record.get("outcome")
    if outcome not in {"complete", "incomplete"}:
        raise ManifestValidationError("sidecar_record_outcome_invalid")
    failure = record.get("failure")
    if outcome == "complete" and failure is not None:
        raise ManifestValidationError("sidecar_record_completion_invalid")
    if outcome == "incomplete" and not isinstance(failure, str):
        raise ManifestValidationError("sidecar_record_failure_invalid")
    status = record.get("status")
    if status is not None and (
        isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599
    ):
        raise ManifestValidationError("sidecar_record_status_invalid")
    content_type = record.get("content_type_class")
    if content_type not in {"event-stream", "json", "other", "absent", "unknown"}:
        raise ManifestValidationError("sidecar_record_content_type_invalid")
    return {
        "outcome": outcome,
        "failure": failure,
        "status": status,
        "content_type_class": content_type,
        "request": _digest_summary(record.get("request"), "sidecar_record_request_invalid"),
        "response": _digest_summary(record.get("response"), "sidecar_record_response_invalid"),
        "sse": _sse_summary(record.get("sse"), "sidecar_record_sse_invalid"),
    }


_SANITIZED_SIDECAR_FIELDS = frozenset(
    {"outcome", "failure", "status", "content_type_class", "request", "response", "sse"}
)


def _validate_sanitized_sidecar(record: Any, *, code_prefix: str) -> dict[str, Any]:
    """Validate one sidecar already embedded in a manifest.

    ``sanitize_sidecar_record`` intentionally drops transport-only fields such
    as ``capture_id`` and ``hop``.  A generated manifest therefore cannot be
    sent through that raw-record validator during reconciliation.  Keep a
    separate validator for the canonical, seven-field sidecar representation
    and normalize it to a fresh mapping so callers never retain untrusted
    nested objects.
    """

    if not isinstance(record, Mapping):
        raise ManifestValidationError(f"{code_prefix}_invalid")
    _require_exact_fields(record, _SANITIZED_SIDECAR_FIELDS, f"{code_prefix}_fields_invalid")
    outcome = record.get("outcome")
    if outcome not in {"complete", "incomplete"}:
        raise ManifestValidationError(f"{code_prefix}_outcome_invalid")
    failure = record.get("failure")
    if outcome == "complete" and failure is not None:
        raise ManifestValidationError(f"{code_prefix}_completion_invalid")
    if outcome == "incomplete" and not isinstance(failure, str):
        raise ManifestValidationError(f"{code_prefix}_failure_invalid")
    status = record.get("status")
    if status is not None and (
        isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599
    ):
        raise ManifestValidationError(f"{code_prefix}_status_invalid")
    content_type = record.get("content_type_class")
    if content_type not in {"event-stream", "json", "other", "absent", "unknown"}:
        raise ManifestValidationError(f"{code_prefix}_content_type_invalid")
    return {
        "outcome": outcome,
        "failure": failure,
        "status": status,
        "content_type_class": content_type,
        "request": _digest_summary(record.get("request"), f"{code_prefix}_request_invalid"),
        "response": _digest_summary(record.get("response"), f"{code_prefix}_response_invalid"),
        "sse": _sse_summary(record.get("sse"), f"{code_prefix}_sse_invalid"),
    }


def _sanitize_name_list(value: Any, allowed: frozenset[str], code: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or item not in allowed for item in value):
        raise ManifestValidationError(code)
    if value != sorted(set(value)):
        raise ManifestValidationError(code)
    return list(value)


def _sanitize_request_shape(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestValidationError("request_shape_invalid")
    expected = frozenset(
        {
            "model",
            "stream",
            "tool_choice",
            "parallel_tool_calls",
            "input_item_types",
            "tool_names",
            "additional_tools_present",
        }
    )
    _require_exact_fields(value, expected, "request_shape_fields_invalid")
    model = value.get("model")
    if not isinstance(model, str) or not model or "/" in model or model.startswith("http"):
        raise ManifestValidationError("request_shape_model_invalid")
    stream = _require_bool(value.get("stream"), "request_shape_stream_invalid")
    tool_choice = value.get("tool_choice")
    if tool_choice not in TOOL_CHOICES:
        raise ManifestValidationError("request_shape_choice_invalid")
    parallel = value.get("parallel_tool_calls")
    if parallel is not None:
        _require_bool(parallel, "request_shape_parallel_invalid")
    input_item_types = value.get("input_item_types")
    if not isinstance(input_item_types, list):
        raise ManifestValidationError("request_shape_input_types_invalid")
    sanitized_input_types: list[dict[str, Any]] = []
    for item in input_item_types:
        if not isinstance(item, Mapping) or frozenset(item) != {"type", "count"}:
            raise ManifestValidationError("request_shape_input_type_entry_invalid")
        item_type = item.get("type")
        if item_type not in ITEM_TYPES:
            raise ManifestValidationError("request_shape_input_type_invalid")
        sanitized_input_types.append(
            {"type": item_type, "count": _require_nonnegative_int(item.get("count"), "request_shape_input_count_invalid")}
        )
    if sanitized_input_types != sorted(sanitized_input_types, key=lambda item: item["type"]):
        raise ManifestValidationError("request_shape_input_types_unsorted")
    tool_names = _sanitize_name_list(value.get("tool_names"), frozenset({"shell_command", "apply_patch", "tool_search", "function"}), "request_shape_tools_invalid")
    return {
        "model": model,
        "stream": stream,
        "tool_choice": tool_choice,
        "parallel_tool_calls": parallel,
        "input_item_types": sanitized_input_types,
        "tool_names": tool_names,
        "additional_tools_present": _require_bool(value.get("additional_tools_present"), "request_shape_additional_tools_invalid"),
    }


def _sanitize_response_shape(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestValidationError("response_shape_invalid")
    expected = frozenset(
        {
            "content_type_class",
            "status_class",
            "terminal",
            "event_types",
            "item_types",
            "unknown_tag_count",
            "response_ref_present",
            "item_ref_count",
            "call_link_count",
        }
    )
    _require_exact_fields(value, expected, "response_shape_fields_invalid")
    content_type = value.get("content_type_class")
    if content_type not in CONTENT_TYPE_CLASSES:
        raise ManifestValidationError("response_shape_content_type_invalid")
    status_class = value.get("status_class")
    if status_class not in STATUS_CLASSES:
        raise ManifestValidationError("response_shape_status_invalid")
    terminal = value.get("terminal")
    if terminal not in TERMINAL_CLASSES:
        raise ManifestValidationError("response_shape_terminal_invalid")
    event_types = value.get("event_types")
    if (
        not isinstance(event_types, list)
        or any(not isinstance(item, str) or item not in EVENT_TYPES for item in event_types)
    ):
        raise ManifestValidationError("response_shape_events_invalid")
    item_types = _sanitize_name_list(value.get("item_types"), ITEM_TYPES, "response_shape_items_invalid")
    return {
        "content_type_class": content_type,
        "status_class": status_class,
        "terminal": terminal,
        # Keep event order: terminal placement and function-call sequencing are
        # part of the core Responses contract.  The values are an allow-listed
        # vocabulary, never raw SSE data.
        "event_types": list(event_types),
        "item_types": item_types,
        "unknown_tag_count": _require_nonnegative_int(value.get("unknown_tag_count"), "response_shape_unknown_count_invalid"),
        # Keep only aggregate reference facts; never retain actual wire IDs.
        "response_ref_present": _require_bool(value.get("response_ref_present"), "response_shape_response_ref_invalid"),
        "item_ref_count": _require_nonnegative_int(value.get("item_ref_count"), "response_shape_item_ref_count_invalid"),
        "call_link_count": _require_nonnegative_int(value.get("call_link_count"), "response_shape_call_link_count_invalid"),
    }


def _sanitize_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestValidationError("identity_invalid")
    expected = frozenset(
        {
            "request_pair_preserved",
            "response_ref_preserved",
            "item_refs_preserved",
            "call_links_preserved",
            "unclassified_core_items",
        }
    )
    _require_exact_fields(value, expected, "identity_fields_invalid")
    result = {
        key: _require_bool(value[key], "identity_boolean_invalid")
        for key in IDENTITY_BOOLEAN_FIELDS
    }
    result["unclassified_core_items"] = _require_nonnegative_int(
        value.get("unclassified_core_items"), "identity_unclassified_invalid"
    )
    return result


def _sanitize_route(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestValidationError("route_identity_invalid")
    expected = frozenset(
        {
            "model",
            "upstream",
            "route_mode",
            "behavior_profile",
            "inbound_format",
            "upstream_format",
            "route_digest",
        }
    )
    _require_exact_fields(value, expected, "route_identity_fields_invalid")
    expected_values = {
        "upstream": "official",
        "route_mode": "official",
        "behavior_profile": "official_codex_app_http_passthrough",
        "inbound_format": "responses",
        "upstream_format": "responses",
    }
    for key, expected_value in expected_values.items():
        if value.get(key) != expected_value:
            raise ManifestValidationError("route_identity_value_invalid")
    model = value.get("model")
    if not isinstance(model, str) or not model or "/" in model:
        raise ManifestValidationError("route_identity_model_invalid")
    _require_hex(value.get("route_digest"), _HEX64, "route_identity_digest_invalid")
    return {key: value[key] for key in expected}


def _pair_equal(pre: Mapping[str, Any], post: Mapping[str, Any], key: str) -> bool:
    left = pre.get(key)
    right = post.get(key)
    if left is None or right is None:
        return left is right
    return left == right


def _require_complete_fingerprints(pre: Mapping[str, Any], post: Mapping[str, Any]) -> None:
    """Require complete request/response fingerprints for a control pair.

    A complete sidecar with a missing digest cannot prove passthrough.  Treat
    that as invalid instead of allowing ``None == None`` to appear equal.
    """

    for side, label in ((pre, "request"), (post, "request")):
        fingerprint = side.get(label)
        if fingerprint is None:
            raise ManifestValidationError("control_request_fingerprint_missing")
        if not isinstance(fingerprint, Mapping) or fingerprint.get("complete") is not True:
            raise ManifestValidationError("control_request_fingerprint_incomplete")
        if not fingerprint.get("sha256") or not fingerprint.get("hmac_sha256"):
            raise ManifestValidationError("control_request_fingerprint_missing_digest")
    for side, label in ((pre, "response"), (post, "response")):
        fingerprint = side.get(label)
        if fingerprint is None:
            raise ManifestValidationError("control_response_fingerprint_missing")
        if not isinstance(fingerprint, Mapping) or fingerprint.get("complete") is not True:
            raise ManifestValidationError("control_response_fingerprint_incomplete")
        if not fingerprint.get("sha256") or not fingerprint.get("hmac_sha256"):
            raise ManifestValidationError("control_response_fingerprint_missing_digest")


def _require_complete_sse_fingerprints(pre: Mapping[str, Any], post: Mapping[str, Any]) -> None:
    """Require complete ordered SSE fingerprints for streaming controls."""

    for side in (pre, post):
        fingerprint = side.get("sse")
        if fingerprint is None:
            raise ManifestValidationError("control_sse_fingerprint_missing")
        if not isinstance(fingerprint, Mapping) or fingerprint.get("complete") is not True:
            raise ManifestValidationError("control_sse_fingerprint_incomplete")
        if not fingerprint.get("sequence_sha256") or not fingerprint.get("sequence_hmac_sha256"):
            raise ManifestValidationError("control_sse_fingerprint_missing_digest")


def _sanitize_control(control: Mapping[str, Any]) -> dict[str, Any]:
    expected = frozenset(
        {
            "name",
            "pre",
            "post",
            "request_shape",
            "response_shape",
            "identity",
            "route_identity",
        }
    )
    if not isinstance(control, Mapping):
        raise ManifestValidationError("control_invalid")
    _require_exact_fields(control, expected, "control_fields_invalid")
    name = control.get("name")
    if name not in CONTROL_NAME_SET:
        raise ManifestValidationError("control_name_invalid")
    pre = sanitize_sidecar_record(control["pre"], expected_hop="pre")
    post = sanitize_sidecar_record(control["post"], expected_hop="post")
    if pre["outcome"] != "complete" or post["outcome"] != "complete":
        raise ManifestValidationError("control_capture_incomplete")
    _require_complete_fingerprints(pre, post)
    if not _pair_equal(pre, post, "request") or not _pair_equal(pre, post, "response"):
        raise ManifestValidationError("control_body_fingerprint_mismatch")
    if pre["content_type_class"] != post["content_type_class"]:
        raise ManifestValidationError("control_content_type_mismatch")
    if pre["content_type_class"] == "event-stream":
        if pre["sse"] is None or post["sse"] is None or not _pair_equal(pre, post, "sse"):
            raise ManifestValidationError("control_sse_fingerprint_mismatch")
        _require_complete_sse_fingerprints(pre, post)
    elif pre["sse"] is not None or post["sse"] is not None:
        raise ManifestValidationError("control_json_sse_mismatch")
    request_shape = _sanitize_request_shape(control.get("request_shape"))
    response_shape = _sanitize_response_shape(control.get("response_shape"))
    identity = _sanitize_identity(control.get("identity"))
    route = _sanitize_route(control.get("route_identity"))
    if request_shape["model"] != route["model"]:
        raise ManifestValidationError("control_model_binding_mismatch")
    if request_shape["stream"] != (pre["content_type_class"] == "event-stream"):
        raise ManifestValidationError("control_stream_mode_mismatch")
    if response_shape["content_type_class"] != pre["content_type_class"]:
        raise ManifestValidationError("control_response_shape_content_mismatch")
    if response_shape["terminal"] == "json_response" and pre["content_type_class"] != "json":
        raise ManifestValidationError("control_terminal_content_mismatch")
    if response_shape["terminal"] != "json_response" and pre["content_type_class"] == "json":
        raise ManifestValidationError("control_json_terminal_invalid")
    _validate_control_semantics(name, request_shape, response_shape)
    if identity["unclassified_core_items"] != 0:
        raise ManifestValidationError("control_identity_unclassified")
    return {
        "name": name,
        "pre": pre,
        "post": post,
        "request_shape": request_shape,
        "response_shape": response_shape,
        "identity": identity,
        "route_identity": route,
        "body_equality": {
            "request": True,
            "response": True,
            "sse": pre["sse"] is not None,
        },
    }


def _validate_control_semantics(
    name: str,
    request_shape: Mapping[str, Any],
    response_shape: Mapping[str, Any],
) -> None:
    """Bind each control label to its intended request/response contract."""

    stream = request_shape["stream"]
    choice = request_shape["tool_choice"]
    parallel = request_shape["parallel_tool_calls"]
    terminal = response_shape["terminal"]
    status = response_shape["status_class"]
    input_types = {item["type"] for item in request_shape["input_item_types"] if item["count"] > 0}
    response_items = set(response_shape["item_types"])
    if name in {"streaming_text", "streaming_function_history", "terminal_success", "terminal_error"} and not stream:
        raise ManifestValidationError("control_label_stream_contract_invalid")
    if name in {"non_streaming_text", "choice_none", "error_json"} and stream:
        raise ManifestValidationError("control_label_non_stream_contract_invalid")
    if name == "choice_auto" and (choice != "auto" or parallel is not True):
        raise ManifestValidationError("control_label_choice_auto_invalid")
    if name == "choice_none" and choice != "none":
        raise ManifestValidationError("control_label_choice_none_invalid")
    if name == "terminal_success" and (terminal != "response.completed" or status != "2xx"):
        raise ManifestValidationError("control_label_terminal_success_invalid")
    if name == "terminal_error" and (terminal != "response.failed" or status not in {"4xx", "5xx"}):
        raise ManifestValidationError("control_label_terminal_error_invalid")
    if name == "error_json" and (terminal != "json_response" or status not in {"4xx", "5xx"}):
        raise ManifestValidationError("control_label_error_json_invalid")
    if name == "streaming_function_history":
        required = {"function_call", "function_call_output"}
        if not required.issubset(input_types) or not required.issubset(response_items):
            raise ManifestValidationError("control_label_function_history_items_invalid")
        if response_shape["call_link_count"] < 1:
            raise ManifestValidationError("control_label_function_history_links_invalid")


def _validate_canonical_control(control: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the canonical control shape stored in a manifest.

    Build inputs contain complete raw sidecar records (including capture-only
    metadata), while manifests contain the sanitized sidecars returned by
    ``_sanitize_control`` plus the derived ``body_equality`` field.  These are
    deliberately different schemas; reconciliation must validate the latter
    directly instead of trying to feed it back through the raw sanitizer.
    """

    expected = frozenset(
        {
            "name",
            "pre",
            "post",
            "request_shape",
            "response_shape",
            "identity",
            "route_identity",
            "body_equality",
        }
    )
    if not isinstance(control, Mapping):
        raise ManifestValidationError("control_invalid")
    _require_exact_fields(control, expected, "canonical_control_fields_invalid")
    name = control.get("name")
    if name not in CONTROL_NAME_SET:
        raise ManifestValidationError("control_name_invalid")
    pre = _validate_sanitized_sidecar(control.get("pre"), code_prefix="canonical_pre")
    post = _validate_sanitized_sidecar(control.get("post"), code_prefix="canonical_post")
    if pre["outcome"] != "complete" or post["outcome"] != "complete":
        raise ManifestValidationError("control_capture_incomplete")
    _require_complete_fingerprints(pre, post)
    request_equal = _pair_equal(pre, post, "request")
    response_equal = _pair_equal(pre, post, "response")
    if not request_equal or not response_equal:
        raise ManifestValidationError("control_body_fingerprint_mismatch")
    content_equal = pre["content_type_class"] == post["content_type_class"]
    if not content_equal:
        raise ManifestValidationError("control_content_type_mismatch")
    if pre["content_type_class"] == "event-stream":
        if pre["sse"] is None or post["sse"] is None or not _pair_equal(pre, post, "sse"):
            raise ManifestValidationError("control_sse_fingerprint_mismatch")
        _require_complete_sse_fingerprints(pre, post)
    elif pre["sse"] is not None or post["sse"] is not None:
        raise ManifestValidationError("control_json_sse_mismatch")

    request_shape = _sanitize_request_shape(control.get("request_shape"))
    response_shape = _sanitize_response_shape(control.get("response_shape"))
    identity = _sanitize_identity(control.get("identity"))
    route = _sanitize_route(control.get("route_identity"))
    if request_shape["model"] != route["model"]:
        raise ManifestValidationError("control_model_binding_mismatch")
    if request_shape["stream"] != (pre["content_type_class"] == "event-stream"):
        raise ManifestValidationError("control_stream_mode_mismatch")
    if response_shape["content_type_class"] != pre["content_type_class"]:
        raise ManifestValidationError("control_response_shape_content_mismatch")
    if response_shape["terminal"] == "json_response" and pre["content_type_class"] != "json":
        raise ManifestValidationError("control_terminal_content_mismatch")
    if response_shape["terminal"] != "json_response" and pre["content_type_class"] == "json":
        raise ManifestValidationError("control_json_terminal_invalid")
    _validate_control_semantics(name, request_shape, response_shape)
    if identity["unclassified_core_items"] != 0:
        raise ManifestValidationError("control_identity_unclassified")

    body_equality = control.get("body_equality")
    if not isinstance(body_equality, Mapping):
        raise ManifestValidationError("body_equality_invalid")
    _require_exact_fields(body_equality, frozenset({"request", "response", "sse"}), "body_equality_fields_invalid")
    expected_equality = {
        "request": request_equal,
        "response": response_equal,
        "sse": pre["sse"] is not None,
    }
    if any(body_equality.get(key) is not expected_value for key, expected_value in expected_equality.items()):
        raise ManifestValidationError("body_equality_mismatch")

    return {
        "name": name,
        "pre": pre,
        "post": post,
        "request_shape": request_shape,
        "response_shape": response_shape,
        "identity": identity,
        "route_identity": route,
        "body_equality": expected_equality,
    }


def _validate_candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = frozenset(
        {
            "codexhub_candidate_sha",
            "cli_version",
            "cli_source_commit",
            "cli_source_commit_status",
            "cli_package_sha256",
            "catalog_snapshot_sha256",
            "catalog_model_entry_id",
            "route_digest",
        }
    )
    if not isinstance(value, Mapping):
        raise ManifestValidationError("candidate_identity_invalid")
    _require_exact_fields(value, expected, "candidate_identity_fields_invalid")
    result = dict(value)
    _require_hex(result["codexhub_candidate_sha"], _SHA1_OR_HEX64, "candidate_codexhub_sha_invalid")
    if not isinstance(result["cli_version"], str) or _SEMVER.fullmatch(result["cli_version"]) is None:
        raise ManifestValidationError("candidate_cli_version_invalid")
    source_status = result["cli_source_commit_status"]
    if source_status not in {"published", "not_published_by_registry"}:
        raise ManifestValidationError("candidate_cli_source_commit_status_invalid")
    source_commit = result["cli_source_commit"]
    if source_status == "published":
        _require_hex(source_commit, _SHA1_OR_HEX64, "candidate_cli_source_commit_invalid")
    elif source_commit is not None:
        raise ManifestValidationError("candidate_cli_source_commit_unexpected")
    _require_hex(result["cli_package_sha256"], _HEX64, "candidate_cli_package_sha_invalid")
    _require_hex(result["catalog_snapshot_sha256"], _HEX64, "candidate_catalog_sha_invalid")
    _require_hex(result["route_digest"], _HEX64, "candidate_route_digest_invalid")
    if result["catalog_model_entry_id"] != "gpt-5.6-sol":
        raise ManifestValidationError("candidate_catalog_model_invalid")
    return result


def _validate_planner(value: Any, *, verification_scope: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ManifestValidationError("planner_invalid")
    _require_exact_fields(value, PLANNER_FIELDS, "planner_fields_invalid")
    model_visible_plan = value.get("model_visible_plan")
    if not isinstance(model_visible_plan, str) or model_visible_plan not in PLANNER_PLAN_STATES:
        raise ManifestValidationError("planner_model_visible_plan_invalid")
    hosted_only = value.get("hosted_only_disposition")
    unknown_tag = value.get("unknown_tag_disposition")
    if (
        not isinstance(hosted_only, str)
        or not isinstance(unknown_tag, str)
        or hosted_only not in PLANNER_DISPOSITIONS
        or unknown_tag not in PLANNER_DISPOSITIONS
    ):
        raise ManifestValidationError("planner_disposition_invalid")
    if verification_scope == SYNTHETIC_SCOPE and (
        model_visible_plan == "complete"
        or hosted_only != "Unqualified"
        or unknown_tag != "Unqualified"
    ):
        raise ManifestValidationError("planner_synthetic_evidence_invalid")
    return {
        "model_visible_plan": model_visible_plan,
        "hosted_only_disposition": hosted_only,
        "unknown_tag_disposition": unknown_tag,
    }


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_control_names(controls: Iterable[Mapping[str, Any]]) -> set[str]:
    return {str(control.get("name")) for control in controls}


def _manifest_core(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": manifest.get("schema"),
        "verification_scope": manifest.get("verification_scope"),
        "candidate_identity": manifest.get("candidate_identity"),
        "controls": manifest.get("controls"),
        "identity_control": manifest.get("identity_control"),
        "planner": manifest.get("planner"),
        "qualification": manifest.get("qualification"),
    }


def _validate_qualification(value: Any, *, verification_scope: str) -> dict[str, Any]:
    expected = frozenset(
        {
            "candidate_identity_complete",
            "all_required_controls_complete",
            "identity_replay",
            "ready_for_issue62",
            "reason",
        }
    )
    if not isinstance(value, Mapping):
        raise ManifestValidationError("qualification_invalid")
    _require_exact_fields(value, expected, "qualification_fields_invalid")
    for key in ("candidate_identity_complete", "all_required_controls_complete", "ready_for_issue62"):
        _require_bool(value.get(key), "qualification_boolean_invalid")
    if value.get("identity_replay") not in {"not_captured", "captured"}:
        raise ManifestValidationError("qualification_identity_replay_invalid")
    reason = value.get("reason")
    if reason != QUALIFICATION_REASONS.get(verification_scope):
        raise ManifestValidationError("qualification_reason_invalid")
    return {
        "candidate_identity_complete": value["candidate_identity_complete"],
        "all_required_controls_complete": value["all_required_controls_complete"],
        "identity_replay": value["identity_replay"],
        "ready_for_issue62": value["ready_for_issue62"],
        "reason": reason,
    }


def _validate_identity_control(value: Any) -> dict[str, Any]:
    expected = frozenset(
        {
            "fail_closed",
            "unclassified_core_items",
            "unclassified_controls",
            "replay_cases",
            "wire_pairing",
        }
    )
    if not isinstance(value, Mapping):
        raise ManifestValidationError("identity_control_invalid")
    _require_exact_fields(value, expected, "identity_control_fields_invalid")
    if _require_bool(value.get("fail_closed"), "identity_control_fail_closed_invalid") is not True:
        raise ManifestValidationError("identity_control_fail_closed_invalid")
    unclassified_count = _require_nonnegative_int(
        value.get("unclassified_core_items"), "identity_control_unclassified_invalid"
    )
    unclassified_controls = value.get("unclassified_controls")
    if (
        not isinstance(unclassified_controls, list)
        or any(item not in CONTROL_NAME_SET for item in unclassified_controls)
        or unclassified_controls != sorted(set(unclassified_controls))
    ):
        raise ManifestValidationError("identity_control_controls_invalid")
    if unclassified_count != len(unclassified_controls):
        raise ManifestValidationError("identity_control_count_mismatch")
    if value.get("replay_cases") != ["identity", "mutation", "deletion", "loss"]:
        raise ManifestValidationError("identity_control_replay_cases")
    if value.get("wire_pairing") != "control_label_ordinal_without_wire_identifier":
        raise ManifestValidationError("identity_control_wire_pairing_invalid")
    return {
        "fail_closed": True,
        "unclassified_core_items": unclassified_count,
        "unclassified_controls": list(unclassified_controls),
        "replay_cases": ["identity", "mutation", "deletion", "loss"],
        "wire_pairing": "control_label_ordinal_without_wire_identifier",
    }


def build_manifest(
    captures: Iterable[Mapping[str, Any]],
    *,
    candidate_identity: Mapping[str, Any],
    verification_scope: str = SYNTHETIC_SCOPE,
    planner: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one deterministic sanitized manifest from control captures."""

    if verification_scope not in VERIFICATION_SCOPES:
        raise ManifestValidationError("verification_scope_invalid")
    controls = [_sanitize_control(capture) for capture in captures]
    if len({control["name"] for control in controls}) != len(controls):
        raise ManifestValidationError("duplicate_control_name")
    missing = sorted(CONTROL_NAME_SET - _required_control_names(controls))
    if missing:
        raise ManifestValidationError("missing_control:" + ",".join(missing))
    route_digests = {control["route_identity"]["route_digest"] for control in controls}
    if len(route_digests) != 1:
        raise ManifestValidationError("control_route_digest_set_invalid")
    route_provenance = {_canonical_digest(control["route_identity"]) for control in controls}
    if len(route_provenance) != 1:
        raise ManifestValidationError("control_route_provenance_inconsistent")
    if not isinstance(candidate_identity, Mapping) or "route_digest" not in candidate_identity:
        raise ManifestValidationError("candidate_route_digest_required")
    candidate = _validate_candidate(candidate_identity)
    route_models = {control["route_identity"]["model"] for control in controls}
    if route_models != {candidate["catalog_model_entry_id"]}:
        raise ManifestValidationError("control_route_model_set_invalid")
    if route_digests != {candidate["route_digest"]}:
        raise ManifestValidationError("control_route_digest_mismatch")
    planner_data = _validate_planner(
        DEFAULT_PLANNER if planner is None else planner,
        verification_scope=verification_scope,
    )
    identity_failures = [
        control["name"]
        for control in controls
        if not all(control["identity"][field] for field in IDENTITY_BOOLEAN_FIELDS)
    ]
    identity_control = {
        "fail_closed": True,
        "unclassified_core_items": len(identity_failures),
        "unclassified_controls": sorted(identity_failures),
        "replay_cases": ["identity", "mutation", "deletion", "loss"],
        "wire_pairing": "control_label_ordinal_without_wire_identifier",
    }
    qualification = {
        "candidate_identity_complete": True,
        "all_required_controls_complete": not missing,
        "identity_replay": "not_captured",
        "ready_for_issue62": False,
        "reason": (
            "synthetic_fixture_only"
            if verification_scope == SYNTHETIC_SCOPE
            else "wire replay and final inventory reconciliation required"
        ),
    }
    core = {
        "schema": SCHEMA,
        "verification_scope": verification_scope,
        "candidate_identity": candidate,
        "controls": sorted(controls, key=lambda item: item["name"]),
        "identity_control": identity_control,
        "planner": planner_data,
        "qualification": qualification,
    }
    core["capture_manifest_sha256"] = _canonical_digest(core)
    return core


def replay_manifest(manifest: Mapping[str, Any], case: str) -> dict[str, Any]:
    """Return a sanitized negative replay copy for fail-closed validation."""

    if case not in {"identity", "mutation", "deletion", "loss"}:
        raise ManifestValidationError("replay_case_invalid")
    clone = copy.deepcopy(dict(manifest))
    if case == "identity":
        return clone
    if case == "mutation":
        clone["controls"][0]["body_equality"]["response"] = False
    elif case == "deletion":
        clone["controls"] = clone["controls"][1:]
    elif case == "loss":
        clone["candidate_identity"].pop("catalog_snapshot_sha256", None)
    return clone


def reconcile_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Reconcile a generated or replayed manifest without reading source bodies."""

    mismatches: list[str] = []
    if not isinstance(manifest, Mapping):
        return {"reconciled": False, "mismatches": ["schema_invalid"]}
    try:
        _require_exact_fields(manifest, MANIFEST_FIELDS, "manifest_fields_invalid")
    except ManifestValidationError as exc:
        mismatches.append(str(exc))
    if manifest.get("schema") != SCHEMA:
        mismatches.append("schema_invalid")
        return {"reconciled": False, "mismatches": ["schema_invalid"]}
    try:
        candidate = _validate_candidate(manifest.get("candidate_identity"))
    except ManifestValidationError as exc:
        mismatches.append(f"candidate:{exc}")
        candidate = {}
    controls = manifest.get("controls")
    if not isinstance(controls, list):
        mismatches.append("controls_invalid")
        controls = []
    names: list[str] = []
    canonical_controls: list[dict[str, Any]] = []
    for raw in controls:
        try:
            sanitized = _validate_canonical_control(raw)
        except ManifestValidationError as exc:
            mismatches.append(f"control:{exc}")
            continue
        name = sanitized["name"]
        names.append(name)
        canonical_controls.append(sanitized)
        if raw != sanitized:
            mismatches.append(f"control:{name}:derived_fields_or_sanitization_drift")
    if len(set(names)) != len(names):
        mismatches.append("duplicate_control_name")
    missing = sorted(CONTROL_NAME_SET - set(names))
    if missing:
        mismatches.append("missing_control:" + ",".join(missing))
    route_digests = {control["route_identity"]["route_digest"] for control in canonical_controls}
    if len(route_digests) != 1:
        mismatches.append("control_route_digest_set_invalid")
    route_provenance = {
        _canonical_digest(control["route_identity"]) for control in canonical_controls
    }
    if len(route_provenance) > 1:
        mismatches.append("control_route_provenance_inconsistent")
    if candidate:
        route_models = {control["route_identity"]["model"] for control in canonical_controls}
        if route_models != {candidate.get("catalog_model_entry_id")}:
            mismatches.append("control_route_model_set_invalid")
        if route_digests != {candidate.get("route_digest")}:
            mismatches.append("control_route_digest_mismatch")
    try:
        identity_control = _validate_identity_control(manifest.get("identity_control"))
    except ManifestValidationError as exc:
        mismatches.append(f"identity_control:{exc}")
        identity_control = None
    if identity_control is not None:
        observed_unclassified = sorted(
            control["name"]
            for control in canonical_controls
            if not all(control["identity"][field] for field in IDENTITY_BOOLEAN_FIELDS)
        )
        if identity_control["unclassified_controls"] != observed_unclassified:
            mismatches.append("identity_control_consistency")
        if identity_control["unclassified_core_items"] != len(observed_unclassified):
            mismatches.append("identity_control_count_consistency")
    verification_scope = manifest.get("verification_scope")
    if verification_scope not in VERIFICATION_SCOPES:
        mismatches.append("verification_scope_invalid")
    try:
        _validate_planner(manifest.get("planner"), verification_scope=verification_scope)
    except ManifestValidationError as exc:
        mismatches.append(f"planner:{exc}")
    try:
        qualification = _validate_qualification(
            manifest.get("qualification"),
            verification_scope=verification_scope,
        )
    except ManifestValidationError as exc:
        mismatches.append(f"qualification:{exc}")
        qualification = {}
    if qualification.get("ready_for_issue62") is True:
        mismatches.append("manifest_qualification_not_performed")
        if manifest.get("verification_scope") == SYNTHETIC_SCOPE:
            mismatches.append("synthetic_scope_cannot_qualify")
    if qualification.get("identity_replay") != "not_captured":
        mismatches.append("live_provenance_not_captured")
    expected_digest = manifest.get("capture_manifest_sha256")
    if not isinstance(expected_digest, str) or _HEX64.fullmatch(expected_digest) is None:
        mismatches.append("capture_manifest_sha256_invalid")
    else:
        core = _manifest_core(manifest)
        if _canonical_digest(core) != expected_digest:
            mismatches.append("capture_manifest_sha256_stale")
    return {"reconciled": not mismatches, "mismatches": mismatches}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestValidationError("input_json_invalid") from exc
    if not isinstance(value, dict):
        raise ManifestValidationError("input_json_root_invalid")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True, help="Sanitized control capture input JSON")
    parser.add_argument("--out", type=Path, required=True, help="Sanitized manifest output JSON")
    parser.add_argument("--verification-scope", choices=sorted(VERIFICATION_SCOPES), default=SYNTHETIC_SCOPE)
    parser.add_argument("--codexhub-candidate-sha", required=True)
    parser.add_argument("--cli-version", required=True)
    parser.add_argument("--cli-source-commit", default=None)
    parser.add_argument(
        "--cli-source-commit-status",
        choices=("published", "not_published_by_registry"),
        default="not_published_by_registry",
    )
    parser.add_argument("--cli-package-sha256", required=True)
    parser.add_argument("--catalog-snapshot-sha256", required=True)
    parser.add_argument("--route-digest", required=True)
    parser.add_argument("--replay-case", choices=("identity", "mutation", "deletion", "loss"), default="identity")
    parser.add_argument("--check", action="store_true", help="Reconcile output without writing it")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        source = _load_json(args.capture)
        captures = source.get("controls")
        if not isinstance(captures, list):
            raise ManifestValidationError("capture_controls_invalid")
        candidate = {
            "codexhub_candidate_sha": args.codexhub_candidate_sha,
            "cli_version": args.cli_version,
            "cli_source_commit": args.cli_source_commit,
            "cli_source_commit_status": args.cli_source_commit_status,
            "cli_package_sha256": args.cli_package_sha256,
            "catalog_snapshot_sha256": args.catalog_snapshot_sha256,
            "catalog_model_entry_id": source.get("catalog_model_entry_id", "gpt-5.6-sol"),
        }
        candidate["route_digest"] = args.route_digest
        manifest = build_manifest(
            captures,
            candidate_identity=candidate,
            verification_scope=args.verification_scope,
            planner=source.get("planner"),
        )
        if args.replay_case != "identity":
            report = reconcile_manifest(replay_manifest(manifest, args.replay_case))
            print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
            return 0 if not report["reconciled"] else 1
        report = reconcile_manifest(manifest)
        if not report["reconciled"]:
            print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
            return 1
        rendered = json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        if not args.check:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(rendered, encoding="utf-8", newline="\n")
        print(json.dumps({"reconciled": True, "capture_manifest_sha256": manifest["capture_manifest_sha256"]}, sort_keys=True))
        return 0
    except ManifestValidationError as exc:
        print(f"MANIFEST_INVALID:{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
