"""Give xAI reasoning occurrences distinct client IDs while retaining wire IDs."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping

from runtime_tool_compatibility import ToolCompatibilityError


PREFIX = "rs_codexhub_xai_"


def _error(classification: str, surface: str) -> ToolCompatibilityError:
    return ToolCompatibilityError("tool_compatibility_boundary", classification, surface=surface)


def _client_id(response_id: str, index: int, wire_id: str) -> str:
    source = json.dumps([response_id, index, wire_id], separators=(",", ":")).encode()
    digest = sha256(source).hexdigest()[:32]
    return PREFIX + digest


def normalize_response(response: Mapping[str, Any], *, surface: str = "response") -> dict[str, Any]:
    """Map each reasoning occurrence, rejecting raw cross-family collisions."""
    result = dict(response)
    output = response.get("output")
    if not isinstance(output, list):
        return result
    response_id = response.get("id")
    raw_types: dict[str, str] = {}
    mapped: list[Any] = []
    for index, item in enumerate(output):
        if not isinstance(item, Mapping):
            mapped.append(item)
            continue
        item_type = item.get("type")
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            previous = raw_types.get(item_id)
            if item_id in raw_types and (previous != "reasoning" or item_type != "reasoning"):
                raise _error("duplicate_item_identity", surface)
            raw_types[item_id] = item_type
        if item_type == "reasoning" and isinstance(item_id, str) and item_id:
            if not isinstance(response_id, str) or not response_id:
                raise _error("missing_stream_identity", surface)
            mapped.append({**item, "id": _client_id(response_id, index, item_id)})
        else:
            mapped.append(item)
    result["output"] = mapped
    return result


class StreamIds:
    """Request attempt local ownership of xAI's indexed reasoning events."""

    def __init__(self) -> None:
        self.response_id: str | None = None
        self.raw_types: dict[str, str] = {}
        self.added: dict[int, str] = {}
        self.done: set[int] = set()

    def normalize(self, event: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(event)
        event_type = event.get("type")
        response = event.get("response")
        if event_type == "response.created" and isinstance(response, Mapping):
            response_id = response.get("id")
            if isinstance(response_id, str) and response_id:
                self.response_id = response_id
        if event_type in {"response.completed", "response.incomplete", "response.failed"} and isinstance(response, Mapping):
            if self.response_id is not None and response.get("id") != self.response_id:
                raise _error("ambiguous_native_identity", "stream")
            normalized = normalize_response(response, surface="stream")
            output = response.get("output")
            if isinstance(output, list) and self.added:
                for index, raw_id in self.added.items():
                    if index >= len(output) or not isinstance(output[index], Mapping) or output[index].get("type") != "reasoning" or output[index].get("id") != raw_id or index not in self.done:
                        raise _error("incomplete_stream", "stream")
                for index, item in enumerate(output):
                    if isinstance(item, Mapping) and item.get("type") == "reasoning" and index not in self.added:
                        raise _error("missing_stream_identity", "stream")
            result["response"] = normalized
            return result

        item = event.get("item")
        index = event.get("output_index")
        if event_type == "response.output_item.added" and isinstance(item, Mapping):
            raw_id = item.get("id")
            item_type = item.get("type")
            if isinstance(raw_id, str) and raw_id:
                previous = self.raw_types.get(raw_id)
                if raw_id in self.raw_types and (previous != "reasoning" or item_type != "reasoning"):
                    raise _error("duplicate_item_identity", "stream")
                self.raw_types[raw_id] = item_type
            if item_type == "reasoning":
                if type(index) is not int or index < 0 or not isinstance(raw_id, str) or not raw_id or index in self.added or not self.response_id:
                    raise _error("ambiguous_native_identity", "stream")
                self.added[index] = raw_id

        raw_id = event.get("item_id")
        if isinstance(item, Mapping) and item.get("type") == "reasoning":
            raw_id = item.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            return result
        owners = [i for i, owner_id in self.added.items() if owner_id == raw_id]
        if not owners:
            if event_type == "response.output_item.done" and isinstance(item, Mapping) and item.get("type") == "reasoning":
                raise _error("missing_stream_identity", "stream")
            return result
        if isinstance(item, Mapping) and item.get("type") != "reasoning":
            raise _error("duplicate_item_identity", "stream")
        if type(index) is not int:
            if len(owners) != 1:
                raise _error("ambiguous_native_identity", "stream")
            index = owners[0]
        if index not in owners:
            raise _error("ambiguous_native_identity", "stream")
        if event_type == "response.output_item.done":
            if index in self.done:
                raise _error("duplicate_item_identity", "stream")
            self.done.add(index)
        mapped_id = _client_id(self.response_id, index, raw_id)
        if isinstance(item, Mapping) and item.get("type") == "reasoning":
            result["item"] = {**item, "id": mapped_id}
        if event.get("item_id") == raw_id:
            result["item_id"] = mapped_id
        return result
