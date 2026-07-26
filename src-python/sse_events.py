from __future__ import annotations

from dataclasses import dataclass


__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "SseAssemblerClosedError",
    "SseEvent",
    "SseEventAssembler",
    "SseFrameTooLargeError",
    "SseLine",
    "SseTermination",
]

DEFAULT_MAX_FRAME_BYTES = 16 * 1024 * 1024


class SseAssemblerClosedError(RuntimeError):
    def __init__(self, disposition: str) -> None:
        self.disposition = disposition
        super().__init__(f"SSE assembler is closed ({disposition})")


class SseFrameTooLargeError(ValueError):
    classification = "sse_frame_too_large"

    def __init__(self, *, pending_bytes: int, max_frame_bytes: int) -> None:
        self.pending_bytes = pending_bytes
        self.max_frame_bytes = max_frame_bytes
        super().__init__(
            f"SSE frame exceeded byte limit "
            f"(pending_bytes={pending_bytes}, max_frame_bytes={max_frame_bytes})"
        )


@dataclass(frozen=True, slots=True)
class SseLine:
    raw: bytes
    kind: str
    name: bytes | None
    value: bytes


@dataclass(frozen=True, slots=True)
class SseEvent:
    raw: bytes
    lines: tuple[SseLine, ...]
    data: bytes
    event: bytes | None
    id: bytes | None
    retry: int | None


@dataclass(frozen=True, slots=True)
class SseTermination:
    events: tuple[SseEvent, ...]
    disposition: str
    discarded_bytes: int


class SseEventAssembler:
    """Assemble lossless SSE frames from arbitrarily partitioned byte chunks.

    ``feed`` emits a frame only after its blank-line boundary is complete.
    ``finish`` resolves a final standalone CR, discards any other partial frame,
    and permanently closes the assembler until ``reset`` is called.
    """

    def __init__(self, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> None:
        if max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes must be positive")
        self._max_frame_bytes = max_frame_bytes
        self._buffer = bytearray()
        self._scan_offset = 0
        self._frame_raw = bytearray()
        self._lines: list[SseLine] = []
        self._at_stream_start = True
        self._closed_disposition: str | None = None

    @property
    def buffered_bytes(self) -> int:
        return len(self._frame_raw) + len(self._buffer)

    def feed(self, chunk: bytes) -> tuple[SseEvent, ...]:
        """Consume bytes and return every complete frame in their original order."""
        self._require_open()
        self._buffer.extend(chunk)
        try:
            events = self._drain(eof=False)
            self._check_size()
            return events
        except SseFrameTooLargeError:
            self._discard()
            self._closed_disposition = "size_limit"
            raise

    def finish(self) -> SseTermination:
        """Close at EOF and report complete final-CR events or discarded bytes."""
        self._require_open()
        events = self._drain(eof=True)
        discarded_bytes = self.buffered_bytes
        disposition = "incomplete" if discarded_bytes else "complete"
        self._discard()
        self._closed_disposition = disposition
        return SseTermination(
            events=events,
            disposition=disposition,
            discarded_bytes=discarded_bytes,
        )

    def cancel(self) -> SseTermination:
        """Discard pending bytes and close without retaining or echoing payloads."""
        self._require_open()
        discarded_bytes = self.buffered_bytes
        self._discard()
        self._closed_disposition = "cancelled"
        return SseTermination(
            events=(),
            disposition="cancelled",
            discarded_bytes=discarded_bytes,
        )

    def reset(self) -> SseTermination:
        """Discard pending state and reopen this instance at a new stream start."""
        discarded_bytes = self.buffered_bytes
        self._discard()
        self._at_stream_start = True
        self._closed_disposition = None
        return SseTermination(
            events=(),
            disposition="reset",
            discarded_bytes=discarded_bytes,
        )

    def _drain(self, *, eof: bool) -> tuple[SseEvent, ...]:
        events: list[SseEvent] = []
        while True:
            extracted = self._extract_line(eof=eof)
            if extracted is None:
                break
            content, raw_line = extracted
            self._frame_raw.extend(raw_line)
            if self._at_stream_start:
                self._at_stream_start = False
                if content.startswith(b"\xef\xbb\xbf"):
                    content = content[3:]
            if content:
                self._lines.append(_parse_line(content, raw_line))
                self._check_size()
                continue
            self._check_size()
            events.append(self._complete_event())
        return tuple(events)

    def _extract_line(self, *, eof: bool) -> tuple[bytes, bytes] | None:
        for index in range(self._scan_offset, len(self._buffer)):
            byte = self._buffer[index]
            if byte == 0x0A:
                raw_line = bytes(self._buffer[: index + 1])
                del self._buffer[: index + 1]
                self._scan_offset = 0
                return raw_line[:-1], raw_line
            if byte != 0x0D:
                continue
            if index + 1 == len(self._buffer) and not eof:
                self._scan_offset = index
                return None
            ending_size = 2 if index + 1 < len(self._buffer) and self._buffer[index + 1] == 0x0A else 1
            raw_line = bytes(self._buffer[: index + ending_size])
            del self._buffer[: index + ending_size]
            self._scan_offset = 0
            return raw_line[:-ending_size], raw_line
        self._scan_offset = len(self._buffer)
        return None

    def _complete_event(self) -> SseEvent:
        lines = tuple(self._lines)
        data_values = [line.value for line in lines if line.name == b"data"]
        event_values = [line.value for line in lines if line.name == b"event"]
        id_values = [line.value for line in lines if line.name == b"id" and b"\0" not in line.value]
        retry_values = [
            int(line.value)
            for line in lines
            if line.name == b"retry" and line.value and line.value.isdigit()
        ]
        event = SseEvent(
            raw=bytes(self._frame_raw),
            lines=lines,
            data=b"\n".join(data_values),
            event=event_values[-1] if event_values else None,
            id=id_values[-1] if id_values else None,
            retry=retry_values[-1] if retry_values else None,
        )
        self._frame_raw.clear()
        self._lines.clear()
        return event

    def _check_size(self) -> None:
        pending_bytes = self.buffered_bytes
        if pending_bytes > self._max_frame_bytes:
            raise SseFrameTooLargeError(
                pending_bytes=pending_bytes,
                max_frame_bytes=self._max_frame_bytes,
            )

    def _discard(self) -> None:
        self._buffer.clear()
        self._scan_offset = 0
        self._frame_raw.clear()
        self._lines.clear()

    def _require_open(self) -> None:
        if self._closed_disposition is not None:
            raise SseAssemblerClosedError(self._closed_disposition)


def _parse_line(content: bytes, raw: bytes) -> SseLine:
    if content.startswith(b":"):
        value = content[1:]
        if value.startswith(b" "):
            value = value[1:]
        return SseLine(raw=raw, kind="comment", name=None, value=value)
    name, separator, value = content.partition(b":")
    if separator and value.startswith(b" "):
        value = value[1:]
    return SseLine(raw=raw, kind="field", name=name, value=value)
