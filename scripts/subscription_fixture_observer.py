"""Opt-in qualification fixture observation. Never imported by production.

Transport fields are received native bytes, not proof of vendor generation or
of which read-ahead fields the backend consumed. Canonical emission is separate.
No envelopes, headers, reasoning, schemas or surrounding text are persisted.
"""
from python_runtime_contract import require_python_313
require_python_313(__file__)

import argparse
from contextlib import contextmanager
import hashlib
import gzip
import io
import json
import os
from pathlib import Path
import re
import runpy
import secrets
import sys
import time

MAX_VALUE = 256
MAX_LEAVES = 64
MAX_PARTS = 256
MAX_REQUESTS = 128
MAX_PARSE_BYTES = 16 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
MAX_REPORT_BYTES = 256 * 1024
MAX_JSON_NODES = 4096
BOUNDARIES = ("fixtureinput", "callerpayload", "adaptedhistory", "servedhistoryblob",
              "nativefield", "canonicalchunks", "downstreamSSEdelta",
              "downstreamSSEcompleted", "callerstdout", "rolloutfinal")


def fixture_plan(value=None):
    """Explicit operator-approved fixture/control whitelist; no substring grants."""
    if value is None:
        random = secrets.token_hex(11)
        value = random[:12] + random[11] * 2 + random[12:]  # 24 bytes, unpredictable, includes a triple repeat.
    if not isinstance(value, str) or not value or len(value.encode()) > MAX_VALUE - 4:
        raise ValueError("invalid-fixture")
    allowed = {value, value + "\n", value[::-1]}
    for text in (value, value[::-1]):
        allowed.update(text[:i] + text[i + 1:] for i in range(len(text)))
        allowed.add(text + "\u200b")
        allowed.add((text + "\u200b")[::-1])
    if len(allowed) > 128:
        raise ValueError("fixture-whitelist-too-large")
    return {"run_id": secrets.token_hex(16), "case": "cursor-to-official", "value": value,
            "approved": sorted(allowed), "raw_limit_utf8_bytes": MAX_VALUE}


def validate_plan(plan):
    expected = fixture_plan(plan.get("value"))
    if (not re.fullmatch(r"[a-f0-9]{24}", plan.get("value", ""))
            or plan.get("case") != "cursor-to-official" or not re.fullmatch(r"[a-f0-9]{32}", plan.get("run_id", ""))
            or plan.get("approved") != expected["approved"] or plan.get("raw_limit_utf8_bytes") != MAX_VALUE):
        raise ValueError("invalid-fixture-approval")
    return plan


def private_json(path, value):
    data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("observation-file-limit")
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as output:
        output.write(data)


def bounded_json(data, limit=MAX_JSON_BYTES):
    """Conservative capacity preflight; stdlib alone validates/parses JSON.

    Count structural tokens outside strings before building any JSON tree.
    No schema, string decoding, value recovery or alternate JSON parser.
    """
    if len(data) > limit:
        raise ValueError("json-byte-limit")
    depth = tokens = 0
    quoted = escaped = scalar = False
    for byte in data:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
            continue
        if byte in b" \t\r\n":
            scalar = False
            continue
        if byte == 34 or byte in b"{}[],:" or not scalar:
            tokens += 1
        if byte == 34:
            quoted = True
        if byte in b"{[":
            depth += 1
        elif byte in b"}]":
            depth -= 1
        scalar = byte not in b'"{}[],:'
        if depth > 16 or tokens > MAX_JSON_NODES:
            raise ValueError("json-structure-limit")
    return json.loads(data)


def request_payload(body, encoding):
    """Observation only: both encoded and decoded limits precede JSON parsing."""
    if len(body) > MAX_PARSE_BYTES:
        raise ValueError("request-byte-limit")
    if encoding == "gzip":
        decoded = bytearray()
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            while part := stream.read1(MAX_PARSE_BYTES - len(decoded) + 1):
                decoded.extend(part)
                if len(decoded) > MAX_PARSE_BYTES:
                    raise ValueError("request-decoded-limit")
        body = bytes(decoded)
    return bounded_json(body, MAX_PARSE_BYTES) if body else {}


class BoundedValue:
    def __init__(self):
        self.hash = hashlib.sha256()
        self.length = 0
        self.raw = bytearray()
        self.parts = []
        self.count = 0

    def add(self, data):
        self.hash.update(data)
        self.length += len(data)
        self.count += 1
        if self.length <= MAX_VALUE:
            self.raw.extend(data)
        else:
            self.raw.clear()
        if len(self.parts) < MAX_PARTS:
            self.parts.append({"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})

    def summary(self, plan, complete):
        complete = complete and self.count <= MAX_PARTS
        row = {"state": "observed", "sha256": self.hash.hexdigest(), "utf8_bytes": self.length,
               "complete": complete, "parts": self.parts, "part_count": self.count,
               "order_complete": self.count <= MAX_PARTS}
        value = None
        if self.length <= MAX_VALUE:
            try:
                value = self.raw.decode("utf-8")
            except UnicodeError:
                pass
        row.update(exact_fixture=value == plan["value"], exact_expected_reverse=value == plan["value"][::-1])
        reason = ("incomplete" if not complete else "over-limit" if self.length > MAX_VALUE
                  else "invalid-utf8" if value is None else "unapproved" if value not in plan["approved"] else None)
        row["rejection"] = reason
        if reason is None:
            row.update(utf8_hex=bytes(self.raw).hex(), codepoints=list(map(ord, value)))
        return row


class FixtureCapture:
    def __init__(self, plan, correlation):
        self.plan, self.correlation = plan, correlation
        self.boundaries = {}
        self.failure = None
        self.deadline = time.monotonic() + 180

    def guard(self, operation, *args):
        """Observation failure rejects capture, never changes the original stream."""
        if self.failure:
            return
        try:
            if time.monotonic() >= self.deadline:
                raise ValueError()
            operation(*args)
        except Exception:
            self.failure = "observation-incomplete"

    def value(self, boundary, value, *, complete=True):
        bounded = BoundedValue()
        bounded.add(value if isinstance(value, bytes) else value.encode("utf-8"))
        self.boundaries[boundary] = bounded.summary(self.plan, complete)

    def leaves(self, boundary, value):
        """Only text leaves of already-selected messages/results, never schema keys."""
        rows = []
        visited = 0
        def walk(node, depth=0):
            nonlocal visited
            visited += 1
            if depth > 16 or visited > 4096 or len(rows) >= MAX_LEAVES:
                raise ValueError("leaf-limit")
            if isinstance(node, str):
                bounded = BoundedValue()
                bounded.add(node.encode("utf-8"))
                rows.append(bounded.summary(self.plan, True))
                # Custom result wrappers can contain the actual fixture leaf.
                # Decode only bounded JSON, no surrounding text/substring extraction.
                if len(node.encode()) <= 4096 and node[:1] in ("{", "["):
                    try:
                        decoded = bounded_json(node.encode())
                    except ValueError:
                        return
                    walk(decoded, depth + 1)
            elif isinstance(node, list):
                for item in node:
                    walk(item, depth + 1)
            elif isinstance(node, dict):
                for key in ("text", "content", "output", "result", "__codexhub_custom_output"):
                    if key in node:
                        walk(node[key], depth + 1)
        previous = self.boundaries.get(boundary, {}).get("leaves", [])
        try:
            walk(value)
        except ValueError:
            self.failure = "observation-incomplete"
        combined = previous + rows
        if len(combined) > MAX_LEAVES:
            self.failure = "observation-incomplete"
        self.boundaries[boundary] = {"state": "observed" if combined else "absent", "leaves": combined[:MAX_LEAVES]}

    def report(self):
        rows = {name: self.boundaries.get(name, {"state": "absent", "complete": False}) for name in BOUNDARIES}
        if self.failure:
            # Never persist raw for a parser/resource/runtime-incomplete capture.
            for row in rows.values():
                for leaf in row.get("leaves", [row]):
                    leaf.pop("utf8_hex", None)
                    leaf.pop("codepoints", None)
                    leaf["complete"] = False
                    leaf["rejection"] = "incomplete"
        return {"correlation": self.correlation, "boundaries": rows, "capture_failure": self.failure,
                "native_coverage": "received transport text fields; read-ahead consumption unproven",
                "caller_request_correlation": "case/epoch only unless request_id is explicit",
                "caller_extraction": "last item.completed agent_message; rollout requires explicit final_answer phase"}

    def persist(self, path):
        try:
            private_json(path, self.report())
        except ValueError:
            private_json(path, {"correlation": self.correlation, "capture_failure": "observation-file-limit",
                               "boundaries": {name: {"state": "incomplete", "complete": False} for name in BOUNDARIES}})


class NativeTap:
    def __init__(self, capture):
        import cursor_subscription_wire as wire
        self.capture = capture
        self.incoming, self.outgoing = wire.Frames(), wire.Frames()
        self.text = BoundedValue()
        self.total = self.decoded = self.frames = 0

    def feed(self, data, incoming):
        import cursor_subscription_wire as wire
        self.total += len(data)
        if self.total > MAX_PARSE_BYTES:
            raise ValueError("native-byte-limit")
        parser = self.incoming if incoming else self.outgoing
        self.consume(parser.feed(data, decoded_budget=MAX_PARSE_BYTES - self.decoded,
                                 frame_budget=4096 - self.frames), incoming)

    def consume(self, frames, incoming):
        import cursor_subscription_wire as wire
        for end, body in frames:
            self.frames += 1
            self.decoded += len(body)
            if self.frames > 4096 or self.decoded > MAX_PARSE_BYTES:
                raise ValueError("native-frame-limit")
            if end:
                continue  # End envelopes/errors are never retained.
            for key, detail in wire.fields(body):
                if incoming and key == 1 and isinstance(detail, bytes):
                    for update, field in wire.fields(detail):
                        if update == 1 and isinstance(field, bytes):
                            raw = wire.get(field, 1)
                            if isinstance(raw, bytes):
                                self.text.add(raw)
                elif not incoming and key == 3 and isinstance(detail, bytes):
                    answer = wire.get(detail, 2)
                    if isinstance(answer, bytes):
                        blob = wire.get(answer, 1)
                        if isinstance(blob, bytes):
                            if len(blob) > MAX_JSON_BYTES:
                                raise ValueError("blob-json-limit")
                            try:
                                message = bounded_json(blob)
                            except (ValueError, UnicodeError):
                                # Binary turn blobs are expected; JSON-looking
                                # rejected history cannot be labelled complete.
                                if blob[:1] in (b"{", b"["):
                                    raise
                                continue
                            if isinstance(message, dict) and message.get("role") in ("user", "assistant", "tool"):
                                self.capture.leaves("servedhistoryblob", message.get("content"))


def observe_cursor(payload, *, capture, transport_factory=None, **kwargs):
    """Wrap public stream_chat + duplex port; close/error/cancel stay owner-owned."""
    from cursor_subscription_backend import stream_chat, HTTP2Duplex
    factory = transport_factory or HTTP2Duplex
    native = NativeTap(capture)
    canonical = BoundedValue()
    capture.guard(capture.leaves, "adaptedhistory", payload.get("messages", []))
    def observed_factory(url, headers, **options):
        real = factory(url, headers, **options)
        if not url.endswith("/Run"):
            return real
        class Duplex:
            def __enter__(self):
                self.stream = real.__enter__()
                return self
            @property
            def status(self):
                return self.stream.status
            def send(self, data):
                result = self.stream.send(data)
                capture.guard(native.feed, data, False)
                return result
            def finish_request(self):
                return self.stream.finish_request()
            def receive(self):
                data = self.stream.receive()
                if data and 200 <= self.stream.status < 300:
                    capture.guard(native.feed, data, True)
                return data  # Same bytes object, including None and EOF.
            def __exit__(self, *args):
                return real.__exit__(*args)
        return Duplex()
    stream = stream_chat(payload, transport_factory=observed_factory, **kwargs)
    completed = False
    try:
        for chunk in stream:
            for choice in chunk.get("choices", []):
                content = choice.get("delta", {}).get("content")
                if isinstance(content, str):
                    capture.guard(canonical.add, content.encode("utf-8"))
            yield chunk  # Original chunk, with original Call/Item identity.
        completed = True
    finally:
        stream.close()
        complete = completed and not capture.failure
        if native.text.count:
            capture.boundaries["nativefield"] = native.text.summary(capture.plan, complete and not native.incoming.buffer)
        if canonical.count:
            capture.boundaries["canonicalchunks"] = canonical.summary(capture.plan, complete)


class DownstreamTap:
    def __init__(self, capture):
        from sse_events import SseEventAssembler
        self.capture = capture
        self.parser = SseEventAssembler(max_frame_bytes=MAX_JSON_BYTES)
        self.delta = BoundedValue()
        self.completed = BoundedValue()
        self.terminal = False
        self.total = self.events = 0

    def feed(self, data):
        self.capture.guard(self.consume, data)

    def consume(self, data):
        self.total += len(data)
        if self.total > MAX_PARSE_BYTES:
            raise ValueError("sse-byte-limit")
        for event in self.parser.feed(data):
            self.events += 1
            if self.events > 4096:
                raise ValueError("sse-event-limit")
            if not event.data or event.data == b"[DONE]":
                continue
            value = bounded_json(event.data)
            if value.get("type") == "response.output_text.delta":
                self.delta.add(value["delta"].encode())
            elif value.get("type") == "response.completed":
                response = value["response"]
                self.terminal = response.get("status") == "completed"
                for item in response.get("output", []):
                    if item.get("type") == "message":
                        for part in item.get("content", []):
                            if part.get("type") == "output_text":
                                self.completed.add(part["text"].encode())

    def finish(self, complete=True):
        termination = None
        try:
            termination = self.parser.finish() if complete else self.parser.cancel()
        except Exception:
            self.capture.failure = "observation-incomplete"
        good = complete and self.terminal and termination is not None and termination.disposition == "complete" and not self.capture.failure
        self.capture.boundaries["downstreamSSEdelta"] = self.delta.summary(self.capture.plan, good)
        self.capture.boundaries["downstreamSSEcompleted"] = self.completed.summary(self.capture.plan, good)


def install_gateway_observation(root, plan):
    """Qualification startup only; use the public owning exchange/socket seam."""
    import subscription_exchange
    original = subscription_exchange.open_subscription
    @contextmanager
    def observed(request, *, provider_id, timeout, backend=None, downstream_socket=None):
        ticket = None
        if provider_id == "cursor-subscription" and backend is None and downstream_socket is not None:
            try:
                host, port = downstream_socket.getpeername()[:2]
                if host != "127.0.0.1":
                    raise ValueError()
                path = root / f"peer-{port}.json"
                if path.stat().st_size > 4096:
                    raise ValueError()
                ticket = json.loads(path.read_bytes())
                if (ticket["run_id"] != plan["run_id"] or ticket["case"] != plan["case"]
                        or ticket["epoch"] not in (0, 1) or not re.fullmatch(r"[a-f0-9]{32}", ticket["request_id"])):
                    raise ValueError()
            except (OSError, ValueError, KeyError, TypeError):
                ticket = None
        if ticket is not None:
            capture = FixtureCapture(plan, ticket)
            def backend(payload, *, cancel, timeout):
                try:
                    yield from observe_cursor(payload, capture=capture, cancel=cancel, timeout=timeout)
                finally:
                    # A failed capture/write must never replace an owner exception.
                    try:
                        capture.persist(root / (ticket["request_id"] + ".json"))
                    except Exception:
                        pass  # Persistence is independent of capture admission.
        with original(request, provider_id=provider_id, timeout=timeout, backend=backend, downstream_socket=downstream_socket) as response:
            yield response
    subscription_exchange.open_subscription = observed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", action="store_true", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--tickets", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    if args.plan.stat().st_size > 65536:
        raise ValueError("fixture-plan-limit")
    plan = validate_plan(json.loads(args.plan.read_bytes()))
    install_gateway_observation(args.tickets, plan)
    sys.argv = ["codex_proxy.py", "--port", str(args.port)]
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "src-python/codex_proxy.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
