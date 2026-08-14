"""Parse and validate the sanitized Beta4.2 CLI qualification summary.

The real-client runner is intentionally kept private to the release operator's
Windows environment. This module is the checked-in boundary for its output:
it accepts only the bounded, hash-based summary that may be retained or
attached to a release. It fails closed rather than attempting to redact a
raw request, Session, or credential-bearing artifact after the fact.
"""

from __future__ import annotations

import hashlib
import json
import re
import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Any


SCHEMA = "codexhub.beta42.cli-e2e.v1"
CLI_VERSION = "0.146.1"
MAX_SUMMARY_BYTES = 512 * 1024
MAX_CASES = 32
MAX_STRING_LENGTH = 512
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_CLASSIFICATION = re.compile(r"^[a-z0-9_:-]{1,96}$")
SAFE_RELATIVE_ARTIFACT = re.compile(r"^[A-Za-z0-9._/-]{1,160}$")

CASE_IDS = frozenset(
    {
        "v1-lifecycle",
        "v2-lifecycle",
        "version-selection-replay",
        "legacy-session-resume",
        "new-binding-integrity",
        "cli-restart-continuity",
        "external-v1-boundary",
        "external-v2-lifecycle",
        "stable-tool-alias-replay",
        "deferred-core-bounded",
    }
)
EXTERNAL_V2_LIFECYCLE = (
    "spawn_agent",
    "list_agents",
    "send_message",
    "followup_task",
    "wait_agent",
    "interrupt_agent",
    "wait_agent",
    "list_agents",
)
EXTERNAL_V2_TOOL_NAMES = tuple(dict.fromkeys(EXTERNAL_V2_LIFECYCLE))
EXTERNAL_V2_ENDPOINT = "https://ollama.com/v1"
UPSTREAM_BLOCKED = "blocked_upstream_provider_aware_delivery"

# These names are forbidden anywhere in retained evidence. The runner may
# keep them in its private working directory, but the release summary must not
# contain the corresponding payloads or secret-bearing fields.
_FORBIDDEN_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "password",
    "credential",
    "secret",
    "prompt",
    "raw_body",
    "request_body",
    "response_body",
    "ciphertext",
    "session_artifact",
)


class EvidenceValidationError(ValueError):
    """Raised when an operator summary is not safe or structurally complete."""


def _fail(code: str) -> None:
    raise EvidenceValidationError(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        _fail(code)


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _walk_sanitized(value: Any, *, key: str | None = None) -> None:
    if isinstance(value, Mapping):
        for child_key, child_value in value.items():
            _require(isinstance(child_key, str), "field_name_invalid")
            lowered = child_key.lower()
            _require(
                lowered == "prompt_cache_key_preserved"
                or not any(part in lowered for part in _FORBIDDEN_KEY_PARTS),
                "secret_or_raw_field",
            )
            _walk_sanitized(child_value, key=lowered)
        return
    if isinstance(value, list):
        _require(len(value) <= MAX_CASES * 64, "list_bound_exceeded")
        for item in value:
            _walk_sanitized(item, key=key)
        return
    if isinstance(value, str):
        _require(len(value) <= MAX_STRING_LENGTH, "string_bound_exceeded")
        if key in {"artifact", "artifact_name", "path", "relative_path"}:
            _require(
                SAFE_RELATIVE_ARTIFACT.fullmatch(value) is not None,
                "artifact_path_invalid",
            )
            _require(
                not value.startswith("/") and not re.match(r"^[A-Za-z]:", value),
                "artifact_path_invalid",
            )
        return
    _require(value is None or isinstance(value, (bool, int, float)), "value_type_invalid")


def parse_summary(value: bytes | str | Mapping[str, Any]) -> dict[str, Any]:
    """Parse one JSON summary without accepting an array or raw JSON text."""

    if isinstance(value, Mapping):
        parsed = dict(value)
    else:
        raw = value.decode("utf-8") if isinstance(value, bytes) else value
        _require(isinstance(raw, str), "summary_encoding_invalid")
        _require(len(raw.encode("utf-8")) <= MAX_SUMMARY_BYTES, "summary_too_large")
        try:
            parsed_value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceValidationError("summary_encoding_invalid") from exc
        _require(isinstance(parsed_value, dict), "summary_root_invalid")
        parsed = parsed_value
    _walk_sanitized(parsed)
    return parsed


def _validate_replay_hashes(case: Mapping[str, Any]) -> None:
    replays = case.get("replays")
    _require(isinstance(replays, list) and 1 <= len(replays) <= 8, "replay_records_invalid")
    caller_hashes: list[str] = []
    upstream_hashes: list[str] = []
    aliases: list[list[str]] = []
    for replay in replays:
        _require(isinstance(replay, Mapping), "replay_record_invalid")
        caller = replay.get("caller_body_sha256")
        upstream = replay.get("upstream_body_sha256")
        replay_aliases = replay.get("aliases")
        _require(_is_hash(caller) and _is_hash(upstream), "replay_hash_invalid")
        _require(
            isinstance(replay_aliases, list)
            and 0 < len(replay_aliases) <= 512
            and all(
                isinstance(alias, str)
                and 1 <= len(alias) <= MAX_STRING_LENGTH
                and alias.startswith("__codexhub_")
                for alias in replay_aliases
            ),
            "replay_aliases_invalid",
        )
        caller_hashes.append(caller)
        upstream_hashes.append(upstream)
        aliases.append(replay_aliases)
    _require(len(set(caller_hashes)) == 1, "caller_body_not_stable")
    _require(len(set(upstream_hashes)) == 1, "upstream_body_not_stable")
    _require(len(set(tuple(alias_list) for alias_list in aliases)) == 1, "aliases_not_stable")
    _require(case.get("prompt_cache_key_preserved") is True, "prompt_cache_key_not_preserved")


def _validate_external_v2(case: Mapping[str, Any]) -> None:
    _require(case.get("provider") == "ollama-cloud", "external_provider_mismatch")
    _require(case.get("model") == "glm-5.2", "external_model_mismatch")
    _require(case.get("upstream_endpoint") == EXTERNAL_V2_ENDPOINT, "external_endpoint_mismatch")
    _require(case.get("native_namespace_count") == 0, "native_namespace_exposed")
    _require(case.get("upstream_function_tool_count") == len(EXTERNAL_V2_TOOL_NAMES), "function_tool_surface_invalid")
    _require(case.get("stream_history") is True, "stream_history_missing")
    _require(case.get("restart_readback") is True, "restart_readback_missing")
    _require(case.get("terminal_replay") is True, "terminal_replay_missing")
    _require(case.get("error_replay") is True, "error_replay_missing")
    _require(case.get("support_claim") == "gateway_adapter", "provider_native_claim")
    lifecycle = case.get("lifecycle")
    _require(lifecycle == list(EXTERNAL_V2_LIFECYCLE), "external_lifecycle_order_invalid")

    adapter = case.get("adapter_evidence")
    _require(isinstance(adapter, Mapping), "adapter_evidence_missing")
    _require(adapter.get("backend") == "codexhub_gateway", "adapter_backend_invalid")
    _require(adapter.get("upstream_surface") == "ordinary_function_tools", "adapter_surface_invalid")
    _require(adapter.get("client_surface") == "collaboration_v2", "adapter_client_surface_invalid")
    _require(adapter.get("inverse_mapping") == "client_owned_v2", "adapter_inverse_mapping_invalid")
    _require(
        adapter.get("inverse_mapping_count") == len(EXTERNAL_V2_LIFECYCLE),
        "adapter_inverse_mapping_count_invalid",
    )

    aliases = case.get("aliases")
    _require(
        isinstance(aliases, list)
        and len(aliases) == len(EXTERNAL_V2_TOOL_NAMES)
        and len(set(aliases)) == len(aliases)
        and all(
            isinstance(alias, str)
            and 1 <= len(alias) <= MAX_STRING_LENGTH
            and alias.startswith("__codexhub_ns_")
            for alias in aliases
        ),
        "external_aliases_invalid",
    )
    alias_map = case.get("alias_map")
    _require(isinstance(alias_map, list) and len(alias_map) == len(EXTERNAL_V2_TOOL_NAMES), "external_alias_map_invalid")
    _require(
        all(isinstance(entry, Mapping) for entry in alias_map),
        "external_alias_map_invalid",
    )
    alias_by_tool: dict[str, str] = {}
    for entry in alias_map:
        tool_name = entry.get("tool")
        alias = entry.get("alias")
        _require(tool_name in EXTERNAL_V2_TOOL_NAMES, "external_alias_tool_invalid")
        _require(isinstance(alias, str) and alias in aliases, "external_alias_map_invalid")
        _require(tool_name not in alias_by_tool and alias not in alias_by_tool.values(), "external_alias_map_not_injective")
        alias_by_tool[tool_name] = alias
    _require(set(alias_by_tool) == set(EXTERNAL_V2_TOOL_NAMES), "external_alias_map_incomplete")
    _require(set(alias_by_tool.values()) == set(aliases), "external_alias_map_incomplete")

    alias_replays = case.get("alias_replays")
    _require(isinstance(alias_replays, list) and len(alias_replays) == 2, "external_alias_replays_invalid")
    for replay_aliases in alias_replays:
        _require(replay_aliases == aliases, "external_aliases_not_stable")

    pairs = case.get("call_output_pairs")
    _require(
        isinstance(pairs, list) and len(pairs) == len(EXTERNAL_V2_LIFECYCLE),
        "call_output_pairs_invalid",
    )
    for expected_tool, pair in zip(EXTERNAL_V2_LIFECYCLE, pairs, strict=True):
        _require(isinstance(pair, Mapping), "call_output_pair_invalid")
        _require(pair.get("tool") == expected_tool, "call_output_order_invalid")
        _require(pair.get("alias") == alias_by_tool.get(expected_tool), "call_output_alias_invalid")
        _require(pair.get("call_count") == 1 and pair.get("output_count") == 1, "call_output_cardinality_invalid")
        _require(pair.get("inverse_mapped") is True, "inverse_mapping_missing")

    target_hash = case.get("target_id_sha256")
    final_target_hash = case.get("final_readback_target_id_sha256")
    _require(_is_hash(target_hash) and final_target_hash == target_hash, "final_readback_target_mismatch")
    _require(case.get("final_readback_terminal") is True, "final_readback_not_terminal")


def validate_summary(
    value: bytes | str | Mapping[str, Any],
    *,
    candidate_sha: str | None = None,
) -> dict[str, Any]:
    """Validate and return only bounded release-safe summary metadata."""

    summary = parse_summary(value)
    _require(summary.get("schema") == SCHEMA, "schema_invalid")
    _require(summary.get("cli_version") == CLI_VERSION, "cli_version_invalid")
    bound_sha = summary.get("candidate_sha")
    _require(
        isinstance(bound_sha, str) and SHA1.fullmatch(bound_sha) is not None,
        "candidate_sha_invalid",
    )
    if candidate_sha is not None:
        _require(SHA1.fullmatch(candidate_sha) is not None, "candidate_sha_invalid")
        _require(bound_sha == candidate_sha, "candidate_sha_mismatch")
    outcome = summary.get("outcome")
    _require(outcome in {"passed", "failed", "not_run", "blocked"}, "outcome_invalid")
    failure = summary.get("failure_classification", "none")
    _require(
        isinstance(failure, str) and SAFE_CLASSIFICATION.fullmatch(failure) is not None,
        "failure_classification_invalid",
    )
    _require(summary.get("upstream_v2_boundary") == UPSTREAM_BLOCKED, "upstream_boundary_missing")

    cases = summary.get("cases")
    _require(isinstance(cases, list) and 0 < len(cases) <= MAX_CASES, "cases_invalid")
    seen: set[str] = set()
    for case in cases:
        _require(isinstance(case, Mapping), "case_invalid")
        case_id = case.get("id")
        _require(
            isinstance(case_id, str) and case_id in CASE_IDS and case_id not in seen,
            "case_id_invalid",
        )
        seen.add(case_id)
        _require(
            case.get("outcome") in {"passed", "failed", "not_run", "blocked"},
            "case_outcome_invalid",
        )
        _require(
            isinstance(case.get("protocol"), str) and len(case["protocol"]) <= 64,
            "case_protocol_invalid",
        )
        _require(
            isinstance(case.get("model"), str)
            and 0 < len(case["model"]) <= MAX_STRING_LENGTH,
            "case_model_invalid",
        )
        for hash_key in ("caller_body_sha256", "upstream_body_sha256", "history_sha256"):
            if hash_key in case:
                _require(_is_hash(case[hash_key]), "case_hash_invalid")
        if case_id == "stable-tool-alias-replay" and case["outcome"] == "passed":
            _validate_replay_hashes(case)
        if case_id == "external-v2-lifecycle" and case["outcome"] == "passed":
            _validate_external_v2(case)

    if outcome == "passed":
        _require(seen == CASE_IDS, "case_coverage_incomplete")
        _require(all(case["outcome"] == "passed" for case in cases), "case_outcome_incomplete")
        _require(failure == "none", "passed_with_failure")
    return {
        "schema": SCHEMA,
        "candidate_sha": bound_sha,
        "cli_version": CLI_VERSION,
        "outcome": outcome,
        "case_count": len(cases),
        "failure_classification": failure,
    }


def digest_bytes(value: bytes) -> str:
    """Return the only body representation allowed in release evidence."""

    return "sha256:" + hashlib.sha256(value).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--candidate-sha")
    args = parser.parse_args(argv)
    try:
        result = validate_summary(
            args.summary.read_bytes(),
            candidate_sha=args.candidate_sha,
        )
    except (OSError, EvidenceValidationError) as exc:
        print(f"BETA42_EVIDENCE_INVALID:{exc}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


__all__ = [
    "CASE_IDS",
    "CLI_VERSION",
    "EXTERNAL_V2_LIFECYCLE",
    "EXTERNAL_V2_ENDPOINT",
    "EXTERNAL_V2_TOOL_NAMES",
    "EvidenceValidationError",
    "SCHEMA",
    "UPSTREAM_BLOCKED",
    "digest_bytes",
    "main",
    "parse_summary",
    "validate_summary",
]


if __name__ == "__main__":
    raise SystemExit(main())
