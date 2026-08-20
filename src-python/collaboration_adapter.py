"""Collaboration protocol boundary and Worker contract adapter.

This module owns the V1/V2 Collaboration boundary and the external Worker
selector, HMAC sidecar, stream ledger, and history-binding contract. It is
deliberately independent of `codex_proxy`; the facade supplies telemetry emit
and signing-root callbacks through a typed `CollaborationAdapter`.

Pure payload classification stays in `codex_semantic_adapter`. V2 identity and
alias handling stay in `runtime_tool_compatibility`. Tool schema injection,
bounded tool_search, structured input rewriting, and third-party tool-surface
adaptation stay in `tool_surface_adapter`. Generic JSON and workflow-state
helpers stay at the facade.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, NoReturn

from codex_semantic_adapter import (
    BINDING_ACCEPTED,
    COLLABORATION_V1,
    COLLABORATION_V2,
    COLLABORATION_V1_ALIAS_PREFIXES,
    COLLABORATION_V1_NAMESPACE,
    COLLABORATION_V1_TOOL_NAMES,
    CollaborationBoundaryError,
    classify_collaboration_payload,
    collaboration_protocols,
    dump_arguments_like,
    json_object_from_arguments,
    strict_json_object,
    synthesize_effective_worker_binding_readback,
    validate_effective_worker_binding,
    validate_requested_worker_binding,
    validate_worker_selector,
)
from gateway_errors import UpstreamProtocolTranslationError
from route_primitives import WORKER_REQUESTED_BINDING_FIELD
from protocol_translation import UnsupportedProtocolTranslationError
import worker_binding_signing


COLLABORATION_BOUNDARY_ERROR_CODE = "invalid_collaboration_boundary"
WORKER_SELECTOR_ERROR_CODE = "external_worker_selector_rejected"
WORKER_BINDING_ERROR_CODE = "external_worker_binding_rejected"
WORKER_REQUESTED_BINDING_VERSION = "codexhub.requested-worker-binding.v1"
WORKER_REQUESTED_BINDING_FIELDS = {
    "contract_version",
    "agent_type",
    "model",
    "reasoning",
    "signature",
}
LEGACY_NATIVE_WORKER_SPAWN_FIELDS = {
    "type",
    "id",
    "call_id",
    "namespace",
    "name",
    "arguments",
}
LEGACY_NATIVE_WORKER_SPAWN_METADATA_FIELD = "internal_chat_message_metadata_passthrough"
SPAWN_AGENT_TOOL = "spawn_agent"
WORKER_STREAM_BINDING_STATE_FIELD = "_worker_stream_binding_state"

_V1_ALIAS_PREFIXES = COLLABORATION_V1_ALIAS_PREFIXES


class EventEmitter(Protocol):
    def __call__(self, event: str, **fields: Any) -> None: ...


class BindingSigner(Protocol):
    def sign(self, payload: bytes) -> str: ...

    def verify(self, payload: bytes, signature: str) -> bool: ...


@dataclass(frozen=True)
class CollaborationFacts:
    """Immutable Worker/Collaboration contract constants for one adapter."""

    signing_root: Path
    collaboration_v1: str = COLLABORATION_V1
    collaboration_v2: str = COLLABORATION_V2
    boundary_error_code: str = COLLABORATION_BOUNDARY_ERROR_CODE
    worker_selector_error_code: str = WORKER_SELECTOR_ERROR_CODE
    worker_binding_error_code: str = WORKER_BINDING_ERROR_CODE
    requested_binding_field: str = WORKER_REQUESTED_BINDING_FIELD
    requested_binding_version: str = WORKER_REQUESTED_BINDING_VERSION
    requested_binding_fields: frozenset[str] = frozenset({
        "contract_version",
        "agent_type",
        "model",
        "reasoning",
        "signature",
    })
    legacy_native_spawn_metadata_field: str = LEGACY_NATIVE_WORKER_SPAWN_METADATA_FIELD
    binding_accepted: str = BINDING_ACCEPTED
    spawn_agent_tool: str = SPAWN_AGENT_TOOL
    stream_binding_state_field: str = WORKER_STREAM_BINDING_STATE_FIELD


@dataclass(frozen=True)
class PathBindingSigner:
    """HMAC signer bound to one Worker-binding secret root."""

    root: Path

    def sign(self, payload: bytes) -> str:
        return worker_binding_signing.sign(self.root, payload)

    def verify(self, payload: bytes, signature: str) -> bool:
        return worker_binding_signing.verify(self.root, payload, signature)


def _v1_tool_name_from_wire(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    if name in COLLABORATION_V1_TOOL_NAMES:
        return name
    for prefix in _V1_ALIAS_PREFIXES:
        if name.startswith(prefix):
            suffix = name[len(prefix) :]
            if suffix in COLLABORATION_V1_TOOL_NAMES:
                return suffix
    return None


def collaboration_v1_function_call_name(item: Mapping[str, Any]) -> str | None:
    """Return the V1 tool name for a namespaced or aliased function call."""
    if item.get("type") != "function_call":
        return None
    namespace = item.get("namespace")
    name = item.get("name")
    tool_name = _v1_tool_name_from_wire(name)
    if namespace == COLLABORATION_V1_NAMESPACE and tool_name is not None:
        return tool_name
    if tool_name is not None and name != tool_name:
        return tool_name
    return None


@dataclass(frozen=True)
class CollaborationAdapter:
    """Typed Collaboration boundary + Worker contract seam."""

    facts: CollaborationFacts
    emit: EventEmitter
    signer: BindingSigner

    def raise_boundary_error(
        self,
        event_context: Mapping[str, Any] | None,
        *,
        classification: str,
        message: str,
        surface: str = "request",
        cause: BaseException | None = None,
    ) -> NoReturn:
        # The normalized boundary error code is the public classification;
        # the raw event context is intentionally not emitted.
        _ = event_context, classification
        self.emit(
            "collaboration_boundary_rejected",
            surface=surface,
            outcome="rejected",
            count=1,
        )
        error = UpstreamProtocolTranslationError(
            UnsupportedProtocolTranslationError(
                self.facts.boundary_error_code,
                message,
            )
        )
        if cause is not None:
            raise error from cause
        raise error

    def resolve_boundary(
        self,
        payload: Any,
        event_context: Mapping[str, Any] | None,
        *,
        surface: str = "request",
    ) -> str | None:
        if surface != "request":
            try:
                protocol = classify_collaboration_payload(payload)
            except CollaborationBoundaryError as exc:
                self.raise_boundary_error(
                    event_context,
                    classification=exc.classification,
                    message="Collaboration protocol boundary is malformed or ambiguous.",
                    surface=surface,
                    cause=exc,
                )
            context_protocol = (
                event_context.get("collaboration_protocol")
                if isinstance(event_context, Mapping)
                else None
            )
            if context_protocol is not None and context_protocol not in {
                self.facts.collaboration_v1,
                self.facts.collaboration_v2,
            }:
                self.raise_boundary_error(
                    event_context,
                    classification="unknown_state",
                    message="Collaboration protocol selection is unknown.",
                    surface=surface,
                )
            if (
                context_protocol is not None
                and protocol is not None
                and protocol != context_protocol
            ):
                self.raise_boundary_error(
                    event_context,
                    classification="conflicting_selection",
                    message="Collaboration protocol selection conflicts with the response.",
                    surface=surface,
                )
        else:
            try:
                request_boundary = {
                    "tools": payload.get("tools", []),
                    "tool_choice": payload.get("tool_choice"),
                } if isinstance(payload, Mapping) else {"tools": [], "tool_choice": None}
                if isinstance(payload, Mapping):
                    for key in ("multi_agent_version", "metadata", "features", "client_metadata"):
                        if key in payload:
                            request_boundary[key] = payload[key]
                current_protocol = classify_collaboration_payload(request_boundary)

                raw_context_protocol = (
                    event_context.get("collaboration_protocol")
                    if isinstance(event_context, Mapping)
                    else None
                )
                context_protocol = raw_context_protocol if raw_context_protocol in {
                    self.facts.collaboration_v1,
                    self.facts.collaboration_v2,
                } else None
                history_boundary = (
                    {"input": payload.get("input", [])}
                    if isinstance(payload, Mapping)
                    else {"input": []}
                )
                if isinstance(payload, Mapping):
                    for key in (
                        "tools",
                        "tool_choice",
                        "multi_agent_version",
                        "metadata",
                        "features",
                        "client_metadata",
                    ):
                        if key in payload:
                            history_boundary[key] = payload[key]
                history_protocols = collaboration_protocols(history_boundary)
                protocol = (
                    current_protocol
                    or context_protocol
                )
                if (
                    current_protocol is not None
                    and context_protocol is not None
                    and current_protocol != context_protocol
                ):
                    self.raise_boundary_error(
                        event_context,
                        classification="conflicting_selection",
                        message="Collaboration protocol selection conflicts with the request.",
                        surface=surface,
                    )
                if len(history_protocols) > 1:
                    self.raise_boundary_error(
                        event_context,
                        classification="mixed_v1_v2",
                        message="Collaboration history contains multiple protocol families.",
                        surface=surface,
                    )
                history_protocol = next(iter(history_protocols), None)
                if (
                    protocol is not None
                    and history_protocol is not None
                    and protocol != history_protocol
                ):
                    self.raise_boundary_error(
                        event_context,
                        classification="conflicting_selection",
                        message="Collaboration protocol selection conflicts with history.",
                        surface=surface,
                    )
                if (
                    raw_context_protocol is not None
                    and context_protocol is None
                    and protocol is None
                ):
                    self.raise_boundary_error(
                        event_context,
                        classification="unknown_state",
                        message="Collaboration protocol selection is unknown.",
                        surface=surface,
                    )
            except CollaborationBoundaryError as exc:
                self.raise_boundary_error(
                    event_context,
                    classification=exc.classification,
                    message="Collaboration protocol boundary is malformed or ambiguous.",
                    surface=surface,
                    cause=exc,
                )

            if protocol is None:
                protocol = history_protocol

        if isinstance(event_context, dict) and protocol is not None:
            event_context["collaboration_protocol"] = protocol
        return protocol

    def is_v2_context(self, event_context: Mapping[str, Any] | None) -> bool:
        return (event_context or {}).get("collaboration_protocol") == self.facts.collaboration_v2

    def context_with_protocol(
        self,
        event_context: Mapping[str, Any] | None,
        protocol: str | None,
    ) -> Mapping[str, Any] | None:
        if protocol is None or isinstance(event_context, dict):
            return event_context
        context = dict(event_context or {})
        context["collaboration_protocol"] = protocol
        return context

    def raise_worker_contract_error(
        self,
        *,
        event: str,
        error_code: str,
        classification: str,
        surface: str | None = None,
    ) -> None:
        fields = {
            "outcome": "rejected",
            "classification": classification,
        }
        if surface is not None:
            fields["surface"] = surface
        self.emit(event, **fields)
        raise UpstreamProtocolTranslationError(
            UnsupportedProtocolTranslationError(
                error_code,
                "External Worker delegation contract validation failed.",
            )
        )

    def worker_caller_carrier_supported(
        self,
        event_context: Mapping[str, Any] | None,
    ) -> bool:
        context = event_context or {}
        caller_format = context.get("_caller_wire_format", context.get("inbound_format", "responses"))
        return caller_format != "chat_completions"

    def validate_external_worker_selectors(
        self,
        value: Any,
        event_context: Mapping[str, Any] | None,
        *,
        surface: str,
    ) -> None:
        if isinstance(value, list):
            for item in value:
                self.validate_external_worker_selectors(item, event_context, surface=surface)
            return
        if not isinstance(value, Mapping):
            return

        if collaboration_v1_function_call_name(value) == self.facts.spawn_agent_tool:
            raw_arguments = value.get("arguments")
            arguments = json_object_from_arguments(raw_arguments)
            if arguments is not None and raw_arguments not in (None, ""):
                agent_type = arguments.get("agent_type")
                if agent_type in {"general", "default"}:
                    pass
                elif agent_type == "worker":
                    if not self.worker_caller_carrier_supported(event_context):
                        self.raise_worker_contract_error(
                            event="worker_selector_validated",
                            error_code=self.facts.worker_selector_error_code,
                            classification="unsupported_caller_carrier",
                            surface=surface,
                        )
                    self.emit(
                        "worker_selector_validated",
                        outcome="accepted",
                        classification="worker_preserved",
                        surface=surface,
                    )
                elif agent_type is not None or bool((event_context or {}).get("_spawn_selector_required")):
                    validation = validate_worker_selector(arguments)
                    self.raise_worker_contract_error(
                        event="worker_selector_validated",
                        error_code=self.facts.worker_selector_error_code,
                        classification=validation.classification,
                        surface=surface,
                    )

        for item in value.values():
            self.validate_external_worker_selectors(item, event_context, surface=surface)

    def reject_missing_worker_selector_for_generated_call(
        self,
        spec: Mapping[str, Any],
        event_context: Mapping[str, Any] | None,
        *,
        surface: str,
    ) -> None:
        if spec.get("tool_name") == self.facts.spawn_agent_tool and bool(
            (event_context or {}).get("_spawn_selector_required")
        ):
            self.raise_worker_contract_error(
                event="worker_selector_validated",
                error_code=self.facts.worker_selector_error_code,
                classification="missing_selector",
                surface=surface,
            )

    def requested_binding_signature_payload(
        self,
        binding: Mapping[str, Any],
        call_id: str,
    ) -> bytes:
        signed_binding = {
            "contract_version": binding.get("contract_version"),
            "agent_type": binding.get("agent_type"),
            "model": binding.get("model"),
            "reasoning": binding.get("reasoning"),
        }
        canonical = json.dumps(
            signed_binding, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return call_id.encode("utf-8") + b"\0" + canonical

    def requested_binding_signature(self, binding: Mapping[str, Any], call_id: str) -> str:
        return self.signer.sign(self.requested_binding_signature_payload(binding, call_id))

    def requested_binding_sidecar(
        self,
        requested: Mapping[str, Any],
        call_id: str,
    ) -> dict[str, Any]:
        validation = validate_requested_worker_binding(requested)
        if validation.outcome != self.facts.binding_accepted:
            self.raise_worker_contract_error(
                event="worker_requested_binding_validated",
                error_code=self.facts.worker_binding_error_code,
                classification=validation.classification,
            )
        binding = {
            "contract_version": self.facts.requested_binding_version,
            "agent_type": requested["agent_type"],
            "model": requested["model"],
            "reasoning": requested["reasoning"],
        }
        return {**binding, "signature": self.requested_binding_signature(binding, call_id)}

    def verified_requested_binding(
        self,
        value: Any,
        call_id: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if value is None:
            return None, "missing_requested_binding_sidecar"
        if not isinstance(value, Mapping) or set(value) != self.facts.requested_binding_fields:
            return None, "unknown_requested_binding_sidecar"
        if value.get("contract_version") != self.facts.requested_binding_version:
            return None, "unknown_requested_binding_sidecar"
        signature = value.get("signature")
        binding = {
            "contract_version": value.get("contract_version"),
            "agent_type": value.get("agent_type"),
            "model": value.get("model"),
            "reasoning": value.get("reasoning"),
        }
        if not self.signer.verify(
            self.requested_binding_signature_payload(binding, call_id),
            signature,
        ):
            return None, "unknown_requested_binding_sidecar"
        requested = {
            "agent_type": binding["agent_type"],
            "model": binding["model"],
            "reasoning": binding["reasoning"],
        }
        validation = validate_requested_worker_binding(requested)
        if validation.outcome != self.facts.binding_accepted:
            return None, validation.classification
        return requested, None

    def is_legacy_native_worker_spawn_call(
        self,
        item: Mapping[str, Any],
        arguments: Mapping[str, Any] | None,
    ) -> bool:
        """Recognize the pre-sidecar native V1 worker call shape.

        Beta4 added signed model/reasoning sidecars to worker calls.  Sessions
        created before that change contain the native CLI's original
        `multi_agent_v1.spawn_agent` item instead, so they cannot be validated
        against a binding that was never persisted.  The pre-selector native
        schema omitted `agent_type` but included `fork_context` and
        `message`. Keep this compatibility predicate deliberately exact: only
        the historical namespace/name and argument shape may bypass the new
        sidecar contract.
        """
        # The first native V1 Responses history used two equivalent wire shapes:
        # the namespace/name form and the flattened `multi_agent_v1__spawn_agent`
        # form.  The CLI also omitted the generated item `id` when replaying old
        # history.  Keep those variants explicit instead of treating every
        # unbound spawn call as legacy.
        item_fields = set(item)
        metadata_field = self.facts.legacy_native_spawn_metadata_field
        allowed_fields = {
            "type",
            "call_id",
            "name",
            "arguments",
            "namespace",
            "id",
            metadata_field,
        }
        if not item_fields.issubset(allowed_fields):
            return False
        if item_fields - {"type", "call_id", "name", "arguments"} not in (
            set(),
            {"namespace"},
            {"id"},
            {"namespace", "id"},
            {"id", metadata_field},
            {"namespace", "id", metadata_field},
        ):
            return False
        if (
            item.get("type") != "function_call"
            or not isinstance(item.get("call_id"), str)
            or not item.get("call_id")
        ):
            return False
        has_id = "id" in item
        if has_id and (not isinstance(item.get("id"), str) or not item.get("id")):
            return False
        if metadata_field in item:
            if not has_id:
                return False
            metadata = item.get(metadata_field)
            if (
                not isinstance(metadata, Mapping)
                or set(metadata) != {"turn_id"}
                or not isinstance(metadata.get("turn_id"), str)
                or not metadata.get("turn_id")
            ):
                return False
        namespace = item.get("namespace")
        name = item.get("name")
        if not (
            (namespace == COLLABORATION_V1_NAMESPACE and name == self.facts.spawn_agent_tool)
            or (namespace is None and name == f"{COLLABORATION_V1_NAMESPACE}__{self.facts.spawn_agent_tool}")
        ):
            return False
        if not isinstance(arguments, Mapping):
            return False
        if set(arguments) not in (
            {"fork_context", "message"},
            {"agent_type", "fork_context", "message"},
        ):
            return False
        if "agent_type" in arguments and arguments.get("agent_type") != "worker":
            return False
        return (
            isinstance(arguments.get("fork_context"), bool)
            and isinstance(arguments.get("message"), str)
        )

    def is_legacy_native_worker_spawn_readback(self, value: Any) -> bool:
        readback = strict_json_object(value)
        return (
            isinstance(readback, Mapping)
            and set(readback) == {"agent_id", "nickname"}
            and isinstance(readback.get("agent_id"), str)
            and bool(readback.get("agent_id"))
            and (readback.get("nickname") is None or isinstance(readback.get("nickname"), str))
        )

    def remember_stream_item(
        self,
        state: dict[str, Any],
        item: Any,
        *,
        terminal: bool = False,
    ) -> None:
        if not isinstance(item, Mapping):
            return
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            return
        tool_name = collaboration_v1_function_call_name(item)
        items = state.setdefault("items", {})
        if not isinstance(items, dict):
            items = {}
            state["items"] = items
        record = items.setdefault(item_id, {})
        if not isinstance(record, dict):
            record = {}
            items[item_id] = record
        if tool_name is not None:
            record["tool_name"] = tool_name
        if tool_name != self.facts.spawn_agent_tool:
            return
        raw_arguments = item.get("arguments")
        if raw_arguments not in (None, ""):
            record["selector_arguments_pending"] = False
            if isinstance(raw_arguments, str):
                record["arguments"] = raw_arguments
            elif isinstance(raw_arguments, Mapping):
                record["arguments"] = json.dumps(
                    raw_arguments, ensure_ascii=True, separators=(",", ":")
                )
            parsed = strict_json_object(record.get("arguments"))
            if parsed is not None and isinstance(parsed.get("agent_type"), str):
                if not record.get("selector_invalid"):
                    record["selector_delta_incomplete"] = False
                    record["agent_type"] = parsed["agent_type"]
            elif terminal:
                record["selector_invalid"] = True
                record.pop("agent_type", None)
            else:
                record["selector_delta_incomplete"] = True
                record.pop("agent_type", None)
        else:
            if not terminal:
                record["selector_arguments_pending"] = True
            elif record.get("selector_arguments_pending") and not record.get("selector_arguments_done"):
                record["selector_invalid"] = True
                record.pop("agent_type", None)

    def remember_stream_event(
        self,
        value: Mapping[str, Any],
        event_context: Mapping[str, Any] | None,
    ) -> None:
        if not isinstance(event_context, dict):
            return
        state = event_context.get(self.facts.stream_binding_state_field)
        if not isinstance(state, dict):
            state = {"items": {}}
            event_context[self.facts.stream_binding_state_field] = state
        event_type = value.get("type")
        if event_type in {"response.output_item.added", "response.output_item.done"}:
            self.remember_stream_item(
                state,
                value.get("item"),
                terminal=event_type == "response.output_item.done",
            )
            return
        if event_type == "response.function_call_arguments.delta":
            item_id = value.get("item_id")
            delta = value.get("delta")
            if not isinstance(item_id, str) or not item_id or not isinstance(delta, str):
                return
            items = state.setdefault("items", {})
            if not isinstance(items, dict):
                return
            record = items.setdefault(item_id, {})
            if not isinstance(record, dict):
                record = {}
                items[item_id] = record
            record["arguments"] = f"{record.get('arguments', '')}{delta}"
            record["selector_arguments_pending"] = True
            parsed = strict_json_object(record["arguments"])
            if parsed is not None and isinstance(parsed.get("agent_type"), str):
                if not record.get("selector_invalid"):
                    record["selector_delta_incomplete"] = False
                    record["agent_type"] = parsed["agent_type"]
            else:
                record["selector_delta_incomplete"] = True
                record.pop("agent_type", None)
            return
        if event_type == "response.function_call_arguments.done":
            item_id = value.get("item_id")
            if not isinstance(item_id, str) or not item_id:
                return
            items = state.setdefault("items", {})
            if not isinstance(items, dict):
                return
            record = items.setdefault(item_id, {})
            if not isinstance(record, dict):
                record = {}
                items[item_id] = record
            arguments = value.get("arguments")
            if isinstance(arguments, str):
                record["arguments"] = arguments
                if record.get("tool_name") != self.facts.spawn_agent_tool:
                    return
                record["selector_arguments_done"] = True
                record["selector_arguments_pending"] = False
                parsed = strict_json_object(arguments)
                if parsed is not None and isinstance(parsed.get("agent_type"), str):
                    if not record.get("selector_invalid"):
                        record["selector_delta_incomplete"] = False
                        record["agent_type"] = parsed["agent_type"]
                else:
                    record["selector_invalid"] = True
                    record.pop("agent_type", None)
            return
        if event_type == "response.completed":
            response = value.get("response")
            output = response.get("output") if isinstance(response, Mapping) else None
            if isinstance(output, list):
                for item in output:
                    self.remember_stream_item(state, item, terminal=True)

    def raise_on_invalid_stream_event(
        self,
        value: Mapping[str, Any],
        event_context: Mapping[str, Any] | None,
        *,
        surface: str,
    ) -> None:
        """Reject a terminal streamed worker call before any semantic repair."""
        if self.is_v2_context(event_context):
            return
        context = event_context or {}
        if not context.get("_worker_binding_required"):
            return
        state = context.get(self.facts.stream_binding_state_field)
        items = state.get("items") if isinstance(state, Mapping) else None
        if not isinstance(items, Mapping):
            return

        event_type = value.get("type")
        item_ids: list[str] = []
        if event_type == "response.function_call_arguments.done":
            item_id = value.get("item_id")
            if isinstance(item_id, str) and item_id:
                item_ids.append(item_id)
        elif event_type == "response.output_item.done":
            item = value.get("item")
            item_id = item.get("id") if isinstance(item, Mapping) else None
            if isinstance(item_id, str) and item_id:
                item_ids.append(item_id)
        elif event_type == "response.completed":
            response = value.get("response")
            output = response.get("output") if isinstance(response, Mapping) else None
            if isinstance(output, list):
                item_ids.extend(
                    item["id"]
                    for item in output
                    if isinstance(item, Mapping)
                    and isinstance(item.get("id"), str)
                    and item.get("id")
                )

        for item_id in item_ids:
            record = items.get(item_id)
            if (
                isinstance(record, Mapping)
                and record.get("tool_name") == self.facts.spawn_agent_tool
                and record.get("selector_invalid")
            ):
                self.raise_worker_contract_error(
                    event="worker_selector_validated",
                    error_code=self.facts.worker_selector_error_code,
                    classification="malformed_arguments",
                    surface=surface,
                )

    def attach_requested_binding_sidecars(
        self,
        value: Any,
        event_context: Mapping[str, Any] | None,
        *,
        capture_stream_event: bool = True,
    ) -> tuple[Any, bool]:
        if isinstance(value, list):
            changed = False
            rewritten = []
            for item in value:
                replacement, item_changed = self.attach_requested_binding_sidecars(
                    item,
                    event_context,
                    capture_stream_event=capture_stream_event,
                )
                rewritten.append(replacement)
                changed = changed or item_changed
            return (rewritten if changed else value), changed
        if not isinstance(value, dict):
            return value, False

        if capture_stream_event:
            self.remember_stream_event(value, event_context)
        changed = False
        rewritten = dict(value)
        for key, item in value.items():
            replacement, item_changed = self.attach_requested_binding_sidecars(
                item,
                event_context,
                capture_stream_event=capture_stream_event,
            )
            if item_changed:
                rewritten[key] = replacement
                changed = True

        if collaboration_v1_function_call_name(rewritten) != self.facts.spawn_agent_tool:
            return (rewritten if changed else value), changed
        # Binding sidecars require an exact selector.  The general argument
        # normalizer intentionally accepts a valid JSON prefix for other repair
        # paths, but that would let malformed streamed arguments inherit a worker
        # binding after the strict stream state has already been cleared.
        raw_arguments = rewritten.get("arguments")
        arguments = strict_json_object(raw_arguments)
        context = event_context or {}
        pending_agent_type = None
        pending_arguments = None
        stream_item_tracked = False
        stream_selector_invalid = False
        stream_state = context.get(self.facts.stream_binding_state_field)
        item_id = rewritten.get("id")
        if isinstance(stream_state, Mapping) and isinstance(item_id, str):
            stream_items = stream_state.get("items")
            record = stream_items.get(item_id) if isinstance(stream_items, Mapping) else None
            if isinstance(record, Mapping):
                stream_item_tracked = True
                pending_agent_type = record.get("agent_type")
                pending_arguments = strict_json_object(record.get("arguments"))
                stream_selector_invalid = bool(record.get("selector_invalid"))
        if arguments is None:
            # Responses streams may publish the function-call item before its
            # arguments.  The arguments delta/done events carry the selector, but
            # the item that Codex persists can still have an empty arguments field.
            # When this request has an external worker binding, carry the signed
            # sidecar on that item so the next turn can validate the reconstructed
            # worker call.  A normal body call has no lifecycle status and keeps the
            # old fail-closed behavior.
            if not (
                raw_arguments in (None, "")
                and bool(context.get("_worker_binding_required"))
                and rewritten.get("status") in {"in_progress", "completed"}
                and pending_agent_type == "worker"
                and pending_arguments is not None
            ):
                return (rewritten if changed else value), changed
            arguments = pending_arguments
        elif arguments.get("agent_type") != "worker":
            return (rewritten if changed else value), changed
        elif stream_item_tracked and (
            stream_selector_invalid or pending_agent_type not in {None, "worker"}
        ):
            return (rewritten if changed else value), changed
        call_id = rewritten.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            self.raise_worker_contract_error(
                event="worker_requested_binding_validated",
                error_code=self.facts.worker_binding_error_code,
                classification="missing_call_identity",
            )
        requested = (event_context or {}).get("_worker_requested_binding")
        if not isinstance(requested, Mapping):
            self.raise_worker_contract_error(
                event="worker_requested_binding_validated",
                error_code=self.facts.worker_binding_error_code,
                classification="missing_requested_binding_sidecar",
            )
        sidecar = self.requested_binding_sidecar(requested, call_id)
        persisted_arguments = dict(arguments)
        # Keep one inert native field as the post-Beta4 marker so removing only the
        # private carrier cannot turn a new call into the exact legacy shape. A
        # null optional model is parsed by Codex CLI as no override, so the worker
        # still inherits the external parent model and reasoning effort.
        persisted_arguments["model"] = None
        persisted_arguments.pop("reasoning_effort", None)
        persisted_arguments[self.facts.requested_binding_field] = sidecar
        encoded_arguments = dump_arguments_like(raw_arguments, persisted_arguments)
        if raw_arguments != encoded_arguments:
            rewritten["arguments"] = encoded_arguments
            changed = True
        if self.facts.requested_binding_field in rewritten:
            rewritten.pop(self.facts.requested_binding_field, None)
            changed = True
        return (rewritten if changed else value), changed

    def apply_external_worker_response_contract(
        self,
        value: Any,
        event_context: Mapping[str, Any] | None,
        *,
        surface: str,
        validate_selectors: bool = True,
        attach_sidecars: bool = True,
        capture_stream_event: bool = True,
    ) -> tuple[Any, bool]:
        if self.is_v2_context(event_context):
            return value, False
        if validate_selectors:
            self.validate_external_worker_selectors(value, event_context, surface=surface)
        if attach_sidecars:
            return self.attach_requested_binding_sidecars(
                value,
                event_context,
                capture_stream_event=capture_stream_event,
            )
        return value, False

    def validate_worker_binding_history(
        self,
        payload: Mapping[str, Any],
    ) -> bool:
        input_items = payload.get("input")
        if not isinstance(input_items, list):
            return False

        changed = False
        worker_calls: dict[str, Mapping[str, Any] | None] = {}
        legacy_worker_calls: set[str] = set()
        validated_call_ids: set[str] = set()
        binding_field = self.facts.requested_binding_field
        for item in input_items:
            if not isinstance(item, Mapping):
                continue
            call_id = item.get("call_id")
            if item.get("type") == "function_call" and collaboration_v1_function_call_name(item) == self.facts.spawn_agent_tool:
                raw_arguments = item.get("arguments")
                arguments = json_object_from_arguments(raw_arguments)
                strict_arguments = strict_json_object(raw_arguments)
                agent_type = arguments.get("agent_type") if arguments is not None else None
                if agent_type in {"general", "default"}:
                    continue
                if not isinstance(call_id, str) or not call_id:
                    self.raise_worker_contract_error(
                        event="worker_effective_binding_validated",
                        error_code=self.facts.worker_binding_error_code,
                        classification="missing_call_identity",
                    )
                if call_id in worker_calls:
                    self.raise_worker_contract_error(
                        event="worker_effective_binding_validated",
                        error_code=self.facts.worker_binding_error_code,
                        classification="duplicate_worker_call_identity",
                    )
                nested_sidecar_present = (
                    isinstance(arguments, Mapping)
                    and binding_field in arguments
                )
                top_level_sidecar_present = binding_field in item
                if (
                    not nested_sidecar_present
                    and not top_level_sidecar_present
                    and self.is_legacy_native_worker_spawn_call(item, strict_arguments)
                ):
                    legacy_worker_calls.add(call_id)
                    worker_calls[call_id] = None
                    continue
                selector_validation = validate_worker_selector(arguments)
                if selector_validation.outcome != self.facts.binding_accepted:
                    self.raise_worker_contract_error(
                        event="worker_selector_validated",
                        error_code=self.facts.worker_selector_error_code,
                        classification=selector_validation.classification,
                        surface="history",
                    )
                if nested_sidecar_present and strict_arguments is None:
                    self.raise_worker_contract_error(
                        event="worker_requested_binding_validated",
                        error_code=self.facts.worker_binding_error_code,
                        classification="unknown_requested_binding_sidecar",
                    )
                nested_sidecar = (
                    strict_arguments.get(binding_field)
                    if nested_sidecar_present and strict_arguments is not None
                    else None
                )
                top_level_sidecar = item.get(binding_field)
                if (
                    nested_sidecar_present
                    and top_level_sidecar_present
                    and nested_sidecar != top_level_sidecar
                ):
                    self.raise_worker_contract_error(
                        event="worker_requested_binding_validated",
                        error_code=self.facts.worker_binding_error_code,
                        classification="conflicting_requested_binding_sidecar",
                    )
                requested, sidecar_failure = self.verified_requested_binding(
                    nested_sidecar if nested_sidecar_present else top_level_sidecar,
                    call_id,
                )
                if requested is None:
                    self.raise_worker_contract_error(
                        event="worker_requested_binding_validated",
                        error_code=self.facts.worker_binding_error_code,
                        classification=sidecar_failure or "unknown_requested_binding_sidecar",
                    )
                if strict_arguments is not None:
                    if nested_sidecar_present and (
                        "model" not in strict_arguments
                        or strict_arguments.get("model") is not None
                    ):
                        self.raise_worker_contract_error(
                            event="worker_requested_binding_validated",
                            error_code=self.facts.worker_binding_error_code,
                            classification="contradictory_requested_model",
                        )
                    if nested_sidecar_present and "reasoning_effort" in strict_arguments:
                        self.raise_worker_contract_error(
                            event="worker_requested_binding_validated",
                            error_code=self.facts.worker_binding_error_code,
                            classification="contradictory_requested_reasoning",
                        )
                    if (
                        not nested_sidecar_present
                        and "model" in strict_arguments
                        and strict_arguments.get("model") != requested["model"]
                    ):
                        self.raise_worker_contract_error(
                            event="worker_requested_binding_validated",
                            error_code=self.facts.worker_binding_error_code,
                            classification="contradictory_requested_model",
                        )
                    if (
                        not nested_sidecar_present
                        and "reasoning_effort" in strict_arguments
                        and strict_arguments.get("reasoning_effort") != requested["reasoning"]
                    ):
                        self.raise_worker_contract_error(
                            event="worker_requested_binding_validated",
                            error_code=self.facts.worker_binding_error_code,
                            classification="contradictory_requested_reasoning",
                        )
                if isinstance(item, dict):
                    if binding_field in item:
                        item.pop(binding_field, None)
                        changed = True
                    if nested_sidecar_present and strict_arguments is not None:
                        forwarded_arguments = dict(strict_arguments)
                        forwarded_arguments.pop(binding_field, None)
                        # The null model is a persistence marker added after the
                        # provider produced the call. Do not replay persistence
                        # metadata as provider-authored tool arguments.
                        forwarded_arguments.pop("model", None)
                        forwarded_arguments.pop("reasoning_effort", None)
                        item["arguments"] = dump_arguments_like(
                            raw_arguments,
                            forwarded_arguments,
                        )
                        changed = True
                worker_calls[call_id] = requested
                continue
            if (
                item.get("type") != "function_call_output"
                or not isinstance(call_id, str)
                or call_id not in worker_calls
            ):
                continue
            if call_id in validated_call_ids:
                self.raise_worker_contract_error(
                    event="worker_effective_binding_validated",
                    error_code=self.facts.worker_binding_error_code,
                    classification="duplicate_worker_effective_output",
                )

            output = item.get("output")
            if call_id in legacy_worker_calls:
                if not self.is_legacy_native_worker_spawn_readback(output):
                    self.raise_worker_contract_error(
                        event="worker_effective_binding_validated",
                        error_code=self.facts.worker_binding_error_code,
                        classification="malformed_readback",
                    )
                self.emit(
                    "worker_effective_binding_validated",
                    outcome="accepted",
                    classification="legacy_native_spawn",
                )
                validated_call_ids.add(call_id)
                continue
            readback = strict_json_object(output)
            if readback is None and isinstance(output, str) and output.strip():
                self.raise_worker_contract_error(
                    event="worker_effective_binding_validated",
                    error_code=self.facts.worker_binding_error_code,
                    classification="malformed_readback",
                )
            requested = worker_calls[call_id]
            readback = synthesize_effective_worker_binding_readback(
                requested,
                readback,
            )
            validation = validate_effective_worker_binding(
                requested,
                readback,
            )
            if validation.outcome != self.facts.binding_accepted:
                self.raise_worker_contract_error(
                    event="worker_effective_binding_validated",
                    error_code=self.facts.worker_binding_error_code,
                    classification=validation.classification,
                )
            self.emit(
                "worker_effective_binding_validated",
                outcome="accepted",
                classification=validation.classification,
            )
            validated_call_ids.add(call_id)

        if set(worker_calls) - validated_call_ids:
            self.raise_worker_contract_error(
                event="worker_effective_binding_validated",
                error_code=self.facts.worker_binding_error_code,
                classification="missing_readback",
            )
        return changed
