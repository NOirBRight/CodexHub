"""Official CLI subscriptions behind the existing upstream exchange port.

Only the configured provider identity selects a backend. This module never
opens its placeholder URL, borrows incoming credentials, retries generation,
executes tools, or owns conversation state. Full history belongs to the caller.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import io
import json
import queue
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import gateway_admission
from subscription_backend_contract import BackendError

PROVIDER_IDS = frozenset({"cursor-subscription", "claude-subscription"})
AUTH_MODE = "official_cli_session"
SYSTEM_CONTEXT_CONSENT = "user-context-v1"


def is_subscription_provider(provider_id: str) -> bool:
    return provider_id in PROVIDER_IDS


def backend_for(provider_id: str) -> Callable[..., Iterator[dict[str, Any]]]:
    if provider_id == "cursor-subscription":
        from cursor_subscription_backend import stream_chat
    elif provider_id == "claude-subscription":
        from claude_subscription_backend import stream_chat
    else:
        raise BackendError("unsupported-provider", "Unknown CLI subscription provider.", 400)
    return stream_chat


def _error_bytes(error: BackendError) -> bytes:
    return json.dumps({"error": {"code": error.code, "message": error.message}}, ensure_ascii=False).encode()


class SubscriptionResponse:
    """Bounded streaming reader; closing it cancels and reaps its backend."""

    status = code = 200

    def __init__(self, payload: Mapping[str, Any], backend: Callable[..., Iterator[dict[str, Any]]], timeout: float) -> None:
        self.headers = {"Content-Type": "text/event-stream" if payload.get("stream") else "application/json"}
        self._cancel = threading.Event()
        self._queue: queue.Queue[bytes | BackendError | None] = queue.Queue(maxsize=8)
        self._buffer = bytearray()
        self._eof = False
        self._deadline = time.monotonic() + timeout
        self._worker = threading.Thread(target=self._produce, args=(dict(payload), backend, timeout), daemon=True, name="cli-subscription-exchange")
        admission = gateway_admission.active_gateway_request()
        if admission is not None:
            admission.attach_upstream_transport(self)
        self._worker.start()

    def _put(self, item: bytes | BackendError | None) -> None:
        while not self._cancel.is_set():
            try:
                self._queue.put(item, timeout=0.1)
                return
            except queue.Full:
                pass

    def _produce(self, payload: dict[str, Any], backend: Callable[..., Iterator[dict[str, Any]]], timeout: float) -> None:
        stream = None
        try:
            stream = backend(payload, cancel=self._cancel, timeout=timeout)
            completed = False
            chunks = []
            collected_bytes = 0
            for chunk in stream:
                if self._cancel.is_set():
                    return
                if not isinstance(chunk, dict) or not isinstance(chunk.get("choices"), list):
                    raise BackendError("invalid-upstream-stream", "Subscription returned an invalid stream.")
                completed |= any(choice.get("finish_reason") is not None for choice in chunk["choices"] if isinstance(choice, dict))
                if payload.get("stream"):
                    self._put(b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n")
                else:
                    collected_bytes += len(json.dumps(chunk).encode())
                    if collected_bytes > 32 * 1024 * 1024:
                        raise BackendError("upstream-output-limit", "Subscription response exceeds the supported size.")
                    chunks.append(chunk)
            if not completed:
                raise BackendError("incomplete-upstream-stream", "Subscription ended before completing the turn.")
            if payload.get("stream"):
                self._put(b"data: [DONE]\n\n")
            else:
                self._put(json.dumps(collect_chat(chunks), ensure_ascii=False).encode())
        except BackendError as exc:
            self._put(exc)
        except Exception:
            self._put(BackendError("backend-failed", "CLI subscription request failed."))
        finally:
            if stream is not None:
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        # Completion/failure is already queued; still wake readers.
                        pass
            self._put(None)

    def prime(self) -> None:
        self._next()

    def _next(self) -> None:
        if self._cancel.is_set():
            raise BackendError("request-cancelled", "CLI subscription request was cancelled.", 499)
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self.close()
            raise BackendError("request-timeout", "CLI subscription request timed out.", 504)
        try:
            item = self._queue.get(timeout=remaining)
        except queue.Empty:
            self.close()
            raise BackendError("request-timeout", "CLI subscription request timed out.", 504) from None
        if self._cancel.is_set():
            raise BackendError("request-cancelled", "CLI subscription request was cancelled.", 499)
        if isinstance(item, BackendError):
            raise item
        if item is None:
            self._eof = True
        else:
            self._buffer.extend(item)

    def readline(self) -> bytes:
        while b"\n" not in self._buffer and not self._eof:
            self._next()
        boundary = self._buffer.find(b"\n") + 1
        if boundary == 0:
            boundary = len(self._buffer)
        result = bytes(self._buffer[:boundary])
        del self._buffer[:boundary]
        return result

    def read(self, size: int = -1) -> bytes:
        while not self._eof and (size < 0 or len(self._buffer) < size):
            self._next()
        boundary = len(self._buffer) if size < 0 else min(size, len(self._buffer))
        result = bytes(self._buffer[:boundary])
        del self._buffer[:boundary]
        return result

    def close(self) -> None:
        self._cancel.set()
        # Wake a pending reader immediately; backend observes the same event.
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=2.0)


def collect_chat(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble actual deltas without inventing usage or tool results."""
    result: dict[str, Any] = {"object": "chat.completion", "choices": []}
    choices: dict[int, dict[str, Any]] = {}
    tools: dict[tuple[int, int], dict[str, Any]] = {}
    for chunk in chunks:
        for key in ("id", "created", "model", "usage"):
            if key in chunk:
                result[key] = chunk[key]
        for item in chunk.get("choices", []):
            index = item.get("index", 0)
            choice = choices.setdefault(index, {"index": index, "message": {"role": "assistant", "content": ""}, "finish_reason": None})
            delta = item.get("delta", {})
            for key in ("content", "reasoning_content", "refusal"):
                if isinstance(delta.get(key), str):
                    choice["message"][key] = choice["message"].get(key, "") + delta[key]
            for call in delta.get("tool_calls", []):
                tool = tools.setdefault((index, call.get("index", 0)), {"type": "function", "function": {"name": "", "arguments": ""}})
                if "id" in call:
                    tool["id"] = call["id"]
                for key in ("name", "arguments"):
                    tool["function"][key] += call.get("function", {}).get(key, "")
            if item.get("finish_reason") is not None:
                choice["finish_reason"] = item["finish_reason"]
    for (index, _), tool in sorted(tools.items()):
        choices[index]["message"].setdefault("tool_calls", []).append(tool)
    result["choices"] = [choices[index] for index in sorted(choices)]
    return result


@contextmanager
def open_subscription(request: Request, *, provider_id: str, timeout: float, backend: Callable[..., Iterator[dict[str, Any]]] | None = None, downstream_socket: Any = None):
    response = None
    stop_watch = gateway_admission.watch_downstream_disconnect(downstream_socket)
    try:
        try:
            payload = json.loads(request.data or b"{}")
        except (ValueError, UnicodeError):
            raise BackendError("invalid-request", "Invalid subscription request JSON.", 400) from None
        if not isinstance(payload, dict):
            raise BackendError("invalid-request", "Subscription payload must be an object.", 400)
        if payload.get("n", 1) != 1:
            raise BackendError("unsupported-request", "CLI subscriptions support one completion per request.", 400)
        response = SubscriptionResponse(payload, backend or backend_for(provider_id), timeout)
        response.prime()
        yield response
    except BackendError as exc:
        raise HTTPError(request.full_url, exc.status, exc.code, {"Content-Type": "application/json"}, io.BytesIO(_error_bytes(exc))) from None
    finally:
        stop_watch.set()
        if response is not None:
            response.close()
