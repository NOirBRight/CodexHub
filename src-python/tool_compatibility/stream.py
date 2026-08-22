"""Request-scoped SSE lifecycle ledger for one compatibility plan.

Generic stream assembly lives here. Collaboration V2 stream operations are
mixed in from ``collab_v2``; V1 repair identity checks are dispatched to
``collab_v1``. This module may import both isolated adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Iterable, Mapping

from collaboration_runtime_contract import (
    COLLABORATION_V2,
    CollaborationContractError,
    validate_collaboration_arguments,
)

from .collab_v1 import is_legacy_flattened_spawn
from .collab_v2 import CollaborationV2StreamMixin, V2_NAMES as _V2_NAMES
from .contracts import ToolCompatibilityError, _copy_mapping, _freeze, _thaw
from .dispositions import (
    ADAPT,
    CUSTOM_FREEFORM,
    NAMESPACE,
    PLAIN_FUNCTION,
    SELECTED_PROVIDER_HOSTED,
    TOOL_SEARCH,
    _hosted_event_spec,
    _hosted_event_spec_for_declaration_kind,
    _hosted_kind_for_item_type,
    _is_unsupported_hosted_stream_event,
)
from .plan import (
    CUSTOM_INPUT_KEY,
    TOOL_SEARCH_INPUT_KEY,
    AliasRecord,
    ToolCompatibilityEntry,
    ToolCompatibilityPlan,
    _is_legacy_message_identity,
    _item_identity,
    _json_object_exact,
    _json_object_with_key,
)

@dataclass(slots=True)
class _PendingStreamItem:
    record: AliasRecord
    item_id: str
    call_id: str | None
    fragments: list[str] = field(default_factory=list)
    delta_done: bool = False
    item_done: bool = False


@dataclass(slots=True)
class _BufferedCustomStreamItem:
    record: AliasRecord
    added_event: dict[str, Any]
    item_id: str
    call_id: str
    fragments: list[str] = field(default_factory=list)
    arguments_done_event: dict[str, Any] | None = None
    native_input: str | None = None


@dataclass(slots=True)
class _BufferedToolSearchStreamItem:
    record: AliasRecord
    added_event: dict[str, Any]
    item_id: str
    call_id: str
    fragments: list[str] = field(default_factory=list)
    arguments_done_event: dict[str, Any] | None = None
    native_arguments: Any = None


@dataclass(slots=True)
class _OpaqueStreamItem:
    """Track a provider-local custom call without claiming a declaration.

    Some upstream Responses streams still expose the legacy ``custom_tool_call``
    lifecycle even when the request did not declare a structured custom tool.
    The wire item itself is the only owner available in that case.  Keep that
    owner opaque, but apply the same identity and terminal checks as declared
    calls.
    """

    item_id: str
    call_id: str
    wire_identity: tuple[Any, Any, Any]
    fragments: list[str] = field(default_factory=list)
    arguments_done: bool = False
    item_done: bool = False


@dataclass(slots=True)
class _HostedStreamState:
    event_kind: str
    stages: tuple[str, ...]
    next_stage: int = 0


class CompatibilityStreamState(CollaborationV2StreamMixin):
    """Assemble one adapted Responses SSE lifecycle before inverse mapping."""

    _TERMINAL_EVENT_TYPES = frozenset(
        {"response.completed", "response.incomplete", "response.failed"}
    )

    def __init__(self, plan: ToolCompatibilityPlan) -> None:
        self.plan = plan
        self._pending: dict[str, _PendingStreamItem] = {}
        self._seen_item_ids: set[str] = set()
        self._seen_call_ids: set[str] = set()
        self._native_pending: dict[str, tuple[str | None, ToolCompatibilityEntry]] = {}
        # The entry identifies the declaration, while this second ledger
        # retains the exact wire child/name that was observed at ``added``.
        # A namespace entry can contain several children, so family alone is
        # not sufficient to reconcile a terminal item.
        self._native_wire_identities: dict[str, tuple[Any, Any, Any]] = {}
        # Buffered custom adapters leave ``_pending`` once their native
        # lifecycle is emitted.  Keep the original adapter wire identity so a
        # terminal envelope can still reconcile item/call/declaration exactly.
        self._adapter_wire_identities: dict[str, tuple[str, AliasRecord, tuple[Any, Any, Any]]] = {}
        self._legacy_unowned_done: dict[
            str,
            tuple[str, ToolCompatibilityEntry, tuple[Any, Any, Any], Any],
        ] = {}
        self._wire_payloads: dict[str, Any] = {}
        self._native_done: set[str] = set()
        self._native_delta_done: set[str] = set()
        self._native_fragments: dict[str, list[str]] = {}
        self._hosted_pending: dict[str, _HostedStreamState] = {}
        self._buffered_custom: dict[str, _BufferedCustomStreamItem] = {}
        self._buffered_tool_search: dict[str, _BufferedToolSearchStreamItem] = {}
        # Provider-local custom calls have no declaration/alias owner.  Keep
        # an opaque stream owner once ``output_item.added`` establishes it so
        # later deltas and terminal events cannot borrow an arbitrary id.
        self._opaque_pending: dict[str, _OpaqueStreamItem] = {}
        self._agent_message_pending: dict[str, Any] = {}
        self._agent_message_output_indices: dict[str, int] = {}
        self._agent_message_added_order: list[str] = []
        self._agent_message_done: set[str] = set()
        self._agent_message_done_order: list[str] = []
        self._collaboration_v2_calls: dict[str, dict[str, Any]] = {}
        self._collaboration_v2_output_indices: dict[str, int] = {}
        self._collaboration_v2_added_order: list[str] = []
        self._collaboration_v2_done: set[str] = set()
        self._collaboration_v2_done_order: list[str] = []
        self._terminal = False

    @property
    def terminal(self) -> bool:
        return self._terminal

    @staticmethod
    def _item_id(value: Mapping[str, Any]) -> str | None:
        item_id = value.get("item_id")
        plain_id = value.get("id")
        if (
            isinstance(item_id, str)
            and item_id
            and isinstance(plain_id, str)
            and plain_id
            and item_id != plain_id
        ):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface="stream",
            )
        for key in ("item_id", "id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    def _pending_for(self, value: Mapping[str, Any]) -> _PendingStreamItem:
        item_id = self._item_id(value)
        if item_id and item_id in self._pending:
            return self._pending[item_id]
        raise ToolCompatibilityError("tool_compatibility_boundary", "missing_stream_identity", surface="stream")

    def _native_entry_for_item(self, item: Mapping[str, Any]) -> ToolCompatibilityEntry | None:
        return self.plan._native_entry_for_item(item)

    def _record_opaque_added(self, item: Mapping[str, Any]) -> _OpaqueStreamItem | None:
        """Record an undeclared legacy custom call established by ``added``."""
        if item.get("type") != "custom_tool_call":
            return None
        item_id = self._item_id(item)
        call_id = item.get("call_id")
        if item_id is None or not isinstance(call_id, str) or not call_id:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "missing_stream_identity",
                surface="stream",
            )
        if item_id in self._seen_item_ids or item_id in self._opaque_pending:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "duplicate_item_identity",
                surface="stream",
            )
        if call_id in self._seen_call_ids or any(
            opaque.call_id == call_id for opaque in self._opaque_pending.values()
        ):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "duplicate_call_identity",
                surface="stream",
            )
        opaque = _OpaqueStreamItem(
            item_id=item_id,
            call_id=call_id,
            wire_identity=self._native_wire_identity(item),
        )
        self._seen_item_ids.add(item_id)
        self._seen_call_ids.add(call_id)
        self._opaque_pending[item_id] = opaque
        return opaque

    @staticmethod
    def _opaque_payload(value: Any) -> tuple[str, Any]:
        return ("opaque_input", _freeze(value))

    def _record_legacy_unowned_native_done(
        self,
        item: Mapping[str, Any],
        entry: ToolCompatibilityEntry,
    ) -> None:
        """Record one complete legacy item that legitimately omits ``added``."""
        item_id = self._item_id(item)
        if item_id is None:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "missing_item_identity",
                surface="stream",
            )
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "missing_call_identity",
                surface="stream",
            )
        if item_id in self._seen_item_ids or item_id in self._native_done:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "duplicate_item_identity",
                surface="stream",
            )
        if call_id in self._seen_call_ids:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "duplicate_call_identity",
                surface="stream",
            )
        self.plan._validate_native_item(item, entry, surface="stream")
        self._seen_item_ids.add(item_id)
        self._seen_call_ids.add(call_id)
        self._native_done.add(item_id)
        self._legacy_unowned_done[item_id] = (
            call_id,
            entry,
            self._native_wire_identity(item),
            self._semantic_wire_payload(item, entry),
        )

    def _complete_native_arguments_from_item(
        self,
        item_id: str,
        item: Mapping[str, Any],
        entry: ToolCompatibilityEntry,
    ) -> None:
        """Accept a complete native item when its separate ``done`` event is omitted."""
        if entry.family == CUSTOM_FREEFORM:
            arguments = item.get("input")
            if not isinstance(arguments, str):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "incomplete_stream_delta",
                    surface="stream",
                )
            fragments = self._native_fragments.get(item_id, [])
            if fragments and "".join(fragments) != arguments:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "incomplete_stream_delta",
                    surface="stream",
                )
            payload_item = {"type": "custom_tool_call", "input": arguments}
        else:
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "incomplete_stream_delta",
                    surface="stream",
                )
            fragments = self._native_fragments.get(item_id, [])
            if fragments and "".join(fragments) != arguments:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "incomplete_stream_delta",
                    surface="stream",
                )
            payload_item = {"type": "function_call", "arguments": arguments}
        payload = self._semantic_wire_payload(payload_item, entry)
        if item_id in self._wire_payloads and self._wire_payloads[item_id] != payload:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface="stream",
            )
        self._wire_payloads[item_id] = payload
        self._native_delta_done.add(item_id)

    def _native_item_id_for_event(self, value: Mapping[str, Any]) -> str | None:
        item = value.get("item")
        nested_item_id = self._item_id(item) if isinstance(item, Mapping) else None
        event_item_id = self._item_id(value)
        if nested_item_id and event_item_id and nested_item_id != event_item_id:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface="stream",
            )
        return nested_item_id or event_item_id

    @staticmethod
    def _native_wire_identity(item: Mapping[str, Any]) -> tuple[Any, Any, Any]:
        return (item.get("type"), item.get("name"), item.get("namespace"))

    @staticmethod
    def _semantic_wire_payload(
        item: Mapping[str, Any],
        owner: ToolCompatibilityEntry | AliasRecord,
    ) -> Any:
        """Return parsed call data used for terminal reconciliation.

        Equivalent JSON argument objects may differ in formatting or key
        order.  Custom adapters instead compare the envelope's actual input
        value.  Non-JSON arguments stay comparable as bounded raw strings;
        this check does not add a new validation rule.
        """
        item_type = item.get("type")
        family = owner.family
        if family == CUSTOM_FREEFORM:
            if item_type == "custom_tool_call":
                return ("custom_input", _freeze(item.get("input")))
            arguments = item.get("arguments")
            if arguments in (None, ""):
                return None
            try:
                envelope = _json_object_exact(arguments)
            except ToolCompatibilityError:
                return ("custom_arguments_raw", arguments)
            return ("custom_input", _freeze(envelope.get(CUSTOM_INPUT_KEY)))
        if family == TOOL_SEARCH and item_type == "function_call":
            arguments = item.get("arguments")
            if arguments in (None, ""):
                return None
            try:
                envelope = _json_object_with_key(arguments, TOOL_SEARCH_INPUT_KEY)
            except ToolCompatibilityError:
                return ("tool_search_arguments_raw", arguments)
            return ("arguments", _freeze(envelope[TOOL_SEARCH_INPUT_KEY]))
        if item_type == "function_call":
            arguments = item.get("arguments")
            if arguments in (None, ""):
                return None
            if isinstance(arguments, Mapping):
                return ("arguments", _freeze(arguments))
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments)
                except (TypeError, ValueError):
                    return ("arguments_raw", arguments)
                return ("arguments", _freeze(parsed))
        if family == TOOL_SEARCH and item_type == "tool_search_call":
            return ("arguments", _freeze(item.get("arguments")))
        return None

    @staticmethod
    def _is_output_item_type(item_type: Any) -> bool:
        return isinstance(item_type, str) and (
            item_type in {
                "function_call_output",
                "custom_tool_call_output",
                "tool_search_output",
            }
            or item_type.endswith("_call_output")
        )

    def _validate_output_item_added(self, item: Mapping[str, Any]) -> None:
        """Require every stream output item to have a prior call owner."""
        if not self._is_output_item_type(item.get("type")):
            return
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "missing_call_identity",
                surface="stream",
            )

        adapter_pending = next(
            (pending for pending in self._pending.values() if pending.call_id == call_id),
            None,
        )
        if adapter_pending is not None:
            record = self.plan.registry.record_for_call(call_id)
            if record is not adapter_pending.record:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_call_identity",
                    surface="stream",
                )
            expected = (
                "function_call_output"
                if adapter_pending.record.family in {NAMESPACE, CUSTOM_FREEFORM}
                else None
            )
            if expected is not None and item.get("type") != expected:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_call_identity",
                    surface="stream",
                )
            return

        native_pending = next(
            (
                (item_id, call_entry)
                for item_id, call_entry in self._native_pending.items()
                if call_entry[0] == call_id
            ),
            None,
        )
        if native_pending is not None:
            _item_id, (_call_id, entry) = native_pending
            expected = self.plan._history_output_type_for_entry(entry)
            if expected is not None and item.get("type") != expected:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_call_identity",
                    surface="stream",
                )
            return

        # A request-local registry may retain calls from a previously decoded
        # body/history.  It does not establish ownership for this SSE stream;
        # require the corresponding ``added`` event in this state.
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "missing_stream_identity",
            surface="stream",
        )

    def _check_alias_in_item(self, item: Mapping[str, Any], *, allow_incomplete: bool = True) -> tuple[dict[str, Any], AliasRecord | None, bool]:
        if item.get("type") == "function_call":
            decoded, record, changed = self.plan._decode_call_compat(item, allow_incomplete=allow_incomplete)
            if (
                record is not None
                and record.version == "v2"
                and record.family == NAMESPACE
                and not allow_incomplete
            ):
                try:
                    validate_collaboration_arguments(
                        COLLABORATION_V2,
                        str(record.child_name),
                        decoded.get("arguments"),
                    )
                except CollaborationContractError as exc:
                    self.plan._raise_collaboration_contract(exc, surface="stream")
            if record is not None and record.family == NAMESPACE:
                supplied_namespace = item.get("namespace")
                if supplied_namespace is not None and supplied_namespace != record.namespace:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unknown_alias",
                        surface="stream",
                    )
            entry = self.plan._entry_for_name(
                item.get("name"),
                item.get("namespace"),
                item_type=item.get("type"),
            )
            if record is None and self.plan._has_adapted_name_conflict(item.get("name"), PLAIN_FUNCTION):
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="stream")
            if record is None and self.plan.has_adaptations and isinstance(item.get("name"), str) and (
                entry is None or entry.disposition == ADAPT
            ):
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="stream")
            return decoded, record, changed
        if self.plan.registry.looks_like_alias(item.get("name")):
            raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="stream")
        return _copy_mapping(item), None, False

    def _decode_hosted_event(
        self,
        event: Mapping[str, Any],
        event_spec: tuple[str, str, tuple[str, ...]],
    ) -> dict[str, Any]:
        event_kind, stage, _stages = event_spec
        item_id = self._native_item_id_for_event(event)
        pending = self._hosted_pending.get(item_id) if item_id is not None else None
        if pending is None or pending.event_kind != event_kind:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "missing_stream_identity",
                surface="stream",
            )
        # Hosted stage events normally carry only ``item_id``.  If a provider
        # includes a nested item, however, its wire identity is part of the
        # lifecycle contract and must remain bound to the exact item observed
        # at ``output_item.added``.  Do not let a matching id borrow another
        # hosted kind or declaration.
        if "item" in event:
            nested_item = event.get("item")
            expected_wire = self._native_wire_identities.get(item_id)
            if (
                not isinstance(nested_item, Mapping)
                or expected_wire is None
                or self._native_wire_identity(nested_item) != expected_wire
            ):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_native_identity",
                    surface="stream",
                )
        if item_id in self._native_done:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "duplicate_item_identity",
                surface="stream",
            )
        if pending.next_stage >= len(pending.stages) or pending.stages[pending.next_stage] != stage:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "invalid_hosted_lifecycle",
                surface="stream",
            )
        pending.next_stage += 1
        return _copy_mapping(event)

    def _finish_terminal(self) -> None:
        if self._terminal:
            raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_terminal", surface="stream")
        incomplete = [pending for pending in self._pending.values() if not pending.item_done]
        native_incomplete = any(
            item_id not in self._native_done
            or (
                entry.family not in {TOOL_SEARCH, SELECTED_PROVIDER_HOSTED}
                and item_id not in self._native_delta_done
            )
            for item_id, (_call_id, entry) in self._native_pending.items()
        )
        opaque_incomplete = any(
            not opaque.arguments_done or not opaque.item_done
            for opaque in self._opaque_pending.values()
        )
        search_incomplete = any(
            pending.arguments_done_event is None
            for pending in self._buffered_tool_search.values()
        )
        agent_message_incomplete = (
            set(self._agent_message_pending) - self._agent_message_done
        )
        collaboration_v2_incomplete = (
            set(self._collaboration_v2_calls) - self._collaboration_v2_done
        )
        if (
            incomplete
            or native_incomplete
            or opaque_incomplete
            or search_incomplete
            or agent_message_incomplete
            or collaboration_v2_incomplete
        ):
            raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream", surface="stream")
        self._terminal = True

    def _validate_terminal_native_output(self, response: Mapping[str, Any]) -> None:
        output = response.get("output")
        required_native_ids = {
            item_id
            for item_id in self._native_pending
        }
        required_collaboration_v2_ids = set(self._collaboration_v2_calls)
        required_adapter_ids = set(self._pending) | set(self._adapter_wire_identities)
        required_agent_message_ids = set(self._agent_message_pending)
        if output is None:
            # Some upstream terminal envelopes omit ``output`` after the SSE
            # lifecycle has already delivered the completed item.  _finish_terminal
            # below still rejects an actually incomplete lifecycle.
            if required_agent_message_ids:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "incomplete_stream",
                    surface="stream",
                )
            return
        if output == []:
            if (
                required_native_ids
                or required_collaboration_v2_ids
                or required_adapter_ids
                or required_agent_message_ids
            ):
                raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream", surface="stream")
            return
        if not isinstance(output, list):
            raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_stream_event", surface="stream")
        seen_output_ids: set[str] = set()
        terminal_agent_message_order: list[str] = []
        terminal_collaboration_v2_order: list[str] = []
        for output_index, item in enumerate(output):
            if not isinstance(item, Mapping):
                continue
            item_id = _item_identity(item)
            if item_id is not None:
                if item_id in seen_output_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="stream")
                seen_output_ids.add(item_id)
            if item.get("type") == "agent_message":
                self.plan._validate_collaboration_v2_items([item], surface="stream")
                if item_id not in self._agent_message_pending:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "missing_stream_identity",
                        surface="stream",
                    )
                expected_output_index = self._agent_message_output_indices[item_id]
                if output_index != expected_output_index:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_native_identity",
                        surface="stream",
                    )
                if item_id not in self._agent_message_done:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "incomplete_stream",
                        surface="stream",
                    )
                if _freeze(item) != self._agent_message_pending[item_id]:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_native_identity",
                        surface="stream",
                    )
                terminal_agent_message_order.append(item_id)
                continue
            collaboration_v2_record = None
            if item_id is not None:
                collaboration_v2_record = self._collaboration_v2_calls.get(item_id)
            alias_record = self.plan.registry.record_for_alias(item.get("name"))
            is_collaboration_v2_item = (
                collaboration_v2_record is not None
                or (
                    item.get("type") == "function_call"
                    and (
                        (
                            item.get("namespace") == "collaboration"
                            and item.get("name") in _V2_NAMES
                        )
                        or (
                            alias_record is not None
                            and alias_record.version == "v2"
                            and alias_record.family == NAMESPACE
                        )
                    )
                )
            )
            if is_collaboration_v2_item:
                # Terminal output is not allowed to establish a new V2 call.
                # The call must have been established by ``output_item.added``
                # and completed by ``output_item.done`` first.
                canonical = self._validate_collaboration_v2_stream_call(
                    item_id or "",
                    item,
                )
                if item_id is None or collaboration_v2_record is None:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "missing_stream_identity",
                        surface="stream",
                    )
                expected_output_index = self._collaboration_v2_output_indices.get(item_id)
                if expected_output_index is None or output_index != expected_output_index:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_native_identity",
                        surface="stream",
                    )
                if item_id not in self._collaboration_v2_done:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "incomplete_stream",
                        surface="stream",
                    )
                if (
                    terminal_collaboration_v2_order
                    and self._collaboration_v2_output_indices[
                        terminal_collaboration_v2_order[-1]
                    ]
                    >= expected_output_index
                ):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_native_identity",
                        surface="stream",
                )
                expected_payload = self._wire_payloads.get(item_id)
                payload_owner = alias_record
                if payload_owner is None:
                    payload_owner = self._native_entry_for_item(canonical)
                if payload_owner is None:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_native_identity",
                        surface="stream",
                    )
                if expected_payload is not None and self._semantic_wire_payload(
                    canonical, payload_owner
                ) != expected_payload:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_native_identity",
                        surface="stream",
                    )
                terminal_collaboration_v2_order.append(item_id)
                continue
            pending = self._native_pending.get(item_id) if item_id is not None else None
            if pending is None:
                legacy_done = self._legacy_unowned_done.get(item_id) if item_id is not None else None
                if legacy_done is not None:
                    expected_call_id, expected_entry, expected_wire, expected_payload = legacy_done
                    if item.get("call_id") != expected_call_id:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_call_identity",
                            surface="stream",
                        )
                    if self._native_wire_identity(item) != expected_wire:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    if self._native_entry_for_item(item) is not expected_entry:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    self.plan._validate_native_item(item, expected_entry, surface="stream")
                    if self._semantic_wire_payload(item, expected_entry) != expected_payload:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    continue
                adapter_pending = self._pending.get(item_id) if item_id is not None else None
                if adapter_pending is not None:
                    if item.get("call_id") != adapter_pending.call_id:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="stream")
                    record = self.plan.registry.record_for_alias(item.get("name"))
                    if record is not adapter_pending.record:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="stream")
                    if (
                        adapter_pending.record.family == NAMESPACE
                        and item.get("namespace") not in {None, adapter_pending.record.namespace}
                    ):
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
                    if (
                        item_id in self._wire_payloads
                        and self._semantic_wire_payload(item, adapter_pending.record)
                        != self._wire_payloads[item_id]
                    ):
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
                    continue
                adapter_identity = self._adapter_wire_identities.get(item_id) if item_id is not None else None
                if adapter_identity is not None:
                    expected_call_id, expected_record, expected_wire = adapter_identity
                    if item.get("call_id") != expected_call_id:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_call_identity",
                            surface="stream",
                        )
                    if self._native_wire_identity(item) != expected_wire:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    if (
                        item_id in self._wire_payloads
                        and self._semantic_wire_payload(item, expected_record)
                        != self._wire_payloads[item_id]
                    ):
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
                    terminal_record = self.plan.registry.record_for_alias(item.get("name"))
                    if terminal_record is not expected_record:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                    )
                    continue
                opaque = self._opaque_pending.get(item_id) if item_id is not None else None
                if opaque is not None:
                    if item.get("call_id") != opaque.call_id:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_call_identity",
                            surface="stream",
                        )
                    if self._native_wire_identity(item) != opaque.wire_identity:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    if "input" in item:
                        terminal_input = item.get("input")
                    elif "arguments" in item:
                        terminal_input = item.get("arguments")
                    else:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "incomplete_stream_delta",
                            surface="stream",
                        )
                    payload = self._opaque_payload(terminal_input)
                    if (
                        item_id in self._wire_payloads
                        and self._wire_payloads[item_id] != payload
                    ):
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    continue
                if isinstance(item.get("call_id"), str) and any(
                    expected_call_id == item.get("call_id")
                    for expected_call_id, _entry, _wire, _payload in self._legacy_unowned_done.values()
                ):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                if isinstance(item.get("call_id"), str) and any(
                    expected_call_id == item.get("call_id")
                    for expected_call_id, _record, _wire in self._adapter_wire_identities.values()
                ):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                bound_record = self.plan.registry.record_for_call(item.get("call_id"))
                if bound_record is not None:
                    terminal_record = self.plan.registry.record_for_alias(item.get("name"))
                    if terminal_record is not bound_record:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_call_identity",
                            surface="stream",
                        )
                    # A buffered custom adapter is removed from _pending once
                    # its native events are emitted.  Its request-local call
                    # binding is still sufficient to verify the terminal
                    # alias, so let the exact match through.
                    continue
                if isinstance(item.get("call_id"), str) and any(
                    opaque.call_id == item.get("call_id")
                    for opaque in self._opaque_pending.values()
                ):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                # Adapter output is accounted for by the request-scoped alias
                # ledger; any other native-shaped item must match a stream
                # owner before it can appear in the terminal envelope.
                if self._native_entry_for_item(item) is not None:
                    # A complete body-style terminal envelope is valid even
                    # when the caller did not expose the preceding SSE
                    # ``added`` event.  Once this stream has a pending tool,
                    # however, a native item without its exact owner is a
                    # mismatched terminal identity.
                    if self._native_pending or self._pending:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                continue
            expected_call_id, expected_entry = pending
            native_entry = self._native_entry_for_item(item)
            if native_entry is not expected_entry:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_native_identity",
                    surface="stream",
                )
            if expected_entry.family == TOOL_SEARCH:
                self.plan._validate_native_item(item, expected_entry, surface="stream")
            if self._native_wire_identities.get(item_id) != self._native_wire_identity(item):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_native_identity",
                    surface="stream",
                )
            if (
                item_id in self._wire_payloads
                and self._semantic_wire_payload(item, expected_entry)
                != self._wire_payloads[item_id]
            ):
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
            if expected_call_id is not None and item.get("call_id") != expected_call_id:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_call_identity",
                    surface="stream",
                )
        missing = (
            required_native_ids
            | required_collaboration_v2_ids
            | required_adapter_ids
            | required_agent_message_ids
        ) - seen_output_ids
        if missing:
            raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream", surface="stream")
        if terminal_agent_message_order != self._agent_message_added_order:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface="stream",
            )
        if terminal_collaboration_v2_order != self._collaboration_v2_added_order:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface="stream",
            )

    def decode_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(event, Mapping):
            raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_stream_event", surface="stream")
        result = _copy_mapping(event)
        event_type = result.get("type")
        self._reject_after_terminal(event_type)
        if _is_unsupported_hosted_stream_event(event_type):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unsupported_hosted_lifecycle",
                surface="stream",
            )
        hosted_event_spec = _hosted_event_spec(event_type)
        if hosted_event_spec is not None:
            return self._decode_hosted_event(result, hosted_event_spec)
        item = result.get("item")
        if isinstance(item, Mapping) and item.get("type") == "agent_message":
            if self.plan._collaboration_v2_entry() is None:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unknown_native_identity",
                    surface="stream",
                )
            self.plan._validate_collaboration_v2_items([item], surface="stream")
            item_id = self._item_id(item)
            if item_id is None:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "missing_item_identity",
                    surface="stream",
                )
            if event_type == "response.output_item.added":
                if item_id in self._seen_item_ids:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "duplicate_item_identity",
                        surface="stream",
                    )
                output_index = self._agent_message_output_index(result)
                if (
                    output_index in self._agent_message_output_indices.values()
                    or (
                        self._agent_message_added_order
                        and output_index
                        <= self._agent_message_output_indices[
                            self._agent_message_added_order[-1]
                        ]
                    )
                ):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_native_identity",
                        surface="stream",
                    )
                self._seen_item_ids.add(item_id)
                self._agent_message_pending[item_id] = _freeze(item)
                self._agent_message_output_indices[item_id] = output_index
                self._agent_message_added_order.append(item_id)
                return result
            if event_type == "response.output_item.done":
                if item_id not in self._agent_message_pending:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "missing_stream_identity",
                        surface="stream",
                    )
                if item_id in self._agent_message_done:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "duplicate_item_identity",
                        surface="stream",
                    )
                output_index = self._agent_message_output_index(result)
                if output_index != self._agent_message_output_indices[item_id]:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_native_identity",
                        surface="stream",
                    )
                if (
                    self._agent_message_done_order
                    and output_index
                    <= self._agent_message_output_indices[
                        self._agent_message_done_order[-1]
                    ]
                ):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_native_identity",
                        surface="stream",
                    )
                if _freeze(item) != self._agent_message_pending[item_id]:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_native_identity",
                        surface="stream",
                    )
                self._agent_message_done.add(item_id)
                self._agent_message_done_order.append(item_id)
                return result
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "invalid_agent_message_lifecycle",
                surface="stream",
            )
        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            response = result.get("response")
            if isinstance(response, Mapping):
                self._validate_terminal_native_output(response)
            elif self._agent_message_pending:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "incomplete_stream",
                    surface="stream",
                )
            self._finish_terminal()
            if isinstance(response, Mapping):
                decoded_output, output_changed = self.plan._decode_items(
                    response.get("output"),
                    reject_omitted_response=True,
                )
                if output_changed:
                    response = _copy_mapping(response)
                    response["output"] = decoded_output
                    result["response"] = response
            return result
        if event_type == "response.output_item.added" and isinstance(item, Mapping):
            self.plan._validate_registered_item_identity(item, surface="stream")
            self._validate_output_item_added(item)
            self.plan._reject_unknown_standard_item(item, surface="stream")
            decoded_item, record, _changed = self._check_alias_in_item(item)
            item_id = self._item_id(item)
            if record is not None:
                if not item_id:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_item_identity", surface="stream")
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="stream")
                if item_id in self._seen_item_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="stream")
                self._seen_item_ids.add(item_id)
                if call_id in self._seen_call_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_call_identity", surface="stream")
                self._seen_call_ids.add(call_id)
                if self.plan.registry.record_for_call(call_id) is None:
                    self.plan.registry.bind_call(call_id, record.alias)
                self._pending[item_id] = _PendingStreamItem(record, item_id, call_id if isinstance(call_id, str) else None)
                self._adapter_wire_identities[item_id] = (
                    call_id,
                    record,
                    self._native_wire_identity(item),
                )
                if record.version == "v2" and record.family == NAMESPACE:
                    self.plan._validate_collaboration_v2_call_item(
                        decoded_item,
                        surface="stream",
                        allow_incomplete_arguments=True,
                    )
                    self._record_collaboration_v2_added(
                        item_id,
                        decoded_item,
                        result,
                    )
                result["item"] = decoded_item
                return result
            native_entry = self._native_entry_for_item(item)
            if native_entry is not None:
                item_id = self._item_id(item)
                call_id = item.get("call_id")
                if item_id is None:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_item_identity", surface="stream")
                if native_entry.family == SELECTED_PROVIDER_HOSTED:
                    self.plan._validate_native_item(item, native_entry, surface="stream")
                    if item_id in self._seen_item_ids:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="stream")
                    self._seen_item_ids.add(item_id)
                    self._native_pending[item_id] = (None, native_entry)
                    self._native_wire_identities[item_id] = self._native_wire_identity(item)
                    hosted_spec = _hosted_event_spec_for_declaration_kind(
                        native_entry.declaration.get("type")
                    )
                    if hosted_spec is None:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "unsupported_hosted_lifecycle",
                            surface="stream",
                        )
                    self._hosted_pending[item_id] = _HostedStreamState(
                        event_kind=hosted_spec[0],
                        stages=hosted_spec[1],
                    )
                    result["item"] = decoded_item
                    return result
                if not isinstance(call_id, str) or not call_id:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="stream")
                self.plan._validate_native_item(item, native_entry, surface="stream")
                if item_id in self._seen_item_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="stream")
                if call_id in self._seen_call_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_call_identity", surface="stream")
                if native_entry.version == "v2" and native_entry.family == NAMESPACE:
                    self.plan._validate_collaboration_v2_call_item(
                        item,
                        surface="stream",
                        allow_incomplete_arguments=True,
                    )
                self._seen_item_ids.add(item_id)
                self._seen_call_ids.add(call_id)
                self._native_pending[item_id] = (call_id, native_entry)
                self._native_wire_identities[item_id] = self._native_wire_identity(item)
                if native_entry.version == "v2" and native_entry.family == NAMESPACE:
                    self._record_collaboration_v2_added(item_id, item, result)
                result["item"] = decoded_item
            elif item.get("type") == "custom_tool_call":
                if self.plan._omitted_response_entry_for_item(item) is not None:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unsupported_hosted_lifecycle",
                        surface="stream",
                    )
                adapted_entry = (
                    self.plan._entry_for_name(
                        item.get("name"),
                        item.get("namespace"),
                        item_type=item.get("type"),
                    )
                    if self.plan.has_adaptations
                    else None
                )
                if adapted_entry is not None and adapted_entry.disposition == ADAPT:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unknown_alias",
                        surface="stream",
                    )
                self._record_opaque_added(item)
            elif self.plan.has_adaptations and item.get("type") == "function_call":
                adapted_entry = self.plan._entry_for_name(
                    item.get("name"),
                    item.get("namespace"),
                    item_type=item.get("type"),
                )
                if adapted_entry is not None and adapted_entry.disposition == ADAPT:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unknown_alias",
                        surface="stream",
                    )
            elif self.plan._omitted_response_entry_for_item(item) is not None:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unsupported_hosted_lifecycle",
                    surface="stream",
                )
            elif _hosted_kind_for_item_type(item.get("type")) is not None or (
                isinstance(item.get("type"), str)
                and item.get("type").endswith("_call")
                and item.get("type")
                not in {"function_call", "custom_tool_call", "tool_search_call"}
            ):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unsupported_hosted_lifecycle",
                    surface="stream",
                )
            return result
        if event_type in {"response.function_call_arguments.delta", "response.custom_tool_call_input.delta"}:
            item_id = self._item_id(result)
            if item_id in self._native_pending:
                expected_call_id, expected_entry = self._native_pending[item_id]
                if expected_entry.family == TOOL_SEARCH:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unsupported_hosted_lifecycle",
                        surface="stream",
                    )
                supplied_call_id = result.get("call_id")
                if supplied_call_id is not None and supplied_call_id != expected_call_id:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                if expected_entry.version == "v2" and expected_entry.family == NAMESPACE:
                    self._validate_collaboration_v2_event_index(item_id, result)
                delta = result.get("delta")
                if not isinstance(delta, str):
                    raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_stream_delta", surface="stream")
                if item_id in self._native_delta_done:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_stream_done", surface="stream")
                self._native_fragments.setdefault(item_id, []).append(delta)
                return result
            opaque = self._opaque_pending.get(item_id) if item_id is not None else None
            if opaque is not None:
                supplied_call_id = result.get("call_id")
                if supplied_call_id is not None and supplied_call_id != opaque.call_id:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                if opaque.arguments_done or opaque.item_done:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "duplicate_stream_done",
                        surface="stream",
                    )
                delta = result.get("delta")
                if not isinstance(delta, str):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "malformed_stream_delta",
                        surface="stream",
                    )
                opaque.fragments.append(delta)
                return result
            if item_id not in self._pending:
                raise ToolCompatibilityError("tool_compatibility_boundary", "missing_stream_identity", surface="stream")
            pending = self._pending_for(result)
            if isinstance(result.get("call_id"), str) and result.get("call_id") != pending.call_id:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="stream")
            if pending.record.version == "v2" and pending.record.family == NAMESPACE:
                self._validate_collaboration_v2_event_index(item_id, result)
            delta = result.get("delta")
            if not isinstance(delta, str):
                raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_stream_delta", surface="stream")
            pending.fragments.append(delta)
            if pending.record.family == CUSTOM_FREEFORM:
                result["type"] = "response.custom_tool_call_input.delta"
            return result
        if event_type in {"response.function_call_arguments.done", "response.custom_tool_call_input.done"}:
            item_id = self._item_id(result)
            if item_id in self._native_pending:
                expected_call_id, expected_entry = self._native_pending[item_id]
                if expected_entry.family == TOOL_SEARCH:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unsupported_hosted_lifecycle",
                        surface="stream",
                    )
                supplied_call_id = result.get("call_id")
                if supplied_call_id is not None and supplied_call_id != expected_call_id:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                if expected_entry.version == "v2" and expected_entry.family == NAMESPACE:
                    self._validate_collaboration_v2_event_index(item_id, result)
                if item_id in self._native_delta_done:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_stream_done", surface="stream")
                arguments = result.get("arguments", result.get("input"))
                if not isinstance(arguments, str):
                    raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream_delta", surface="stream")
                fragments = self._native_fragments.get(item_id, [])
                if fragments and "".join(fragments) != arguments:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream_delta", surface="stream")
                if expected_entry.version == "v2" and expected_entry.family == NAMESPACE:
                    complete_item = _copy_mapping(
                        self._collaboration_v2_calls.get(item_id, {})
                    )
                    complete_item["arguments"] = arguments
                    self._validate_collaboration_v2_stream_call(
                        item_id,
                        complete_item,
                    )
                payload_item = (
                    {"type": "custom_tool_call", "input": result.get("input", arguments)}
                    if expected_entry.family == CUSTOM_FREEFORM
                    else {"type": "function_call", "arguments": arguments}
                )
                payload = self._semantic_wire_payload(payload_item, expected_entry)
                if item_id in self._wire_payloads and self._wire_payloads[item_id] != payload:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
                self._wire_payloads[item_id] = payload
                self._native_delta_done.add(item_id)
                return result
            opaque = self._opaque_pending.get(item_id) if item_id is not None else None
            if opaque is not None:
                supplied_call_id = result.get("call_id")
                if supplied_call_id is not None and supplied_call_id != opaque.call_id:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                if opaque.arguments_done:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "duplicate_stream_done",
                        surface="stream",
                    )
                arguments = result.get("arguments", result.get("input"))
                if not isinstance(arguments, str):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "incomplete_stream_delta",
                        surface="stream",
                    )
                fragments = "".join(opaque.fragments)
                if fragments and not arguments.startswith(fragments):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "incomplete_stream_delta",
                        surface="stream",
                    )
                opaque.arguments_done = True
                self._wire_payloads[item_id] = self._opaque_payload(arguments)
                return result
            if item_id not in self._pending:
                raise ToolCompatibilityError("tool_compatibility_boundary", "missing_stream_identity", surface="stream")
            pending = self._pending_for(result)
            if isinstance(result.get("call_id"), str) and result.get("call_id") != pending.call_id:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="stream")
            if pending.record.version == "v2" and pending.record.family == NAMESPACE:
                self._validate_collaboration_v2_event_index(item_id, result)
            if pending.delta_done:
                raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_stream_done", surface="stream")
            arguments = result.get("arguments", result.get("input"))
            if not isinstance(arguments, str):
                arguments = "".join(pending.fragments)
            elif pending.fragments and "".join(pending.fragments) != arguments:
                raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream_delta", surface="stream")
            if not arguments:
                raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream_delta", surface="stream")
            if pending.record.version == "v2" and pending.record.family == NAMESPACE:
                complete_item = {
                    "type": "function_call",
                    "id": item_id,
                    "call_id": pending.call_id,
                    "name": pending.record.alias,
                    "arguments": arguments,
                }
                self._validate_collaboration_v2_stream_call(
                    item_id,
                    complete_item,
                )
            pending.delta_done = True
            if pending.record.family == CUSTOM_FREEFORM:
                envelope = _json_object_exact(arguments)
                result["type"] = "response.custom_tool_call_input.done"
                result["input"] = envelope[CUSTOM_INPUT_KEY]
                result.pop("arguments", None)
            else:
                result["arguments"] = arguments
            payload_item = (
                {"type": "custom_tool_call", "input": result.get("input")}
                if pending.record.family == CUSTOM_FREEFORM
                else {"type": "function_call", "arguments": arguments}
            )
            payload = self._semantic_wire_payload(payload_item, pending.record)
            if item_id in self._wire_payloads and self._wire_payloads[item_id] != payload:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
            self._wire_payloads[item_id] = payload
            return result
        if event_type == "response.output_item.done" and isinstance(item, Mapping):
            # A legacy provider may omit the ``added`` lifecycle and emit the
            # adapted client-owned search as a plain function call.  Decode
            # that exact spelling before the normal registry/stream-owner
            # checks so the client still receives the native terminal search
            # item and the provider cannot claim an arbitrary function.
            legacy_record = self.plan._legacy_tool_search_record(item)
            if legacy_record is not None:
                decoded_item, _record, _changed = self.plan._decode_call_compat(
                    item,
                    allow_incomplete=False,
                )
                entry = next(
                    (
                        candidate
                        for candidate in self.plan.entries
                        if candidate.declaration_index == legacy_record.declaration_index
                    ),
                    None,
                )
                if entry is None:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unknown_alias",
                        surface="stream",
                    )
                self._record_legacy_unowned_native_done(decoded_item, entry)
                result["item"] = decoded_item
                return result
            self.plan._validate_registered_item_identity(item, surface="stream")
            self._validate_output_item_added(item)
            self.plan._reject_unknown_standard_item(item, surface="stream")
            record = self.plan.registry.record_for_alias(item.get("name"))
            if record is None:
                if self.plan.registry.looks_like_alias(item.get("name")):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unknown_alias",
                        surface="stream",
                    )
                native_item_id = self._item_id(item)
                opaque = self._opaque_pending.get(native_item_id) if native_item_id else None
                if opaque is not None:
                    if item.get("call_id") != opaque.call_id:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_call_identity",
                            surface="stream",
                        )
                    if self._native_wire_identity(item) != opaque.wire_identity:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    if not opaque.arguments_done:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "incomplete_stream_delta",
                            surface="stream",
                        )
                    if opaque.item_done:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "duplicate_item_identity",
                            surface="stream",
                        )
                    if "input" in item:
                        terminal_input = item.get("input")
                    elif "arguments" in item:
                        terminal_input = item.get("arguments")
                    else:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "incomplete_stream_delta",
                            surface="stream",
                        )
                    payload = self._opaque_payload(terminal_input)
                    if (
                        native_item_id in self._wire_payloads
                        and self._wire_payloads[native_item_id] != payload
                    ):
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    opaque.item_done = True
                    self._wire_payloads[native_item_id] = payload
                    return result
                native_pending = self._native_pending.get(native_item_id) if native_item_id else None
                if native_pending is not None:
                    call_id = item.get("call_id")
                    if call_id != native_pending[0]:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="stream")
                    expected_entry = native_pending[1]
                    if self._native_wire_identities.get(native_item_id) != self._native_wire_identity(item):
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    is_v2_call = (
                        expected_entry.version == "v2"
                        and expected_entry.family == NAMESPACE
                    )
                    if is_v2_call:
                        output_index = self._collaboration_v2_output_index(result)
                        expected_output_index = self._collaboration_v2_output_indices.get(
                            native_item_id
                        )
                        if (
                            expected_output_index is None
                            or output_index != expected_output_index
                            or native_item_id in self._collaboration_v2_done
                            or (
                                self._collaboration_v2_done_order
                                and output_index
                                <= self._collaboration_v2_output_indices[
                                    self._collaboration_v2_done_order[-1]
                                ]
                            )
                        ):
                            raise ToolCompatibilityError(
                                "tool_compatibility_boundary",
                                "ambiguous_native_identity",
                                surface="stream",
                            )
                        self._validate_collaboration_v2_stream_call(
                            native_item_id,
                            item,
                        )
                    if expected_entry.family == SELECTED_PROVIDER_HOSTED:
                        hosted_entry = self._native_entry_for_item(item)
                        if hosted_entry is None or hosted_entry is not expected_entry:
                            raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
                        hosted_state = self._hosted_pending.get(native_item_id)
                        if hosted_state is None or hosted_state.next_stage != len(hosted_state.stages):
                            raise ToolCompatibilityError(
                                "tool_compatibility_boundary",
                                "incomplete_hosted_lifecycle",
                                surface="stream",
                            )
                        self.plan._validate_native_item(
                            item,
                            hosted_entry,
                            require_completed=True,
                            surface="stream",
                        )
                    elif expected_entry.family == TOOL_SEARCH:
                        self.plan._validate_native_item(item, expected_entry, surface="stream")
                    elif expected_entry.family != TOOL_SEARCH and native_item_id not in self._native_delta_done:
                        self._complete_native_arguments_from_item(
                            native_item_id,
                            item,
                            expected_entry,
                        )
                    if native_item_id in self._native_done:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="stream")
                    payload = self._semantic_wire_payload(item, expected_entry)
                    if native_item_id in self._wire_payloads and self._wire_payloads[native_item_id] != payload:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
                    self._wire_payloads[native_item_id] = payload
                    self._native_done.add(native_item_id)
                    if is_v2_call:
                        self._collaboration_v2_done.add(native_item_id)
                        self._collaboration_v2_done_order.append(native_item_id)
                    return result
                native_entry = self._native_entry_for_item(item)
                if native_entry is not None:
                    if (
                        native_entry.family == TOOL_SEARCH
                        and item.get("type") == "tool_search_call"
                    ):
                        self._record_legacy_unowned_native_done(item, native_entry)
                        return result
                    if (
                        native_entry.family == PLAIN_FUNCTION
                        and native_entry.original_name == "tool_search"
                        and item.get("type") == "function_call"
                        and item.get("name") == "tool_search"
                        and item.get("namespace") is None
                    ):
                        self._record_legacy_unowned_native_done(item, native_entry)
                        return result
                    if (
                        native_entry.family == PLAIN_FUNCTION
                        and is_legacy_flattened_spawn(item, native_entry.original_name)
                    ):
                        # A legacy worker-selector stream can emit this
                        # flattened native item without an ``added`` event.
                        # Keep this one compatibility passthrough narrow; a
                        # plain unqualified native call still requires an
                        # established stream owner.
                        self._record_legacy_unowned_native_done(item, native_entry)
                        return result
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_stream_identity", surface="stream")
                if item.get("type") == "custom_tool_call":
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "missing_stream_identity",
                        surface="stream",
                    )
                if self.plan._omitted_response_entry_for_item(item) is not None:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unsupported_hosted_lifecycle",
                        surface="stream",
                    )
                if _hosted_kind_for_item_type(item.get("type")) is not None or (
                    isinstance(item.get("type"), str)
                    and item.get("type").endswith("_call")
                    and item.get("type")
                    not in {"function_call", "custom_tool_call", "tool_search_call"}
                ):
                    raise ToolCompatibilityError("tool_compatibility_boundary", "unsupported_hosted_lifecycle", surface="stream")
                # A Responses stream can contain ordinary assistant messages
                # (and other non-tool output items) alongside adapted tool
                # lifecycles.  Their ``output_item.done`` event is complete
                # on its own and has no runtime-tool owner to reconcile.
                # Only reject an otherwise-unowned item when it is a
                # tool-shaped lifecycle that could cross the compatibility
                # boundary ambiguously.
                if native_item_id is not None and self.plan.has_adaptations and (
                    isinstance(item.get("type"), str)
                    and (
                        item.get("type") in {"function_call", "custom_tool_call", "tool_search_call"}
                        or item.get("type").endswith("_call")
                        or item.get("type").endswith("_call_output")
                    )
                ):
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_stream_identity", surface="stream")
                return result
            pending = self._pending_for(item)
            if (
                pending.record.family == CUSTOM_FREEFORM
                and self._native_wire_identity(item)
                != self._adapter_wire_identities[pending.item_id][2]
            ):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_native_identity",
                    surface="stream",
                )
            if item.get("call_id") != pending.call_id:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="stream")
            if pending.item_done:
                raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="stream")
            if pending.record.family == CUSTOM_FREEFORM:
                decoded_item, _record, _changed = self.plan._decode_call(item, allow_incomplete=False)
            else:
                decoded_item, _record, _changed = self._check_alias_in_item(item, allow_incomplete=False)
            is_v2_call = pending.record.version == "v2" and pending.record.family == NAMESPACE
            if is_v2_call:
                output_index = self._collaboration_v2_output_index(result)
                expected_output_index = self._collaboration_v2_output_indices.get(
                    pending.item_id
                )
                if (
                    expected_output_index is None
                    or output_index != expected_output_index
                    or pending.item_id in self._collaboration_v2_done
                    or (
                        self._collaboration_v2_done_order
                        and output_index
                        <= self._collaboration_v2_output_indices[
                            self._collaboration_v2_done_order[-1]
                        ]
                    )
                ):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_native_identity",
                        surface="stream",
                    )
                decoded_item = self._validate_collaboration_v2_stream_call(
                    pending.item_id,
                    decoded_item,
                )
            pending.item_done = True
            payload = self._semantic_wire_payload(item, pending.record)
            if pending.item_id in self._wire_payloads and self._wire_payloads[pending.item_id] != payload:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
            self._wire_payloads[pending.item_id] = payload
            if is_v2_call:
                self._collaboration_v2_done.add(pending.item_id)
                self._collaboration_v2_done_order.append(pending.item_id)
            result["item"] = decoded_item
            return result
        if isinstance(item, Mapping) and self.plan.registry.looks_like_alias(item.get("name")):
            decoded_item, _record, _changed = self._check_alias_in_item(item)
            result["item"] = decoded_item
        return result

    @staticmethod
    def _stream_error(classification: str) -> ToolCompatibilityError:
        return ToolCompatibilityError(
            "tool_compatibility_boundary",
            classification,
            surface="stream",
        )

    def _reject_after_terminal(self, event_type: Any) -> None:
        if self._terminal and event_type not in self._TERMINAL_EVENT_TYPES:
            raise self._stream_error("stream_after_terminal")

    @staticmethod
    def _native_custom_item(
        item: Mapping[str, Any],
        record: AliasRecord,
        native_input: str,
    ) -> dict[str, Any]:
        result = _copy_mapping(item)
        result["type"] = "custom_tool_call"
        result["name"] = record.original_name
        result["input"] = native_input
        result.pop("arguments", None)
        return result

    @staticmethod
    def _native_tool_search_item(
        item: Mapping[str, Any],
        native_arguments: Any,
    ) -> dict[str, Any]:
        """Convert an adapted function-call item back to tool_search."""
        result = _copy_mapping(item)
        result["type"] = "tool_search_call"
        result.pop("name", None)
        result.pop("namespace", None)
        # ``status`` is a Chat/Responses function-call field.  Codex CLI's
        # native client-owned tool_search item does not carry it; retaining
        # the adapted transport marker makes the item fail the native shape
        # check before MCP discovery.
        result.pop("status", None)
        result["execution"] = "client"
        result["arguments"] = {} if native_arguments is None else _thaw(native_arguments)
        return result

    def decode_events_for_event(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(event, Mapping):
            raise self._stream_error("malformed_stream_event")
        value = _copy_mapping(event)
        event_type = value.get("type")
        self._reject_after_terminal(event_type)
        item = value.get("item")

        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            if self._buffered_custom or self._buffered_tool_search:
                raise self._stream_error("incomplete_stream")
            return [self.decode_event(value)]

        if event_type == "response.output_item.added" and isinstance(item, Mapping):
            self.plan._validate_registered_item_identity(item, surface="stream")
            self.plan._reject_unknown_standard_item(item, surface="stream")
            record = self.plan.registry.record_for_alias(item.get("name"))
            if record is None:
                record = self.plan._legacy_tool_search_record(item)
            if record is None or record.family not in {CUSTOM_FREEFORM, TOOL_SEARCH}:
                return [self.decode_event(value)]
            if record.family == CUSTOM_FREEFORM:
                if item.get("type") != "function_call":
                    raise self._stream_error("ambiguous_call_identity")
                item_id = self._item_id(item)
                call_id = item.get("call_id")
                if (
                    not item_id
                    or not isinstance(call_id, str)
                    or not call_id
                    or item_id in self._seen_item_ids
                    or call_id in self._seen_call_ids
                    or item.get("arguments") not in {None, ""}
                    or item.get("namespace") is not None
                ):
                    raise self._stream_error("invalid_custom_stream_identity")
                self._seen_item_ids.add(item_id)
                self._seen_call_ids.add(call_id)
                if self.plan.registry.record_for_call(call_id) is None:
                    self.plan.registry.bind_call(call_id, record.alias)
                self._buffered_custom[item_id] = _BufferedCustomStreamItem(
                    record=record,
                    added_event=value,
                    item_id=item_id,
                    call_id=call_id,
                )
                self._adapter_wire_identities[item_id] = (
                    call_id,
                    record,
                    self._native_wire_identity(item),
                )
                return []

        if event_type == "response.output_item.added" and isinstance(item, Mapping):
            record = self.plan.registry.record_for_alias(item.get("name"))
            if record is None:
                legacy_entry = self.plan._legacy_tool_search_record(item)
                if legacy_entry is not None and legacy_entry.aliases:
                    record = self.plan.registry.record_for_alias(legacy_entry.aliases[0])
            if record is not None and record.family == TOOL_SEARCH:
                if item.get("type") != "function_call":
                    raise self._stream_error("ambiguous_call_identity")
                item_id = self._item_id(item)
                call_id = item.get("call_id")
                if (
                    not item_id
                    or not isinstance(call_id, str)
                    or not call_id
                    or item_id in self._seen_item_ids
                    or call_id in self._seen_call_ids
                    or item.get("arguments") not in {None, ""}
                    or item.get("namespace") is not None
                ):
                    raise self._stream_error("invalid_tool_search_stream_identity")
                self._seen_item_ids.add(item_id)
                self._seen_call_ids.add(call_id)
                if self.plan.registry.record_for_call(call_id) is None:
                    self.plan.registry.bind_call(call_id, record.alias)
                self._buffered_tool_search[item_id] = _BufferedToolSearchStreamItem(
                    record=record,
                    added_event=value,
                    item_id=item_id,
                    call_id=call_id,
                )
                self._adapter_wire_identities[item_id] = (
                    call_id,
                    record,
                    self._native_wire_identity(item),
                )
                return []

        item_id = self._item_id(value)
        pending = self._buffered_custom.get(item_id) if item_id else None
        if event_type == "response.function_call_arguments.delta" and pending is not None:
            supplied_call_id = value.get("call_id")
            if supplied_call_id is not None and supplied_call_id != pending.call_id:
                raise self._stream_error("ambiguous_call_identity")
            delta = value.get("delta")
            if not isinstance(delta, str) or pending.arguments_done_event is not None:
                raise self._stream_error("malformed_stream_delta")
            pending.fragments.append(delta)
            return []

        if event_type == "response.function_call_arguments.done" and pending is not None:
            supplied_call_id = value.get("call_id")
            if supplied_call_id is not None and supplied_call_id != pending.call_id:
                raise self._stream_error("ambiguous_call_identity")
            if pending.arguments_done_event is not None:
                raise self._stream_error("duplicate_stream_done")
            arguments = value.get("arguments")
            if not isinstance(arguments, str) or (
                pending.fragments and "".join(pending.fragments) != arguments
            ):
                raise self._stream_error("incomplete_stream_delta")
            try:
                envelope = _json_object_exact(arguments)
            except ToolCompatibilityError as exc:
                raise self._stream_error(exc.classification) from exc
            native_input = envelope[CUSTOM_INPUT_KEY]
            if not isinstance(native_input, str):
                raise self._stream_error("malformed_stream_custom_input")
            pending.arguments_done_event = value
            pending.native_input = native_input
            return []

        search_pending = self._buffered_tool_search.get(item_id) if item_id else None
        if event_type == "response.function_call_arguments.delta" and search_pending is not None:
            supplied_call_id = value.get("call_id")
            if supplied_call_id is not None and supplied_call_id != search_pending.call_id:
                raise self._stream_error("ambiguous_call_identity")
            delta = value.get("delta")
            if not isinstance(delta, str) or search_pending.arguments_done_event is not None:
                raise self._stream_error("malformed_stream_delta")
            search_pending.fragments.append(delta)
            return []

        if event_type == "response.function_call_arguments.done" and search_pending is not None:
            supplied_call_id = value.get("call_id")
            if supplied_call_id is not None and supplied_call_id != search_pending.call_id:
                raise self._stream_error("ambiguous_call_identity")
            if search_pending.arguments_done_event is not None:
                raise self._stream_error("duplicate_stream_done")
            arguments = value.get("arguments")
            if not isinstance(arguments, str) or (
                search_pending.fragments and "".join(search_pending.fragments) != arguments
            ):
                raise self._stream_error("incomplete_stream_delta")
            try:
                envelope = _json_object_with_key(arguments, TOOL_SEARCH_INPUT_KEY)
            except ToolCompatibilityError as exc:
                # Legacy providers use the original ``tool_search`` name and
                # send the native query object directly instead of the
                # adapter envelope.  Accept only that request-bound spelling;
                # an aliased function must still carry the exact envelope.
                added_item = search_pending.added_event.get("item")
                legacy_wire = isinstance(added_item, Mapping) and added_item.get("name") == "tool_search"
                if not legacy_wire:
                    raise self._stream_error(exc.classification) from exc
                try:
                    native_arguments = json.loads(arguments)
                except (TypeError, ValueError) as parse_error:
                    raise self._stream_error("malformed_envelope") from parse_error
                if not isinstance(native_arguments, Mapping):
                    raise self._stream_error("invalid_envelope")
                envelope = {TOOL_SEARCH_INPUT_KEY: native_arguments}
            search_pending.arguments_done_event = value
            search_pending.native_arguments = envelope[TOOL_SEARCH_INPUT_KEY]
            # Preserve the legacy provider's plain arguments-done event.  The
            # outer bounded-query guard can then suppress only the repeated
            # query while allowing a distinct search to remain visible; the
            # adapted alias envelope itself is still kept provider-local.
            added_item = search_pending.added_event.get("item")
            if isinstance(added_item, Mapping) and added_item.get("name") == "tool_search":
                return [value]
            return []

        if event_type == "response.output_item.done" and isinstance(item, Mapping):
            done_item_id = self._item_id(item)
            pending = self._buffered_custom.get(done_item_id) if done_item_id else None
            if pending is not None:
                expected_wire = self._adapter_wire_identities.get(pending.item_id)
                if (
                    expected_wire is None
                    or self._native_wire_identity(item) != expected_wire[2]
                ):
                    raise self._stream_error("ambiguous_native_identity")
                if (
                    pending.arguments_done_event is None
                    or pending.native_input is None
                    or item.get("call_id") != pending.call_id
                    or item.get("name") != pending.record.alias
                    or item.get("arguments") != pending.arguments_done_event.get("arguments")
                ):
                    raise self._stream_error("incomplete_stream")

                added_event = _copy_mapping(pending.added_event)
                added_event["item"] = self._native_custom_item(
                    added_event["item"],
                    pending.record,
                    "",
                )

                delta_event = _copy_mapping(pending.arguments_done_event)
                delta_event["type"] = "response.custom_tool_call_input.delta"
                delta_event["delta"] = pending.native_input
                delta_event.pop("arguments", None)

                input_done_event = _copy_mapping(pending.arguments_done_event)
                input_done_event["type"] = "response.custom_tool_call_input.done"
                input_done_event["input"] = pending.native_input
                input_done_event.pop("arguments", None)

                value["item"] = self._native_custom_item(
                    item,
                    pending.record,
                    pending.native_input,
                )
                self._wire_payloads[pending.item_id] = self._semantic_wire_payload(item, pending.record)
                del self._buffered_custom[pending.item_id]
                return [added_event, delta_event, input_done_event, value]

        if event_type == "response.output_item.done" and isinstance(item, Mapping):
            done_item_id = self._item_id(item)
            search_pending = self._buffered_tool_search.get(done_item_id) if done_item_id else None
            if search_pending is None:
                return [self.decode_event(value)]
            expected_wire = self._adapter_wire_identities.get(search_pending.item_id)
            if (
                expected_wire is None
                or self._native_wire_identity(item) != expected_wire[2]
                or search_pending.arguments_done_event is None
                or item.get("call_id") != search_pending.call_id
                or item.get("name") not in {search_pending.record.alias, "tool_search"}
                or item.get("arguments") != search_pending.arguments_done_event.get("arguments")
            ):
                raise self._stream_error("incomplete_stream")
            native_added = _copy_mapping(search_pending.added_event)
            native_added.pop("output_index", None)
            native_added["item"] = self._native_tool_search_item(
                native_added["item"],
                search_pending.native_arguments,
            )
            value = _copy_mapping(value)
            value.pop("output_index", None)
            value["item"] = self._native_tool_search_item(item, search_pending.native_arguments)
            self._wire_payloads[search_pending.item_id] = self._semantic_wire_payload(
                item,
                search_pending.record,
            )
            self._native_done.add(search_pending.item_id)
            del self._buffered_tool_search[search_pending.item_id]
            # Codex CLI 0.146 consumes the client-owned search lifecycle as
            # a terminal output item.  The adapted function envelope is an
            # upstream-only transport detail; forwarding an ``added`` event
            # causes the native client to treat the search call as an
            # ordinary provider function and skip MCP discovery.  Preserve
            # the state transition above, but expose only the native terminal
            # item downstream.
            return [value]

        return [self.decode_event(value)]

    def decode_events(self, events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        decoded: list[dict[str, Any]] = []
        for event in events:
            decoded.extend(self.decode_events_for_event(event))
        return decoded


