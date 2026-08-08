#!/usr/bin/env python3
"""Validate a sanitized Issue #278/#280 CLI runner summary."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


SCHEMA = "codexhub.issue278.cli-tool-search.v1"
DEFAULT_SUMMARY = Path("docs/evidence/issue-278/summary.template.json")
CASE_IDS = {"native_explicit", "native_no_hint", "adapted_explicit", "adapted_no_hint"}
EXPLICIT_CASES = {"native_explicit", "adapted_explicit"}
NO_HINT_CASES = CASE_IDS - EXPLICIT_CASES
SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "access_token",
    "bearer",
    "cookie",
    "credential",
    "credentials",
    "header",
    "headers",
    "password",
    "prompt",
    "request_body",
    "response_body",
}
SENSITIVE_MARKERS = ("sk-", "bearer ", "-----begin ", "http://", "https://", "fixture-target.txt")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
MAX_SUMMARY_BYTES = 512 * 1024
MAX_CASES = 4
MAX_ROUTE_DIGESTS = 8
MAX_ROUTE_OBSERVATIONS = 32
MAX_EVENT_TYPES = 128
MAX_CLI_EVENTS = 256
MAX_MCP_COUNT = 64
MAX_WALK_NODES = 8192
MAX_WALK_DEPTH = 32
PROTOCOL_BY_CASE = {
    "native_explicit": "responses_structured",
    "native_no_hint": "responses_structured",
    "adapted_explicit": "chat_tools",
    "adapted_no_hint": "chat_tools",
}
FIXTURE_KEY = "codexhub-issue278-fixture"
FIXTURE_KEY_BY_CASE = {
    "native_explicit": FIXTURE_KEY,
    "native_no_hint": FIXTURE_KEY,
    "adapted_explicit": "codexhub-issue278-volc-fixture",
    "adapted_no_hint": "codexhub-issue278-volc-fixture",
}
PROVIDER_BY_CASE = {
    "native_explicit": "ollama-cloud",
    "native_no_hint": "ollama-cloud",
    "adapted_explicit": "volc",
    "adapted_no_hint": "volc",
}
UPSTREAM_MODEL_BY_CASE = {case_id: "glm-5.2" for case_id in CASE_IDS}
MODEL_BY_CASE = {
    "native_explicit": "ollama-cloud/glm-5.2",
    "native_no_hint": "ollama-cloud/glm-5.2",
    "adapted_explicit": "volc/glm-5.2",
    "adapted_no_hint": "volc/glm-5.2",
}
TRACE_DECLARATION = "tool_search.declaration"
TRACE_NOT_SELECTED = "tool_search.not_selected"
TRACE_SEARCH_CALL = "tool_search.call"
TRACE_SEARCH_RESULT = "tool_search.result"
TRACE_DISCOVERED_DECLARATION = "discovered.declaration"
TRACE_DISCOVERED_CALL = "discovered.call"
TRACE_DISCOVERED_RESULT = "discovered.result"
WORKFLOW_TOOL_NAMES = ("shell_command", "apply_patch", "shell_command")


class EvidenceValidationError(ValueError):
    pass


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise EvidenceValidationError(code)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _identity_observation(tokens: list[tuple[str, str]]) -> dict[str, Any]:
    digests = [{"role": role, "digest": _digest(value)} for role, value in tokens]
    return {"tokens": digests, "identity_digest": _digest(digests)}


def _expected_response_identity_observations(case_id: str) -> list[dict[str, Any]]:
    if case_id in NO_HINT_CASES:
        return [_identity_observation([("response_id", "fixture-response-1"), ("item_id", "fixture-message-item")])]
    search_item_id = (
        "fixture-search-call-item"
        if case_id.startswith("adapted_")
        else "fixture-search-item"
    )
    calls = [
        ("fixture-response-1", search_item_id, "fixture-search-call"),
        ("fixture-response-2", "fixture-discovered-item", "fixture-discovered-call"),
        ("fixture-response-3", "fixture-code-item-3", "fixture-code-call-1"),
        ("fixture-response-4", "fixture-code-item-4", "fixture-code-call-2"),
        ("fixture-response-5", "fixture-code-item-5", "fixture-code-call-3"),
    ]
    observations = [
        _identity_observation(
            [("response_id", response_id), ("item_id", item_id), ("call_id", call_id)]
        )
        for response_id, item_id, call_id in calls
    ]
    observations.append(
        _identity_observation([("response_id", "fixture-response-6"), ("item_id", "fixture-message-item")])
    )
    return observations


def _expected_request_call_ids(case_id: str) -> list[str | None]:
    if case_id in NO_HINT_CASES:
        return [None]
    return [
        None,
        "fixture-search-call",
        "fixture-discovered-call",
        "fixture-code-call-1",
        "fixture-code-call-2",
        "fixture-code-call-3",
    ]


def _expected_trace(case_id: str) -> list[str]:
    if case_id in NO_HINT_CASES:
        return [TRACE_DECLARATION, TRACE_NOT_SELECTED]
    return [
        TRACE_DECLARATION,
        TRACE_SEARCH_CALL,
        TRACE_SEARCH_RESULT,
        TRACE_DISCOVERED_DECLARATION,
        TRACE_DISCOVERED_CALL,
        TRACE_DISCOVERED_RESULT,
        "code_mode.shell_command.call",
        "code_mode.shell_command.result",
        "code_mode.apply_patch.call",
        "code_mode.apply_patch.result",
        "code_mode.shell_command.call",
        "code_mode.shell_command.result",
    ]


def _function_call_shapes() -> list[str]:
    return [
        "response.created",
        "response.output_item.added:function_call",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done:function_call",
        "response.completed",
    ]


def _text_response_shapes() -> list[str]:
    return [
        "response.created",
        "response.output_item.added:message",
        "response.output_text.delta",
        "response.output_item.done:message",
        "response.completed",
    ]


def _custom_tool_call_shapes() -> list[str]:
    return [
        "response.created",
        "response.output_item.added:custom_tool_call",
        "response.custom_tool_call_input.delta",
        "response.custom_tool_call_input.done",
        "response.output_item.done:custom_tool_call",
        "response.completed",
    ]


def _expected_response_shapes(case_id: str) -> list[str]:
    if case_id in NO_HINT_CASES:
        return _text_response_shapes()
    search = (
        [
            "response.created",
            "response.output_item.done:tool_search_call",
            "response.completed",
        ]
        if case_id.startswith("native_")
        else _function_call_shapes()
    )
    if case_id.startswith("native_"):
        # Native Responses keeps the free-form apply_patch lifecycle as a
        # custom_tool_call; Chat adaptation receives an ordinary function
        # envelope instead.  Keep the source protocol distinction explicit.
        tool_calls = (
            _function_call_shapes()
            + _function_call_shapes()
            + _custom_tool_call_shapes()
            + _function_call_shapes()
        )
    else:
        tool_calls = _function_call_shapes() * 4
    return search + tool_calls + _text_response_shapes()


def _expected_provenance(case_id: str) -> dict[str, Any]:
    trace = _expected_trace(case_id)
    explicit = case_id in EXPLICIT_CASES
    search_trace = trace[:6] if explicit else trace
    code_trace = trace[6:] if explicit else []
    code_steps = list(WORKFLOW_TOOL_NAMES) if explicit else []
    protocols = (["responses"] if case_id.startswith("native_") else ["chat_completions"]) * (6 if explicit else 1)
    response_shapes = _expected_response_shapes(case_id)
    history_order_value = {"trace": trace, "protocols": protocols, "response_shapes": response_shapes}
    identity_slots = [{"ordinal": ordinal, "role": token} for ordinal, token in enumerate(trace, start=1)]
    return {
        "trace": trace,
        "trace_digest": _digest(trace),
        "search": {
            "ordered_stages": search_trace,
            "stage_count": len(search_trace),
            "call_count": search_trace.count(TRACE_SEARCH_CALL),
            "result_count": search_trace.count(TRACE_SEARCH_RESULT),
            "discovered_declaration_count": search_trace.count(TRACE_DISCOVERED_DECLARATION),
            "subsequent_call_count": search_trace.count(TRACE_DISCOVERED_CALL),
            "subsequent_result_count": search_trace.count(TRACE_DISCOVERED_RESULT),
            "order_digest": _digest(search_trace),
        },
        "code_mode": {
            "ordered_steps": code_steps,
            "call_count": sum(token.endswith(".call") for token in code_trace),
            "result_count": sum(token.endswith(".result") for token in code_trace),
            "order_digest": _digest(code_trace),
        },
        "history": {
            "request_count": 6 if explicit else 1,
            "response_count": 6 if explicit else 1,
            "protocol_sequence": protocols,
            "response_shapes": response_shapes,
            "order_digest": _digest(history_order_value),
            "identity_digest": _digest(identity_slots),
        },
    }


def _walk_sanitized(value: Any, *, _nodes: list[int] | None = None, _depth: int = 0) -> None:
    if _nodes is None:
        _nodes = [0]
    _nodes[0] += 1
    _require(_nodes[0] <= MAX_WALK_NODES and _depth <= MAX_WALK_DEPTH, "summary_too_complex")
    if isinstance(value, dict):
        for key, child in value.items():
            _require(str(key).lower() not in SENSITIVE_KEYS, "sensitive_field_retained")
            _walk_sanitized(child, _nodes=_nodes, _depth=_depth + 1)
    elif isinstance(value, list):
        for child in value:
            _walk_sanitized(child, _nodes=_nodes, _depth=_depth + 1)
    elif isinstance(value, str):
        lowered = value.lower()
        _require(not any(marker in lowered for marker in SENSITIVE_MARKERS), "sensitive_value_retained")
        _require(len(value) <= 4096, "summary_string_too_large")
        # The two fixed loopback protocol paths are bounded route shape, not
        # filesystem paths.  They are retained so the validator can prove
        # which wire endpoint actually received each request.
        if value in {"/v1/responses", "/v1/chat/completions"}:
            return
        _require(
            re.search(r"(?:^[A-Za-z]:[\\/]|^/|\\\\Users\\|/Users/|/home/|/tmp/)", value) is None,
            "path_retained",
        )


def _load(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        _require(len(raw) <= MAX_SUMMARY_BYTES, "summary_too_large")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceValidationError("summary_unreadable") from error
    _require(isinstance(value, dict), "summary_root_invalid")
    _walk_sanitized(value)
    return value


def _validate_provenance(case_id: str, provenance: Any) -> dict[str, Any]:
    _require(isinstance(provenance, dict), "provenance_missing")
    expected = _expected_provenance(case_id)
    trace = provenance.get("trace")
    _require(trace == expected["trace"], "provenance_trace_invalid")
    _require(provenance.get("trace_digest") == _digest(trace), "provenance_digest_invalid")

    search = provenance.get("search")
    _require(isinstance(search, dict), "search_provenance_missing")
    expected_search = expected["search"]
    for key in (
        "ordered_stages",
        "stage_count",
        "call_count",
        "result_count",
        "discovered_declaration_count",
        "subsequent_call_count",
        "subsequent_result_count",
    ):
        _require(search.get(key) == expected_search[key], "search_provenance_invalid")
    _require(search.get("order_digest") == _digest(search["ordered_stages"]), "provenance_digest_invalid")

    code_mode = provenance.get("code_mode")
    _require(isinstance(code_mode, dict), "code_mode_provenance_missing")
    expected_code_mode = expected["code_mode"]
    for key in ("ordered_steps", "call_count", "result_count"):
        _require(code_mode.get(key) == expected_code_mode[key], "code_mode_provenance_invalid")
    code_trace = []
    for name in code_mode["ordered_steps"]:
        code_trace.extend((f"code_mode.{name}.call", f"code_mode.{name}.result"))
    _require(code_mode.get("order_digest") == _digest(code_trace), "provenance_digest_invalid")

    history = provenance.get("history")
    _require(isinstance(history, dict), "history_provenance_missing")
    expected_history = expected["history"]
    for key in ("request_count", "response_count", "protocol_sequence", "response_shapes"):
        _require(history.get(key) == expected_history[key], "history_provenance_invalid")
    history_order_value = {
        "trace": trace,
        "protocols": history["protocol_sequence"],
        "response_shapes": history["response_shapes"],
    }
    _require(history.get("order_digest") == _digest(history_order_value), "provenance_digest_invalid")
    identity = history.get("identity")
    _require(isinstance(identity, dict), "provenance_identity_missing")
    for key in ("response_identity_digests", "request_identity_digests"):
        values = identity.get(key)
        _require(
            isinstance(values, list)
            and all(isinstance(value, str) and SHA256.fullmatch(value) is not None for value in values),
            "provenance_identity_invalid",
        )
    explicit = case_id in EXPLICIT_CASES
    expected_count = 6 if explicit else 1
    _require(len(identity["response_identity_digests"]) == expected_count, "provenance_identity_invalid")
    _require(len(identity["request_identity_digests"]) == expected_count, "provenance_identity_invalid")
    _require(identity.get("identity_binding_count") == (5 if explicit else 0), "provenance_identity_invalid")
    _require(identity.get("search_identity_binding_count") == (1 if explicit else 0), "provenance_identity_invalid")
    _require(identity.get("identity_mismatch_count") == 0, "provenance_identity_invalid")
    _require(identity.get("request_identity_mismatch_count") == 0, "provenance_identity_invalid")
    expected_observation_count = 6 if explicit else 1
    for observation_key, digest_key in (
        ("response_identity_observations", "response_identity_digests"),
        ("request_identity_observations", "request_identity_digests"),
    ):
        observations = identity.get(observation_key)
        _require(
            isinstance(observations, list) and len(observations) == expected_observation_count,
            "provenance_identity_observation_invalid",
        )
        recomputed: list[str] = []
        for observation in observations:
            _require(isinstance(observation, dict), "provenance_identity_observation_invalid")
            tokens = observation.get("tokens")
            _require(isinstance(tokens, list) and len(tokens) <= 64, "provenance_identity_observation_invalid")
            for token in tokens:
                _require(
                    isinstance(token, dict)
                    and token.get("role") in {"call_id", "item_id", "response_id"}
                    and isinstance(token.get("digest"), str)
                    and SHA256.fullmatch(token["digest"]) is not None,
                    "provenance_identity_observation_invalid",
                )
            digest = observation.get("identity_digest")
            _require(digest == _digest(tokens), "provenance_identity_observation_invalid")
            recomputed.append(digest)
        _require(recomputed == identity.get(digest_key), "provenance_identity_observation_invalid")
        if observation_key == "response_identity_observations":
            _require(
                observations == _expected_response_identity_observations(case_id),
                "provenance_response_identity_unbound",
            )
        else:
            expected_calls = _expected_request_call_ids(case_id)
            for ordinal, (observation, expected_call) in enumerate(zip(observations, expected_calls)):
                call_digests = {
                    token["digest"]
                    for token in observation.get("tokens", [])
                    if isinstance(token, dict) and token.get("role") == "call_id"
                }
                if expected_call is None:
                    _require(not call_digests, "provenance_request_identity_unbound")
                else:
                    if not call_digests and expected_call == "fixture-search-call" and ordinal == 1:
                        # Both native 0.146 and the Chat adapter may consume
                        # the assistant search-call ID before serializing the
                        # result request.  The response-side search identity
                        # plus search_identity_binding_count still proves the
                        # link; later tool-result requests must retain IDs.
                        continue
                    _require(_digest(expected_call) in call_digests, "provenance_request_identity_unbound")
    _require(history.get("identity_digest") == _digest(identity), "provenance_identity_invalid")
    diagnostics = history.get("diagnostics")
    _require(isinstance(diagnostics, dict), "provenance_diagnostics_missing")
    _require(
        diagnostics.get("response_stage") is None
        or diagnostics.get("response_stage") in {"search_call", "discovered_call", "workflow_call:1", "workflow_call:2", "workflow_call:3", "text"},
        "provenance_diagnostics_invalid",
    )
    _require(
        diagnostics.get("awaiting_input") in {None, "tool_search_output", "function_call_output"},
        "provenance_diagnostics_invalid",
    )
    pending_digest = diagnostics.get("pending_call_id_digest")
    _require(
        pending_digest is None or (isinstance(pending_digest, str) and SHA256.fullmatch(pending_digest) is not None),
        "provenance_diagnostics_invalid",
    )
    protocol_error = diagnostics.get("protocol_error")
    _require(
        protocol_error is None
        or (isinstance(protocol_error, str) and re.fullmatch(r"[a-z0-9_]+", protocol_error) is not None),
        "provenance_diagnostics_invalid",
    )
    return expected


def _validate_route_observation(case_id: str, observation: Any, route_identity_digest: Any) -> None:
    """Recompute route binding from retained observations, never from labels."""

    _require(isinstance(observation, dict), "route_observation_missing")
    paths = observation.get("paths")
    model_digests = observation.get("model_digests")
    provider_digests = observation.get("provider_digests")
    protocols = observation.get("protocols")
    observation_digests = observation.get("observation_digests")
    request_count = observation.get("request_count")
    _require(type(request_count) is int and 0 < request_count <= MAX_ROUTE_OBSERVATIONS, "route_observation_invalid")
    for values in (paths, model_digests, provider_digests, protocols, observation_digests):
        _require(isinstance(values, list) and len(values) == request_count, "route_observation_invalid")
    _require(all(isinstance(value, str) and SHA256.fullmatch(value) is not None for value in model_digests), "route_observation_invalid")
    _require(all(isinstance(value, str) and SHA256.fullmatch(value) is not None for value in provider_digests), "route_observation_invalid")
    _require(all(isinstance(value, str) and SHA256.fullmatch(value) is not None for value in observation_digests), "route_observation_invalid")
    expected_path = "/v1/chat/completions" if case_id.startswith("adapted_") else "/v1/responses"
    expected_protocol = "chat_completions" if case_id.startswith("adapted_") else "responses"
    # The fixture records the model field after the Gateway has applied the
    # selected provider's immutable upstream binding.  The client-facing
    # ``provider/model`` slug is separately bound by the Gateway route
    # observation; the upstream body intentionally carries the provider-local
    # bare model.
    expected_model_digest = _digest(UPSTREAM_MODEL_BY_CASE[case_id])
    expected_provider_digest = _digest(f"Bearer {FIXTURE_KEY_BY_CASE[case_id]}")
    _require(model_digests == [expected_model_digest] * request_count, "route_observation_model_mismatch")
    _require(provider_digests == [expected_provider_digest] * request_count, "route_observation_provider_mismatch")
    _require(paths == [expected_path] * request_count, "route_observation_invalid")
    _require(protocols == [expected_protocol] * request_count, "route_observation_invalid")
    _require(len(set(model_digests)) == 1 and len(set(provider_digests)) == 1, "route_observation_cross_provider")
    expected_observations = [
        _digest({
            "path": path,
            "model_digest": model_digest,
            "provider_digest": provider_digest,
            "protocol": protocol,
        })
        for path, model_digest, provider_digest, protocol in zip(
            paths, model_digests, provider_digests, protocols
        )
    ]
    _require(observation_digests == expected_observations, "route_observation_digest_invalid")
    material = {
        "paths": paths,
        "model_digests": model_digests,
        "provider_digests": provider_digests,
        "protocols": protocols,
    }
    upstream_identity_digest = _digest(material)
    _require(observation.get("upstream_identity_digest") == upstream_identity_digest, "route_observation_digest_invalid")
    gateway = observation.get("gateway")
    _validate_gateway_route_observation(case_id, gateway)
    combined_material = {
        "upstream": upstream_identity_digest,
        "gateway": gateway["identity_digest"],
    }
    _require(observation.get("identity_digest") == _digest(combined_material), "route_observation_digest_invalid")
    _require(route_identity_digest == observation["identity_digest"], "route_identity_digest_mismatch")
    _require(observation.get("cross_provider_requests") == 0, "route_observation_cross_provider")


def _validate_gateway_route_observation(case_id: str, observation: Any) -> None:
    """Validate the selected client route independently of upstream body data."""

    _require(isinstance(observation, dict), "gateway_route_observation_missing")
    request_count = observation.get("request_count")
    records = observation.get("observations")
    _require(type(request_count) is int and 0 < request_count <= MAX_ROUTE_OBSERVATIONS, "gateway_route_observation_invalid")
    _require(isinstance(records, list) and len(records) == request_count, "gateway_route_observation_invalid")
    _require(observation.get("source") == "gateway_request_complete", "gateway_route_observation_invalid")
    expected_provider_digest = _digest(PROVIDER_BY_CASE[case_id])
    expected_model_digest = _digest(MODEL_BY_CASE[case_id])
    expected_upstream_digest = _digest(UPSTREAM_MODEL_BY_CASE[case_id])
    expected_path = "/v1/chat/completions" if case_id.startswith("adapted_") else "/v1/responses"
    expected_endpoint_digest = _digest(
        {
            "scheme": "http",
            "hostname": "127.0.0.1",
            "path": expected_path,
        }
    )
    normalized: list[dict[str, str]] = []
    for record in records:
        _require(isinstance(record, dict), "gateway_route_observation_invalid")
        for key in (
            "provider_digest",
            "model_requested_digest",
            "model_canonical_digest",
            "upstream_model_digest",
            "endpoint_digest",
        ):
            value = record.get(key)
            _require(isinstance(value, str) and SHA256.fullmatch(value) is not None, "gateway_route_observation_invalid")
        _require(record["provider_digest"] == expected_provider_digest, "gateway_route_provider_mismatch")
        _require(record["model_requested_digest"] == expected_model_digest, "gateway_route_model_mismatch")
        _require(record["model_canonical_digest"] == expected_model_digest, "gateway_route_model_mismatch")
        _require(record["upstream_model_digest"] == expected_upstream_digest, "gateway_route_upstream_model_mismatch")
        _require(record["endpoint_digest"] == expected_endpoint_digest, "gateway_route_endpoint_mismatch")
        normalized.append({key: record[key] for key in (
            "provider_digest",
            "model_requested_digest",
            "model_canonical_digest",
            "upstream_model_digest",
            "endpoint_digest",
        )})
    _require(observation.get("identity_digest") == _digest({"observations": normalized}), "gateway_route_digest_invalid")


def validate_summary(value: dict[str, Any], *, candidate_sha: str | None = None) -> dict[str, Any]:
    try:
        encoded_size = len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        raise EvidenceValidationError("summary_encoding_invalid")
    _require(encoded_size <= MAX_SUMMARY_BYTES, "summary_too_large")
    _walk_sanitized(value)
    _require(
        set(value).issubset(
            {"schema", "evidence_status", "qualification_status", "candidate_sha", "cli_version", "route", "cases", "negative_controls", "sanitization", "failure"}
        ),
        "unknown_summary_field",
    )
    _require(value.get("schema") == SCHEMA, "schema_invalid")
    _require(value.get("evidence_status") == "observed_synthetic_upstream", "evidence_status_invalid")
    _require(value.get("qualification_status") in {"unqualified", "not_run", "failed", "passed"}, "qualification_status_invalid")
    bound_sha = value.get("candidate_sha")
    _require(bound_sha is None or (isinstance(bound_sha, str) and SHA1.fullmatch(bound_sha) is not None), "candidate_sha_invalid")
    if value.get("qualification_status") in {"passed", "failed"}:
        _require(bound_sha is not None, "candidate_sha_required")
    if candidate_sha is not None:
        _require(isinstance(candidate_sha, str) and SHA1.fullmatch(candidate_sha) is not None, "candidate_sha_invalid")
        _require(bound_sha == candidate_sha, "candidate_sha_mismatch")

    route = value.get("route")
    _require(isinstance(route, dict), "route_invalid")
    _require(route.get("selected_model") == "opaque-selected-model", "route_model_not_opaque")
    _require(route.get("selected_provider") == "opaque-selected-provider", "route_provider_not_opaque")
    _require(route.get("cross_provider_requests") == 0, "cross_provider_request")
    _require(route.get("hosted_search_substitution") is False, "hosted_search_substitution")
    _require(route.get("identity_observation") == "observed_request_route", "route_identity_observation_invalid")
    route_digests = route.get("identity_digests")
    _require(
        isinstance(route_digests, list)
        and len(route_digests) <= MAX_ROUTE_DIGESTS
        and all(isinstance(value, str) and SHA256.fullmatch(value) is not None for value in route_digests),
        "route_identity_digest_invalid",
    )

    sanitization = value.get("sanitization")
    _require(
        sanitization
        == {
            "raw_bodies_retained": False,
            "prompts_retained": False,
            "credentials_retained": False,
            "ids_opaque_or_hashed": True,
        },
        "sanitization_invalid",
    )
    controls = value.get("negative_controls")
    _require(
        controls == {"unknown_alias": "passed", "duplicate_identity": "passed", "malformed_envelope": "passed"},
        "negative_controls_invalid",
    )

    cases = value.get("cases")
    _require(isinstance(cases, list) and len(cases) <= MAX_CASES, "cases_invalid")
    seen: set[str] = set()
    for case in cases:
        _require(isinstance(case, dict), "case_invalid")
        case_id = case.get("id")
        _require(isinstance(case_id, str) and case_id in CASE_IDS and case_id not in seen, "case_id_invalid")
        seen.add(case_id)
        _require(case.get("protocol") == PROTOCOL_BY_CASE[case_id], "protocol_invalid")
        _require(case.get("planner_eligible") is True, "planner_eligibility_invalid")
        _require(case.get("tool_search_visible") is True, "tool_search_visibility_invalid")
        _require(case.get("planner_eligibility_source") == "client_tool_surface_observed", "planner_eligibility_source_invalid")
        _require(case.get("gateway_owned_tool_execution_count") == 0, "gateway_tool_execution_observed")
        _require(case.get("identity_preserved") is True, "identity_not_preserved")
        observation = case.get("identity_observation")
        _require(
            observation == {"response_stream": "observed", "request_history": "observed"},
            "identity_observation_invalid",
        )
        for key in ("mcp_tools_list_count", "mcp_tools_call_count"):
            _require(type(case.get(key)) is int and 0 <= case[key] <= MAX_MCP_COUNT, "mcp_ledger_invalid")
        _require(case.get("workspace_mutation_verified") is True, "workspace_mutation_unverified")
        route_digest = case.get("route_identity_digest")
        _require(isinstance(route_digest, str) and SHA256.fullmatch(route_digest) is not None, "route_identity_digest_invalid")
        _validate_route_observation(case_id, case.get("route_observation"), route_digest)
        digest = case.get("history_order_digest")
        _require(isinstance(digest, str) and SHA256.fullmatch(digest) is not None, "history_digest_invalid")
        provenance = _validate_provenance(case_id, case.get("provenance"))
        _require(digest == provenance["history"]["order_digest"], "history_digest_mismatch")
        event_types = case.get("sse_event_types")
        _require(
            isinstance(event_types, list)
            and len(event_types) <= MAX_EVENT_TYPES
            and all(isinstance(event, str) and len(event) <= 128 for event in event_types),
            "sse_event_types_invalid",
        )
        cli_event_count = case.get("cli_event_shape_count")
        _require(type(cli_event_count) is int and 0 < cli_event_count <= MAX_CLI_EVENTS, "cli_event_shape_invalid")
        cli_event_shapes = case.get("cli_event_shapes")
        _require(
            isinstance(cli_event_shapes, list)
            and len(cli_event_shapes) == cli_event_count
            and len(cli_event_shapes) <= MAX_CLI_EVENTS,
            "cli_event_ledger_invalid",
        )
        for shape in cli_event_shapes:
            _require(isinstance(shape, dict), "cli_event_ledger_invalid")
            event_type = shape.get("type")
            item_type = shape.get("item_type")
            identity_digest = shape.get("identity_digest")
            _require(
                isinstance(event_type, str)
                and 0 < len(event_type) <= 128
                and (item_type is None or (isinstance(item_type, str) and len(item_type) <= 128))
                and isinstance(identity_digest, str)
                and SHA256.fullmatch(identity_digest) is not None,
                "cli_event_ledger_invalid",
            )
        _require(
            case.get("cli_identity_digest")
            == _digest([shape["identity_digest"] for shape in cli_event_shapes]),
            "cli_identity_digest_invalid",
        )
        _require(
            case.get("cli_event_ledger_digest") == _digest(cli_event_shapes),
            "cli_event_ledger_invalid",
        )
        _require(case.get("cli_terminal_event") == "turn.completed", "cli_terminal_event_invalid")
        _require(
            any(shape.get("type") == "turn.completed" for shape in cli_event_shapes),
            "cli_terminal_event_invalid",
        )
        if case_id in EXPLICIT_CASES:
            _require(case.get("selection") == "selected", "explicit_selection_invalid")
            _require(case.get("classification") == "completed", "explicit_classification_invalid")
        else:
            _require(case.get("selection") == "model_not_selected", "no_hint_selection_invalid")
            _require(case.get("classification") == "model_not_selected", "no_hint_classification_invalid")
    expected_route_digests = [
        case.get("route_identity_digest")
        for case in cases
        if isinstance(case.get("route_identity_digest"), str)
    ]
    _require(route_digests == expected_route_digests, "route_identity_digest_mismatch")
    _require(
        route.get("cross_provider_requests")
        == sum(
            int(case["route_observation"].get("cross_provider_requests", 0))
            for case in cases
            if isinstance(case.get("route_observation"), dict)
        ),
        "route_observation_cross_provider",
    )
    if value.get("qualification_status") == "passed":
        _require(seen == CASE_IDS, "case_coverage_incomplete")
    return {"schema": SCHEMA, "case_count": len(cases), "status": "pass"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--candidate-sha")
    args = parser.parse_args(argv)
    try:
        result = validate_summary(_load(args.summary), candidate_sha=args.candidate_sha)
    except EvidenceValidationError as error:
        print(f"ISSUE_278_EVIDENCE_INVALID:{error}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
