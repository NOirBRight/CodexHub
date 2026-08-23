"""Request-scoped runtime tool compatibility for the selected Gateway route.

This module deliberately knows only about declaration shape and protocol
capabilities.  It does not select a Provider, execute a tool, or repair a
model response.  The request plan is immutable; stream assembly lives in
``stream``.  Isolated Collaboration V1 repair and V2 adaptation are dispatched
to sibling modules that must not import each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Iterable, Mapping

from .collab_v1 import (
    CollaborationV1PlanMixin,
    is_opaque_v1_history_item,
    validate_plain_native_item,
    validate_v1_fields,
)
from .collab_v2 import (
    CollaborationV2PlanMixin,
    apply_v2_namespace_decode,
    is_opaque_v2_history_item,
    strip_encrypted_annotations as strip_v2_encrypted_annotations,
    validate_v2_fields,
    validate_v2_native_arguments,
)
from .contracts import (
    CUSTOM_INPUT_KEY,
    CUSTOM_OUTPUT_KEY,
    TOOL_SEARCH_INPUT_KEY,
    TOOL_SEARCH_OUTPUT_KEY,
    HostedCapabilityFacts,
    ProtocolCapabilities,
    RequiredToolUnavailableError,
    ToolCompatibilityEntry,
    ToolCompatibilityError,
    _copy_mapping,
    _dump_envelope,
    _freeze,
    _is_legacy_message_identity,
    _item_identity,
    _json_object_exact,
    _json_object_with_key,
    _provider_function_declaration,
    _thaw,
)
from .dispositions import (
    ADAPT,
    CUSTOM_FREEFORM,
    NATIVE,
    NAMESPACE,
    OMIT,
    PLAIN_FUNCTION,
    REQUIRED_BUT_UNAVAILABLE,
    SELECTED_PROVIDER_HOSTED,
    TOOL_SEARCH,
    UNKNOWN_FUTURE_KIND,
    _declaration_key,
    _declaration_valid_for_family,
    _family_for_item_type,
    _has_explicit_named_tool_choice,
    _history_output_type_for_entry,
    _hosted_entry_item_kind,
    _hosted_event_spec_for_declaration_kind,
    _hosted_event_spec_for_item_type,
    _hosted_history_item_key,
    _hosted_kind_for_item_type,
    _hosted_output_kind_for_item_type,
    _name_of,
    _namespace_details,
    _required_by_rule,
    _tool_choice_matches_declaration,
    _unknown_response_item_kind,
    build_tool_compatibility_plan,
    classify_declaration,
)
from .registry import (
    AliasRecord,
    CompatibilityDiagnostics,
    RequestScopedToolAliasRegistry,
)

def is_opaque_collaboration_history_item(item: Mapping[str, Any]) -> bool:
    if item.get("type") != "function_call":
        return False
    return is_opaque_v1_history_item(item) or is_opaque_v2_history_item(item)


_is_opaque_collaboration_history_item = is_opaque_collaboration_history_item


def _validate_version_fields(item: Mapping[str, Any], record: AliasRecord) -> None:
    if record.version == "v1":
        validate_v1_fields(item)
    elif record.version == "v2":
        validate_v2_fields(item)
    arguments = item.get("arguments")
    if arguments in (None, ""):
        return
    if isinstance(arguments, Mapping):
        parsed = _copy_mapping(arguments)
    elif isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_arguments") from None
    else:
        raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_arguments")
    if not isinstance(parsed, Mapping):
        raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_arguments")
    if record.version == "v1":
        validate_v1_fields(parsed)
    elif record.version == "v2":
        validate_v2_fields(parsed)


@dataclass(frozen=True, slots=True)
class ToolCompatibilityPlan(CollaborationV1PlanMixin, CollaborationV2PlanMixin):
    selected_protocol: str
    capabilities: ProtocolCapabilities
    entries: tuple[ToolCompatibilityEntry, ...]
    registry: RequestScopedToolAliasRegistry = field(repr=False, compare=False)
    diagnostics: CompatibilityDiagnostics
    collaboration_protocol: str | None = None
    tool_choice: Any = None
    provider_hosted_kinds: frozenset[str] = frozenset()

    @property
    def aliases(self) -> tuple[str, ...]:
        return self.registry.aliases

    @property
    def has_adaptations(self) -> bool:
        return any(entry.disposition == ADAPT for entry in self.entries)

    def entry(self, index: int) -> ToolCompatibilityEntry:
        return self.entries[index]

    def new_stream(self) -> "CompatibilityStreamState":
        from .stream import CompatibilityStreamState

        return CompatibilityStreamState(self)

    def new_attempt(self) -> "ToolCompatibilityPlan":
        """Return an attempt-local plan with stable aliases and fresh calls."""
        return ToolCompatibilityPlan(
            selected_protocol=self.selected_protocol,
            capabilities=self.capabilities,
            entries=self.entries,
            registry=self.registry.new_attempt(),
            diagnostics=self.diagnostics,
            collaboration_protocol=self.collaboration_protocol,
            tool_choice=self.tool_choice,
            provider_hosted_kinds=self.provider_hosted_kinds,
        )

    def with_final_declarations(
        self,
        declarations: Iterable[Mapping[str, Any]],
        *,
        tool_choice: Any = None,
    ) -> "ToolCompatibilityPlan":
        """Add model-visible native declarations while preserving request aliases."""
        final = list(declarations)
        entries = list(self.entries)
        effective_tool_choice = self.tool_choice if tool_choice is None else tool_choice
        native_names: set[str] = set()
        for declaration in final:
            if not isinstance(declaration, Mapping):
                continue
            name = declaration.get("name")
            if isinstance(name, str):
                native_names.add(name)
            family = classify_declaration(declaration)
            if family == NAMESPACE:
                _namespace, children, _version, _valid = _namespace_details(declaration)
                native_names.update(
                    str(child.get("name")) for child in children if isinstance(child.get("name"), str)
                )
            key = _declaration_key(declaration)
            if any(_declaration_key(entry.declaration) == key for entry in self.entries):
                continue
            if not _declaration_valid_for_family(declaration, family):
                continue
            is_required = _required_by_rule(
                declaration,
                len(entries),
                required=None,
                tool_choice=effective_tool_choice,
            )
            disposition = OMIT
            reason = "lifecycle_unavailable"
            aliases: list[str] = []
            if family == PLAIN_FUNCTION:
                if self.capabilities.function_lifecycle:
                    disposition, reason = NATIVE, "native_function_lifecycle"
            elif family == NAMESPACE:
                namespace, children, version, _valid = _namespace_details(declaration)
                if self.capabilities.namespace_lifecycle:
                    disposition, reason = NATIVE, "native_namespace_lifecycle"
                elif self.capabilities.function_lifecycle and self.capabilities.accepts_namespace_adapter:
                    disposition, reason = ADAPT, "namespace_function_adapter"
                    aliases = [
                        self.registry.allocate_namespace(
                            declaration_index=len(entries),
                            namespace=str(namespace),
                            child_index=child_index,
                            child_name=str(child.get("name")),
                            version=version,
                        )
                        for child_index, child in enumerate(children)
                    ]
            elif family == CUSTOM_FREEFORM:
                if self.capabilities.custom_lifecycle:
                    disposition, reason = NATIVE, "native_custom_lifecycle"
                elif self.capabilities.function_lifecycle and self.capabilities.accepts_custom_adapter:
                    disposition, reason = ADAPT, "custom_function_envelope"
                    aliases = [
                        self.registry.allocate_custom(
                            declaration_index=len(entries),
                            original_name=str(_name_of(declaration)),
                            version=None,
                        )
                    ]
            elif family == TOOL_SEARCH:
                if self.capabilities.tool_search_lifecycle:
                    disposition, reason = NATIVE, "native_client_tool_search"
                elif self.capabilities.function_lifecycle and self.capabilities.accepts_tool_search_adapter:
                    disposition, reason = ADAPT, "tool_search_function_envelope"
                    aliases = [self.registry.allocate_tool_search(declaration_index=len(entries))]
            elif family == SELECTED_PROVIDER_HOSTED:
                # Provider capability facts are intentionally not inferred for
                # declarations added after the request plan was built.
                if (
                    _hosted_event_spec_for_declaration_kind(declaration.get("type")) is not None
                    and declaration.get("type") in self.provider_hosted_kinds
                    and declaration.get("type") in self.capabilities.hosted_lifecycles
                ):
                    disposition, reason = NATIVE, "selected_provider_hosted_lifecycle"
            elif family == UNKNOWN_FUTURE_KIND:
                reason = "unknown_lifecycle_contract_unavailable"
            if disposition == OMIT and is_required:
                disposition = REQUIRED_BUT_UNAVAILABLE
                reason = "required_unavailable"
            entries.append(
                ToolCompatibilityEntry(
                    declaration_index=len(entries),
                    family=family,
                    disposition=disposition,
                    required=is_required,
                    declaration=_freeze(_copy_mapping(declaration)),
                    reason=reason,
                    aliases=tuple(aliases),
                    namespace=_namespace_details(declaration)[0] if family == NAMESPACE else None,
                    version=_namespace_details(declaration)[2] if family == NAMESPACE else None,
                    child_names=tuple(
                        str(child.get("name"))
                        for child in _namespace_details(declaration)[1]
                        if isinstance(child.get("name"), str)
                    ) if family == NAMESPACE else (),
                )
            )
        # ``_set_required_subagent_tool_choice`` may already have translated a
        # namespace child to this request's generated alias.  That alias is a
        # valid choice even though it is not the original declaration spelling
        # present in ``final``.  Unknown aliases remain fail-closed.
        choice_name = (
            effective_tool_choice
            if isinstance(effective_tool_choice, str)
            else effective_tool_choice.get("name")
            if isinstance(effective_tool_choice, Mapping)
            else None
        )
        choice_is_known_alias = (
            self.registry.record_for_alias(choice_name) is not None
            or self.registry.remapped_alias(choice_name) is not None
        )
        if _has_explicit_named_tool_choice(effective_tool_choice) and not choice_is_known_alias and not any(
            _tool_choice_matches_declaration(declaration, effective_tool_choice)
            for declaration in final
            if isinstance(declaration, Mapping)
        ):
            raise RequiredToolUnavailableError()
        if any(entry.disposition == REQUIRED_BUT_UNAVAILABLE for entry in entries):
            raise RequiredToolUnavailableError()
        alias_remap = self.registry.reserve_native_names(native_names)
        if alias_remap:
            entries = [
                ToolCompatibilityEntry(
                    declaration_index=entry.declaration_index,
                    family=entry.family,
                    disposition=entry.disposition,
                    required=entry.required,
                    declaration=entry.declaration,
                    reason=entry.reason,
                    aliases=tuple(alias_remap.get(alias, alias) for alias in entry.aliases),
                    namespace=entry.namespace,
                    version=entry.version,
                    child_names=entry.child_names,
                )
                for entry in entries
            ]
        if len(entries) == len(self.entries) and not alias_remap:
            return self
        return ToolCompatibilityPlan(
            selected_protocol=self.selected_protocol,
            capabilities=self.capabilities,
            entries=tuple(entries),
            registry=self.registry,
            diagnostics=CompatibilityDiagnostics.from_entries(entries),
            collaboration_protocol=self.collaboration_protocol,
            tool_choice=self.tool_choice,
            provider_hosted_kinds=self.provider_hosted_kinds,
        )

    def _entry_for_declaration(self, declaration: Mapping[str, Any], occurrence: dict[tuple[Any, ...], int]) -> ToolCompatibilityEntry | None:
        key = _declaration_key(declaration)
        matching = [entry for entry in self.entries if _declaration_key(entry.declaration) == key]
        offset = occurrence.get(key, 0)
        occurrence[key] = offset + 1
        return matching[offset] if offset < len(matching) else None

    def _entry_for_name(
        self,
        name: Any,
        namespace: Any = None,
        *,
        expected_family: str | None = None,
        item_type: Any = None,
    ) -> ToolCompatibilityEntry | None:
        if not isinstance(name, str):
            return None
        expected_family = expected_family or _family_for_item_type(item_type, namespace)
        matches: list[ToolCompatibilityEntry] = []
        if namespace is not None:
            matches = [
                entry
                for entry in self.entries
                if entry.family == NAMESPACE
                and entry.namespace == namespace
                and name in entry.child_names
            ]
        else:
            # An unqualified child name is not enough to identify an adapted
            # namespace call.  Prefer an exact non-namespace declaration (in
            # particular a native plain function) and only accept the
            # explicit flattened ``namespace__child`` spelling for an
            # adapted namespace call.
            matches = [entry for entry in self.entries if entry.family != NAMESPACE and entry.original_name == name]
            if not matches:
                matches = [
                    entry
                    for entry in self.entries
                    if entry.family == NAMESPACE
                    and any(name == f"{entry.namespace}__{child}" for child in entry.child_names)
                ]
        if expected_family is not None:
            filtered = [entry for entry in matches if entry.family == expected_family]
            if (
                not filtered
                and expected_family == PLAIN_FUNCTION
                and namespace is None
                and any(entry.family == NAMESPACE for entry in matches)
            ):
                expected_family = NAMESPACE
                filtered = [entry for entry in matches if entry.family == NAMESPACE]
            matches = filtered
        if len(matches) > 1 and (expected_family is not None or item_type is not None):
            raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity")
        return matches[0] if len(matches) == 1 else None

    def _entries_for_name(
        self,
        name: Any,
        namespace: Any = None,
        *,
        expected_family: str | None = None,
        item_type: Any = None,
    ) -> list[ToolCompatibilityEntry]:
        if not isinstance(name, str):
            return []
        expected_family = expected_family or _family_for_item_type(item_type, namespace)
        matches: list[ToolCompatibilityEntry] = []
        for entry in self.entries:
            if entry.family == NAMESPACE:
                if namespace is not None and namespace != entry.namespace:
                    continue
                if namespace is not None and name in entry.child_names:
                    matches.append(entry)
                elif namespace is None and any(
                    name == f"{entry.namespace}__{child}" for child in entry.child_names
                ):
                    matches.append(entry)
            elif entry.original_name == name:
                if namespace is None:
                    matches.append(entry)
        if expected_family is not None:
            filtered = [entry for entry in matches if entry.family == expected_family]
            if (
                not filtered
                and expected_family == PLAIN_FUNCTION
                and namespace is None
                and any(entry.family == NAMESPACE for entry in matches)
            ):
                expected_family = NAMESPACE
                filtered = [entry for entry in matches if entry.family == NAMESPACE]
            matches = filtered
        if len(matches) > 1 and (expected_family is not None or item_type is not None):
            raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity")
        return matches

    def _has_unqualified_adapted_child(self, name: Any) -> bool:
        return isinstance(name, str) and any(
            entry.family == NAMESPACE
            and entry.disposition == ADAPT
            and name in entry.child_names
            for entry in self.entries
        )

    def _has_adapted_name_conflict(self, name: Any, expected_family: str) -> bool:
        if not isinstance(name, str):
            return False
        if any(
            entry.disposition == NATIVE
            and entry.family == expected_family
            and entry.original_name == name
            for entry in self.entries
        ):
            return False
        return any(
            entry.disposition == ADAPT
            and entry.family != expected_family
            and entry.original_name == name
            for entry in self.entries
        )

    def _alias_for(self, entry: ToolCompatibilityEntry, *, child_name: str | None = None) -> str | None:
        if entry.family == NAMESPACE:
            if child_name not in entry.child_names and isinstance(child_name, str):
                child_name = next(
                    (
                        child
                        for child in entry.child_names
                        if child_name == f"{entry.namespace}__{child}"
                    ),
                    child_name,
                )
            try:
                child_index = entry.child_names.index(child_name or "")
            except ValueError:
                return None
            return entry.aliases[child_index]
        return entry.aliases[0] if entry.aliases else None

    def owns_wire_value(self, value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        if _is_opaque_collaboration_history_item(value):
            return True
        name = value.get("name")
        namespace = value.get("namespace")
        item_type = value.get("type")
        if self.registry.record_for_alias(name) is not None:
            return True
        if self.registry.looks_like_alias(name):
            return True
        if self._entry_for_name(
            name,
            namespace,
            expected_family=_family_for_item_type(item_type, namespace),
            item_type=item_type,
        ) is not None:
            return True
        return self.registry.record_for_call(value.get("call_id")) is not None

    def _encode_tool_declarations(self, tools: Any) -> tuple[Any, bool]:
        if not isinstance(tools, list):
            return tools, False
        occurrence: dict[tuple[Any, ...], int] = {}
        encoded: list[Any] = []
        changed = False
        for raw_tool in tools:
            if not isinstance(raw_tool, Mapping):
                encoded.append(raw_tool)
                continue
            entry = self._entry_for_declaration(raw_tool, occurrence)
            if entry is None:
                encoded.append(_copy_mapping(raw_tool))
                continue
            if entry.disposition == OMIT:
                changed = True
                continue
            if entry.disposition != ADAPT:
                encoded.append(_copy_mapping(raw_tool))
                continue
            if entry.family == NAMESPACE:
                namespace, children, _version, valid = _namespace_details(raw_tool)
                if not valid:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_declaration")
                for child_index, child in enumerate(children):
                    child = (
                        strip_v2_encrypted_annotations(child)
                        if namespace == "collaboration"
                        else child
                    )
                    encoded.append(
                        _provider_function_declaration(child, entry.aliases[child_index])
                    )
                changed = True
                continue
            if entry.family == CUSTOM_FREEFORM:
                alias = entry.aliases[0]
                encoded.append(
                    {
                        "type": "function",
                        "name": alias,
                        "parameters": {
                            "type": "object",
                            # The Responses custom/freeform contract is a
                            # text format.  Keep the adapter envelope
                            # explicit so Chat models do not infer an object
                            # value for the freeform input and trip the
                            # stream decoder's string boundary.
                            "properties": {CUSTOM_INPUT_KEY: {"type": "string"}},
                            "required": [CUSTOM_INPUT_KEY],
                            "additionalProperties": False,
                        },
                    }
                )
                changed = True
                continue
            if entry.family == TOOL_SEARCH:
                alias = entry.aliases[0]
                function = _copy_mapping(raw_tool)
                function["type"] = "function"
                function["name"] = alias
                function.pop("execution", None)
                function.pop("description", None)
                source_parameters = function.get("parameters")
                if not isinstance(source_parameters, Mapping):
                    source_parameters = {"type": "object"}
                function["parameters"] = {
                    "type": "object",
                    "properties": {TOOL_SEARCH_INPUT_KEY: source_parameters},
                    "required": [TOOL_SEARCH_INPUT_KEY],
                    "additionalProperties": False,
                }
                encoded.append(function)
                changed = True
                continue
            encoded.append(_copy_mapping(raw_tool))
        return encoded, changed

    def _encode_tool_choice(self, value: Any) -> tuple[Any, bool]:
        thawed = _thaw(value)
        if isinstance(thawed, str):
            if self.registry.is_native_name(thawed):
                return thawed, False
            remapped = self.registry.remapped_alias(thawed)
            if remapped is not None:
                return remapped, True
            entry = self._entry_for_name(thawed)
            if entry is None and thawed == "tool_search":
                candidates = [
                    candidate
                    for candidate in self.entries
                    if candidate.family == TOOL_SEARCH
                ]
                if len(candidates) == 1:
                    entry = candidates[0]
            alias = self._alias_for(entry) if entry is not None and entry.disposition == ADAPT else None
            return (alias, True) if alias else (thawed, False)
        if not isinstance(thawed, Mapping):
            return thawed, False
        result = dict(thawed)
        name = result.get("name")
        choice_type = result.get("type")
        if self.registry.is_native_name(name) and result.get("namespace") is None:
            return result, False
        remapped = self.registry.remapped_alias(name)
        if remapped is not None:
            result["name"] = remapped
            result["type"] = "function"
            result.pop("namespace", None)
            return result, True
        namespace_choice = name if isinstance(name, str) else result.get("namespace")
        if choice_type == "namespace" and isinstance(namespace_choice, str):
            candidates = [
                entry
                for entry in self.entries
                if entry.family == NAMESPACE
                and entry.namespace == namespace_choice
                and entry.disposition == ADAPT
            ]
            if candidates:
                aliases = [alias for entry in candidates for alias in entry.aliases]
                if len(aliases) != 1:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_tool_choice")
                result["type"] = "function"
                result["name"] = aliases[0]
                return result, True
        entry = self._entry_for_name(name, result.get("namespace"))
        if choice_type == "function" and isinstance(name, str):
            candidates = [
                candidate
                for candidate in self._entries_for_name(name, result.get("namespace"))
                if candidate.disposition == ADAPT
            ]
            if len(candidates) > 1:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_tool_choice")
        alias = (
            self._alias_for(entry, child_name=name)
            if entry is not None and entry.disposition == ADAPT
            else None
        )
        if alias is None and name == "tool_search" and result.get("type") == "function":
            candidates = [
                candidate
                for candidate in self.entries
                if candidate.family == TOOL_SEARCH and candidate.disposition == ADAPT
            ]
            if len(candidates) == 1:
                alias = candidates[0].aliases[0]
        if alias:
            result["name"] = alias
            result["type"] = "function"
            result.pop("namespace", None)
            return result, True
        return result, False

    def _encode_item(self, raw_item: Any, call_aliases: dict[str, str]) -> tuple[Any, bool]:
        if not isinstance(raw_item, Mapping):
            return raw_item, False
        item = _copy_mapping(raw_item)
        item_type = item.get("type")
        name = item.get("name")
        namespace = item.get("namespace")
        if item_type == "agent_message":
            return self._encode_agent_message(item)
        if item_type in {"tool_search_call", "tool_search_output"}:
            native_entry = self._native_entry_for_item(item)
            if native_entry is not None:
                self._validate_native_item(item, native_entry)
        if _hosted_output_kind_for_item_type(item_type) is not None:
            raise ToolCompatibilityError("tool_compatibility_boundary", "unsupported_hosted_lifecycle", surface="history")
        entry = self._entry_for_name(name, namespace, item_type=item_type)
        if entry is None and item_type in {"tool_search_call", "tool_search_output"}:
            candidates = [candidate for candidate in self.entries if candidate.family == TOOL_SEARCH]
            if len(candidates) == 1:
                entry = candidates[0]
        if item_type == "function_call" and entry is not None and entry.disposition == ADAPT:
            alias = self._alias_for(entry, child_name=name)
            if alias is None:
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias")
            _validate_version_fields(item, self.registry.record_for_alias(alias))  # type: ignore[arg-type]
            call_id = item.get("call_id")
            self.registry.bind_call(call_id, alias)
            call_aliases[str(call_id)] = alias
            item["name"] = alias
            item.pop("namespace", None)
            if entry.version == "v2" and entry.family == NAMESPACE:
                item.pop("encrypted_function_args", None)
            if entry.family == CUSTOM_FREEFORM:
                if "input" not in item:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope")
                item["type"] = "function_call"
                item["arguments"] = _dump_envelope(CUSTOM_INPUT_KEY, item.pop("input"))
            return item, True
        if item_type == "custom_tool_call" and entry is not None and entry.disposition == ADAPT:
            alias = self._alias_for(entry)
            if alias is None or "input" not in item:
                raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope")
            call_id = item.get("call_id")
            self.registry.bind_call(call_id, alias)
            call_aliases[str(call_id)] = alias
            item["type"] = "function_call"
            item["name"] = alias
            item["arguments"] = _dump_envelope(CUSTOM_INPUT_KEY, item.pop("input"))
            return item, True
        if item_type == "tool_search_call" and entry is not None and entry.disposition == ADAPT:
            alias = self._alias_for(entry)
            if alias is None:
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias")
            if item.get("execution") != "client":
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "invalid_tool_search_execution",
                    surface="history",
                )
            call_id = item.get("call_id")
            self.registry.bind_call(call_id, alias)
            call_aliases[str(call_id)] = alias
            item["type"] = "function_call"
            item["name"] = alias
            item.pop("namespace", None)
            item.pop("execution", None)
            item["arguments"] = _dump_envelope(TOOL_SEARCH_INPUT_KEY, item.pop("arguments", {}))
            return item, True
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = item.get("call_id")
            alias = call_aliases.get(call_id) if isinstance(call_id, str) else None
            record = self.registry.record_for_call(call_id) if alias is None else self.registry.record_for_alias(alias)
            if record is not None and record.family == CUSTOM_FREEFORM:
                if "output" not in item:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope")
                item["type"] = "function_call_output"
                item["output"] = _dump_envelope(CUSTOM_OUTPUT_KEY, item["output"])
                return item, True
            if record is not None and record.family == NAMESPACE:
                _validate_version_fields(item, record)
            return item, False
        if item_type == "tool_search_output":
            call_id = item.get("call_id")
            alias = call_aliases.get(call_id) if isinstance(call_id, str) else None
            record = self.registry.record_for_call(call_id) if alias is None else self.registry.record_for_alias(alias)
            if record is not None and record.family == TOOL_SEARCH:
                if item.get("execution") != "client":
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "invalid_tool_search_execution",
                        surface="history",
                    )
                payload = _copy_mapping(item)
                for key in ("type", "execution", "id", "item_id", "call_id"):
                    payload.pop(key, None)
                item["type"] = "function_call_output"
                item["output"] = _dump_envelope(TOOL_SEARCH_OUTPUT_KEY, payload)
                for key in payload:
                    item.pop(key, None)
                item.pop("execution", None)
                return item, True
            return item, False
        return item, False

    def _hosted_history_item_key(self, item_type: Any) -> tuple[str, bool] | None:
        # Standard Responses tool lifecycles are resolved by their declaration
        # family (and, for adapted calls, the request-scoped alias registry).
        # Never reinterpret them as a provider-hosted ``<kind>_call`` merely
        # because an unrelated unknown declaration happens to use the same
        # prefix (for example ``custom_tool``).  Keeping this guard at the
        # plan-level entry point is important for request-history filtering as
        # well as response decoding.
        if item_type in {
            "function_call",
            "function_call_output",
            "custom_tool_call",
            "custom_tool_call_output",
            "tool_search_call",
            "tool_search_output",
        }:
            return None
        key = _hosted_history_item_key(item_type)
        if key is not None or not isinstance(item_type, str):
            return key
        for entry in self.entries:
            if entry.disposition not in {NATIVE, OMIT}:
                continue
            hosted_kind = _hosted_entry_item_kind(entry)
            if hosted_kind is None:
                continue
            if item_type == f"{hosted_kind}_call":
                return hosted_kind, False
            if item_type == f"{hosted_kind}_call_output":
                return hosted_kind, True
        return None

    def _unknown_entry_for_item(
        self,
        item: Mapping[str, Any],
        *,
        surface: str,
    ) -> ToolCompatibilityEntry | None:
        unknown_kind = _unknown_response_item_kind(item.get("type"))
        if unknown_kind is None:
            return None
        matches = [
            entry
            for entry in self.entries
            if entry.family == UNKNOWN_FUTURE_KIND
            and entry.disposition in {NATIVE, OMIT}
            and entry.declaration.get("type") == unknown_kind
        ]
        item_name = item.get("name")
        if isinstance(item_name, str):
            named_matches = [
                entry for entry in matches if entry.declaration.get("name") == item_name
            ]
            if named_matches:
                matches = named_matches
            elif any(isinstance(entry.declaration.get("name"), str) for entry in matches):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unknown_native_identity",
                    surface=surface,
                )
        if len(matches) > 1:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface=surface,
            )
        return matches[0] if matches else None

    def _expected_response_output_type(self, entry: ToolCompatibilityEntry) -> str | None:
        expected = _history_output_type_for_entry(entry)
        if entry.disposition == ADAPT and entry.family in {NAMESPACE, CUSTOM_FREEFORM, TOOL_SEARCH}:
            return "function_call_output"
        return expected

    def _standard_contract_families_for_item(self, item: Mapping[str, Any]) -> frozenset[str]:
        item_type = item.get("type")
        if item_type == "function_call":
            return frozenset({NAMESPACE}) if item.get("namespace") is not None else frozenset({PLAIN_FUNCTION, NAMESPACE})
        if item_type == "custom_tool_call":
            return frozenset({CUSTOM_FREEFORM})
        if item_type in {"tool_search_call", "tool_search_output"}:
            return frozenset({TOOL_SEARCH})
        return frozenset()

    def _has_standard_contract_for_item(self, item: Mapping[str, Any]) -> bool:
        families = self._standard_contract_families_for_item(item)
        # Only an omitted standard declaration creates a representational
        # boundary that an unknown item could bypass.  Native-only plans may
        # still carry provider-local historical calls that are intentionally
        # left untouched.
        return bool(families) and any(
            entry.family in families and entry.disposition == OMIT
            for entry in self.entries
        )

    def _reject_unknown_standard_item(self, item: Mapping[str, Any], *, surface: str) -> None:
        """Reject an undeclared standard lifecycle when this plan has a contract.

        A standard item with a typo or a provider-local name must not bypass an
        omitted/native declaration merely because name lookup returned no exact
        match.  Adapter aliases remain valid function-call wire names.
        """
        if surface == "history" and _is_opaque_collaboration_history_item(item):
            return
        if item.get("type") == "function_call" and self.registry.record_for_alias(item.get("name")) is not None:
            return
        if (
            item.get("type") == "custom_tool_call"
            and item.get("namespace") is not None
            and any(
                entry.family == CUSTOM_FREEFORM and entry.disposition == ADAPT
                for entry in self.entries
            )
        ):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unknown_native_identity",
                surface=surface,
            )
        if (
            item.get("type") == "function_call"
            and item.get("namespace") is None
            and isinstance(item.get("name"), str)
        ):
            name = item.get("name")
            has_native_plain_owner = any(
                entry.family == PLAIN_FUNCTION
                and entry.disposition == NATIVE
                and entry.original_name == name
                for entry in self.entries
            )
            has_native_namespace_owner = any(
                entry.family == NAMESPACE
                and entry.disposition == NATIVE
                and name in entry.child_names
                for entry in self.entries
            )
            if has_native_namespace_owner and not has_native_plain_owner:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unknown_native_identity",
                    surface=surface,
                )
        if self._reject_unknown_flattened_identity(item, surface=surface):
            return
        if not self._has_standard_contract_for_item(item):
            return
        if self._standard_entry_for_item(item, surface=surface) is None:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unknown_native_identity",
                surface=surface,
            )

    def _entry_for_call_owner(
        self,
        item: Mapping[str, Any],
        *,
        surface: str,
    ) -> ToolCompatibilityEntry | None:
        """Resolve a call item to its request/response declaration owner."""
        if item.get("type") not in {"function_call", "custom_tool_call", "tool_search_call"}:
            return None
        entry = self._native_entry_for_item(item)
        if entry is None:
            record = self.registry.record_for_alias(item.get("name"))
            if record is not None:
                entry = next(
                    (
                        candidate
                        for candidate in self.entries
                        if candidate.declaration_index == record.declaration_index
                    ),
                    None,
                )
        if entry is None:
            entry = self._standard_entry_for_item(item, surface=surface)
        if entry is None:
            # Older Responses providers emit the client-owned search as a
            # plain ``function_call`` named ``tool_search`` even when the
            # request declaration was adapted to an opaque alias.  Resolve
            # that exact legacy spelling to the one adapted search entry;
            # unrelated provider functions with the same name are not
            # eligible because ``_legacy_tool_search_record`` only considers
            # an explicit adapted search declaration.
            legacy_record = self._legacy_tool_search_record(item)
            if legacy_record is not None:
                entry = next(
                    (
                        candidate
                        for candidate in self.entries
                        if candidate.declaration_index == legacy_record.declaration_index
                    ),
                    None,
                )
        if entry is None:
            entry = self._unknown_entry_for_item(item, surface=surface)
        return entry

    def _standard_entry_for_item(
        self,
        item: Mapping[str, Any],
        *,
        owner: ToolCompatibilityEntry | None = None,
        surface: str = "response",
    ) -> ToolCompatibilityEntry | None:
        """Resolve a standard Responses item before hosted/unknown matching.

        A provider-specific declaration such as ``custom_tool`` shares the
        ``custom_tool_call`` prefix with the standard custom lifecycle.  The
        standard declaration (or request-scoped adapted call identity) must
        therefore win before the generic hosted matcher is consulted.  For
        output items the wire shape has no tool name, so prefer a native
        standard family when one exists and otherwise retain an omitted
        standard family as the fail-closed result.
        """
        item_type = item.get("type")
        if item_type in {"function_call", "custom_tool_call"}:
            return self._entry_for_name(
                item.get("name"),
                item.get("namespace"),
                item_type=item_type,
            )

        if item_type == "tool_search_call":
            matches = [
                entry
                for entry in self.entries
                if entry.family == TOOL_SEARCH and entry.disposition in {NATIVE, ADAPT, OMIT}
            ]
            native = next((entry for entry in matches if entry.disposition == NATIVE), None)
            return native or (matches[0] if matches else None)

        if item_type in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
            if owner is not None:
                expected_output_type = (
                    self._expected_response_output_type(owner)
                    if surface == "response"
                    else _history_output_type_for_entry(owner)
                )
                if item_type != expected_output_type:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface=surface,
                    )
                return owner
            record = self.registry.record_for_call(item.get("call_id"))
            if record is not None:
                entry = next(
                    (
                        entry
                        for entry in self.entries
                        if entry.declaration_index == record.declaration_index
                    ),
                    None,
                )
                # Registry records describe adapter wire calls.  Both the
                # namespace and custom adapters emit a function lifecycle;
                # a differently typed output cannot borrow that call_id.
                expected_output_type = (
                    "function_call_output"
                    if record.family in {NAMESPACE, CUSTOM_FREEFORM}
                    else None
                )
                if item_type != expected_output_type:
                    if self._unknown_entry_for_item(item, surface=surface) is not None:
                        return None
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface=surface,
                    )
                return entry
            if item_type == "function_call_output":
                families = {PLAIN_FUNCTION, NAMESPACE}
            elif item_type == "custom_tool_call_output":
                families = {CUSTOM_FREEFORM}
            else:
                families = {TOOL_SEARCH}
            matches = [
                entry
                for entry in self.entries
                if entry.family in families and entry.disposition in {NATIVE, OMIT}
            ]
            if matches and self._unknown_entry_for_item(item, surface=surface) is not None:
                # Without a call owner, the output spelling alone cannot
                # distinguish a standard lifecycle from an exact unknown kind
                # that uses the same prefix.  Let the unknown OMIT matcher
                # reject it instead of silently claiming it as native.
                return None
            native = next((entry for entry in matches if entry.disposition == NATIVE), None)
            return native or (matches[0] if matches else None)
        return None

    def _validate_response_owner_item(
        self,
        item: Mapping[str, Any],
        *,
        owner: ToolCompatibilityEntry | None,
        call_index: int | None,
        item_index: int,
        surface: str,
    ) -> None:
        """Validate output-family ownership before response decoding starts."""
        item_type = item.get("type")
        if not isinstance(item_type, str) or (
            item_type != "tool_search_output" and not item_type.endswith("_call_output")
        ):
            return
        if owner is None:
            return
        expected = (
            _history_output_type_for_entry(owner)
            if surface == "history"
            else self._expected_response_output_type(owner)
        )
        if expected is not None and item_type != expected:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_call_identity",
                surface=surface,
            )
        if surface == "response" and call_index is not None and item_index < call_index:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_call_identity",
                surface=surface,
            )

    def _omitted_response_entry_for_item(
        self,
        item: Mapping[str, Any],
        *,
        owner: ToolCompatibilityEntry | None = None,
    ) -> ToolCompatibilityEntry | None:
        standard_entry = self._standard_entry_for_item(item, owner=owner, surface="response")
        if standard_entry is not None:
            return standard_entry if standard_entry.disposition == OMIT else None
        hosted_item_key = self._hosted_history_item_key(item.get("type"))
        if hosted_item_key is not None:
            hosted_item_kind, _is_output = hosted_item_key
            matches = [
                entry
                for entry in self.entries
                if entry.disposition in {NATIVE, OMIT}
                and _hosted_entry_item_kind(entry) == hosted_item_kind
            ]
            if len(matches) > 1:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="response")
            return matches[0] if matches and matches[0].disposition == OMIT else None
        unknown_entry = self._unknown_entry_for_item(item, surface="response")
        return unknown_entry if unknown_entry is not None and unknown_entry.disposition == OMIT else None

    def _omitted_history_entry(
        self,
        item: Mapping[str, Any],
        *,
        owner: ToolCompatibilityEntry | None = None,
    ) -> ToolCompatibilityEntry | None:
        item_type = item.get("type")
        standard_entry = self._standard_entry_for_item(item, owner=owner, surface="history")
        if standard_entry is not None:
            return standard_entry if standard_entry.disposition == OMIT else None
        hosted_item_key = self._hosted_history_item_key(item_type)
        if hosted_item_key is not None:
            hosted_item_kind, _is_output = hosted_item_key
            matches = [
                entry
                for entry in self.entries
                if entry.disposition in {NATIVE, OMIT}
                and _hosted_entry_item_kind(entry) == hosted_item_kind
            ]
            if len(matches) > 1:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="history")
            if matches and matches[0].disposition == OMIT:
                return matches[0]
            return None
        unknown_entry = self._unknown_entry_for_item(item, surface="history")
        if unknown_entry is not None:
            return unknown_entry if unknown_entry.disposition == OMIT else None
        if item_type in {"function_call", "custom_tool_call"}:
            entry = self._entry_for_name(
                item.get("name"),
                item.get("namespace"),
                item_type=item_type,
            )
            if entry is not None and entry.disposition == OMIT:
                return entry
            self._reject_unknown_standard_item(item, surface="history")
            return None
        self._reject_unknown_standard_item(item, surface="history")
        if item_type in {"tool_search_call", "tool_search_output"}:
            return next(
                (
                    entry
                    for entry in self.entries
                    if entry.family == TOOL_SEARCH and entry.disposition == OMIT
                ),
                None,
            )
        if isinstance(item_type, str):
            return next(
                (
                    entry
                    for entry in self.entries
                    if entry.disposition == OMIT
                    and isinstance(entry.declaration.get("type"), str)
                    and item_type.startswith(f"{entry.declaration.get('type')}_")
                ),
                None,
            )
        return None

    def encode_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = _copy_mapping(payload)
        encoded_tools, tools_changed = self._encode_tool_declarations(result.get("tools"))
        if tools_changed:
            result["tools"] = encoded_tools
        chosen, choice_changed = self._encode_tool_choice(result.get("tool_choice"))
        if choice_changed:
            result["tool_choice"] = chosen
        call_aliases: dict[str, str] = {}
        raw_input = result.get("input")
        if isinstance(raw_input, list):
            self._validate_collaboration_v2_items(raw_input, surface="history")
            encoded_input: list[Any] = []
            changed = tools_changed or choice_changed
            history_call_owners: dict[str, ToolCompatibilityEntry] = {}
            history_call_positions: dict[str, int] = {}
            seen_history_call_ids: dict[str, ToolCompatibilityEntry | None] = {}
            for item_index, item in enumerate(raw_input):
                if not isinstance(item, Mapping) or item.get("type") not in {
                    "function_call",
                    "custom_tool_call",
                    "tool_search_call",
                }:
                    continue
                owner = self._entry_for_call_owner(item, surface="history")
                call_id = item.get("call_id")
                if isinstance(call_id, str) and call_id:
                    if call_id in seen_history_call_ids:
                        previous = seen_history_call_ids[call_id]
                        classification = (
                            "ambiguous_call_identity"
                            if previous is not None
                            and owner is not None
                            and (previous.disposition == OMIT or owner.disposition == OMIT)
                            else "duplicate_call_identity"
                        )
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            classification,
                            surface="history",
                        )
                    seen_history_call_ids[call_id] = owner
                    if owner is not None:
                        history_call_owners[call_id] = owner
                        history_call_positions[call_id] = item_index

            def omitted_history_entry(item: Mapping[str, Any]) -> ToolCompatibilityEntry | None:
                owner = None
                if item.get("type") in {
                    "function_call_output",
                    "custom_tool_call_output",
                    "tool_search_output",
                }:
                    call_id = item.get("call_id")
                    if isinstance(call_id, str):
                        owner = history_call_owners.get(call_id)
                return self._omitted_history_entry(item, owner=owner)

            def validate_history_output_owner(item: Mapping[str, Any], item_index: int) -> None:
                item_type = item.get("type")
                is_result = isinstance(item_type, str) and (
                    item_type == "tool_search_output" or item_type.endswith("_call_output")
                )
                if not is_result:
                    return
                omitted_entry = omitted_history_entry(item)
                if omitted_entry is not None:
                    return
                call_id = item.get("call_id")
                owner = history_call_owners.get(call_id) if isinstance(call_id, str) else None
                if owner is None:
                    # A standard output can be retained when the call lives
                    # outside this request's history slice.  Unknown output
                    # families have no such safe interpretation: without a
                    # request-local owner they must fail closed.
                    if item_type in {
                        "function_call_output",
                        "custom_tool_call_output",
                        "tool_search_output",
                    }:
                        return
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unknown_call_identity",
                        surface="history",
                    )
                self._validate_response_owner_item(
                    item,
                    owner=owner,
                    call_index=history_call_positions.get(call_id),
                    item_index=item_index,
                    surface="history",
                )

            seen_history_item_ids: dict[str, bool] = {}
            seen_history_output_call_ids: set[str] = set()
            for item_index, item in enumerate(raw_input):
                if not isinstance(item, Mapping):
                    continue
                item_type = item.get("type")
                validate_history_output_owner(item, item_index)
                omitted_entry = omitted_history_entry(item)
                hosted_item_key = self._hosted_history_item_key(item_type)
                is_optional_omitted_hosted = omitted_entry is not None and hosted_item_key is not None
                if not is_optional_omitted_hosted:
                    item_id = _item_identity(item)
                    if item_id is not None:
                        # Older Codex Desktop rollouts reused a synthetic id
                        # such as ``message_1`` for ordinary message items.
                        # Permit only that message-to-message duplicate; dual
                        # IDs and collisions with every other history family
                        # remain fail-closed.
                        legacy_message = (
                            item_type == "message" and _is_legacy_message_identity(item_id)
                        )
                        prior_legacy_message = seen_history_item_ids.get(item_id)
                        if prior_legacy_message is not None and not (
                            prior_legacy_message and legacy_message
                        ):
                            raise ToolCompatibilityError(
                                "tool_compatibility_boundary",
                                "duplicate_item_identity",
                                surface="history",
                            )
                        seen_history_item_ids.setdefault(item_id, legacy_message)
                if item_type in {"function_call", "custom_tool_call", "tool_search_call"}:
                    call_id = item.get("call_id")
                    if omitted_entry is None and (not isinstance(call_id, str) or not call_id):
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "missing_call_identity",
                            surface="history",
                        )
                if isinstance(item_type, str) and (
                    item_type == "tool_search_output" or item_type.endswith("_call_output")
                ):
                    call_id = item.get("call_id")
                    if (
                        not is_optional_omitted_hosted
                        and isinstance(call_id, str)
                        and call_id
                    ):
                        if call_id in seen_history_output_call_ids:
                            raise ToolCompatibilityError(
                                "tool_compatibility_boundary",
                                "duplicate_call_identity",
                                surface="history",
                            )
                        seen_history_output_call_ids.add(call_id)

            omitted_hosted_ids: dict[str, tuple[str, bool, bool]] = {}
            for item in raw_input:
                if not isinstance(item, Mapping):
                    continue
                omitted_entry = omitted_history_entry(item)
                hosted_item_key = self._hosted_history_item_key(item.get("type"))
                if (
                    omitted_entry is None
                    or hosted_item_key is None
                ):
                    continue
                item_id = _item_identity(item)
                if item_id is None:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "missing_item_identity",
                        surface="history",
                    )
                hosted_item_kind, is_output = hosted_item_key
                previous = omitted_hosted_ids.get(item_id)
                if previous is not None:
                    previous_kind, previous_is_output, pair_consumed = previous
                    if previous_kind != hosted_item_kind:
                        classification = "ambiguous_native_identity"
                    elif pair_consumed or previous_is_output == is_output:
                        classification = "duplicate_item_identity"
                    else:
                        # Some hosted protocols use one item id for call/result
                        # pairs; others use distinct ids.  Both are valid when
                        # the hosted kind is already uniquely omitted.
                        omitted_hosted_ids[item_id] = (hosted_item_kind, is_output, True)
                        continue
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        classification,
                        surface="history",
                    )
                omitted_hosted_ids[item_id] = (hosted_item_kind, is_output, False)
            omitted_call_items = [
                item
                for item in raw_input
                if isinstance(item, Mapping)
                and omitted_history_entry(item) is not None
                and not (
                    self._hosted_history_item_key(item.get("type")) is not None
                    if omitted_history_entry(item) is not None
                    else False
                )
                and (
                    item.get("type") in {"function_call", "custom_tool_call", "tool_search_call"}
                    or (isinstance(item.get("type"), str) and item.get("type").endswith("_call"))
                )
            ]
            omitted_call_ids: set[str] = set()
            for item in omitted_call_items:
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="history")
                if call_id in omitted_call_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_call_identity", surface="history")
                omitted_call_ids.add(call_id)
            retained_call_ids = {
                item.get("call_id")
                for item in raw_input
                if isinstance(item, Mapping)
                and (
                    item.get("type") in {"function_call", "custom_tool_call", "tool_search_call"}
                    or (isinstance(item.get("type"), str) and item.get("type").endswith("_call"))
                )
                and omitted_history_entry(item) is None
                and isinstance(item.get("call_id"), str)
            }
            if omitted_call_ids.intersection(retained_call_ids):
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="history")
            for item in raw_input:
                if not isinstance(item, Mapping):
                    continue
                if item.get("type") in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
                    call_id = item.get("call_id")
                    if not isinstance(call_id, str) or not call_id:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="history")
                    if omitted_history_entry(item) is not None and call_id not in omitted_call_ids:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="history")
            for item in raw_input:
                if isinstance(item, Mapping) and (
                    (
                        omitted_history_entry(item) is not None
                        and self._hosted_history_item_key(item.get("type")) is None
                    )
                    or item.get("call_id") in omitted_call_ids
                ):
                    changed = True
                    continue
                if isinstance(item, Mapping):
                    omitted_entry = omitted_history_entry(item)
                    hosted_item_key = self._hosted_history_item_key(item.get("type"))
                    if (
                        omitted_entry is not None
                        and hosted_item_key is not None
                    ):
                        changed = True
                        continue
                encoded, item_changed = self._encode_item(item, call_aliases)
                encoded_input.append(encoded)
                changed = changed or item_changed
            if changed:
                result["input"] = encoded_input
        return result

    def _decode_call(self, item: Mapping[str, Any], *, allow_incomplete: bool = False) -> tuple[dict[str, Any], AliasRecord | None, bool]:
        result = _copy_mapping(item)
        name = result.get("name")
        record = self.registry.record_for_alias(name)
        if record is None:
            if self.registry.looks_like_alias(name):
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="response")
            return result, None, False
        _validate_version_fields(result, record)
        if record.family == NAMESPACE and result.get("namespace") not in {None, record.namespace}:
            raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="response")
        if record.family == CUSTOM_FREEFORM and result.get("namespace") is not None:
            raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="response")
        call_id = result.get("call_id")
        if isinstance(call_id, str) and call_id:
            previous = self.registry.record_for_call(call_id)
            if previous is not None and previous.alias != record.alias:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_call_identity",
                    surface="response",
                )
            if previous is None:
                self.registry.bind_call(call_id, record.alias)
        elif not allow_incomplete:
            raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="response")
        result["name"] = record.child_name if record.family == NAMESPACE else record.original_name
        if record.family == NAMESPACE:
            result["namespace"] = record.namespace
            apply_v2_namespace_decode(result, record)
        elif record.family == CUSTOM_FREEFORM:
            if "arguments" in result:
                envelope = _json_object_exact(result["arguments"])
                result["type"] = "custom_tool_call"
                result["input"] = envelope[CUSTOM_INPUT_KEY]
                result.pop("arguments", None)
            elif not allow_incomplete:
                raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope", surface="response")
            else:
                result["type"] = "custom_tool_call"
        elif record.family == TOOL_SEARCH:
            result.pop("name", None)
            result.pop("namespace", None)
            result["type"] = "tool_search_call"
            result["execution"] = "client"
            if "arguments" in result and result.get("arguments") is not None and result.get("arguments") != "":
                envelope = _json_object_with_key(result["arguments"], TOOL_SEARCH_INPUT_KEY)
                result["arguments"] = envelope[TOOL_SEARCH_INPUT_KEY]
            elif not allow_incomplete:
                raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope", surface="response")
        return result, record, True

    def _legacy_tool_search_record(self, item: Mapping[str, Any]) -> AliasRecord | None:
        """Return the one adapted search record for legacy function-shaped SSE.

        Older third-party Responses routes emit the original ``tool_search``
        function name rather than the request-scoped alias.  Accept that shape
        only when this request has exactly one explicit client search entry;
        an unrelated provider function with the same name remains plain.
        """

        if item.get("type") != "function_call" or item.get("name") != "tool_search":
            return None
        # A request may legitimately contain a provider-owned ordinary
        # function with the same spelling as Codex's client search.  Once
        # that declaration exists, the unqualified legacy wire item is
        # ambiguous and must remain provider-owned instead of being silently
        # rebound to the adapted search alias.
        if any(
            entry.family == PLAIN_FUNCTION and entry.original_name == "tool_search"
            for entry in self.entries
        ):
            return None
        candidates = [
            entry
            for entry in self.entries
            if entry.family == TOOL_SEARCH
            and entry.original_name == "tool_search"
            and entry.disposition == ADAPT
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _decode_call_compat(
        self,
        item: Mapping[str, Any],
        *,
        allow_incomplete: bool = False,
    ) -> tuple[dict[str, Any], AliasRecord | None, bool]:
        legacy_record = self._legacy_tool_search_record(item)
        if legacy_record is None:
            return self._decode_call(item, allow_incomplete=allow_incomplete)
        transformed = _copy_mapping(item)
        transformed["name"] = legacy_record.aliases[0]
        arguments = transformed.get("arguments")
        if arguments is not None and arguments != "":
            if isinstance(arguments, str):
                try:
                    arguments_value = json.loads(arguments)
                except (TypeError, ValueError) as exc:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "malformed_envelope",
                        surface="response",
                    ) from exc
            elif isinstance(arguments, Mapping):
                arguments_value = arguments
            else:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "malformed_envelope",
                    surface="response",
                )
            transformed["arguments"] = json.dumps(
                {TOOL_SEARCH_INPUT_KEY: arguments_value},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        return self._decode_call(transformed, allow_incomplete=allow_incomplete)

    def _validate_registered_item_identity(self, item: Mapping[str, Any], *, surface: str) -> None:
        """Ensure a bound adapter call keeps its wire family and alias."""
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return
        record = self.registry.record_for_call(call_id)
        if record is None:
            return
        item_type = item.get("type")
        if item_type == "function_call":
            if item.get("name") != record.alias:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_call_identity",
                    surface=surface,
                )
            return
        if item_type == "function_call_output":
            if record.family == CUSTOM_FREEFORM and item.get("namespace") is not None:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unknown_alias",
                    surface=surface,
                )
            return
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "ambiguous_call_identity",
            surface=surface,
        )

    def _decode_result(self, item: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        result = _copy_mapping(item)
        call_id = result.get("call_id")
        record = self.registry.record_for_call(call_id)
        if record is None:
            if self.registry.looks_like_alias(result.get("name")):
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="response")
            return result, False
        _validate_version_fields(result, record)
        if record.family == CUSTOM_FREEFORM:
            if "output" not in result:
                raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope", surface="response")
            envelope = _json_object_exact(result["output"], output=True)
            result["type"] = "custom_tool_call_output"
            result["output"] = envelope[CUSTOM_OUTPUT_KEY]
        elif record.family == TOOL_SEARCH:
            envelope = _json_object_with_key(result.get("output"), TOOL_SEARCH_OUTPUT_KEY)
            payload = envelope[TOOL_SEARCH_OUTPUT_KEY]
            if not isinstance(payload, Mapping):
                raise ToolCompatibilityError("tool_compatibility_boundary", "invalid_envelope", surface="response")
            result["type"] = "tool_search_output"
            result["execution"] = "client"
            result.pop("output", None)
            for key, value in payload.items():
                if key in {"type", "execution", "id", "item_id", "call_id"}:
                    continue
                result[key] = value
        return result, True

    def _native_entry_for_item(self, item: Mapping[str, Any]) -> ToolCompatibilityEntry | None:
        item_type = item.get("type")
        if item_type in {"function_call", "custom_tool_call"}:
            entry = self._entry_for_name(
                item.get("name"),
                item.get("namespace"),
                item_type=item_type,
            )
            if entry is not None and entry.disposition == NATIVE:
                return entry
            # A native plain declaration can be surfaced by providers using
            # the explicit namespace/child shape that normally belongs to a
            # native namespace declaration.  Resolve only the exact,
            # injective ``namespace__child`` spelling; arbitrary namespace
            # decoration must remain an unknown identity.
            if item_type == "function_call":
                return self._flattened_native_plain_entry(item)
            return None
        if item_type in {"tool_search_call", "tool_search_output"}:
            matches = [
                entry
                for entry in self.entries
                if entry.family == TOOL_SEARCH and entry.disposition == NATIVE
            ]
            return matches[0] if len(matches) == 1 else None
        hosted_item_spec = _hosted_event_spec_for_item_type(item_type)
        if hosted_item_spec is not None and item_type == hosted_item_spec[0]:
            hosted_event_kind, _stages = hosted_item_spec
            matches = [
                entry
                for entry in self.entries
                if entry.family == SELECTED_PROVIDER_HOSTED
                and entry.disposition == NATIVE
                and (
                    declaration_spec := _hosted_event_spec_for_declaration_kind(entry.declaration.get("type"))
                ) is not None
                and declaration_spec[0] == hosted_event_kind
            ]
            if len(matches) > 1:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity")
            return matches[0] if matches else None
        return None

    def _has_native_structural_family(self) -> bool:
        return any(
            entry.disposition == NATIVE
            and entry.family in {PLAIN_FUNCTION, NAMESPACE, CUSTOM_FREEFORM, TOOL_SEARCH}
            for entry in self.entries
        )

    @staticmethod
    def _validate_native_item(
        item: Mapping[str, Any],
        entry: ToolCompatibilityEntry,
        *,
        require_completed: bool = False,
        surface: str = "history",
    ) -> None:
        if entry.version is not None:
            _validate_version_fields(
                item,
                AliasRecord(
                    alias="",
                    family=entry.family,
                    declaration_index=entry.declaration_index,
                    child_index=None,
                    namespace=entry.namespace,
                    child_name=item.get("name") if isinstance(item.get("name"), str) else None,
                    original_name=entry.original_name,
                    version=entry.version,
                ),
            )
        if (
            entry.version == "v2"
            and entry.family == NAMESPACE
            and item.get("arguments") is not None
            and item.get("arguments") != ""
        ):
            validate_v2_native_arguments(item, surface=surface)
        item_type = item.get("type")
        if entry.family == PLAIN_FUNCTION:
            validate_plain_native_item(item, entry, surface=surface)
        elif entry.family == NAMESPACE:
            if (
                item_type != "function_call"
                or item.get("namespace") != entry.namespace
                or item.get("name") not in entry.child_names
            ):
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_native_identity", surface=surface)
        elif entry.family == CUSTOM_FREEFORM:
            if item_type != "custom_tool_call" or item.get("name") != entry.original_name:
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_native_identity", surface=surface)
        elif entry.family == TOOL_SEARCH:
            if item_type not in {"tool_search_call", "tool_search_output"}:
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_native_identity", surface=surface)
            if item.get("execution") != "client":
                raise ToolCompatibilityError("tool_compatibility_boundary", "invalid_tool_search_execution", surface=surface)
        elif entry.family == SELECTED_PROVIDER_HOSTED:
            hosted_spec = _hosted_event_spec_for_declaration_kind(entry.declaration.get("type"))
            if hosted_spec is None or item_type != hosted_spec[0]:
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_native_identity", surface=surface)
            if _item_identity(item) is None:
                raise ToolCompatibilityError("tool_compatibility_boundary", "missing_item_identity", surface=surface)
            if require_completed and item.get("status") not in {None, "completed"}:
                raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_hosted_lifecycle", surface=surface)

    def _decode_items(
        self,
        items: Any,
        *,
        reject_omitted_response: bool = False,
        decode_agent_messages: bool = True,
    ) -> tuple[Any, bool]:
        if not isinstance(items, list):
            return items, False
        result: list[Any] = []
        changed = False
        seen_item_ids: set[str] = set()
        seen_call_ids: set[str] = set()
        seen_result_call_ids: set[str] = set()
        response_call_owners: dict[str, ToolCompatibilityEntry] = {}
        response_call_positions: dict[str, int] = {}
        surface = "response" if reject_omitted_response else "history"
        for item_index, raw_item in enumerate(items):
            if not isinstance(raw_item, Mapping):
                continue
            if raw_item.get("type") not in {"function_call", "custom_tool_call", "tool_search_call"}:
                continue
            call_id = raw_item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            owner = self._entry_for_call_owner(raw_item, surface=surface)
            if owner is None:
                continue
            previous = response_call_owners.get(call_id)
            if previous is not None:
                classification = (
                    "ambiguous_call_identity"
                    if previous.disposition == OMIT or owner.disposition == OMIT
                    else "duplicate_call_identity"
                )
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    classification,
                    surface=surface,
                )
            response_call_owners[call_id] = owner
            response_call_positions[call_id] = item_index
        response_output_call_ids: set[str] = set()
        for item_index, raw_item in enumerate(items):
            if not isinstance(raw_item, Mapping):
                continue
            call_id = raw_item.get("call_id")
            item_type = raw_item.get("type")
            if isinstance(item_type, str) and (
                item_type == "tool_search_output" or item_type.endswith("_call_output")
            ):
                if isinstance(call_id, str) and call_id:
                    if call_id in response_output_call_ids:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "duplicate_call_identity",
                            surface=surface,
                        )
                    response_output_call_ids.add(call_id)
                owner = response_call_owners.get(call_id) if isinstance(call_id, str) else None
                self._validate_response_owner_item(
                    raw_item,
                    owner=owner,
                    call_index=response_call_positions.get(call_id) if isinstance(call_id, str) else None,
                    item_index=item_index,
                    surface=surface,
                )
        for raw_item in items:
            if not isinstance(raw_item, Mapping):
                result.append(raw_item)
                continue
            item_type = raw_item.get("type")
            item = _copy_mapping(raw_item)
            call_id = item.get("call_id")
            owner = response_call_owners.get(call_id) if isinstance(call_id, str) else None
            self._validate_registered_item_identity(item, surface=surface)
            if reject_omitted_response and self._omitted_response_entry_for_item(item, owner=owner) is not None:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unsupported_hosted_lifecycle",
                    surface=surface,
                )
            item_id = _item_identity(item)
            if item_type in {"function_call", "custom_tool_call", "tool_search_call"}:
                if not isinstance(call_id, str) or not call_id:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="history")
            if isinstance(item_id, str) and item_id:
                if item_id in seen_item_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="history")
                seen_item_ids.add(item_id)
            if isinstance(call_id, str) and call_id:
                if call_id in seen_call_ids and item_type in {"function_call", "custom_tool_call", "tool_search_call"}:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_call_identity", surface="history")
                if item_type in {"function_call", "custom_tool_call", "tool_search_call"}:
                    seen_call_ids.add(call_id)
            if item_type == "function_call":
                record = self.registry.record_for_alias(item.get("name"))
                legacy_record = self._legacy_tool_search_record(item)
                if record is not None or legacy_record is not None:
                    decoded, _record, item_changed = self._decode_call_compat(item)
                else:
                    native_entry = self._native_entry_for_item(item)
                    if native_entry is not None:
                        self._validate_native_item(item, native_entry)
                        decoded, item_changed = item, False
                    elif self.registry.looks_like_alias(item.get("name")):
                        raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="history")
                    elif self._has_adapted_name_conflict(item.get("name"), PLAIN_FUNCTION):
                        raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="history")
                    elif self.has_adaptations and (
                        self._entry_for_name(
                            item.get("name"),
                            item.get("namespace"),
                            item_type=item_type,
                        ) is not None
                        or (
                            item.get("namespace") is None
                            and self._has_unqualified_adapted_child(item.get("name"))
                        )
                    ):
                        raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="history")
                    else:
                        self._reject_unknown_standard_item(item, surface=surface)
                        decoded, item_changed = item, False
            elif item_type == "custom_tool_call":
                native_entry = self._native_entry_for_item(item)
                if native_entry is not None:
                    self._validate_native_item(item, native_entry)
                    decoded, item_changed = item, False
                elif self.has_adaptations and (
                    self._entry_for_name(
                        item.get("name"),
                        item.get("namespace"),
                        item_type=item_type,
                    ) is not None
                    or (
                        item.get("namespace") is None
                        and self._has_unqualified_adapted_child(item.get("name"))
                    )
                ):
                    raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="history")
                else:
                    self._reject_unknown_standard_item(item, surface=surface)
                    decoded, item_changed = item, False
            elif item_type == "tool_search_call":
                native_entry = self._native_entry_for_item(item)
                if native_entry is not None:
                    self._validate_native_item(item, native_entry)
                    decoded, item_changed = item, False
                elif self._omitted_response_entry_for_item(item, owner=owner) is not None:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unsupported_hosted_lifecycle",
                        surface=surface,
                    )
                else:
                    self._reject_unknown_standard_item(item, surface=surface)
                    decoded, item_changed = item, False
            elif _hosted_kind_for_item_type(item_type) is not None:
                native_entry = self._native_entry_for_item(item)
                if native_entry is None:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "unsupported_hosted_lifecycle", surface="history")
                self._validate_native_item(item, native_entry, require_completed=True)
                decoded, item_changed = item, False
            elif isinstance(item_type, str) and item_type.endswith("_call"):
                raise ToolCompatibilityError("tool_compatibility_boundary", "unsupported_hosted_lifecycle", surface="history")
            elif item_type in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
                if not isinstance(call_id, str) or not call_id:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="history")
                if call_id in seen_result_call_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_call_identity", surface="history")
                seen_result_call_ids.add(call_id)
                decoded, item_changed = self._decode_result(item)
                if not item_changed and self.has_adaptations and not self._has_native_structural_family():
                    raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_call_identity", surface="history")
                if item_type == "tool_search_output":
                    native_entry = self._native_entry_for_item(item)
                    if native_entry is not None:
                        self._validate_native_item(item, native_entry)
                    elif self.has_adaptations and not self._has_native_structural_family():
                        raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_call_identity", surface="history")
            elif _hosted_output_kind_for_item_type(item_type) is not None:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unsupported_hosted_lifecycle",
                    surface=surface,
                )
            elif decode_agent_messages and item_type == "message":
                decoded, item_changed = self._decode_agent_message(item)
            else:
                decoded, item_changed = item, False
                if self.registry.looks_like_alias(item.get("name")):
                    raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="history")
            result.append(decoded)
            changed = changed or item_changed
        self._validate_collaboration_v2_items(result, surface=surface)
        return result, changed

    def decode_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = _copy_mapping(payload)
        for key in ("output", "input", "history"):
            if key in result:
                decoded, item_changed = self._decode_items(
                    result[key],
                    reject_omitted_response=key == "output",
                    decode_agent_messages=key != "output",
                )
                if item_changed:
                    result[key] = decoded
        return result

    def encode_history(self, items: Iterable[Mapping[str, Any]]) -> list[Any]:
        payload = self.encode_payload({"input": list(items)})
        return payload.get("input", [])

    def decode_history(self, items: Iterable[Mapping[str, Any]]) -> list[Any]:
        payload = self.decode_payload({"input": list(items)})
        return payload.get("input", [])


# Short aliases used by route/request callers and by compatibility-focused
# tests.  They intentionally all construct the same immutable plan.
build_plan = build_tool_compatibility_plan
ToolPlan = ToolCompatibilityPlan
AliasRegistry = RequestScopedToolAliasRegistry


__all__ = [
    "ADAPT",
    "AliasRecord",
    "AliasRegistry",
    "CompatibilityDiagnostics",
    "CompatibilityStreamState",
    "CUSTOM_FREEFORM",
    "CUSTOM_INPUT_KEY",
    "CUSTOM_OUTPUT_KEY",
    "TOOL_SEARCH_INPUT_KEY",
    "TOOL_SEARCH_OUTPUT_KEY",
    "HostedCapabilityFacts",
    "NAMESPACE",
    "NATIVE",
    "OMIT",
    "PLAIN_FUNCTION",
    "ProtocolCapabilities",
    "REQUIRED_BUT_UNAVAILABLE",
    "RequiredToolUnavailableError",
    "RequestScopedToolAliasRegistry",
    "SELECTED_PROVIDER_HOSTED",
    "TOOL_SEARCH",
    "ToolCompatibilityEntry",
    "ToolCompatibilityError",
    "ToolCompatibilityPlan",
    "ToolPlan",
    "UNKNOWN_FUTURE_KIND",
    "build_plan",
    "build_tool_compatibility_plan",
    "classify_declaration",
]


def __getattr__(name: str):
    if name == "CompatibilityStreamState":
        from .stream import CompatibilityStreamState

        return CompatibilityStreamState
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

