"""Request-scoped alias registry for adapted tool declarations."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
from typing import Any, Iterable, Mapping

from .collab_v2 import AGENT_MESSAGE_ENVELOPE_PREFIX
from .contracts import ToolCompatibilityEntry, ToolCompatibilityError, _copy_mapping
from .dispositions import CUSTOM_FREEFORM, NAMESPACE, TOOL_SEARCH

_NAMESPACE_ALIAS_PREFIX = "__codexhub_ns_"
_CUSTOM_ALIAS_PREFIX = "__codexhub_custom_"
_TOOL_SEARCH_ALIAS_PREFIX = "__codexhub_search_"

@dataclass(frozen=True, slots=True)
class CompatibilityDiagnostics:
    counts: tuple[tuple[str, str, int], ...] = ()
    failures: tuple[str, ...] = ()

    @classmethod
    def from_entries(cls, entries: Iterable[ToolCompatibilityEntry]) -> "CompatibilityDiagnostics":
        counts = Counter((entry.family, entry.disposition) for entry in entries)
        return cls(
            counts=tuple(
                (family, disposition, count)
                for (family, disposition), count in sorted(counts.items())
            )
        )

    def __repr__(self) -> str:
        return f"CompatibilityDiagnostics(counts={self.counts!r}, failures={self.failures!r})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": [
                {"family": family, "disposition": disposition, "count": count}
                for family, disposition, count in self.counts
            ],
            "failure_classifications": list(self.failures),
        }


@dataclass(frozen=True, slots=True)
class AliasRecord:
    alias: str
    family: str
    declaration_index: int
    child_index: int | None
    namespace: str | None
    child_name: str | None
    original_name: str | None
    version: str | None


class RequestScopedToolAliasRegistry:
    """One request-local, opaque alias map with explicit reverse lookups."""

    def __init__(
        self,
        *,
        request_token: str,
        native_names: Iterable[str] = (),
        max_tool_name_length: int = 128,
        max_alias_attempts: int = 128,
    ) -> None:
        self._token = hashlib.sha256(str(request_token).encode("utf-8")).hexdigest()[:10]
        self._native_names = frozenset(str(name) for name in native_names if isinstance(name, str))
        self._max_length = max_tool_name_length
        self._max_attempts = max_alias_attempts
        self._aliases: dict[str, AliasRecord] = {}
        self._remapped_aliases: dict[str, str] = {}
        self._by_declaration: dict[tuple[int, int | None], str] = {}
        self._calls: dict[str, AliasRecord] = {}
        self._agent_messages: dict[str, dict[str, Any]] = {}
        # ``max_alias_attempts`` bounds collision probing for one allocation;
        # it must not cap the total number of aliases in a request.  Keep the
        # next ordinal per alias family so a request with more than that many
        # adapted namespace/custom tools remains representable.
        self._next_ordinals: dict[str, int] = {}

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(self._aliases)

    def reserve_native_names(self, names: Iterable[str]) -> dict[str, str]:
        requested = {str(name) for name in names if isinstance(name, str) and name}
        self._native_names = frozenset(
            set(self._native_names)
            | requested
        )
        remapped: dict[str, str] = {}
        for alias, record in tuple(self._aliases.items()):
            if alias not in requested:
                continue
            self._aliases.pop(alias, None)
            self._by_declaration.pop((record.declaration_index, record.child_index), None)
            prefix = {
                NAMESPACE: _NAMESPACE_ALIAS_PREFIX,
                CUSTOM_FREEFORM: _CUSTOM_ALIAS_PREFIX,
                TOOL_SEARCH: _TOOL_SEARCH_ALIAS_PREFIX,
            }.get(record.family, _CUSTOM_ALIAS_PREFIX)
            replacement = self._allocate(record, prefix)
            remapped[alias] = replacement
            self._remapped_aliases[alias] = replacement
        return remapped

    def is_native_name(self, value: Any) -> bool:
        return isinstance(value, str) and value in self._native_names

    @staticmethod
    def looks_like_alias(value: Any) -> bool:
        return isinstance(value, str) and value.startswith(
            (_NAMESPACE_ALIAS_PREFIX, _CUSTOM_ALIAS_PREFIX, _TOOL_SEARCH_ALIAS_PREFIX)
        )

    def _allocate(self, record_without_alias: AliasRecord, prefix: str) -> str:
        start_ordinal = self._next_ordinals.get(prefix, 1)
        for offset in range(self._max_attempts):
            ordinal = start_ordinal + offset
            candidate = f"{prefix}{self._token}_{ordinal}"
            if len(candidate) > self._max_length:
                raise ToolCompatibilityError(
                    "tool_compatibility_alias_limit",
                    "alias_length_exhausted",
                )
            if candidate in self._native_names or candidate in self._aliases:
                continue
            record = AliasRecord(
                alias=candidate,
                family=record_without_alias.family,
                declaration_index=record_without_alias.declaration_index,
                child_index=record_without_alias.child_index,
                namespace=record_without_alias.namespace,
                child_name=record_without_alias.child_name,
                original_name=record_without_alias.original_name,
                version=record_without_alias.version,
            )
            self._aliases[candidate] = record
            self._by_declaration[(record.declaration_index, record.child_index)] = candidate
            self._next_ordinals[prefix] = ordinal + 1
            return candidate
        raise ToolCompatibilityError(
            "tool_compatibility_alias_limit",
            "alias_collision_exhausted",
        )

    def allocate_namespace(
        self,
        *,
        declaration_index: int,
        namespace: str,
        child_index: int,
        child_name: str,
        version: str | None,
    ) -> str:
        return self._allocate(
            AliasRecord(
                alias="",
                family=NAMESPACE,
                declaration_index=declaration_index,
                child_index=child_index,
                namespace=namespace,
                child_name=child_name,
                original_name=child_name,
                version=version,
            ),
            _NAMESPACE_ALIAS_PREFIX,
        )

    def allocate_custom(self, *, declaration_index: int, original_name: str, version: str | None) -> str:
        return self._allocate(
            AliasRecord(
                alias="",
                family=CUSTOM_FREEFORM,
                declaration_index=declaration_index,
                child_index=None,
                namespace=None,
                child_name=None,
                original_name=original_name,
                version=version,
            ),
            _CUSTOM_ALIAS_PREFIX,
        )

    def allocate_tool_search(self, *, declaration_index: int) -> str:
        return self._allocate(
            AliasRecord(
                alias="",
                family=TOOL_SEARCH,
                declaration_index=declaration_index,
                child_index=None,
                namespace=None,
                child_name="tool_search",
                original_name="tool_search",
                version=None,
            ),
            _TOOL_SEARCH_ALIAS_PREFIX,
        )

    def record_for_alias(self, alias: Any) -> AliasRecord | None:
        return self._aliases.get(alias) if isinstance(alias, str) else None

    def remapped_alias(self, alias: Any) -> str | None:
        if not isinstance(alias, str):
            return None
        current = alias
        seen: set[str] = set()
        while current not in seen:
            seen.add(current)
            replacement = self._remapped_aliases.get(current)
            if replacement is None:
                return current if current != alias else None
            current = replacement
        return None

    def alias_for(self, declaration_index: int, child_index: int | None = None) -> str | None:
        return self._by_declaration.get((declaration_index, child_index))

    def bind_call(self, call_id: Any, alias: str) -> None:
        if not isinstance(call_id, str) or not call_id:
            raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity")
        record = self.record_for_alias(alias)
        if record is None:
            raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias")
        previous = self._calls.get(call_id)
        if previous is not None and previous.alias != alias:
            raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity")
        if previous is not None:
            raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_call_identity")
        self._calls[call_id] = record

    def record_for_call(self, call_id: Any) -> AliasRecord | None:
        return self._calls.get(call_id) if isinstance(call_id, str) else None

    def bind_agent_message(self, envelope: str, item: Mapping[str, Any]) -> None:
        if not envelope.startswith(AGENT_MESSAGE_ENVELOPE_PREFIX):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "invalid_agent_message_envelope",
                surface="history",
            )
        previous = self._agent_messages.get(envelope)
        if previous is not None:
            if previous == item:
                return
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "duplicate_item_identity",
                surface="history",
            )
        self._agent_messages[envelope] = _copy_mapping(item)

    def agent_message_for_envelope(self, envelope: Any) -> dict[str, Any] | None:
        if not isinstance(envelope, str):
            return None
        item = self._agent_messages.get(envelope)
        return _copy_mapping(item) if item is not None else None

    def new_attempt(self) -> "RequestScopedToolAliasRegistry":
        """Copy immutable alias ownership while starting a fresh call ledger.

        Alias allocation and declaration ownership are request-scoped and must
        remain stable across permitted upstream retries.  Call ownership is
        stream-attempt scoped, however: a provider may legitimately reuse a
        call id after a transport failure, so a retry cannot inherit ``_calls``
        from the failed attempt.
        """
        attempt = object.__new__(RequestScopedToolAliasRegistry)
        attempt._token = self._token
        attempt._native_names = self._native_names
        attempt._max_length = self._max_length
        attempt._max_attempts = self._max_attempts
        attempt._aliases = dict(self._aliases)
        attempt._remapped_aliases = dict(self._remapped_aliases)
        attempt._by_declaration = dict(self._by_declaration)
        attempt._calls = {}
        attempt._agent_messages = dict(self._agent_messages)
        attempt._next_ordinals = dict(self._next_ordinals)
        return attempt


