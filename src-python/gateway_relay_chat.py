"""Commit ordinary Chat text while retaining the final compatibility pass."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gateway_errors import UpstreamProtocolTranslationError
from protocol_translation import GatewayChatToResponsesStreamConverter, UnsupportedProtocolTranslationError


class ChatTextPrefix:
    """Tools and reasoning stay buffered; exposed text must survive adaptation."""

    def __init__(self) -> None:
        self.converter = GatewayChatToResponsesStreamConverter()
        self.blocked = False
        self.text_parts: list[str] = []
        self.header: dict[str, Any] | None = None

    def chunks_for_payload(self, payload: Mapping[str, Any] | str) -> list[dict[str, Any]]:
        if payload == "[DONE]":
            self.converter.events_for_done()
            return []
        events = self.converter.events_for_chunk(payload)
        # Preserve the buffered ordering after reasoning or a tool. A tool
        # can also be adapted into a transcript ahead of subsequent text.
        if self.converter.tool_states or any(
            event["type"] == "response.reasoning_summary_text.delta" for event in events
        ):
            self.blocked = True
        if self.blocked:
            return []
        chunks = []
        for event in events:
            if event["type"] != "response.output_text.delta":
                continue
            if self.header is None:
                self.header = {
                    "id": self.converter.response_id, "object": "chat.completion.chunk",
                    "created": self.converter.created_at, "model": self.converter.model,
                }
                chunks.append(self._chunk({"role": "assistant"}))
            text = event["delta"]
            self.text_parts.append(text)
            chunks.append(self._chunk({"content": text}))
        return chunks

    def _chunk(self, delta: dict[str, Any]) -> dict[str, Any]:
        return {**self.header, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}

    def remaining_chunks(self, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.header is None:
            return chunks
        prefix = "".join(self.text_parts)
        final_text = "".join(choice.get("delta", {}).get("content", "")
                             for chunk in chunks for choice in chunk.get("choices", []))
        if not final_text.startswith(prefix):
            raise UpstreamProtocolTranslationError(
                UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Final Chat compatibility output changed already exposed text.",
                ),
            )
        remaining = []
        for chunk in chunks:
            choices = []
            for choice in chunk.get("choices", []):
                delta = dict(choice.get("delta", {}))
                delta.pop("role", None)
                text = delta.get("content")
                if isinstance(text, str):
                    count = min(len(prefix), len(text))
                    prefix = prefix[count:]
                    if text[count:]:
                        delta["content"] = text[count:]
                    else:
                        delta.pop("content")
                if delta or choice.get("finish_reason") is not None:
                    choices.append({**choice, "delta": delta})
            if choices:
                remaining.append({**chunk, **self.header, "choices": choices})
        return remaining
