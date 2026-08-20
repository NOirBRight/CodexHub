"""Gateway apply_patch request/history/response/stream adapter.

This module owns third-party freeform ``apply_patch`` adaptation: custom-tool
history reconstruction, argument/input normalization, response-body rewriting,
stream event adaptation, and fail-closed validation. It is deliberately
independent of ``codex_proxy``; the facade supplies telemetry through a typed
``ApplyPatchAdapter``.

Generic tool-surface adaptation stays in ``tool_surface_adapter``. Collaboration
V1/V2 worker lifecycle stays in ``collaboration_adapter``. The facade wires
those adapters as dependencies/hooks rather than duplicating them here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NoReturn, Protocol

from gateway_errors import UpstreamProtocolTranslationError
from protocol_translation import UnsupportedProtocolTranslationError
from tool_surface_adapter import APPLY_PATCH_FUNCTION_NAME


APPLY_PATCH_ADAPTER_EVENT = "third_party_apply_patch_freeform_adapter"
APPLY_PATCH_ADAPTER_ERROR_CODE = "invalid_apply_patch_function_call"
APPLY_PATCH_FUNCTION_CALL_FIELDS = frozenset(
    {"id", "type", "status", "call_id", "name", "arguments"}
)
APPLY_PATCH_HISTORY_ADAPTER_EVENT = "third_party_apply_patch_freeform_history_adapter"
APPLY_PATCH_CUSTOM_TOOL_HISTORY_CALL_FIELDS = frozenset(
    {"type", "status", "call_id", "name", "input"}
)
APPLY_PATCH_CUSTOM_TOOL_HISTORY_OUTPUT_FIELDS = frozenset(
    {"type", "call_id", "output"}
)
APPLY_PATCH_CUSTOM_TOOL_HISTORY_NATIVE_FIELDS = frozenset({"id"})
APPLY_PATCH_ENABLED_CONTEXT_FIELD = "_apply_patch_adapter_enabled"
DEFAULT_TERMINAL_EVENT_TYPES = frozenset(
    {
        "response.completed",
        "response.failed",
        "response.incomplete",
        "error",
    }
)


class AdapterEventWriter(Protocol):
    def __call__(
        self,
        event_context: Mapping[str, Any] | None,
        event: str,
        **fields: Any,
    ) -> None: ...


class _ApplyPatchAdapterFailure(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _is_apply_patch_function_call(item: Any, function_name: str) -> bool:
    return (
        isinstance(item, Mapping)
        and item.get("type") == "function_call"
        and item.get("name") == function_name
    )


def _is_apply_patch_custom_tool_call(item: Any, function_name: str) -> bool:
    return (
        isinstance(item, Mapping)
        and item.get("type") == "custom_tool_call"
        and item.get("name") == function_name
    )


def _require_exact_apply_patch_function_call_fields(
    item: Mapping[str, Any],
    required_fields: frozenset[str],
) -> None:
    if set(item) != required_fields:
        raise _ApplyPatchAdapterFailure("function_call_fields_not_exact")


def _apply_patch_arguments_text_and_input(arguments: Any) -> tuple[str, str]:
    def unique_object(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, value in pairs:
            if not isinstance(key, str) or key in parsed:
                raise _ApplyPatchAdapterFailure("duplicate_argument_key")
            parsed[key] = value
        return parsed

    def reject_json_constant(_: str) -> None:
        raise _ApplyPatchAdapterFailure("invalid_arguments")

    if isinstance(arguments, Mapping):
        parsed = dict(arguments)
        arguments_text = json.dumps(parsed, ensure_ascii=True, separators=(",", ":"))
    elif isinstance(arguments, str):
        arguments_text = arguments
        try:
            parsed = json.loads(
                arguments,
                object_pairs_hook=unique_object,
                parse_constant=reject_json_constant,
            )
        except _ApplyPatchAdapterFailure:
            raise
        except (TypeError, ValueError):
            raise _ApplyPatchAdapterFailure("invalid_arguments") from None
    else:
        raise _ApplyPatchAdapterFailure("missing_arguments")

    if not isinstance(parsed, dict) or set(parsed) != {"patch"}:
        raise _ApplyPatchAdapterFailure("arguments_not_exact")
    patch = parsed.get("patch")
    if not isinstance(patch, str):
        raise _ApplyPatchAdapterFailure("patch_not_string")
    if not patch.strip():
        raise _ApplyPatchAdapterFailure("patch_empty")
    return arguments_text, patch


def _apply_patch_item_identity(item: Mapping[str, Any]) -> tuple[str, str]:
    item_id = item.get("id")
    call_id = item.get("call_id")
    if not isinstance(item_id, str) or not item_id:
        raise _ApplyPatchAdapterFailure("missing_item_id")
    if not isinstance(call_id, str) or not call_id:
        raise _ApplyPatchAdapterFailure("missing_call_id")
    return item_id, call_id


def _custom_apply_patch_item(item: Mapping[str, Any], patch: str) -> dict[str, Any]:
    rewritten = dict(item)
    rewritten["type"] = "custom_tool_call"
    rewritten["input"] = patch
    rewritten.pop("arguments", None)
    return rewritten


def _has_exact_apply_patch_custom_tool_history_fields(
    item: Mapping[str, Any],
    required_fields: frozenset[str],
    native_fields: frozenset[str],
) -> bool:
    fields = set(item)
    if fields == required_fields:
        return True
    return (
        fields == required_fields | native_fields
        and isinstance(item.get("id"), str)
        and bool(item["id"])
    )


@dataclass(frozen=True)
class ApplyPatchFacts:
    """Immutable apply_patch adapter constants for one adapter."""

    function_name: str = APPLY_PATCH_FUNCTION_NAME
    adapter_event: str = APPLY_PATCH_ADAPTER_EVENT
    adapter_error_code: str = APPLY_PATCH_ADAPTER_ERROR_CODE
    function_call_fields: frozenset[str] = APPLY_PATCH_FUNCTION_CALL_FIELDS
    history_adapter_event: str = APPLY_PATCH_HISTORY_ADAPTER_EVENT
    custom_tool_history_call_fields: frozenset[str] = APPLY_PATCH_CUSTOM_TOOL_HISTORY_CALL_FIELDS
    custom_tool_history_output_fields: frozenset[str] = APPLY_PATCH_CUSTOM_TOOL_HISTORY_OUTPUT_FIELDS
    custom_tool_history_native_fields: frozenset[str] = APPLY_PATCH_CUSTOM_TOOL_HISTORY_NATIVE_FIELDS
    enabled_context_field: str = APPLY_PATCH_ENABLED_CONTEXT_FIELD
    terminal_event_types: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_TERMINAL_EVENT_TYPES)
    )


@dataclass(frozen=True)
class ApplyPatchAdapter:
    """Typed apply_patch request/history/response/stream seam."""

    facts: ApplyPatchFacts
    write_event: AdapterEventWriter

    def enabled(self, event_context: Mapping[str, Any] | None) -> bool:
        return not bool(
            event_context and event_context.get(self.facts.enabled_context_field) is False
        )

    def is_function_call(self, item: Any) -> bool:
        return _is_apply_patch_function_call(item, self.facts.function_name)

    def is_custom_tool_call(self, item: Any) -> bool:
        return _is_apply_patch_custom_tool_call(item, self.facts.function_name)

    def write_adapter_event(
        self,
        event_context: Mapping[str, Any] | None,
        *,
        surface: str,
        outcome: str,
        count: int = 1,
        reason: str | None = None,
    ) -> None:
        fields: dict[str, Any] = {"surface": surface, "outcome": outcome, "count": count}
        if reason is not None:
            fields["reason"] = reason
        self.write_event(event_context, self.facts.adapter_event, **fields)

    def raise_adapter_failure(
        self,
        event_context: Mapping[str, Any] | None,
        *,
        surface: str,
        reason: str,
    ) -> NoReturn:
        self.write_adapter_event(
            event_context,
            surface=surface,
            outcome="rejected",
            reason=reason,
        )
        raise UpstreamProtocolTranslationError(
            UnsupportedProtocolTranslationError(
                self.facts.adapter_error_code,
                "Third-party apply_patch function call is not an exact freeform patch invocation.",
            )
        )

    def write_history_event(
        self,
        event_context: Mapping[str, Any] | None,
        *,
        outcome: str,
        count: int = 1,
    ) -> None:
        """Emit count-only telemetry for the request-history inverse adapter."""
        self.write_event(
            event_context,
            self.facts.history_adapter_event,
            outcome=outcome,
            count=count,
        )

    def raise_history_failure(
        self,
        event_context: Mapping[str, Any] | None,
    ) -> NoReturn:
        self.write_history_event(event_context, outcome="rejected")
        raise UpstreamProtocolTranslationError(
            UnsupportedProtocolTranslationError(
                self.facts.adapter_error_code,
                "Third-party apply_patch custom-tool history is not an exact completed pair.",
            )
        )

    def adapt_custom_tool_history(
        self,
        input_items: list[Any],
        *,
        event_context: Mapping[str, Any] | None,
    ) -> tuple[list[Any], set[str], bool]:
        """Reconstruct exact structured apply_patch history for third-party tools.

        The downstream Codex client represents freeform ``apply_patch`` calls as a
        custom-tool call/result pair.  Structured third-party Responses providers
        need that one completed pair in function-call form to retain the call/result
        relationship.  All other custom-tool history stays on the pre-existing
        compatibility path.
        """
        if not self.enabled(event_context):
            return input_items, set(), False

        rewritten_items: list[Any] = []
        pending_call_ids: set[str] = set()
        adapted_call_ids: set[str] = set()
        foreign_call_ids: set[str] = set()
        unmatched_custom_output_ids: set[str] = set()
        adapted = 0
        untouched = 0

        for raw_item in input_items:
            if not isinstance(raw_item, Mapping):
                rewritten_items.append(raw_item)
                continue

            item_type = raw_item.get("type")
            call_id = raw_item.get("call_id")
            if self.is_custom_tool_call(raw_item):
                if (
                    not _has_exact_apply_patch_custom_tool_history_fields(
                        raw_item,
                        self.facts.custom_tool_history_call_fields,
                        self.facts.custom_tool_history_native_fields,
                    )
                    or raw_item.get("status") != "completed"
                    or not isinstance(call_id, str)
                    or not call_id
                    or not isinstance(raw_item.get("input"), str)
                    or not raw_item["input"].strip()
                    or call_id in pending_call_ids
                    or call_id in adapted_call_ids
                    or call_id in foreign_call_ids
                    or call_id in unmatched_custom_output_ids
                ):
                    self.raise_history_failure(event_context)

                patch = raw_item["input"]
                pending_call_ids.add(call_id)
                adapted_call_ids.add(call_id)
                rewritten_items.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": self.facts.function_name,
                        "arguments": json.dumps(
                            {"patch": patch},
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ),
                    }
                )
                adapted += 1
                continue

            if item_type == "custom_tool_call_output":
                if isinstance(call_id, str) and call_id in pending_call_ids:
                    if not _has_exact_apply_patch_custom_tool_history_fields(
                        raw_item,
                        self.facts.custom_tool_history_output_fields,
                        self.facts.custom_tool_history_native_fields,
                    ):
                        self.raise_history_failure(event_context)
                    pending_call_ids.remove(call_id)
                    rewritten_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": raw_item["output"],
                        }
                    )
                    continue
                if isinstance(call_id, str):
                    if call_id in adapted_call_ids:
                        self.raise_history_failure(event_context)
                    unmatched_custom_output_ids.add(call_id)
                rewritten_items.append(raw_item)
                continue

            if isinstance(call_id, str) and call_id in adapted_call_ids:
                self.raise_history_failure(event_context)

            if item_type in {"function_call", "function_call_output"}:
                if isinstance(call_id, str) and call_id:
                    foreign_call_ids.add(call_id)
            elif item_type == "custom_tool_call":
                if isinstance(call_id, str) and call_id:
                    foreign_call_ids.add(call_id)
                untouched += 1
            rewritten_items.append(raw_item)

        if pending_call_ids:
            self.raise_history_failure(event_context)

        if adapted:
            self.write_history_event(
                event_context,
                outcome="adapted",
                count=adapted,
            )
        if untouched:
            self.write_history_event(
                event_context,
                outcome="untouched",
                count=untouched,
            )
        return rewritten_items, adapted_call_ids, bool(adapted)

    def adapt_response_body(
        self,
        payload: Any,
        event_context: Mapping[str, Any] | None,
    ) -> tuple[Any, bool]:
        if not self.enabled(event_context) or not isinstance(payload, dict):
            return payload, False
        output = payload.get("output")
        if not isinstance(output, list):
            return payload, False

        adapted = 0
        untouched = 0
        seen_item_ids: set[str] = set()
        seen_call_ids: set[str] = set()
        seen_custom_item_ids: set[str] = set()
        seen_custom_call_ids: set[str] = set()
        seen_custom_keys: set[str] = set()
        rewritten_output: list[Any] = []

        for index, raw_item in enumerate(output):
            if self.is_function_call(raw_item):
                assert isinstance(raw_item, Mapping)
                try:
                    _require_exact_apply_patch_function_call_fields(
                        raw_item,
                        self.facts.function_call_fields,
                    )
                    item_id, call_id = _apply_patch_item_identity(raw_item)
                    _, patch = _apply_patch_arguments_text_and_input(raw_item.get("arguments"))
                    if item_id in seen_item_ids or item_id in seen_custom_item_ids:
                        raise _ApplyPatchAdapterFailure("duplicate_item_id")
                    if call_id in seen_call_ids or call_id in seen_custom_call_ids:
                        raise _ApplyPatchAdapterFailure("duplicate_call_id")
                except _ApplyPatchAdapterFailure as exc:
                    self.raise_adapter_failure(event_context, surface="body", reason=exc.reason)
                seen_item_ids.add(item_id)
                seen_call_ids.add(call_id)
                rewritten_output.append(_custom_apply_patch_item(raw_item, patch))
                adapted += 1
                continue

            if self.is_custom_tool_call(raw_item):
                assert isinstance(raw_item, Mapping)
                raw_item_id = raw_item.get("id")
                raw_call_id = raw_item.get("call_id")
                if isinstance(raw_item_id, str) and raw_item_id:
                    if raw_item_id in seen_item_ids:
                        self.raise_adapter_failure(
                            event_context,
                            surface="body",
                            reason="duplicate_item_id",
                        )
                    seen_custom_item_ids.add(raw_item_id)
                if isinstance(raw_call_id, str) and raw_call_id:
                    if raw_call_id in seen_call_ids:
                        self.raise_adapter_failure(
                            event_context,
                            surface="body",
                            reason="duplicate_call_id",
                        )
                    seen_custom_call_ids.add(raw_call_id)
                key = (
                    f"item:{raw_item_id}"
                    if isinstance(raw_item_id, str) and raw_item_id
                    else f"call:{raw_call_id}"
                    if isinstance(raw_call_id, str) and raw_call_id
                    else f"index:{index}"
                )
                if key not in seen_custom_keys:
                    seen_custom_keys.add(key)
                    untouched += 1
            rewritten_output.append(raw_item)

        if adapted:
            payload = dict(payload)
            payload["output"] = rewritten_output
            self.write_adapter_event(
                event_context,
                surface="body",
                outcome="adapted",
                count=adapted,
            )
        if untouched:
            self.write_adapter_event(
                event_context,
                surface="body",
                outcome="untouched",
                count=untouched,
            )
        return payload, bool(adapted)

    def stream_adapter(
        self,
        event_context: Mapping[str, Any] | None,
        *,
        surface: str = "stream",
    ) -> _ThirdPartyApplyPatchStreamAdapter:
        return _ThirdPartyApplyPatchStreamAdapter(self, event_context, surface=surface)

    def adapt_stream_events(
        self,
        events: list[Mapping[str, Any]],
        *,
        event_context: Mapping[str, Any] | None = None,
    ) -> tuple[list[Mapping[str, Any]], bool]:
        if not self.enabled(event_context):
            return events, False
        adapter = self.stream_adapter(event_context)
        changed = False
        rewritten: list[Mapping[str, Any]] = []
        for event in events:
            event_replacements, event_changed = adapter.events_for_event(event)
            rewritten.extend(event_replacements)
            changed = changed or event_changed
        adapter.finish()
        return (rewritten if changed else events), changed


@dataclass
class _ApplyPatchStreamState:
    item_id: str
    call_id: str
    output_index: int
    initial_arguments: str | None
    arguments: str | None = None
    patch: str | None = None
    delta_arguments: str = ""
    arguments_done: bool = False
    item_done: bool = False


class _ThirdPartyApplyPatchStreamAdapter:
    def __init__(
        self,
        adapter: ApplyPatchAdapter,
        event_context: Mapping[str, Any] | None,
        *,
        surface: str = "stream",
    ):
        self._adapter = adapter
        self._event_context = event_context
        self._surface = surface
        self._states: dict[str, _ApplyPatchStreamState] = {}
        self._item_id_by_call_id: dict[str, str] = {}
        self._adapted_item_ids: set[str] = set()
        self._custom_item_ids: set[str] = set()
        self._custom_call_ids: set[str] = set()
        self._untouched_keys: set[str] = set()
        self._terminal_seen = False
        self._finished = False

    def _fail(self, reason: str) -> NoReturn:
        self._adapter.raise_adapter_failure(
            self._event_context,
            surface=self._surface,
            reason=reason,
        )

    def _output_index(self, event: Mapping[str, Any]) -> int:
        output_index = event.get("output_index")
        if isinstance(output_index, bool) or not isinstance(output_index, int) or output_index < 0:
            self._fail("missing_output_index")
        return output_index

    def _remember_untouched(self, item: Mapping[str, Any], fallback: str) -> None:
        item_id = item.get("id")
        call_id = item.get("call_id")
        if isinstance(item_id, str) and item_id:
            if item_id in self._states:
                self._fail("duplicate_item_id")
            self._custom_item_ids.add(item_id)
        if isinstance(call_id, str) and call_id:
            if call_id in self._item_id_by_call_id:
                self._fail("duplicate_call_id")
            self._custom_call_ids.add(call_id)
        key = (
            f"item:{item_id}"
            if isinstance(item_id, str) and item_id
            else f"call:{call_id}"
            if isinstance(call_id, str) and call_id
            else fallback
        )
        self._untouched_keys.add(key)

    def _state_from_added_item(
        self,
        item: Mapping[str, Any],
        output_index: int,
    ) -> tuple[_ApplyPatchStreamState, str]:
        try:
            _require_exact_apply_patch_function_call_fields(
                item,
                self._adapter.facts.function_call_fields,
            )
            item_id, call_id = _apply_patch_item_identity(item)
            arguments = item.get("arguments")
            if isinstance(arguments, str) and not arguments:
                initial_arguments = None
            else:
                initial_arguments, _ = _apply_patch_arguments_text_and_input(arguments)
        except _ApplyPatchAdapterFailure as exc:
            self._fail(exc.reason)
        if item_id in self._states or item_id in self._custom_item_ids:
            self._fail("duplicate_item_added")
        if call_id in self._item_id_by_call_id or call_id in self._custom_call_ids:
            self._fail("duplicate_call_id")
        state = _ApplyPatchStreamState(
            item_id=item_id,
            call_id=call_id,
            output_index=output_index,
            initial_arguments=initial_arguments,
        )
        self._states[item_id] = state
        self._item_id_by_call_id[call_id] = item_id
        return state, ""

    def _state_for_event(self, event: Mapping[str, Any]) -> _ApplyPatchStreamState:
        item_id = event.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            self._fail("missing_item_id")
        state = self._states.get(item_id)
        if state is None:
            self._fail("unpaired_stream_event")
        output_index = self._output_index(event)
        if output_index != state.output_index:
            self._fail("conflicting_output_index")
        return state

    def _check_completed_item(
        self,
        item: Mapping[str, Any],
        state: _ApplyPatchStreamState,
    ) -> None:
        try:
            _require_exact_apply_patch_function_call_fields(
                item,
                self._adapter.facts.function_call_fields,
            )
            item_id, call_id = _apply_patch_item_identity(item)
            arguments, patch = _apply_patch_arguments_text_and_input(item.get("arguments"))
        except _ApplyPatchAdapterFailure as exc:
            self._fail(exc.reason)
        if item_id != state.item_id or call_id != state.call_id:
            self._fail("conflicting_item_identity")
        if not state.arguments_done or state.arguments is None or state.patch is None:
            self._fail("missing_arguments_done")
        if arguments != state.arguments or patch != state.patch:
            self._fail("conflicting_arguments")

    def _rewrite_terminal_response(self, event: Mapping[str, Any]) -> tuple[Mapping[str, Any], bool]:
        response = event.get("response")
        if not isinstance(response, Mapping):
            return event, False
        output = response.get("output")
        if not isinstance(output, list):
            return event, False
        changed = False
        seen_terminal_item_ids: set[str] = set()
        rewritten_output: list[Any] = []
        for index, raw_item in enumerate(output):
            if self._adapter.is_function_call(raw_item):
                assert isinstance(raw_item, Mapping)
                try:
                    _require_exact_apply_patch_function_call_fields(
                        raw_item,
                        self._adapter.facts.function_call_fields,
                    )
                    item_id, call_id = _apply_patch_item_identity(raw_item)
                    arguments, patch = _apply_patch_arguments_text_and_input(raw_item.get("arguments"))
                except _ApplyPatchAdapterFailure as exc:
                    self._fail(exc.reason)
                if item_id in seen_terminal_item_ids:
                    self._fail("duplicate_terminal_item")
                seen_terminal_item_ids.add(item_id)
                state = self._states.get(item_id)
                if state is None:
                    self._fail("unpaired_terminal_item")
                if call_id != state.call_id or not state.item_done or state.arguments != arguments or state.patch != patch:
                    self._fail("conflicting_terminal_item")
                rewritten_output.append(_custom_apply_patch_item(raw_item, patch))
                changed = True
                continue
            if self._adapter.is_custom_tool_call(raw_item):
                assert isinstance(raw_item, Mapping)
                self._remember_untouched(raw_item, f"terminal:{index}")
            rewritten_output.append(raw_item)
        if not changed:
            return event, False
        rewritten_response = dict(response)
        rewritten_response["output"] = rewritten_output
        rewritten_event = dict(event)
        rewritten_event["response"] = rewritten_response
        return rewritten_event, True

    def _ensure_terminal_lifecycle(self) -> None:
        for state in self._states.values():
            if not state.item_done:
                self._fail("incomplete_tool_lifecycle")

    def events_for_event(self, event: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], bool]:
        event_type = event.get("type")
        if self._terminal_seen and isinstance(event_type, str) and (event_type.startswith("response.") or event_type == "error"):
            self._fail("post_terminal_semantic_event")

        if event_type == "response.output_item.added":
            item = event.get("item")
            if self._adapter.is_function_call(item):
                assert isinstance(item, Mapping)
                output_index = self._output_index(event)
                self._state_from_added_item(item, output_index)
                rewritten_event = dict(event)
                rewritten_event["item"] = _custom_apply_patch_item(item, "")
                return [rewritten_event], True
            if self._adapter.is_custom_tool_call(item):
                assert isinstance(item, Mapping)
                self._remember_untouched(item, "added")
            return [event], False

        if event_type == "response.function_call_arguments.delta":
            item_id = event.get("item_id")
            if isinstance(item_id, str) and item_id in self._states:
                state = self._state_for_event(event)
                delta = event.get("delta")
                if state.arguments_done or state.item_done or not isinstance(delta, str):
                    self._fail("invalid_arguments_delta")
                # Do not expose the third-party JSON wrapper as freeform input.
                # The validated raw patch is emitted only by the matching done event.
                state.delta_arguments += delta
                return [], True
            return [event], False

        if event_type == "response.function_call_arguments.done":
            item_id = event.get("item_id")
            if isinstance(item_id, str) and item_id in self._states:
                state = self._state_for_event(event)
                if state.arguments_done or state.item_done:
                    self._fail("duplicate_arguments_done")
                try:
                    arguments, patch = _apply_patch_arguments_text_and_input(event.get("arguments"))
                except _ApplyPatchAdapterFailure as exc:
                    self._fail(exc.reason)
                if state.initial_arguments is not None and arguments != state.initial_arguments:
                    self._fail("conflicting_arguments")
                if state.delta_arguments and arguments != state.delta_arguments:
                    self._fail("conflicting_arguments")
                state.arguments = arguments
                state.patch = patch
                state.arguments_done = True
                self._adapted_item_ids.add(state.item_id)
                rewritten_event = dict(event)
                rewritten_event["type"] = "response.custom_tool_call_input.done"
                rewritten_event["input"] = patch
                rewritten_event.pop("arguments", None)
                return [rewritten_event], True
            return [event], False

        if event_type == "response.output_item.done":
            item = event.get("item")
            if self._adapter.is_function_call(item):
                assert isinstance(item, Mapping)
                try:
                    _require_exact_apply_patch_function_call_fields(
                        item,
                        self._adapter.facts.function_call_fields,
                    )
                    item_id, _ = _apply_patch_item_identity(item)
                except _ApplyPatchAdapterFailure as exc:
                    self._fail(exc.reason)
                state = self._states.get(item_id)
                if state is None:
                    self._fail("unpaired_stream_item")
                if state.item_done:
                    self._fail("duplicate_item_done")
                if self._output_index(event) != state.output_index:
                    self._fail("conflicting_output_index")
                self._check_completed_item(item, state)
                state.item_done = True
                rewritten_event = dict(event)
                rewritten_event["item"] = _custom_apply_patch_item(item, state.patch or "")
                return [rewritten_event], True
            if self._adapter.is_custom_tool_call(item):
                assert isinstance(item, Mapping)
                self._remember_untouched(item, "done")
            return [event], False

        if isinstance(event_type, str) and event_type in self._adapter.facts.terminal_event_types:
            rewritten_event, changed = self._rewrite_terminal_response(event)
            self._ensure_terminal_lifecycle()
            self._terminal_seen = True
            return [rewritten_event], changed

        return [event], False

    def finish(self, *, allow_missing_terminal: bool = False) -> None:
        if self._finished:
            return
        self._finished = True
        if self._states and not self._terminal_seen:
            if not allow_missing_terminal:
                self._fail("missing_terminal_event")
            self._ensure_terminal_lifecycle()
        if self._adapted_item_ids:
            self._adapter.write_adapter_event(
                self._event_context,
                surface=self._surface,
                outcome="adapted",
                count=len(self._adapted_item_ids),
            )
        if self._untouched_keys:
            self._adapter.write_adapter_event(
                self._event_context,
                surface=self._surface,
                outcome="untouched",
                count=len(self._untouched_keys),
            )
