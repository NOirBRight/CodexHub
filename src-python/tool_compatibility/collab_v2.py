"""Collaboration V2 adaptation: namespace validation, envelopes, stream ledger.

This module must not import the V1 adapter. V2 adaptation cannot execute V1 paths.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from collaboration_runtime_contract import (
    COLLABORATION_V2,
    EXPECTED_PARAMETER_SCHEMAS,
    V1_TOOLS,
    V2_TOOLS,
    failed_argument_call_ids,
    CollaborationContractError,
    normalize_collaboration_arguments,
    validate_agent_message,
    validate_collaboration_arguments,
    validate_collaboration_result,
)

from .contracts import ToolCompatibilityEntry, ToolCompatibilityError, copy_mapping as _copy_mapping
from .dispositions import ADAPT, NAMESPACE


V2_NAMES = frozenset(
    {"spawn_agent", "send_message", "followup_task", "wait_agent", "interrupt_agent", "list_agents"}
)
V2_FORBIDDEN = frozenset({"agent_id", "fork_context"})
V2_NAMESPACE = "collaboration"
AGENT_MESSAGE_ENVELOPE_PREFIX = "__codexhub_agent_message_v2__:"


def validate_v2_fields(fields: Mapping[str, Any]) -> None:
    if V2_FORBIDDEN.intersection(fields):
        raise ToolCompatibilityError("tool_compatibility_boundary", "mixed_v1_v2_fields")


def is_opaque_v2_history_item(item: Mapping[str, Any]) -> bool:
    return item.get("namespace") == V2_NAMESPACE and item.get("name") in V2_NAMES


def strip_encrypted_annotations(value: Any) -> Any:
    """Drop Official-only ``encrypted`` schema markers from a V2 child declaration."""

    if isinstance(value, Mapping):
        return {
            key: strip_encrypted_annotations(child)
            for key, child in value.items()
            if key != "encrypted"
        }
    if isinstance(value, list):
        return [strip_encrypted_annotations(child) for child in value]
    return value


CHAT_OFFICIAL_V2_NAME_MAP_KEY = "_chat_official_v2_name_map"
_V1_ONLY_TOOLS = frozenset(V1_TOOLS) - V2_NAMES


def _v2_alias_to_name() -> dict[str, str]:
    from .registry import RequestScopedToolAliasRegistry

    registry = RequestScopedToolAliasRegistry(request_token="request")
    mapping: dict[str, str] = {}
    for index, name in enumerate(V2_TOOLS):
        alias = registry.allocate_namespace(
            declaration_index=0,
            namespace=V2_NAMESPACE,
            child_index=index,
            child_name=name,
            version="v2",
        )
        mapping[alias] = name
    return mapping


def _function_tool_name(declaration: Mapping[str, Any]) -> str | None:
    name = declaration.get("name")
    if isinstance(name, str) and name:
        return name
    function = declaration.get("function")
    if isinstance(function, Mapping):
        nested = function.get("name")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _canonical_v2_name(name: str | None, alias_to_name: Mapping[str, str]) -> str | None:
    if not isinstance(name, str) or not name:
        return None
    if name in V2_NAMES:
        return name
    return alias_to_name.get(name)


def _official_v2_namespace(source_by_name: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    children: list[dict[str, Any]] = []
    schemas = EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2]
    for name in V2_TOOLS:
        source = source_by_name[name]
        description = source.get("description")
        nested = source.get("function")
        if not isinstance(description, str) and isinstance(nested, Mapping):
            description = nested.get("description")
        parameters = copy.deepcopy(schemas[name])
        children.append(
            {
                "type": "function",
                "name": name,
                "description": description if isinstance(description, str) else name,
                "strict": False,
                "parameters": parameters,
            }
        )
    return {
        "type": "namespace",
        "name": V2_NAMESPACE,
        "description": V2_NAMESPACE,
        "tools": children,
    }


def _expand_plaintext_agent_message(item: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    if item.get("type") != "message" or item.get("role") != "user":
        return _copy_mapping(item), False
    content = item.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return _copy_mapping(item), False
    part = content[0]
    if not isinstance(part, Mapping):
        return _copy_mapping(item), False
    text = part.get("text")
    if not isinstance(text, str) or not text.startswith(AGENT_MESSAGE_ENVELOPE_PREFIX):
        return _copy_mapping(item), False
    try:
        original = json.loads(text[len(AGENT_MESSAGE_ENVELOPE_PREFIX) :])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "unknown_agent_message_envelope",
            surface="history",
        ) from exc
    if not isinstance(original, dict):
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "unknown_agent_message_envelope",
            surface="history",
        )
    try:
        validate_agent_message(original)
    except CollaborationContractError as exc:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            exc.classification,
            surface="history",
        ) from exc
    content_parts = original.get("content")
    if isinstance(content_parts, list) and any(
        isinstance(part, Mapping) and part.get("type") == "encrypted_content"
        for part in content_parts
    ):
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "encrypted_agent_message_unavailable",
            surface="history",
        )
    return original, True


def expand_chat_v2_for_official(
    payload: dict[str, Any],
    event_context: Mapping[str, Any] | None = None,
) -> bool:
    """Fold a complete Chat V2 six-pack (or aliases) into the native namespace."""

    tools = payload.get("tools")
    if not isinstance(tools, list):
        tools = []
    alias_to_name = _v2_alias_to_name()
    v2_sources: dict[str, Mapping[str, Any]] = {}
    remaining: list[Any] = []
    saw_v1 = False
    saw_v2_name = False
    for tool in tools:
        if not isinstance(tool, Mapping):
            remaining.append(tool)
            continue
        name = _function_tool_name(tool)
        if name in _V1_ONLY_TOOLS:
            saw_v1 = True
            remaining.append(tool)
            continue
        canonical = _canonical_v2_name(name, alias_to_name)
        if canonical is None:
            remaining.append(tool)
            continue
        saw_v2_name = True
        if canonical in v2_sources:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "namespace_child_duplicate",
                surface="request",
            )
        v2_sources[canonical] = tool
    if saw_v1:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "mixed_v1_v2",
            surface="request",
        )
    if not saw_v2_name and not v2_sources:
        input_items = payload.get("input")
        if isinstance(input_items, list):
            for item in input_items:
                if not isinstance(item, Mapping):
                    continue
                if item.get("type") == "function_call":
                    name = item.get("name")
                    if name in _V1_ONLY_TOOLS:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "mixed_v1_v2",
                            surface="history",
                        )
                    if _canonical_v2_name(name if isinstance(name, str) else None, alias_to_name):
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "namespace_child_set_invalid",
                            surface="history",
                        )
        return False
    if set(v2_sources) != V2_NAMES:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "namespace_child_set_invalid",
            surface="request",
        )
    if payload.get("tool_choice") not in (None, "auto"):
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "tool_choice_invalid",
            surface="request",
        )
    payload["tool_choice"] = "auto"
    payload["tools"] = [_official_v2_namespace(v2_sources), *remaining]
    name_map = {
        canonical: _function_tool_name(source) or canonical
        for canonical, source in v2_sources.items()
    }
    if isinstance(event_context, dict):
        event_context[CHAT_OFFICIAL_V2_NAME_MAP_KEY] = name_map

    input_items = payload.get("input")
    if isinstance(input_items, list):
        rewritten: list[Any] = []
        for item in input_items:
            if not isinstance(item, Mapping):
                rewritten.append(item)
                continue
            if item.get("type") == "function_call":
                next_item = _copy_mapping(item)
                canonical = _canonical_v2_name(
                    next_item.get("name") if isinstance(next_item.get("name"), str) else None,
                    alias_to_name,
                )
                if canonical is None:
                    if next_item.get("name") in _V1_ONLY_TOOLS:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "mixed_v1_v2",
                            surface="history",
                        )
                    rewritten.append(next_item)
                    continue
                encrypted_args = next_item.get("encrypted_function_args")
                if encrypted_args not in (None, []):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "encrypted_collaboration_arguments_unavailable",
                        surface="history",
                    )
                arguments = next_item.get("arguments")
                if isinstance(arguments, str) and arguments:
                    try:
                        parsed = json.loads(arguments)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed = None
                    if isinstance(parsed, Mapping):
                        validate_v2_fields(parsed)
                next_item["name"] = canonical
                next_item["namespace"] = V2_NAMESPACE
                next_item["encrypted_function_args"] = []
                rewritten.append(next_item)
                continue
            decoded, changed_item = _expand_plaintext_agent_message(item)
            rewritten.append(decoded)
        payload["input"] = rewritten
    return True


def collapse_official_v2_names_for_chat(
    payload: dict[str, Any],
    event_context: Mapping[str, Any] | None,
) -> bool:
    """Rewrite Official namespace names back to the Chat-declared spelling."""

    name_map = (event_context or {}).get(CHAT_OFFICIAL_V2_NAME_MAP_KEY)
    if not isinstance(name_map, Mapping) or not name_map:
        return False
    changed = False
    output = payload.get("output")
    if not isinstance(output, list):
        return False
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        original = item.get("name")
        chat_name = name_map.get(original)
        if isinstance(chat_name, str) and chat_name and chat_name != original:
            item["name"] = chat_name
            changed = True
        encrypted_args = item.get("encrypted_function_args")
        if encrypted_args not in (None, []):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "encrypted_collaboration_arguments_unavailable",
                surface="response",
            )
        if "encrypted_function_args" in item:
            item.pop("encrypted_function_args")
            changed = True
        if item.get("namespace") == V2_NAMESPACE:
            item.pop("namespace", None)
            changed = True
    return changed


def apply_v2_namespace_decode(result: dict[str, Any], record: Any) -> None:
    if record.version == "v2":
        # Codex 0.148 uses an explicitly empty list to distinguish
        # plaintext V2 message arguments from an encrypted handoff.
        result["encrypted_function_args"] = []


def validate_v2_native_arguments(item: Mapping[str, Any], *, surface: str) -> None:
    try:
        # Native passthrough is validation-only.  Adapted paths normalize the
        # known CLI integer representation explicitly and must not leak that
        # rewrite into a provider's native namespace lifecycle.
        validate_collaboration_arguments(
            COLLABORATION_V2,
            str(item.get("name")),
            item.get("arguments"),
        )
    except CollaborationContractError as exc:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            exc.classification,
            surface=surface,
        ) from exc


def normalize_v2_native_arguments(item: Mapping[str, Any], *, surface: str) -> tuple[str, bool]:
    try:
        return normalize_collaboration_arguments(
            COLLABORATION_V2, str(item.get("name")), item.get("arguments")
        )
    except CollaborationContractError as exc:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary", exc.classification, surface=surface
        ) from exc


class CollaborationV2PlanMixin:
    """V2 namespace adaptation mixed into ``ToolCompatibilityPlan``."""

    __slots__ = ()

    def _collaboration_v2_entry(self) -> ToolCompatibilityEntry | None:
        matches = [
            entry
            for entry in self.entries
            if entry.family == NAMESPACE
            and entry.version == "v2"
            and entry.namespace == "collaboration"
        ]
        return matches[0] if len(matches) == 1 else None

    def _collaboration_v2_active(self) -> bool:
        return (
            self.collaboration_protocol == COLLABORATION_V2
            or self._collaboration_v2_entry() is not None
        )

    @staticmethod
    def _raise_collaboration_contract(
        error: CollaborationContractError,
        *,
        surface: str,
    ) -> None:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            error.classification,
            surface=surface,
        ) from error

    def _validate_collaboration_v2_call_item(
        self,
        item: Mapping[str, Any],
        *,
        surface: str,
        allow_incomplete_arguments: bool = False,
        allow_failed_arguments: bool = False,
    ) -> tuple[str, str]:
        if (
            item.get("type") != "function_call"
            or item.get("namespace") != "collaboration"
            or item.get("name") not in V2_NAMES
        ):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unknown_native_identity",
                surface=surface,
            )
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "missing_item_identity",
                surface=surface,
            )
        required_fields = {
            "type",
            "id",
            "call_id",
            "namespace",
            "name",
            "arguments",
        }
        valid_field_sets = [required_fields, required_fields | {"encrypted_function_args"}]
        if surface in {"response", "stream"}:
            valid_field_sets.extend(
                [
                    required_fields | {"status"},
                    required_fields | {"status", "encrypted_function_args"},
                ]
            )
        if set(item) not in valid_field_sets:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "collaboration_call_fields_invalid",
                surface=surface,
            )
        if "encrypted_function_args" in item:
            encrypted_function_args = item.get("encrypted_function_args")
            if not isinstance(encrypted_function_args, list) or not all(
                isinstance(name, str) for name in encrypted_function_args
            ):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "collaboration_call_fields_invalid",
                    surface=surface,
                )
            entry = self._collaboration_v2_entry()
            if entry is not None and entry.disposition == ADAPT and encrypted_function_args:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "encrypted_collaboration_arguments_unavailable",
                    surface=surface,
                )
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "missing_call_identity",
                surface=surface,
            )
        arguments = item.get("arguments")
        if allow_incomplete_arguments and (arguments is None or arguments == ""):
            return item_id, call_id
        if allow_failed_arguments:
            return item_id, call_id
        try:
            validate_collaboration_arguments(
                COLLABORATION_V2,
                str(item["name"]),
                arguments,
            )
        except CollaborationContractError as exc:
            self._raise_collaboration_contract(exc, surface=surface)
        return item_id, call_id

    def _validate_collaboration_v2_items(
        self,
        items: Any,
        *,
        surface: str,
    ) -> None:
        if not isinstance(items, list):
            return
        if not self._collaboration_v2_active():
            if any(
                isinstance(item, Mapping) and item.get("type") == "agent_message"
                for item in items
            ):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unknown_native_identity",
                    surface=surface,
                )
            return

        def claims_collaboration_v2_identity(item: Mapping[str, Any]) -> bool:
            if item.get("namespace") == "collaboration":
                return True
            record = self.registry.record_for_alias(item.get("name"))
            return bool(
                record is not None
                and record.family == NAMESPACE
                and record.version == "v2"
                and record.namespace == "collaboration"
            )

        # Results do not repeat the function identity.  Preclassify local call
        # ownership so only results paired with an unrelated call bypass V2
        # validation; result identities without a local owner remain closed.
        collaboration_call_ids: set[str] = set()
        unrelated_call_ids: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping) or item.get("type") != "function_call":
                continue
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            target = (
                collaboration_call_ids
                if claims_collaboration_v2_identity(item)
                else unrelated_call_ids
            )
            target.add(call_id)

        calls: dict[str, str] = {}
        failed_calls = failed_argument_call_ids(items)
        seen_result_call_ids: set[str] = set()
        seen_item_ids: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            if item_type == "agent_message":
                try:
                    validate_agent_message(item)
                except CollaborationContractError as exc:
                    self._raise_collaboration_contract(exc, surface=surface)
                item_id = item.get("id")
            elif item_type == "function_call" and claims_collaboration_v2_identity(item):
                item_call_id = item.get("call_id")
                item_id, call_id = self._validate_collaboration_v2_call_item(
                    item,
                    surface=surface,
                    allow_failed_arguments=(
                        surface == "history" and item_call_id in failed_calls
                    ),
                )
                if call_id in calls:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "duplicate_call_identity",
                        surface=surface,
                    )
                calls[call_id] = str(item["name"])
            elif item_type == "function_call_output":
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "missing_call_identity",
                        surface=surface,
                    )
                record = self.registry.record_for_call(call_id)
                result_claims_collaboration_v2 = (
                    claims_collaboration_v2_identity(item)
                    or (
                        record is not None
                        and record.family == NAMESPACE
                        and record.version == "v2"
                        and record.namespace == "collaboration"
                    )
                )
                if (
                    call_id in unrelated_call_ids
                    and call_id not in collaboration_call_ids
                    and not result_claims_collaboration_v2
                ):
                    continue
                if call_id not in calls:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unknown_call_identity",
                        surface=surface,
                    )
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "missing_item_identity",
                        surface=surface,
                    )
                if set(item) != {"type", "id", "call_id", "output"}:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "collaboration_result_fields_invalid",
                        surface=surface,
                    )
                if call_id in seen_result_call_ids:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "duplicate_call_identity",
                        surface=surface,
                    )
                try:
                    validate_collaboration_result(
                        COLLABORATION_V2,
                        calls[call_id],
                        item.get("output"),
                    )
                except CollaborationContractError as exc:
                    self._raise_collaboration_contract(exc, surface=surface)
                seen_result_call_ids.add(call_id)
            else:
                continue
            if not isinstance(item_id, str) or not item_id:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "missing_item_identity",
                    surface=surface,
                )
            if item_id in seen_item_ids:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "duplicate_item_identity",
                    surface=surface,
                )
            seen_item_ids.add(item_id)


    def _encode_agent_message(self, item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Adapt plaintext V2 handoff history for an ordinary provider.

        ``agent_message`` is an Official collaboration item. A third-party
        function-capable endpoint can consume its plaintext meaning only as a
        request-bound user-message envelope; the registry preserves the exact
        author, recipient, item id, and content for inverse history conversion.
        It cannot decrypt Official encrypted content, so that boundary rejects.
        """

        if not self._collaboration_v2_active():
            return item, False
        entry = self._collaboration_v2_entry()
        should_adapt = (
            entry.disposition == ADAPT
            if entry is not None
            else (
                not self.capabilities.namespace_lifecycle
                and self.capabilities.function_lifecycle
                and self.capabilities.accepts_namespace_adapter
            )
        )
        if not should_adapt:
            if self.capabilities.namespace_lifecycle:
                return item, False
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "required_unavailable",
                surface="history",
            )

        try:
            validate_agent_message(item)
        except CollaborationContractError as exc:
            self._raise_collaboration_contract(exc, surface="history")

        content = item["content"]
        if any(part.get("type") == "encrypted_content" for part in content):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "encrypted_agent_message_unavailable",
                surface="history",
            )

        envelope = AGENT_MESSAGE_ENVELOPE_PREFIX + json.dumps(
            item,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.registry.bind_agent_message(envelope, item)
        return {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": envelope}],
        }, True

    def _decode_agent_message(self, item: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        if item.get("type") != "message" or item.get("role") != "user":
            return _copy_mapping(item), False
        content = item.get("content")
        if not isinstance(content, list) or len(content) != 1:
            return _copy_mapping(item), False
        part = content[0]
        if not isinstance(part, Mapping) or set(part) != {"type", "text"}:
            return _copy_mapping(item), False
        text = part.get("text")
        if not isinstance(text, str) or not text.startswith(AGENT_MESSAGE_ENVELOPE_PREFIX):
            return _copy_mapping(item), False
        original = self.registry.agent_message_for_envelope(text)
        if original is None:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unknown_agent_message_envelope",
                surface="history",
            )
        try:
            validate_agent_message(original)
        except CollaborationContractError as exc:
            self._raise_collaboration_contract(exc, surface="history")
        return original, True



class CollaborationV2StreamMixin:
    """V2 stream-ledger operations mixed into ``CompatibilityStreamState``."""

    __slots__ = ()

    @staticmethod
    def _agent_message_output_index(event: Mapping[str, Any]) -> int:
        output_index = event.get("output_index")
        if type(output_index) is not int or output_index < 0:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface="stream",
            )
        return output_index

    @staticmethod
    def _collaboration_v2_output_index(event: Mapping[str, Any]) -> int:
        output_index = event.get("output_index")
        if type(output_index) is not int or output_index < 0:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface="stream",
            )
        return output_index

    def _record_collaboration_v2_added(
        self,
        item_id: str,
        item: Mapping[str, Any],
        event: Mapping[str, Any],
    ) -> None:
        if item_id in self._collaboration_v2_calls:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "duplicate_item_identity",
                surface="stream",
            )
        output_index = self._collaboration_v2_output_index(event)
        if (
            output_index in self._collaboration_v2_output_indices.values()
            or (
                self._collaboration_v2_added_order
                and output_index
                <= self._collaboration_v2_output_indices[
                    self._collaboration_v2_added_order[-1]
                ]
            )
        ):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface="stream",
            )
        self._collaboration_v2_calls[item_id] = _copy_mapping(item)
        self._collaboration_v2_output_indices[item_id] = output_index
        self._collaboration_v2_added_order.append(item_id)

    def _validate_collaboration_v2_stream_call(
        self,
        item_id: str,
        item: Mapping[str, Any],
        *,
        surface: str = "stream",
    ) -> dict[str, Any]:
        canonical = _copy_mapping(item)
        record = self.plan.registry.record_for_alias(canonical.get("name"))
        if record is not None and record.version == "v2" and record.family == NAMESPACE:
            canonical, _record, _changed = self._check_alias_in_item(
                canonical,
                allow_incomplete=False,
            )
        self.plan._validate_collaboration_v2_call_item(
            canonical,
            surface=surface,
        )
        # Body and SSE must expose the same lossless timeout normalization.
        # The validator above establishes identity and schema; this second
        # step only canonicalizes the one CLI integer mismatch and leaves all
        # other argument bytes untouched.
        if record is not None and isinstance(canonical.get("arguments"), str) and canonical.get("arguments") != "":
            canonical["arguments"], _ = normalize_v2_native_arguments(
                {"name": canonical.get("name"), "arguments": canonical["arguments"]},
                surface=surface,
            )
        expected = self._collaboration_v2_calls.get(item_id)
        if expected is None:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "missing_stream_identity",
                surface=surface,
            )
        for key in ("id", "call_id", "namespace", "name"):
            if canonical.get(key) != expected.get(key):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_native_identity",
                    surface=surface,
                )
        return canonical

    def _canonicalize_collaboration_v2_stream_arguments(
        self, item_id: str, arguments: str
    ) -> str:
        complete_item = _copy_mapping(self._collaboration_v2_calls.get(item_id, {}))
        record = self.plan.registry.record_for_call(complete_item.get("call_id"))
        if record is not None:
            complete_item["name"] = record.alias
        complete_item["arguments"] = arguments
        return self._validate_collaboration_v2_stream_call(item_id, complete_item)["arguments"]

    def _validate_collaboration_v2_event_index(
        self,
        item_id: str,
        event: Mapping[str, Any],
    ) -> None:
        output_index = self._collaboration_v2_output_index(event)
        expected_output_index = self._collaboration_v2_output_indices.get(item_id)
        if expected_output_index is None or output_index != expected_output_index:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface="stream",
            )
