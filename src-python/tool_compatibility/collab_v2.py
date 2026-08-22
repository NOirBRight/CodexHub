"""Collaboration V2 adaptation: namespace validation, envelopes, stream ledger.

This module must not import the V1 adapter. V2 adaptation cannot execute V1 paths.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from collaboration_runtime_contract import (
    COLLABORATION_V2,
    CollaborationContractError,
    validate_agent_message,
    validate_collaboration_arguments,
    validate_collaboration_result,
)

from .contracts import ToolCompatibilityError, _copy_mapping, _freeze
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


def apply_v2_namespace_decode(result: dict[str, Any], record: Any) -> None:
    if record.version == "v2":
        # Codex 0.148 uses an explicitly empty list to distinguish
        # plaintext V2 message arguments from an encrypted handoff.
        result["encrypted_function_args"] = []


def validate_v2_native_arguments(item: Mapping[str, Any], *, surface: str) -> None:
    try:
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


class CollaborationV2PlanMixin:
    """V2 namespace adaptation mixed into ``ToolCompatibilityPlan``."""

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
        if allow_incomplete_arguments and arguments in {None, ""}:
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
                item_id, call_id = self._validate_collaboration_v2_call_item(
                    item,
                    surface=surface,
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

