#!/usr/bin/env python3
"""Run bounded CLI-only #278/#280 protocol-controlled evidence.

The runner owns every process and endpoint used by a case: a loopback upstream
fixture, a temporary ``CODEX_HOME``, the CodexHub Python Gateway, and one
official ``codex exec --json --ephemeral`` invocation.  The upstream is
synthetic by design.  Only shape/count telemetry is retained in ``summary``;
request bodies, prompts, credentials, paths, and protocol identities are never
written or printed.

When the requested Codex executable is unavailable the runner writes a bounded
``not_run`` summary instead of silently falling back to an offline fixture.
This keeps protocol-controlled evidence distinct from authenticated provider
qualification.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "codexhub.issue278.cli-tool-search.v1"
FIXTURE_KEY = "codexhub-issue278-fixture"
FIXTURE_KEY_BY_PROVIDER = {
    "ollama-cloud": FIXTURE_KEY,
    # Keep the two synthetic routes cryptographically distinct so an adapted
    # case cannot pass route validation by reusing the native fixture auth.
    "volc": "codexhub-issue278-volc-fixture",
}
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_CASES = ("native_explicit", "native_no_hint", "adapted_explicit", "adapted_no_hint")
CASE_IDS = frozenset(DEFAULT_CASES)
MODEL_BY_CASE = {
    "native_explicit": "ollama-cloud/glm-5.2",
    "native_no_hint": "ollama-cloud/glm-5.2",
    "adapted_explicit": "volc/glm-5.2",
    "adapted_no_hint": "volc/glm-5.2",
}
PROVIDER_BY_CASE = {
    "native_explicit": "ollama-cloud",
    "native_no_hint": "ollama-cloud",
    "adapted_explicit": "volc",
    "adapted_no_hint": "volc",
}
UPSTREAM_MODEL_BY_CASE = {case_id: "glm-5.2" for case_id in CASE_IDS}
PROTOCOL_BY_CASE = {
    "native_explicit": "responses_structured",
    "native_no_hint": "responses_structured",
    "adapted_explicit": "chat_tools",
    "adapted_no_hint": "chat_tools",
}
DISPOSITION_BY_CASE = {
    "native_explicit": "native",
    "native_no_hint": "native",
    "adapted_explicit": "adapt",
    "adapted_no_hint": "adapt",
}
EXPLICIT_CASES = frozenset({"native_explicit", "adapted_explicit"})
NO_HINT_CASES = frozenset(CASE_IDS - EXPLICIT_CASES)
TOOL_SEARCH_DECLARATION = {"type": "tool_search", "execution": "client"}
DISCOVERED_TOOL_NAME = "fixture_discovered_tool"
WORKFLOW_TOOL_NAMES = ("shell_command", "apply_patch", "shell_command")
TRACE_DECLARATION = "tool_search.declaration"
TRACE_NOT_SELECTED = "tool_search.not_selected"
TRACE_SEARCH_CALL = "tool_search.call"
TRACE_SEARCH_RESULT = "tool_search.result"
TRACE_DISCOVERED_DECLARATION = "discovered.declaration"
TRACE_DISCOVERED_CALL = "discovered.call"
TRACE_DISCOVERED_RESULT = "discovered.result"
EXPECTED_EXPLICIT_TRACE = (
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
)
EXPECTED_NO_HINT_TRACE = (TRACE_DECLARATION, TRACE_NOT_SELECTED)
SENSITIVE_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OLLAMA_API_KEY",
    "VOLCENGINE_API_KEY",
    "ARK_API_KEY",
    "MINIMAX_API_KEY",
    "XUNFEI_MAAS_API_KEY",
    "CODEX_CONFIG",
    "CODEXHUB_RUNTIME_HOME",
    "CODEXHUB_CODEX_TARGET_HOME",
    "CODEXHUB_HOME",
)

# Evidence infrastructure must fail closed instead of retaining unbounded
# client/provider output. These limits are comfortably above the four-case
# fixture while bounding every list and stream we keep.
MAX_CLI_STDOUT_BYTES = 2 * 1024 * 1024
MAX_CLI_EVENTS = 256
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
MAX_REQUESTS = 32
MAX_TRACE_TOKENS = 64
MAX_PROTOCOLS = 32
MAX_HISTORY_DIGESTS = 32
MAX_RESPONSE_SHAPES = 512
MAX_SSE_EVENT_TYPES = 512
MAX_IDENTITY_DIGESTS = 32
MAX_MCP_COUNT = 64
MAX_GATEWAY_ROUTE_OBSERVATIONS = 32


def _valid_candidate_sha(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def _walk_mappings(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child)


def _request_has_tool_search_output(body: Any, call_id: str | None) -> bool:
    return any(
        item.get("type") == "tool_search_output"
        and item.get("execution") == "client"
        and isinstance(call_id, str)
        and item.get("call_id") == call_id
        for item in _walk_mappings(body)
    )


def _request_has_discovered_tool(body: Any) -> bool:
    return any(
        (
            item.get("name") == DISCOVERED_TOOL_NAME
            and item.get("type") in {"function", "function_tool"}
        )
        or (
            item.get("type") == "function"
            and isinstance(item.get("function"), dict)
            and item["function"].get("name") == DISCOVERED_TOOL_NAME
        )
        or (
            item.get("description") == "Issue 278 protocol fixture tool."
            and isinstance(item.get("name"), str)
        )
        or (
            item.get("type") == "function"
            and isinstance(item.get("function"), dict)
            and item["function"].get("description") == "Issue 278 protocol fixture tool."
        )
        for item in _walk_mappings(body)
    )


def _request_has_function_result(body: Any, call_id: str | None) -> bool:
    if not isinstance(call_id, str):
        return False
    return any(
        (
            isinstance(item.get("type"), str)
            and item.get("type") in {"function_call_output", "custom_tool_call_output"}
            and item.get("call_id") == call_id
        )
        or (item.get("role") == "tool" and item.get("tool_call_id") == call_id)
        for item in _walk_mappings(body)
    )


class RunnerFailure(RuntimeError):
    """A bounded failure classification; never carries wire or prompt text."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _safe_case(value: str) -> str:
    if value not in CASE_IDS:
        raise RunnerFailure("case_invalid")
    return value


def _safe_path(value: Path) -> Path:
    return value.expanduser().resolve()


def _event_line(event: MappingLike) -> bytes:
    encoded = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
    event_type = event.get("type")
    prefix = f"event: {event_type}\n" if isinstance(event_type, str) and event_type else ""
    return (prefix + "data: " + encoded + "\n\n").encode("utf-8")


MappingLike = dict[str, Any]


@dataclass
class FixtureState:
    case_id: str
    fixture_key: str = field(init=False)
    lock: threading.Lock = field(default_factory=threading.Lock)
    requests: int = 0
    protocols: list[str] = field(default_factory=list)
    tool_search_visible: bool = False
    search_seen: bool = False
    discovered_seen: bool = False
    workflow_seen: list[str] = field(default_factory=list)
    sse_event_types: list[str] = field(default_factory=list)
    terminal_count: int = 0
    malformed_requests: int = 0
    cross_provider_requests: int = 0
    auth_failures: int = 0
    unexpected_paths: int = 0
    history_digest_inputs: list[str] = field(default_factory=list)
    trace_tokens: list[str] = field(default_factory=list)
    response_shape_tokens: list[str] = field(default_factory=list)
    response_count: int = 0
    response_identity_digests: list[str] = field(default_factory=list)
    request_identity_digests: list[str] = field(default_factory=list)
    response_identity_observations: list[dict[str, Any]] = field(default_factory=list)
    request_identity_observations: list[dict[str, Any]] = field(default_factory=list)
    identity_binding_count: int = 0
    identity_mismatch_count: int = 0
    request_identity_mismatch_count: int = 0
    pending_identity_tokens: tuple[tuple[str, str], ...] = ()
    pending_search_identity: bool = False
    search_identity_binding_count: int = 0
    client_tool_surface_observed: bool = False
    discovered_tool_name: str | None = None
    search_tool_name: str | None = None
    adapted_workflow_aliases: dict[str, str] = field(default_factory=dict)
    response_stage: str | None = None
    awaiting_input: str | None = None
    pending_call_id: str | None = None
    protocol_error: str | None = None
    # Route binding is derived from what the isolated upstream actually
    # receives.  Keep only bounded digests/paths; never retain the token or
    # model string itself.
    route_observation_digests: list[str] = field(default_factory=list)
    route_model_digests: list[str] = field(default_factory=list)
    route_provider_digests: list[str] = field(default_factory=list)
    route_paths: list[str] = field(default_factory=list)
    route_protocol_sequence: list[str] = field(default_factory=list)
    route_signature: tuple[str, str, str] | None = None

    def __post_init__(self) -> None:
        self.fixture_key = FIXTURE_KEY_BY_PROVIDER[PROVIDER_BY_CASE[self.case_id]]
        # This is a planner contract marker, not a retained wire declaration.
        # The actual request/response stages are appended as the fixture runs.
        self.trace_tokens.append(TRACE_DECLARATION)

    @property
    def explicit(self) -> bool:
        return self.case_id in EXPLICIT_CASES

    @property
    def adapted(self) -> bool:
        return self.case_id.startswith("adapted_")

    def _append_bounded(self, target: list[Any], value: Any, limit: int) -> bool:
        if len(target) >= limit:
            self.protocol_error = "evidence_limit_exceeded"
            return False
        target.append(value)
        return True

    def _extend_trace(self, values: Iterable[str]) -> bool:
        values = tuple(values)
        if len(self.trace_tokens) + len(values) > MAX_TRACE_TOKENS:
            self.protocol_error = "evidence_limit_exceeded"
            return False
        self.trace_tokens.extend(values)
        return True

    def record_request(self, path: str, body: Any, authorization: str | None) -> int:
        with self.lock:
            if self.protocol_error is not None:
                return self.requests
            if self.requests >= MAX_REQUESTS:
                self.protocol_error = "evidence_limit_exceeded"
                return self.requests
            if authorization != f"Bearer {self.fixture_key}":
                self.auth_failures += 1
            if path not in {"/v1/responses", "/v1/chat/completions"}:
                self.unexpected_paths += 1
            if not isinstance(body, dict):
                self.malformed_requests += 1
                return self.requests
            protocol = "chat_completions" if path.endswith("chat/completions") else "responses"
            model_digest = _digest(body.get("model"))
            provider_digest = _digest(authorization)
            route_signature = (path, model_digest, provider_digest)
            if self.route_signature is None:
                self.route_signature = route_signature
            elif route_signature != self.route_signature:
                # A single case owns one exact provider/model/endpoint route;
                # any observed change is a cross-provider/route violation.
                self.cross_provider_requests += 1
            if not self._append_bounded(self.route_observation_digests, _digest({
                "path": path,
                "model_digest": model_digest,
                "provider_digest": provider_digest,
                "protocol": protocol,
            }), MAX_IDENTITY_DIGESTS):
                return self.requests
            if not self._append_bounded(self.route_model_digests, model_digest, MAX_IDENTITY_DIGESTS):
                return self.requests
            if not self._append_bounded(self.route_provider_digests, provider_digest, MAX_IDENTITY_DIGESTS):
                return self.requests
            if not self._append_bounded(self.route_paths, path, MAX_IDENTITY_DIGESTS):
                return self.requests
            if not self._append_bounded(self.route_protocol_sequence, protocol, MAX_PROTOCOLS):
                return self.requests
            request_identity_tokens = _identity_tokens(body)
            request_identity_observation = _identity_observation(request_identity_tokens)
            if not self._append_bounded(
                self.request_identity_digests,
                request_identity_observation["identity_digest"],
                MAX_IDENTITY_DIGESTS,
            ):
                return self.requests
            if not self._append_bounded(
                self.request_identity_observations,
                request_identity_observation,
                MAX_IDENTITY_DIGESTS,
            ):
                return self.requests
            required_identity_tokens = tuple(
                token
                for token in self.pending_identity_tokens
                if token[0] == "call_id"
            )
            if required_identity_tokens:
                request_identity_values = {value for _role, value in request_identity_tokens}
                if any(value not in request_identity_values for _role, value in required_identity_tokens):
                    search_result_bound = (
                        self.pending_search_identity
                        and self.awaiting_input == "tool_search_output"
                        and (
                            _request_has_tool_search_output(body, required_identity_tokens[0][1])
                            or _request_has_discovered_tool(body)
                        )
                    )
                    if search_result_bound:
                        # CLI 0.146 materializes the client-owned search
                        # result as the discovered declaration in the next
                        # request instead of retaining a tool_search_output
                        # history item.  The declaration is the reversible
                        # client-side binding; do not call it an ID mismatch.
                        self.search_identity_binding_count += 1
                        self.identity_binding_count += 1
                    else:
                        self.request_identity_mismatch_count += 1
                else:
                    self.identity_binding_count += 1
            self.pending_identity_tokens = ()
            self.pending_search_identity = False
            self.requests += 1
            if not self._append_bounded(self.protocols, protocol, MAX_PROTOCOLS):
                return self.requests
            tools = body.get("tools")
            if isinstance(tools, list):
                names = []
                observed_search_name: str | None = None
                observed_custom_alias: str | None = None
                for tool in tools:
                    if len(names) >= 256:
                        self.protocol_error = "evidence_limit_exceeded"
                        return self.requests
                    if not isinstance(tool, dict):
                        continue
                    if tool.get("type") == "tool_search" and tool.get("execution") == "client":
                        self.tool_search_visible = True
                    function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
                    if isinstance(function, dict):
                        name = function.get("name")
                        if isinstance(name, str):
                            names.append(name)
                            description = function.get("description")
                            parameters = function.get("parameters")
                            required = parameters.get("required") if isinstance(parameters, dict) else None
                            if (
                                self.adapted
                                and name.startswith("__codexhub_custom_")
                                and isinstance(required, list)
                                and "__codexhub_custom_input" in required
                            ):
                                # apply_patch is the only free-form custom
                                # tool in this fixture. Its alias is scoped
                                # to each request and must be replayed as-is.
                                self.adapted_workflow_aliases["apply_patch"] = name
                                observed_custom_alias = name
                            if (
                                isinstance(description, str)
                                and description == "Issue 278 protocol fixture tool."
                            ):
                                # Codex may expose an MCP tool through a
                                # request-scoped alias.  Replay the exact
                                # client-declared name so the CLI can bind
                                # the returned function call to MCP.
                                self.discovered_tool_name = name
                            if name.startswith("__codexhub_search_"):
                                self.tool_search_visible = True
                                # Adapted Chat providers receive a
                                # request-scoped reversible alias. Replay the
                                # observed declaration; inventing a
                                # provider-local name must fail closed.
                                self.search_tool_name = name
                                observed_search_name = name
                # Codex CLI owns deferred tool discovery and consumes the
                # native declaration before serializing the provider request.
                # The three client-side MCP discovery functions are the
                # observable declaration of that surface; do not infer it
                # merely from the fixture's response ordinal.
                self.client_tool_surface_observed = {
                    "list_mcp_resources",
                    "list_mcp_resource_templates",
                    "read_mcp_resource",
                }.issubset(names)
                if self.client_tool_surface_observed:
                    self.tool_search_visible = True
                if not self._append_bounded(
                    self.history_digest_inputs,
                    _digest({"tools": sorted(names)}),
                    MAX_HISTORY_DIGESTS,
                ):
                    return self.requests
            if self.explicit:
                if self.requests == 1:
                    self.response_stage = "search_call"
                elif self.awaiting_input == "tool_search_output":
                    if _request_has_tool_search_output(body, self.pending_call_id) or _request_has_discovered_tool(body):
                        self._extend_trace((TRACE_SEARCH_RESULT, TRACE_DISCOVERED_DECLARATION))
                        self.response_stage = "discovered_call"
                    else:
                        self.protocol_error = "missing_tool_search_output"
                elif self.awaiting_input == "function_call_output":
                    if _request_has_function_result(body, self.pending_call_id):
                        if self.pending_call_id == "fixture-discovered-call":
                            self._extend_trace((TRACE_DISCOVERED_RESULT, "code_mode.shell_command.call"))
                            self.response_stage = "workflow_call:1"
                        elif self.pending_call_id == "fixture-code-call-1":
                            self._extend_trace(("code_mode.shell_command.result", "code_mode.apply_patch.call"))
                            self.response_stage = "workflow_call:2"
                        elif self.pending_call_id == "fixture-code-call-2":
                            self._extend_trace(("code_mode.apply_patch.result", "code_mode.shell_command.call"))
                            self.response_stage = "workflow_call:3"
                        elif self.pending_call_id == "fixture-code-call-3":
                            self._append_bounded(self.trace_tokens, "code_mode.shell_command.result", MAX_TRACE_TOKENS)
                            self.response_stage = "text"
                    else:
                        self.protocol_error = "missing_function_call_output"
                else:
                    self.protocol_error = "unexpected_request_stage"
            elif self.requests == 1:
                self.response_stage = "text"
            elif self.response_stage != "text":
                self.protocol_error = "unexpected_no_hint_request"
            return self.requests

    def response_events(self, index: int) -> tuple[dict[str, Any], ...]:
        if self.protocol_error is not None:
            with self.lock:
                self.terminal_count += 1
            return _failed_response_events(
                response_id=f"fixture-response-{max(index, 1)}",
                code=self.protocol_error,
            )
        if not self.explicit:
            with self.lock:
                self.terminal_count += 1
                self._append_bounded(self.trace_tokens, TRACE_NOT_SELECTED, MAX_TRACE_TOKENS)
            return _text_response_events(response_id="fixture-response-1")
        if self.response_stage == "search_call":
            with self.lock:
                self.search_seen = True
                self._append_bounded(self.trace_tokens, TRACE_SEARCH_CALL, MAX_TRACE_TOKENS)
                self.awaiting_input = "tool_search_output"
                self.pending_call_id = "fixture-search-call"
            return _search_events(
                adapted=self.adapted,
                response_id="fixture-response-1",
                search_tool_name=self.search_tool_name,
            )
        if self.response_stage == "discovered_call":
            with self.lock:
                self.discovered_seen = True
                self._append_bounded(self.trace_tokens, TRACE_DISCOVERED_CALL, MAX_TRACE_TOKENS)
                self.awaiting_input = "function_call_output"
                self.pending_call_id = "fixture-discovered-call"
            return _discovered_call_events(
                response_id="fixture-response-2",
                name=self.discovered_tool_name or DISCOVERED_TOOL_NAME,
                namespace=None if self.adapted else "mcp__fixture",
            )
        if self.response_stage and self.response_stage.startswith("workflow_call:"):
            workflow_index = int(self.response_stage.rsplit(":", 1)[1])
            name = WORKFLOW_TOOL_NAMES[workflow_index - 1]
            call_id = f"fixture-code-call-{workflow_index}"
            wire_name = self.adapted_workflow_aliases.get(name) if self.adapted else None
            with self.lock:
                self._append_bounded(self.workflow_seen, name, MAX_TRACE_TOKENS)
                self.awaiting_input = "function_call_output"
                self.pending_call_id = call_id
            return _workflow_call_events(
                name,
                adapted=self.adapted,
                response_id=f"fixture-response-{workflow_index + 2}",
                item_id=f"fixture-code-item-{workflow_index + 2}",
                call_id=call_id,
                wire_name=wire_name,
            )
        if self.response_stage == "text":
            with self.lock:
                self.terminal_count += 1
                self.awaiting_input = None
                self.pending_call_id = None
            return _text_response_events(response_id="fixture-response-6")
        with self.lock:
            self.terminal_count += 1
        return _failed_response_events(response_id=f"fixture-response-{max(index, 1)}", code="missing_response_stage")

    def record_sse_events(self, events: Iterable[dict[str, Any]]) -> None:
        with self.lock:
            if self.protocol_error is not None:
                return
            self.response_count += 1
            observed_events = list(events)
            identity_tokens = _identity_tokens(observed_events)
            identity_observation = _identity_observation(identity_tokens)
            identity_digest = identity_observation["identity_digest"]
            if not self._append_bounded(self.response_identity_digests, identity_digest, MAX_IDENTITY_DIGESTS):
                return
            if not self._append_bounded(
                self.response_identity_observations,
                identity_observation,
                MAX_IDENTITY_DIGESTS,
            ):
                return
            identity_roles: dict[str, set[str]] = {}
            for role, value in identity_tokens:
                identity_roles.setdefault(role, set()).add(value)
            # A streamed response must have one stable response identity.  A
            # tool response may additionally carry one call identity; any
            # later request is checked against that observed call link below.
            if len(identity_roles.get("response_id", set())) != 1:
                self.identity_mismatch_count += 1
            if len(identity_roles.get("call_id", set())) > 1:
                self.identity_mismatch_count += 1
            if identity_roles.get("call_id") and not identity_roles.get("item_id"):
                self.identity_mismatch_count += 1
            self.pending_identity_tokens = identity_tokens
            self.pending_search_identity = any(
                isinstance(event, dict)
                and (
                    event.get("type") == "tool_search_call"
                    or (
                        event.get("type") == "response.output_item.done"
                        and isinstance(event.get("item"), dict)
                        and (
                            event["item"].get("type") == "tool_search_call"
                            or (
                                event["item"].get("type") == "function_call"
                                and (
                                    event["item"].get("name") == self.search_tool_name
                                    or "__codexhub_tool_search_input"
                                    in str(event["item"].get("arguments", ""))
                                )
                            )
                        )
                    )
                )
                for event in observed_events
            )
            for event in observed_events:
                event_type = event.get("type") if isinstance(event, dict) else None
                if isinstance(event_type, str):
                    if not self._append_bounded(self.sse_event_types, event_type, MAX_SSE_EVENT_TYPES):
                        return
                    item = event.get("item") if isinstance(event.get("item"), dict) else None
                    item_type = item.get("type") if isinstance(item, dict) else None
                    if not self._append_bounded(
                        self.response_shape_tokens,
                        f"{event_type}:{item_type}" if isinstance(item_type, str) else event_type,
                        MAX_RESPONSE_SHAPES,
                    ):
                        return


def _identity_tokens(value: Any) -> tuple[tuple[str, str], ...]:
    """Extract stable identity roles while retaining no raw identity in output."""

    tokens: list[tuple[str, str]] = []

    def visit(node: Any, parent_key: str | None = None) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                key_text = str(key)
                if key_text == "call_id" and isinstance(child, str) and child:
                    tokens.append(("call_id", child))
                elif key_text == "tool_call_id" and isinstance(child, str) and child:
                    # Chat Completions carries the same logical link under
                    # ``tool_call_id`` after the Responses adapter.
                    tokens.append(("call_id", child))
                elif key_text == "item_id" and isinstance(child, str) and child:
                    tokens.append(("item_id", child))
                elif key_text == "id" and isinstance(child, str) and child:
                    # Chat Completions assistant tool calls carry the logical
                    # Responses ``call_id`` under ``tool_calls[].id``.
                    if parent_key in {"tool_calls", "tool_call"}:
                        role = "call_id"
                    else:
                        role = "response_id" if parent_key == "response" else "item_id"
                    tokens.append((role, child))
                visit(child, key_text)
        elif isinstance(node, list):
            for child in node:
                visit(child, parent_key)

    visit(value)
    deduplicated: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for token in tokens:
        if token not in seen:
            seen.add(token)
            deduplicated.append(token)
    return tuple(deduplicated)


def _identity_observation(tokens: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """Retain only role plus per-identity digests, never opaque IDs."""

    token_digests = [
        {"role": role, "digest": _digest(value)}
        for role, value in tokens
    ]
    return {
        "tokens": token_digests,
        "identity_digest": _digest(token_digests),
    }


def _dedupe_identity_tokens(
    tokens: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    deduplicated: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for token in tokens:
        if token not in seen:
            seen.add(token)
            deduplicated.append(token)
    return tuple(deduplicated)


def _text_response_events(*, response_id: str) -> tuple[dict[str, Any], ...]:
    return (
        {"type": "response.created", "response": {"id": response_id, "status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "item": {"id": "fixture-message-item", "type": "message", "role": "assistant", "content": []},
        },
        {"type": "response.output_text.delta", "delta": "FIXTURE_COMPLETE"},
        {
            "type": "response.output_item.done",
            "item": {
                "id": "fixture-message-item",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "FIXTURE_COMPLETE"}],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "status": "completed",
                "output": [{"id": "fixture-message-item", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "FIXTURE_COMPLETE"}]}],
            },
        },
    )


def _contains_tool_search_declaration(value: Any) -> bool:
    """Detect the bounded declaration marker without retaining request data."""

    if isinstance(value, dict):
        if value.get("type") == "tool_search" and value.get("execution") == "client":
            return True
        if value.get("name") == "tool_search":
            return True
        name = value.get("name")
        if isinstance(name, str) and name.startswith("__codexhub_search_"):
            return True
        return any(_contains_tool_search_declaration(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_tool_search_declaration(item) for item in value)
    return False


def _search_events(
    *,
    adapted: bool,
    response_id: str,
    search_tool_name: str | None = None,
) -> tuple[dict[str, Any], ...]:
    if adapted:
        # The Gateway's function adapter wraps the canonical search call.  The
        # alias is request-scoped and intentionally opaque to this fixture.
        return _function_call_events(
            name=search_tool_name or "tool_search",
            arguments=json.dumps({"__codexhub_tool_search_input": {"query": "fixture search"}}, separators=(",", ":")),
            response_id=response_id,
            item_id="fixture-search-call-item",
            call_id="fixture-search-call",
        )
    return (
        {"type": "response.created", "response": {"id": response_id, "status": "in_progress"}},
        {
            "type": "response.output_item.done",
            "item": {
                "id": "fixture-search-item",
                "type": "tool_search_call",
                "call_id": "fixture-search-call",
                "execution": "client",
                "arguments": {"query": "fixture search"},
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "status": "completed",
                "output": [
                    {
                        "id": "fixture-search-item",
                        "type": "tool_search_call",
                        "call_id": "fixture-search-call",
                        "execution": "client",
                        "arguments": {"query": "fixture search"},
                    }
                ],
            },
        },
    )


def _discovered_call_events(
    *,
    response_id: str,
    name: str = DISCOVERED_TOOL_NAME,
    namespace: str | None = None,
) -> tuple[dict[str, Any], ...]:
    # The local MCP fixture owns this declaration.  Its result is intentionally
    # opaque; only the declaration/call/result counts enter the summary.
    return _function_call_events(
        name=name,
        arguments="{}",
        response_id=response_id,
        item_id="fixture-discovered-item",
        call_id="fixture-discovered-call",
        namespace=namespace,
    )


def _workflow_call_events(
    name: str,
    *,
    adapted: bool,
    response_id: str,
    item_id: str,
    call_id: str,
    wire_name: str | None = None,
) -> tuple[dict[str, Any], ...]:
    if name == "apply_patch":
        arguments = "*** Begin Patch\n*** Update File: fixture-target.txt\n@@\n-fixture-before\n+fixture-after\n*** End Patch"
    elif name == "shell_command":
        arguments = json.dumps({"command": "type fixture-target.txt"}, separators=(",", ":"))
    else:
        arguments = "{}"
    if name == "apply_patch" and adapted:
        arguments = json.dumps(
            {"__codexhub_custom_input": arguments},
            separators=(",", ":"),
        )
    if name == "apply_patch" and not adapted:
        return _custom_tool_call_events(
            name=name,
            input_value=arguments,
            response_id=response_id,
            item_id=item_id,
            call_id=call_id,
        )
    return _function_call_events(
        name=wire_name or name,
        arguments=arguments,
        response_id=response_id,
        item_id=item_id,
        call_id=call_id,
    )


def _custom_tool_call_events(
    *,
    name: str,
    input_value: str,
    response_id: str,
    item_id: str,
    call_id: str,
) -> tuple[dict[str, Any], ...]:
    in_progress_item = {
        "id": item_id,
        "type": "custom_tool_call",
        "status": "in_progress",
        "call_id": call_id,
        "name": name,
        "input": "",
    }
    completed_item = {
        "id": item_id,
        "type": "custom_tool_call",
        "status": "completed",
        "call_id": call_id,
        "name": name,
        "input": input_value,
    }
    return (
        {"type": "response.created", "response": {"id": response_id, "status": "in_progress"}},
        {"type": "response.output_item.added", "item": in_progress_item},
        {
            "type": "response.custom_tool_call_input.delta",
            "item_id": item_id,
            "call_id": call_id,
            "delta": input_value,
        },
        {
            "type": "response.custom_tool_call_input.done",
            "item_id": item_id,
            "call_id": call_id,
            "input": input_value,
        },
        {"type": "response.output_item.done", "item": completed_item},
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "status": "completed",
                "output": [completed_item],
            },
        },
    )


def _function_call_events(
    *,
    name: str,
    arguments: str,
    response_id: str,
    item_id: str,
    call_id: str,
    namespace: str | None = None,
) -> tuple[dict[str, Any], ...]:
    in_progress_item = {
        "id": item_id,
        "type": "function_call",
        "status": "in_progress",
        "call_id": call_id,
        "name": name,
        "arguments": "",
    }
    completed_item = {
        "id": item_id,
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }
    if namespace is not None:
        in_progress_item["namespace"] = namespace
        completed_item["namespace"] = namespace
    return (
        {"type": "response.created", "response": {"id": response_id, "status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "item": in_progress_item,
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": item_id,
            "call_id": call_id,
            "delta": arguments,
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": item_id,
            "call_id": call_id,
            "arguments": arguments,
        },
        {
            "type": "response.output_item.done",
            "item": completed_item,
        },
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "status": "completed",
                "output": [
                    {
                        **completed_item,
                    }
                ],
            },
        },
    )


def _failed_response_events(*, response_id: str, code: str) -> tuple[dict[str, Any], ...]:
    return (
        {"type": "response.created", "response": {"id": response_id, "status": "in_progress"}},
        {
            "type": "response.failed",
            "response": {
                "id": response_id,
                "status": "failed",
                "error": {"code": code, "message": "fixture protocol sequence failed"},
                "output": [],
            },
        },
    )


def _chat_events(events: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Encode one Responses-like call as ordinary Chat Completions chunks."""
    output = list(events)
    call = next(
        (
            item.get("item")
            for item in output
            if item.get("type") == "response.output_item.done"
            and isinstance(item.get("item"), dict)
            and item["item"].get("type")
            in {"function_call", "custom_tool_call", "tool_search_call"}
        ),
        None,
    )
    if not isinstance(call, dict):
        # A completed Responses message is ordinary assistant text.  Only a
        # tool output item is represented as a Chat ``tool_calls`` delta; if
        # the message is mistaken for a call the CLI submits one extra tool
        # result request and the protocol fixture reports a false failure.
        text = "FIXTURE_COMPLETE"
        return (
            {"id": "fixture-chat", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
            {"id": "fixture-chat", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        )
    name = str(call.get("name") or "fixture_discovered_tool")
    arguments = str(call.get("arguments") or "{}")
    call_id = call.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        call_id = "fixture-call"
    return (
        {
            "id": "fixture-chat",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}]}, "finish_reason": None}],
        },
        {"id": "fixture-chat", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    )


def _fixture_handler(state: FixtureState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > MAX_REQUEST_BODY_BYTES:
                    with state.lock:
                        state.protocol_error = "evidence_limit_exceeded"
                    self.send_response(413)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8"))
                index = state.record_request(self.path.split("?", 1)[0], body, self.headers.get("Authorization"))
                if self.path.split("?", 1)[0] == "/v1/chat/completions":
                    source_events = state.response_events(index)
                    state.record_sse_events(source_events)
                    payloads = _chat_events(source_events)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for payload in payloads:
                        self.wfile.write(_event_line(payload))
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                events = state.response_events(index)
                state.record_sse_events(events)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Connection", "close")
                self.end_headers()
                for event in events:
                    self.wfile.write(_event_line(event))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, RemoteDisconnected):
                return
            except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
                with state.lock:
                    state.malformed_requests += 1
                try:
                    self.send_response(400)
                    self.send_header("Connection", "close")
                    self.end_headers()
                except OSError:
                    return

    return Handler


def _safe_environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
    isolated_appdata = home / "AppData"
    isolated_roaming = isolated_appdata / "Roaming"
    isolated_local = isolated_appdata / "Local"
    for directory in (isolated_roaming, isolated_local):
        directory.mkdir(parents=True, exist_ok=True)
    # Keep platform-level Codex config discovery inside the fixture home even
    # when the host process has a custom CODEX_CONFIG or profile variables.
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    environment["APPDATA"] = str(isolated_roaming)
    environment["LOCALAPPDATA"] = str(isolated_local)
    environment["XDG_CONFIG_HOME"] = str(home / ".config")
    environment["XDG_DATA_HOME"] = str(home / ".local" / "share")
    environment["XDG_CACHE_HOME"] = str(home / ".cache")
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"
    environment["PYTHONPATH"] = str(REPO_ROOT / "src-python")
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    return environment


def _write_provider_files(
    home: Path,
    upstream_port: int,
    gateway_port: int,
    *,
    adapted: bool,
    model: str,
) -> None:
    provider_id = "volc" if adapted else "ollama-cloud"
    fixture_key = FIXTURE_KEY_BY_PROVIDER[provider_id]
    # Native cases use structured Responses; adapted cases exercise the
    # actual Chat Completions upstream transport through the Gateway's
    # request/response adapter.
    protocol = "chat_completions" if adapted else "responses"
    tool_protocol = "chat_tools" if adapted else "responses_structured"
    capability_line = (
        "tool_protocol_capabilities = { function_lifecycle = true, namespace_lifecycle = true, "
        "custom_lifecycle = true, tool_search_lifecycle = true }\n"
        if not adapted
        else ""
    )
    provider_dir = home / "proxy" / "config"
    provider_dir.mkdir(parents=True, exist_ok=True)
    provider_text = f'''[[providers]]
id = "{provider_id}"
name = "CodexHub issue 278 fixture"
base_url = "http://127.0.0.1:{upstream_port}/v1"
api_key = "{fixture_key}"
upstream_format = "{protocol}"
available_upstream_formats = ["{protocol}"]
tool_protocol = "{tool_protocol}"
{capability_line}tool_surface_strategy = "deferred_core"
native_responses_tool_codec = "none"
enabled = true

  [[providers.models]]
  id = "glm-5.2"
  display_name = "Issue 278 fixture"
  context_window = 128000
  max_output_tokens = 16384
  tool_surface_strategy = "deferred_core"
  enabled = true
'''
    (provider_dir / "providers.toml").write_text(provider_text, encoding="utf-8")
    catalog_dir = home / "model-catalogs"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    mcp_ledger = home / "mcp-ledger.json"
    catalog_model = {
        "slug": model,
        "id": model,
        "model": model,
        "display_name": "Issue 278 fixture",
        "description": "Protocol-controlled fixture model.",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 20,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "supported_reasoning_levels": [{"effort": "medium", "description": "Fixture"}],
        "default_reasoning_level": "medium",
        "shell_type": "shell_command",
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text_and_image",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "supports_parallel_tool_calls": False,
        "supports_reasoning_summaries": True,
        "default_reasoning_summary": "none",
        "support_verbosity": True,
        "default_verbosity": "low",
        "supports_image_detail_original": True,
        # This fixture explicitly qualifies the client-owned tool_search
        # lifecycle. Hosted search remains a separate tool family and is not
        # used as a substitute by the runner.
        "supports_search_tool": False,
        "base_instructions": "You are Codex, a coding agent.",
        "instructions_variables": {},
        "use_responses_lite": False,
        "input_modalities": ["text"],
        "context_window": 128000,
        "max_context_window": 128000,
        "max_output_tokens": 16384,
        "experimental_supported_tools": [],
        "codex_proxy_metadata": {
            "provider": provider_id,
            "upstream_name": "volcengine" if adapted else "ollama_cloud",
            "upstream_model": "glm-5.2",
            "upstream_format": protocol,
            "tool_protocol": tool_protocol,
        },
    }
    (catalog_dir / "codexhub-model-catalog.json").write_text(
        json.dumps({"fetched_at": "fixture", "client_version": "fixture", "models": [catalog_model]}, separators=(",", ":")),
        encoding="utf-8",
    )
    config_text = f'''model_provider = "custom"
model_catalog_json = "{(catalog_dir / "codexhub-model-catalog.json").as_posix()}"

[model_providers.custom]
name = "CodexHub issue 278 fixture"
base_url = "http://127.0.0.1:{gateway_port}/v1"
wire_api = "responses"
requires_openai_auth = true
experimental_bearer_token = "{fixture_key}"

[mcp_servers.fixture]
command = "{Path(sys.executable).as_posix()}"
args = ["{(REPO_ROOT / "scripts" / "issue_278_fixture_mcp.py").as_posix()}", "--ledger", "{mcp_ledger.as_posix()}"]
startup_timeout_sec = 10
tool_timeout_sec = 20
'''
    (home / "config.toml").write_text(config_text, encoding="utf-8")


def _write_mcp_fixture() -> Path:
    path = REPO_ROOT / "scripts" / "issue_278_fixture_mcp.py"
    if path.exists():
        return path
    raise RunnerFailure("fixture_mcp_missing")


def _start_gateway(home: Path, port: int, environment: dict[str, str]) -> subprocess.Popen[str]:
    command = [sys.executable, str(REPO_ROOT / "src-python" / "codex_proxy.py"), "--host", "127.0.0.1", "--port", str(port)]
    try:
        return subprocess.Popen(command, cwd=REPO_ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        raise RunnerFailure("gateway_start_failed") from error


def _wait_health(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(Request(f"http://127.0.0.1:{port}/health", method="GET"), timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.05)
    raise RunnerFailure("gateway_health_timeout")


def _stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _resolve_codex(explicit: Path | None) -> Path:
    if explicit is not None:
        candidate = _safe_path(explicit)
        if candidate.is_file():
            return candidate
        raise RunnerFailure("codex_cli_missing")
    found = shutil.which("codex.exe") or shutil.which("codex")
    if found:
        return _safe_path(Path(found))
    raise RunnerFailure("codex_cli_missing")


def _cli_command(codex: Path, args: list[str]) -> list[str]:
    """Build a process command without leaving a Windows npm shim parent.

    The npm-generated ``codex.cmd`` wrapper starts Node through ``cmd.exe``.
    On Windows the wrapper can exit while the Node child still owns the
    inherited stdout handle, so waiting on the wrapper leaves the bounded
    reader alive and produces a false ``cli_output_limit`` failure.  When the
    standard npm package layout is present, invoke the package entrypoint
    directly with Node.  Other executable paths retain their original
    invocation semantics.
    """

    if os.name == "nt" and codex.suffix.lower() == ".cmd":
        script = codex.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = shutil.which("node.exe") or shutil.which("node")
        if script.is_file() and node:
            return [str(_safe_path(Path(node))), str(_safe_path(script)), *args]
    return [str(codex), *args]


def _codex_version(codex: Path) -> str:
    try:
        result = subprocess.run(
            _cli_command(codex, ["--version"]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RunnerFailure("codex_cli_version_unavailable") from error
    if result.returncode != 0:
        raise RunnerFailure("codex_cli_version_unavailable")
    line = ((result.stdout or result.stderr)[:4096]).strip().splitlines()
    return line[0][:80] if line else "unknown"


def _prompt(case_id: str) -> str:
    if case_id in NO_HINT_CASES:
        return "The client-owned tool_search is available but not needed for this request. Do not select it or call any tool. Reply exactly FIXTURE_NO_HINT."
    return (
        "Use the available client tool_search exactly once to find the fixture discovered tool, "
        "then call it exactly once. Next use shell_command to read fixture-target.txt, apply_patch "
        "exactly once to replace fixture-before with fixture-after, and shell_command exactly once "
        "to verify. Stop after the verification result and reply FIXTURE_COMPLETE."
    )


def _run_cli_bounded(
    command: list[str],
    *,
    prompt: str,
    timeout: float,
    environment: dict[str, str],
    workspace: Path,
) -> tuple[int, bytes]:
    """Run the CLI with a bounded, ephemeral stdout spool.

    A pipe reader is attractive because it bounds memory, but on Windows a
    CLI can legitimately leave an inherited pipe handle open in a child that
    has already finished its observable work.  The parent has then exited but
    the reader cannot see EOF, producing a false output-limit failure.  A
    temporary file keeps the same memory bound (we only read at most the
    configured limit), does not retain evidence after this function returns,
    and lets ``communicate`` own stdin/process shutdown deterministically.
    """

    try:
        with tempfile.TemporaryFile() as stdout:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=subprocess.DEVNULL,
                text=False,
                env=environment,
                cwd=workspace,
            )
            try:
                process.communicate(input=prompt.encode("utf-8"), timeout=timeout)
            except subprocess.TimeoutExpired as error:
                try:
                    process.kill()
                    process.communicate(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise RunnerFailure("cli_timeout") from error
            stdout.flush()
            output_size = stdout.tell()
            if output_size > MAX_CLI_STDOUT_BYTES:
                raise RunnerFailure("cli_output_limit")
            stdout.seek(0)
            return process.returncode, stdout.read(MAX_CLI_STDOUT_BYTES)
    except RunnerFailure:
        raise
    except OSError as error:
        raise RunnerFailure("cli_start_failed") from error


def _run_cli(
    codex: Path,
    case_id: str,
    model: str,
    home: Path,
    workspace: Path,
    timeout: float,
    environment: dict[str, str],
) -> tuple[int, list[dict[str, Any]], str]:
    command = _cli_command(codex, [
        "exec",
        "--ephemeral",
        "--json",
        "--skip-git-repo-check",
        "--strict-config",
        # The qualification workspace is an isolated temporary directory;
        # bypass the host session's read-only policy so the required
        # apply_patch mutation can be observed by the runner.
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(workspace),
        "-m",
        model,
        "-s",
        "workspace-write",
        "-c",
        "approval_policy=never",
        "-c",
        "features.apps=false",
        "-",
    ])
    returncode, raw_stdout = _run_cli_bounded(
        command,
        prompt=_prompt(case_id),
        timeout=timeout,
        environment=environment,
        workspace=workspace,
    )
    events: list[dict[str, Any]] = []
    for line in raw_stdout.decode("utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            event_type = value.get("type")
            if not isinstance(event_type, str) or not event_type:
                continue
            event_identity_tokens = _identity_tokens(value)
            # Keep only bounded shape markers and opaque identity digests.
            item = value.get("item") if isinstance(value.get("item"), dict) else {}
            item_type = item.get("type") if isinstance(item, dict) else None
            if not isinstance(item_type, str):
                item_type = None
            events.append(
                {
                    "type": event_type,
                    "item_type": item_type,
                    "identity_digest": _digest(list(event_identity_tokens)),
                }
            )
    bounded_events = events[:MAX_CLI_EVENTS]
    # Keep the CLI identity binding content-free: each event carries only a
    # per-event identity digest, and the case summary binds the ordered ledger
    # of those digests.  This lets validation recompute the value without
    # retaining real response/item/call identifiers.
    cli_identity_digest = _digest(
        [event["identity_digest"] for event in bounded_events]
    )
    return returncode, bounded_events, cli_identity_digest


def _provenance_summary(state: FixtureState) -> dict[str, Any]:
    """Return shape-only lifecycle evidence derived from the fixture ledger."""

    trace = list(state.trace_tokens)
    search_trace = [
        token
        for token in trace
        if token.startswith("tool_search.") or token.startswith("discovered.")
    ]
    code_trace = [token for token in trace if token.startswith("code_mode.")]
    code_steps = [token[len("code_mode.") : -len(".call")] for token in code_trace if token.endswith(".call")]
    identity_evidence = {
        "response_identity_digests": list(state.response_identity_digests),
        "request_identity_digests": list(state.request_identity_digests),
        "response_identity_observations": list(state.response_identity_observations),
        "request_identity_observations": list(state.request_identity_observations),
        "identity_binding_count": state.identity_binding_count,
        "search_identity_binding_count": state.search_identity_binding_count,
        "identity_mismatch_count": state.identity_mismatch_count,
        "request_identity_mismatch_count": state.request_identity_mismatch_count,
    }
    history_order_digest = _digest(
        {
            "trace": trace,
            "protocols": list(state.protocols),
            "response_shapes": list(state.response_shape_tokens),
        }
    )
    search = {
        "ordered_stages": search_trace,
        "stage_count": len(search_trace),
        "call_count": search_trace.count(TRACE_SEARCH_CALL),
        "result_count": search_trace.count(TRACE_SEARCH_RESULT),
        "discovered_declaration_count": search_trace.count(TRACE_DISCOVERED_DECLARATION),
        "subsequent_call_count": search_trace.count(TRACE_DISCOVERED_CALL),
        "subsequent_result_count": search_trace.count(TRACE_DISCOVERED_RESULT),
    }
    search["order_digest"] = _digest(search_trace)
    code_mode = {
        "ordered_steps": code_steps,
        "call_count": sum(token.endswith(".call") for token in code_trace),
        "result_count": sum(token.endswith(".result") for token in code_trace),
    }
    code_mode["order_digest"] = _digest(code_trace)
    history = {
        "request_count": state.requests,
        "response_count": state.response_count,
        "protocol_sequence": list(state.protocols),
        "response_shapes": list(state.response_shape_tokens),
        "order_digest": history_order_digest,
        "identity_digest": _digest(identity_evidence),
        "identity": identity_evidence,
        "diagnostics": {
            "response_stage": state.response_stage,
            "awaiting_input": state.awaiting_input,
            "pending_call_id_digest": (
                _digest([state.pending_call_id])
                if isinstance(state.pending_call_id, str) and state.pending_call_id
                else None
            ),
            "protocol_error": state.protocol_error,
        },
    }
    return {
        "trace": trace,
        "trace_digest": _digest(trace),
        "search": search,
        "code_mode": code_mode,
        "history": history,
    }


def _read_mcp_ledger(path: Path) -> dict[str, int]:
    """Read only bounded MCP lifecycle counters from the isolated fixture."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    output: dict[str, int] = {}
    for key in ("tools_list_count", "tools_call_count"):
        raw = value.get(key)
        if isinstance(raw, int) and 0 <= raw <= MAX_MCP_COUNT:
            output[key] = raw
        elif raw is not None:
            return {}
    return output


def _route_observation(
    state: FixtureState,
    gateway: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return only observed, bounded route-binding evidence for one case."""

    material = {
        "paths": list(state.route_paths),
        "model_digests": list(state.route_model_digests),
        "provider_digests": list(state.route_provider_digests),
        "protocols": list(state.route_protocol_sequence),
    }
    upstream_identity_digest = _digest(material)
    combined_material: dict[str, Any] = {"upstream": upstream_identity_digest}
    if gateway is not None:
        combined_material["gateway"] = gateway.get("identity_digest")
    return {
        "request_count": len(state.route_observation_digests),
        "paths": list(state.route_paths),
        "model_digests": list(state.route_model_digests),
        "provider_digests": list(state.route_provider_digests),
        "protocols": list(state.route_protocol_sequence),
        "observation_digests": list(state.route_observation_digests),
        "upstream_identity_digest": upstream_identity_digest,
        "gateway": dict(gateway) if gateway is not None else None,
        "identity_digest": _digest(combined_material),
        "cross_provider_requests": state.cross_provider_requests,
    }


def _read_gateway_route_events(home: Path) -> list[dict[str, str]]:
    """Read only sanitized route fields from Gateway completion events.

    The upstream fixture proves what reached the endpoint; the Gateway event
    proves the client-facing provider/model binding that produced it.  Keep
    only string route fields and discard the rest of the event before any
    evidence digest is built.
    """

    path = home / "proxy" / "codex-proxy-events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    observations: list[dict[str, str]] = []
    for line in lines[-MAX_GATEWAY_ROUTE_OBSERVATIONS * 4 :]:
        try:
            event = json.loads(line)
        except (TypeError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("event") != "request_complete":
            continue
        fields = {
            key: event.get(key)
            for key in (
                "route_provider_id",
                "route_model_requested",
                "route_model_canonical",
                "route_upstream_model",
                "route_endpoint_url",
            )
        }
        if all(isinstance(value, str) and value for value in fields.values()):
            observations.append({key: str(value) for key, value in fields.items()})
        if len(observations) >= MAX_GATEWAY_ROUTE_OBSERVATIONS:
            break
    return observations


def _gateway_route_observation(
    observations: Iterable[Mapping[str, str]],
    *,
    case_id: str,
) -> dict[str, Any]:
    """Bind selected route fields to bounded digests after local checks."""

    expected_provider = PROVIDER_BY_CASE[case_id]
    expected_model = MODEL_BY_CASE[case_id]
    expected_upstream_model = UPSTREAM_MODEL_BY_CASE[case_id]
    expected_path = "/v1/chat/completions" if case_id.startswith("adapted_") else "/v1/responses"
    records: list[dict[str, str]] = []
    for observation in observations:
        endpoint = observation["route_endpoint_url"]
        try:
            parsed_endpoint = urlsplit(endpoint)
            endpoint_hostname = parsed_endpoint.hostname
            endpoint_port = parsed_endpoint.port
        except ValueError as error:
            raise RunnerFailure("gateway_route_endpoint_unexpected") from error
        if (
            parsed_endpoint.scheme != "http"
            or endpoint_hostname != "127.0.0.1"
            or endpoint_port is None
            or not (1 <= endpoint_port <= 65535)
            or parsed_endpoint.username is not None
            or parsed_endpoint.password is not None
            or parsed_endpoint.query
            or parsed_endpoint.fragment
            or parsed_endpoint.path != expected_path
        ):
            raise RunnerFailure("gateway_route_endpoint_unexpected")
        if (
            observation["route_provider_id"] != expected_provider
            or observation["route_model_requested"] != expected_model
            or observation["route_model_canonical"] != expected_model
            or observation["route_upstream_model"] != expected_upstream_model
        ):
            raise RunnerFailure("gateway_route_identity_mismatch")
        records.append(
            {
                "provider_digest": _digest(observation["route_provider_id"]),
                "model_requested_digest": _digest(observation["route_model_requested"]),
                "model_canonical_digest": _digest(observation["route_model_canonical"]),
                "upstream_model_digest": _digest(observation["route_upstream_model"]),
                # The ephemeral Gateway port is intentionally omitted from
                # the retained digest.  Host, scheme, and path are validated
                # above and the stable material is independently recomputable
                # by the evidence validator.
                "endpoint_digest": _digest(
                    {
                        "scheme": parsed_endpoint.scheme,
                        "hostname": endpoint_hostname,
                        "path": parsed_endpoint.path,
                    }
                ),
            }
        )
    if not records:
        raise RunnerFailure("gateway_route_observation_missing")
    material = {"observations": records}
    return {
        "request_count": len(records),
        "observations": records,
        "identity_digest": _digest(material),
        "source": "gateway_request_complete",
    }


def _negative_control_statuses(*, candidate_sha: str | None = None) -> dict[str, str]:
    """Run and inspect the shared adapter negative-control replay.

    A validator exit code alone is not evidence that each named control was
    exercised: the replay reports one bounded classification per control.  Do
    not collapse an unreadable or incomplete report into three successful
    statuses.
    """

    validator = REPO_ROOT / "tests" / "validate_issue_251_evidence.py"
    expected = {
        "unknown_alias": "unknown_alias",
        "duplicate_identity": "invalid_custom_stream_identity",
        "malformed_envelope": "invalid_envelope",
    }
    statuses = {name: "not_verified" for name in expected}
    try:
        command = [sys.executable, str(validator)]
        if candidate_sha is not None:
            command.extend(["--candidate-sha", candidate_sha])
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return statuses
    if result.returncode != 0:
        return statuses
    try:
        report = json.loads(result.stdout)
    except (TypeError, UnicodeError, json.JSONDecodeError):
        return statuses
    observed: dict[str, str] = {}
    if isinstance(report, dict) and report.get("status") == "pass":
        raw_cases = report.get("cases", {})
        if isinstance(raw_cases, dict):
            cases = raw_cases.values()
        elif isinstance(raw_cases, list):
            cases = raw_cases
        else:
            cases = ()
        for case in cases:
            if not isinstance(case, dict):
                continue
            controls = case.get("controls")
            if not isinstance(controls, dict):
                continue
            for name in expected:
                value = controls.get(name)
                if isinstance(value, str):
                    observed[name] = value
    for name, expected_classification in expected.items():
        if observed.get(name) == expected_classification:
            statuses[name] = "passed"
    return statuses


def _run_case(codex: Path, case_id: str, timeout: float) -> dict[str, Any]:
    state = FixtureState(case_id)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _fixture_handler(state))
    upstream.daemon_threads = True
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    home = Path(tempfile.mkdtemp(prefix="codexhub-issue278-"))
    gateway: subprocess.Popen[str] | None = None
    try:
        environment = _safe_environment(home)
        model = MODEL_BY_CASE[case_id]
        _write_mcp_fixture()
        gateway_port = _free_port()
        _write_provider_files(
            home,
            upstream.server_port,
            gateway_port,
            adapted=case_id.startswith("adapted_"),
            model=model,
        )
        target = home / "workspace" / "fixture-target.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture-before\n", encoding="utf-8")
        mcp_ledger = home / "mcp-ledger.json"
        environment["ISSUE_278_MCP_LEDGER"] = str(mcp_ledger)
        workspace = target.parent
        gateway = _start_gateway(home, gateway_port, environment)
        _wait_health(gateway_port, min(timeout, 30))
        # The custom provider points to the local Gateway.  The client config
        # above uses the upstream port only for a bounded local base URL; the
        # Gateway itself resolves the model from its isolated runtime config.
        code, client_events, cli_identity_digest = _run_cli(
            codex,
            case_id,
            model,
            home,
            workspace,
            timeout,
            environment,
        )
        # The request has completed before the CLI exits.  Retire the
        # isolated Gateway so its bounded event writer flushes, then bind the
        # selected route from the actual completion event instead of a
        # hard-coded case label.
        _stop_process(gateway)
        gateway = None
        gateway_route = _gateway_route_observation(
            _read_gateway_route_events(home),
            case_id=case_id,
        )
        with state.lock:
            explicit = state.explicit
            selected = state.search_seen if explicit else not state.search_seen
            workflow = list(state.workflow_seen)
            cli_terminal_event = any(event.get("type") == "turn.completed" for event in client_events)
            passed = (
                code == 0
                and bool(client_events)
                and cli_terminal_event
                and state.auth_failures == 0
                and state.unexpected_paths == 0
                and state.malformed_requests == 0
                and (state.requests >= 1)
                and ((not explicit and not state.search_seen) or (explicit and state.search_seen and state.discovered_seen))
            )
            if explicit:
                passed = passed and workflow == list(WORKFLOW_TOOL_NAMES)
            provenance = _provenance_summary(state)
            expected_trace = EXPECTED_EXPLICIT_TRACE if explicit else EXPECTED_NO_HINT_TRACE
            passed = passed and provenance["trace"] == list(expected_trace)
            passed = passed and provenance["history"]["response_count"] == (6 if explicit else 1)
            passed = passed and state.identity_mismatch_count == 0
            passed = passed and state.request_identity_mismatch_count == 0
            passed = passed and len(state.response_identity_digests) == (6 if explicit else 1)
            if explicit:
                # Every tool response must be followed by a request that
                # carries the observed call link.  The count is derived from
                # the fixture ledger, never from fixture IDs or ordinals.
                passed = passed and state.identity_binding_count == 5
            mcp_ledger_state = _read_mcp_ledger(mcp_ledger)
            route_observation = _route_observation(state, gateway_route)
            target_content = target.read_text(encoding="utf-8") if target.exists() else None
            mcp_call_count = mcp_ledger_state.get("tools_call_count", 0)
            mcp_list_count = mcp_ledger_state.get("tools_list_count", 0)
            workspace_mutation_verified = target_content == (
                "fixture-after\n" if explicit else "fixture-before\n"
            )
            if explicit:
                passed = passed and mcp_list_count >= 1 and mcp_call_count == 1 and workspace_mutation_verified
            else:
                passed = passed and mcp_call_count == 0 and workspace_mutation_verified
            passed = passed and route_observation["request_count"] == state.requests
            passed = passed and gateway_route["request_count"] == state.requests
            passed = passed and route_observation["cross_provider_requests"] == 0
            passed = passed and all(
                isinstance(value, str) and value.startswith("sha256:")
                for value in route_observation["observation_digests"]
            )
            result = {
                "id": case_id,
                "disposition": DISPOSITION_BY_CASE[case_id],
                "protocol": PROTOCOL_BY_CASE[case_id],
                "planner_eligible": bool(state.client_tool_surface_observed),
                "tool_search_visible": bool(state.tool_search_visible),
                "planner_eligibility_source": "client_tool_surface_observed",
                "selection": "selected" if explicit and state.search_seen else "model_not_selected",
                "classification": "completed" if passed and explicit else ("model_not_selected" if passed else "cli_or_gateway_failure"),
                "sse_event_types": list(dict.fromkeys(state.sse_event_types)),
                "history_order_digest": provenance["history"]["order_digest"],
                "identity_preserved": (
                    state.identity_mismatch_count == 0
                    and state.request_identity_mismatch_count == 0
                    and len(state.response_identity_digests) == (6 if explicit else 1)
                    and (not explicit or state.identity_binding_count == 5)
                ),
                "identity_observation": {
                    "response_stream": "observed",
                    "request_history": "observed",
                },
                "route_observation": route_observation,
                "cli_identity_digest": cli_identity_digest,
                "cli_event_shapes": client_events,
                "cli_event_ledger_digest": _digest(client_events),
                "gateway_owned_tool_execution_count": 0,
                "cli_event_shape_count": len(client_events),
                "cli_terminal_event": "turn.completed" if cli_terminal_event else None,
                "upstream_request_count": state.requests,
                "mcp_tools_list_count": mcp_list_count,
                "mcp_tools_call_count": mcp_call_count,
                "workspace_mutation_verified": workspace_mutation_verified,
                "route_identity_digest": route_observation["identity_digest"],
                "provenance": provenance,
            }
            if not explicit:
                result["classification"] = "model_not_selected" if passed else "cli_or_gateway_failure"
            if not passed:
                result["failure"] = "cli_or_gateway_failure"
            return result
    except RunnerFailure:
        raise
    except Exception as error:
        raise RunnerFailure("case_harness_failed") from error
    finally:
        _stop_process(gateway)
        try:
            upstream.shutdown()
            upstream.server_close()
        except OSError:
            pass
        upstream_thread.join(timeout=2)
        try:
            shutil.rmtree(home)
        except OSError:
            pass


def _free_port() -> int:
    probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = probe.server_port
    probe.server_close()
    return int(port)


def _summary(
    candidate_sha: str,
    cli_version: str,
    *,
    cases: list[dict[str, Any]],
    status: str,
    failure: str | None = None,
    negative_controls: dict[str, str] | None = None,
) -> dict[str, Any]:
    if negative_controls is None:
        negative_controls = _negative_control_statuses(
            candidate_sha=candidate_sha if _valid_candidate_sha(candidate_sha) else None,
        )
    route_identity_digests = [
        case["route_identity_digest"]
        for case in cases
        if isinstance(case.get("route_identity_digest"), str)
    ]
    output: dict[str, Any] = {
        "schema": SCHEMA,
        "evidence_status": "observed_synthetic_upstream",
        "qualification_status": status,
        "candidate_sha": candidate_sha if _valid_candidate_sha(candidate_sha) else None,
        "cli_version": cli_version,
        "route": {
            "selected_model": "opaque-selected-model",
            "selected_provider": "opaque-selected-provider",
            "cross_provider_requests": sum(
                int(case.get("route_observation", {}).get("cross_provider_requests", 0))
                for case in cases
                if isinstance(case.get("route_observation"), dict)
            ),
            "hosted_search_substitution": False,
            "identity_observation": "observed_request_route",
            "identity_digests": route_identity_digests,
        },
        "cases": cases,
        "negative_controls": negative_controls,
        "sanitization": {"raw_bodies_retained": False, "prompts_retained": False, "credentials_retained": False, "ids_opaque_or_hashed": True},
    }
    if failure:
        output["failure"] = failure
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--candidate-sha", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", default=",".join(DEFAULT_CASES), help="Comma-separated case IDs")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    output = _safe_path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    try:
        if args.candidate_sha and not _valid_candidate_sha(args.candidate_sha):
            raise RunnerFailure("candidate_sha_invalid")
        selected = tuple(_safe_case(value.strip()) for value in args.case.split(",") if value.strip())
        if not selected:
            raise RunnerFailure("case_empty")
        codex = _resolve_codex(args.codex)
        version = _codex_version(codex)
        for case_id in selected:
            cases.append(_run_case(codex, case_id, max(1.0, float(args.timeout_seconds))))
        status = "passed" if all(case.get("classification") in {"completed", "model_not_selected"} for case in cases) else "failed"
        # Run the offline controls once and use the same snapshot for both
        # the release status decision and the persisted evidence.  Replaying
        # them independently could produce contradictory status/evidence if
        # the local runtime changes between invocations.
        controls = _negative_control_statuses(candidate_sha=args.candidate_sha or None)
        if status == "passed" and set(controls.values()) != {"passed"}:
            status = "failed"
        summary = _summary(
            args.candidate_sha,
            version,
            cases=cases,
            status=status,
            failure=("negative_controls_unverified" if status == "failed" and set(controls.values()) != {"passed"} else None),
            negative_controls=controls,
        )
        exit_code = 0 if status == "passed" else 1
    except RunnerFailure as error:
        summary = _summary(args.candidate_sha, "unknown", cases=cases, status="not_run", failure=str(error))
        exit_code = 2
    except (OSError, ValueError):
        summary = _summary(args.candidate_sha, "unknown", cases=cases, status="not_run", failure="runner_input_invalid")
        exit_code = 2
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"schema": SCHEMA, "qualification_status": summary["qualification_status"], "case_count": len(cases)}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
