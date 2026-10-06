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
import math
import os
from pathlib import Path
import re
import runpy
import secrets
import sys
import threading
import time

MAX_VALUE = 256
MAX_LEAVES = 64
MAX_PARTS = 256
MAX_REQUESTS = 128
MAX_PARSE_BYTES = 16 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
MAX_REPORT_BYTES = 256 * 1024
MAX_JSON_NODES = 4096
MAX_PHASE_RECORDS = 64
MAX_PHASE_BYTES = 64 * 1024
PHASE_OPERATIONS = {"connection", "dns", "connect", "tls", "http2", "write", "read"}
PHASE_ERRORS = {"cancelled", "upstream-timeout", "upstream-interrupted", "upstream-protocol-error",
                "backend-unavailable", "upstream-connection-error", "upstream-dns-error",
                "upstream-connect-error", "upstream-tls-error", "upstream-read-error",
                "upstream-write-error", "upstream-http2-error"}
PHASE_CLOSURES = {"missing-binding", "missing-phase", "missing-end", "drop", "observer-error", "cap"}
PHASE_NUMBERS = {"begin_monotonic", "end_monotonic", "request_timeout_seconds", "request_deadline_monotonic",
                 "backend_deadline_monotonic", "phase_timeout_seconds", "phase_deadline_monotonic",
                 "remaining_request_seconds", "remaining_phase_seconds"}
BOUNDARIES = ("fixtureinput", "callerpayload", "adaptedhistory", "servedhistoryblob",
              "nativefield", "canonicalchunks", "downstreamSSEdelta",
              "downstreamSSEcompleted", "callerstdout", "rolloutfinal",
              "officialoutgoinghistory", "officialreceived", "downstreamOfficialmessages")


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


def require_private_storage():
    """Mode bits prove owner-only creation on POSIX, not Windows ACL privacy."""
    if os.name != "posix" or not callable(getattr(os, "fchmod", None)):
        raise NotImplementedError("private-storage-unsupported: POSIX owner-only creation required; Windows ACLs unproven")


def private_json(path, value):
    require_private_storage()
    data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("observation-file-limit")
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as output:
        output.write(data)


class CursorPhaseObservation:
    """Fixed Cursor code identities only; no provider values or frame retention."""

    def __init__(self, root):
        import cursor_subscription_backend as cursor
        import subscription_exchange
        from subscription_backend_contract import BackendError

        self.pid, self.parent_pid = os.getpid(), os.getppid()
        self.root = root / f"phase-{self.pid}"
        self.root.mkdir(mode=0o700)
        self.codes = {
            "init": cursor.HTTP2Duplex.__init__.__code__, "exit": cursor.HTTP2Duplex.__exit__.__code__,
            "pump": cursor.HTTP2Duplex._pump.__code__, "check": cursor.HTTP2Duplex._check.__code__,
            "request": cursor.stream_chat.__code__, "config": cursor._agent_url.__code__,
            "exchange": subscription_exchange.SubscriptionResponse._produce.__code__,
            "error": BackendError.__init__.__code__,
        }
        self.states = {}
        self.actors = {"startup": False, "exchange": False, "http2": False}
        self.flags = set()
        self.closure_lock = threading.Lock()
        self.count = self.bytes = 0
        self.installed = sys.getprofile() is None and threading.getprofile() is None
        if self.installed:
            self.actors["startup"] = True
            sys.setprofile(self.profile)
            threading.setprofile(self.profile)
        else:
            self.flags.add("missing-binding")
        self.emit({"event": "binding"})

    def emit(self, row):
        value = {"pid": self.pid, "parent_pid": self.parent_pid,
                 "actors": dict(self.actors), "closure": sorted(self.flags), **row}
        size = len(json.dumps(value, separators=(",", ":")).encode()) + 1
        if self.count >= MAX_PHASE_RECORDS or self.bytes + size > MAX_PHASE_BYTES:
            self.flags.update({"cap", "drop"})
            self.disclose()
            return
        self.count += 1
        self.bytes += size
        try:
            private_json(self.root / f"{self.count}.json", value)
        except Exception:
            self.flags.add("observer-error")
            self.disclose()

    def disclose(self):
        with self.closure_lock:
            temporary = self.root / ".closure.json"
            try:
                private_json(temporary, {"pid": self.pid, "parent_pid": self.parent_pid,
                                         "actors": dict(self.actors),
                                         "closure": sorted(self.flags), "event": "closure"})
                os.replace(temporary, self.root / "closure.json")
            except Exception:
                pass  # Collector still discloses unreadable/missing receipts.
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def profile(self, frame, event, argument):
        code = frame.f_code
        if code not in (self.codes["init"], self.codes["exit"], self.codes["pump"], self.codes["error"]):
            return
        if "cap" in self.flags:
            return
        try:
            if code == self.codes["init"] and event == "return":
                phase = "run"
                request = None
                exchange_deadline = None
                parent = frame.f_back
                for _ in range(12):
                    if parent is None:
                        break
                    if parent.f_code == self.codes["config"]:
                        phase = "config"
                    if parent.f_code == self.codes["exchange"]:
                        self.actors["exchange"] = True
                        exchange_deadline = parent.f_locals["self"]._deadline
                    if parent.f_code == self.codes["request"]:
                        request = (parent.f_locals.get("timeout"), parent.f_locals.get("started"))
                    parent = parent.f_back
                timeout = frame.f_locals.get("timeout")
                transport = frame.f_locals["self"]
                deadline = transport.deadline
                values = (*request, timeout, deadline, exchange_deadline) if request else ()
                if len(values) != 5 or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
                    self.flags.add("drop")
                    self.disclose()
                    return
                request_timeout, started, timeout, deadline, exchange_deadline = values
                now = time.monotonic()
                row = {"phase": phase, "begin_monotonic": deadline - timeout,
                       "request_timeout_seconds": request_timeout, "request_deadline_monotonic": exchange_deadline,
                       "backend_deadline_monotonic": started + request_timeout,
                       "phase_timeout_seconds": timeout, "phase_deadline_monotonic": deadline,
                       "remaining_request_seconds": max(0, exchange_deadline - now),
                       "remaining_phase_seconds": max(0, deadline - now)}
                self.states[id(transport)] = row
                self.emit({"event": "begin", **row})
            elif code == self.codes["pump"]:
                self.actors["http2"] = True
                if event == "return":
                    row = self.states.get(id(frame.f_locals["self"]))
                    stage = frame.f_locals.get("stage")
                    if row is not None and stage in PHASE_OPERATIONS:
                        row["operation"] = stage
            elif code == self.codes["error"] and event == "return":
                error_code = frame.f_locals.get("code")
                if type(error_code) is not str or error_code not in PHASE_ERRORS:
                    return
                parent = frame.f_back
                for _ in range(12):
                    if parent is None:
                        break
                    if parent.f_code in (self.codes["pump"], self.codes["check"]):
                        row = self.states.get(id(parent.f_locals["self"]))
                        if row is not None:
                            row["coded_error"] = error_code
                        break
                    parent = parent.f_back
            elif code == self.codes["exit"] and event == "return":
                row = self.states.pop(id(frame.f_locals["self"]), None)
                if row is not None:
                    now = time.monotonic()
                    row["remaining_request_seconds"] = max(0, row["request_deadline_monotonic"] - now)
                    row["remaining_phase_seconds"] = max(0, row["phase_deadline_monotonic"] - now)
                    self.emit({"event": "end", "end_monotonic": now, **row})
        except Exception:
            self.flags.add("observer-error")
            self.disclose()

    def close(self):
        if self.installed:
            sys.setprofile(None)
            threading.setprofile(None)
            self.installed = False


def install_cursor_phase_observation(root):
    """Only explicit qualification startup calls this hook."""
    return CursorPhaseObservation(root)


def collect_cursor_phase_observation(root, pids):
    """Known Gateway actors only; coverage never changes qualification checks."""
    records, flags = [], set()
    actors = {"startup": False, "exchange": False, "http2": False}
    if any(type(pid) is not int or pid <= 0 for pid in pids):
        flags.add("observer-error")
    pids = [pid for pid in pids if type(pid) is int and pid > 0]
    for pid in pids:
        folder = root / f"phase-{pid}"
        if not folder.is_dir():
            flags.add("missing-binding")
            continue
        if not (folder / "1.json").is_file():
            flags.add("missing-binding")
        total = 0
        paths = [folder / f"{index}.json" for index in range(1, MAX_PHASE_RECORDS + 1)] + [folder / "closure.json"]
        for path in paths:
            if not path.exists():
                continue
            try:
                size = path.stat().st_size
                total += size
                if path.is_symlink() or size > 2048 or total > MAX_PHASE_BYTES + 2048:
                    raise ValueError()
                row = bounded_json(path.read_bytes(), 2048)
                allowed = PHASE_NUMBERS | {"pid", "parent_pid", "actors", "closure", "event", "phase", "operation", "coded_error"}
                if (not isinstance(row, dict) or row.keys() - allowed or row.get("pid") != pid
                        or type(row.get("pid")) is not int
                        or type(row.get("parent_pid")) is not int or row["parent_pid"] <= 0
                        or row.get("event") not in {"binding", "begin", "end", "closure"}
                        or not isinstance(row.get("actors"), dict) or row["actors"].keys() != actors.keys()
                        or any(type(value) is not bool for value in row["actors"].values())
                        or not isinstance(row.get("closure"), list)
                        or any(type(value) is not str or value not in PHASE_CLOSURES for value in row["closure"])
                        or any(type(row[key]) not in (int, float) or not math.isfinite(row[key])
                               for key in PHASE_NUMBERS & row.keys())
                        or "operation" in row and row["operation"] not in PHASE_OPERATIONS
                        or "coded_error" in row and row["coded_error"] not in PHASE_ERRORS
                        or "phase" in row and row["phase"] not in {"config", "run"}
                        or row["event"] in {"begin", "end"} and (
                            row.get("phase") not in {"config", "run"}
                            or PHASE_NUMBERS - {"end_monotonic"} - row.keys()
                            or row["event"] == "end" and "end_monotonic" not in row)):
                    raise ValueError()
                actors = {key: actors[key] or row["actors"][key] is True for key in actors}
                flags.update(row["closure"])
                records.append(row)
            except Exception:
                flags.add("observer-error")
    begins = [row for row in records if row["event"] == "begin"]
    ends = {(row["pid"], row["phase"], row["begin_monotonic"]) for row in records if row["event"] == "end"}
    if {row["phase"] for row in begins} != {"config", "run"}:
        flags.add("missing-phase")
    if any((row["pid"], row["phase"], row["begin_monotonic"]) not in ends for row in begins):
        flags.add("missing-end")
    return {"process_ids": list(pids), "actors": actors, "records": records, "closure": sorted(flags)}


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


def request_payload(body, encoding, budget=None):
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
    if budget is not None:
        budget.total += len(body)
        if budget.total > MAX_PARSE_BYTES:
            raise ValueError("official-shared-byte-limit")
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
        self.association = {}

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
                **self.association,
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


def identity(value):
    """Retain actual identifiers only as bounded hashes; never derive them from text."""
    if not isinstance(value, str) or not value or len(value.encode()) > MAX_VALUE:
        raise ValueError("official-identity-unavailable")
    return hashlib.sha256(value.encode()).hexdigest()


def item_digest(item):
    return hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def official_message_names(payload):
    from gateway_compat.collaboration_delivery import ALIAS, MESSAGE_TOOLS, portable_handler_names
    from code_mode_collaboration import expose_declared_collaboration
    payload = dict(payload)
    expose_declared_collaboration(payload)  # Existing typed declaration contract; never generated exec input.
    # Downstream declarations retain the original namespace. Only this known
    # namespace is mapped for selection; no generated source/task inference.
    groups = [payload.get("tools", [])] + [item.get("tools", []) for item in payload.get("input", [])
              if isinstance(item, dict) and item.get("type") == "additional_tools"]
    tools = [{**tool, "name": ALIAS, "tools": [child for child in tool.get("tools", [])
              if isinstance(child, dict) and child.get("type") == "function"
              and child.get("parameters", {}).get("properties", {}).get("message", {}).get("type") == "string"]}
             for group in groups if isinstance(group, list) for tool in group if isinstance(tool, dict)
             and tool.get("type") == "namespace" and tool.get("name") in ("collaboration", ALIAS)]
    return set(portable_handler_names({"tools": tools})) & MESSAGE_TOOLS


class OfficialTap:
    """Selected Responses leaves. All argument JSON stays bounded and ephemeral.

    Native feed is the response reader's successful receive, possibly read-ahead.
    It proves neither relay consumption nor downstream/caller receipt. The
    downstream tap runs on the qualifier's separately forwarded response bytes.
    """
    def __init__(self, capture, names, boundary="officialreceived", namespace="codexhub_plaintext_collaboration"):
        from sse_events import SseEventAssembler
        from gateway_compat.collaboration_delivery import MESSAGE_TOOLS
        self.capture, self.names, self.boundary, self.namespace = capture, names, boundary, namespace
        self.message_tools = MESSAGE_TOOLS
        self.parser = SseEventAssembler(max_frame_bytes=MAX_JSON_BYTES)
        self.total = self.events = self.parts = self.argument_bytes = self.leaf_count = 0
        self.items = {}
        self.response = None
        self.sequence = None
        self.sequence_present = None
        self.terminal = False
        self.rows = []
        self.values = []
        self.unavailable = False

    def history(self, payload):
        rows = []
        for index, item in enumerate(payload.get("input", [])):
            if not isinstance(item, dict):
                continue
            selected = []
            if item.get("type") == "message" and item.get("role") == "assistant" and item.get("status") == "completed":
                selected = [("output_text", i, part["text"]) for i, part in enumerate(item.get("content", []))
                            if part.get("type") == "output_text" and isinstance(part.get("text"), str)]
            elif self.message_call(item) and item.get("status") in (None, "completed"):
                arguments = item.get("arguments")
                if isinstance(arguments, str):
                    parsed = bounded_json(arguments.encode())
                    if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
                        selected = [("arguments.message", None, parsed["message"])]
            for channel, content_index, text in selected:
                self.reserve_leaf()
                bounded = BoundedValue()
                bounded.add(text.encode())
                rows.append({**bounded.summary(self.capture.plan, True), "channel": channel, "input_index": index,
                             "content_index": content_index, "item_sha256": identity(item.get("id")),
                             "call_sha256": identity(item["call_id"]) if channel == "arguments.message" else None,
                             "boundary": "actual outgoing Responses input before open"})
        self.capture.boundaries["officialoutgoinghistory"] = {"state": "observed" if rows else "unavailable", "leaves": rows}

    def reserve_leaf(self):
        self.leaf_count += 1
        if self.leaf_count > MAX_LEAVES:
            raise ValueError("official-shared-leaf-limit")

    def message_call(self, item):
        return (item.get("type") == "function_call" and item.get("namespace") == self.namespace
                and item.get("name") in self.names and item.get("encrypted_function_args", []) == [])

    def feed(self, data):
        self.total += len(data)
        if self.total > MAX_PARSE_BYTES:
            raise ValueError("official-byte-limit")
        for event in self.parser.feed(data):
            if event.data and event.data != b"[DONE]":
                self.accept(bounded_json(event.data))

    def accept(self, event):
        self.events += 1
        if self.events > 4096 or self.terminal:
            raise ValueError("official-event-limit-or-post-terminal")
        sequence = event.get("sequence_number")
        if self.sequence_present is None:
            self.sequence_present = sequence is not None
        elif self.sequence_present != (sequence is not None):
            raise ValueError("official-event-sequence-unavailable")
        if sequence is not None:
            if type(sequence) is not int or sequence < 0 or (self.sequence is not None and sequence != self.sequence + 1):
                raise ValueError("official-event-order")
            self.sequence = sequence
        if "response_id" in event and identity(event["response_id"]) != self.response:
            raise ValueError("official-response-identity")
        kind = event.get("type")
        if kind == "response.created":
            if self.response is not None:
                raise ValueError("official-duplicate-response")
            self.response = identity(event["response"]["id"])
        elif kind == "response.output_item.added":
            index, item = event["output_index"], event["item"]
            if self.response is None or type(index) is not int or index != len(self.items) or len(self.items) >= MAX_LEAVES:
                raise ValueError("official-item-order")
            key = identity(item.get("id"))
            if any(state["id"] == key for state in self.items.values()):
                raise ValueError("official-duplicate-item")
            if (item.get("type") == "function_call" and item.get("namespace") == self.namespace
                    and item.get("name") in self.message_tools and not self.message_call(item)):
                self.unavailable = True
            self.items[index] = {"id": key, "call": identity(item["call_id"]) if item.get("type") == "function_call" else None,
                "selected_call": self.message_call(item), "selected_text": item.get("type") == "message" and item.get("role") == "assistant",
                "arguments": bytearray(), "argument_hash": None, "texts": {}, "done": None,
                "type": item.get("type"), "name": item.get("name"), "namespace": item.get("namespace")}
        elif kind in ("response.function_call_arguments.delta", "response.function_call_arguments.done",
                      "response.output_text.delta", "response.output_text.done"):
            index = event["output_index"]
            if type(index) is not int or index < 0 or index not in self.items:
                raise ValueError("official-output-index")
            state = self.items[index]
            if identity(event["item_id"]) != state["id"] or state["done"] is not None:
                raise ValueError("official-item-identity")
            if kind.startswith("response.function_call_arguments.") and state["selected_call"]:
                if state["argument_hash"] is not None:
                    raise ValueError("official-argument-order")
                if kind.endswith(".delta"):
                    data = event["delta"].encode()
                    self.parts += 1
                    self.argument_bytes += len(data)
                    if self.parts > MAX_PARTS or self.argument_bytes > MAX_JSON_BYTES:
                        raise ValueError("official-shared-argument-limit")
                    state["arguments"].extend(data)
                else:
                    data = event["arguments"].encode()
                    if data != state["arguments"]:
                        raise ValueError("official-argument-delta-mismatch")
                    state["argument_hash"] = hashlib.sha256(data).hexdigest()
            elif kind.startswith("response.output_text.") and state["selected_text"]:
                index = event["content_index"]
                if type(index) is not int or index < 0:
                    raise ValueError("official-content-index")
                if index not in state["texts"]:
                    self.reserve_leaf()
                    state["texts"][index] = (BoundedValue(), None)
                value, done = state["texts"][index]
                if done is not None:
                    raise ValueError("official-text-order")
                if kind.endswith(".delta"):
                    self.parts += 1
                    if self.parts > MAX_PARTS:
                        raise ValueError("official-shared-part-limit")
                    value.add(event["delta"].encode())
                else:
                    data = event["text"].encode()
                    if not value.count or value.length != len(data) or value.hash.hexdigest() != hashlib.sha256(data).hexdigest():
                        raise ValueError("official-text-delta-mismatch")
                    state["texts"][index] = (value, hashlib.sha256(data).hexdigest())
        elif kind == "response.output_item.done":
            index, item = event["output_index"], event["item"]
            if type(index) is not int or index < 0 or index not in self.items:
                raise ValueError("official-output-index")
            state = self.items[index]
            if (identity(item.get("id")) != state["id"] or state["done"] is not None or item.get("type") != state["type"]
                    or item.get("name") != state["name"] or item.get("namespace") != state["namespace"]):
                raise ValueError("official-completed-item-mismatch")
            common = {"response_sha256": self.response, "item_sha256": state["id"], "call_sha256": state["call"],
                      "output_index": index, "event_ordinal": self.events, "sequence_number": sequence,
                      "phase": "output_item.done; terminal membership checked separately"}
            if state["selected_call"]:
                if (not self.message_call(item) or item.get("status") not in (None, "completed")
                        or identity(item.get("call_id")) != state["call"]):
                    raise ValueError("official-call-identity")
                data = item["arguments"].encode()
                if state["argument_hash"] is None or hashlib.sha256(data).hexdigest() != state["argument_hash"]:
                    raise ValueError("official-completed-arguments-mismatch")
                parsed = bounded_json(bytes(state["arguments"]))
                if not isinstance(parsed, dict) or not isinstance(parsed.get("message"), str):
                    raise ValueError("official-message-unavailable")
                self.reserve_leaf()
                value = BoundedValue()
                value.add(parsed["message"].encode())
                self.values.append(value)
                self.rows.append({**value.summary(self.capture.plan, False), **common, "channel": "arguments.message", "content_index": None})
                # Only the whole message is eligible; full arguments/target are discarded.
                state["arguments"].clear()
            if state["selected_text"]:
                if item.get("role") != "assistant" or item.get("status") != "completed":
                    raise ValueError("official-assistant-incomplete")
                content = item.get("content", [])
                indices = {i for i, part in enumerate(content) if part.get("type") == "output_text"}
                if indices != set(state["texts"]):
                    raise ValueError("official-content-membership")
                for i, (value, done) in state["texts"].items():
                    if done is None or hashlib.sha256(content[i]["text"].encode()).hexdigest() != done:
                        raise ValueError("official-completed-text-mismatch")
                    self.values.append(value)
                    self.rows.append({**value.summary(self.capture.plan, False), **common, "channel": "output_text", "content_index": i})
            state["done"] = item_digest(item)
        elif kind == "response.completed":
            response = event["response"]
            output = response.get("output", [])
            if (identity(response.get("id")) != self.response or response.get("status") != "completed"
                    or len(output) != len(self.items) or any(self.items[i]["done"] != item_digest(item) for i, item in enumerate(output))):
                raise ValueError("official-terminal-membership")
            self.terminal = True
        elif kind in ("error", "response.failed", "response.incomplete"):
            raise ValueError("official-stream-error")

    def finish(self, complete=True, *, framed=None):
        def finalize():
            if framed is None:
                end = self.parser.finish() if complete else self.parser.cancel()
                framing = end.disposition == "complete" and end.discarded_bytes == 0 and not end.events
            else:
                framing = framed
            good = complete and framing and self.terminal and bool(self.rows) and not self.unavailable and not self.capture.failure
            for row, value in zip(self.rows, self.values):
                # Re-admit raw only after every Item and terminal binding succeeds.
                row["complete"] = good
                if good and row["utf8_bytes"] <= MAX_VALUE:
                    # BoundedValue's summary deliberately withheld raw while partial.
                    row.update(value.summary(self.capture.plan, True))
                if row["rejection"] == "unapproved":
                    row.update(state="unavailable", complete=False)
            unapproved = any(row["rejection"] == "unapproved" for row in self.rows)
            good = good and not unapproved
            self.capture.boundaries[self.boundary] = {"state": "observed" if self.rows and not unapproved else "unavailable",
                "complete": good, "leaves": self.rows, "cause": "unknown" if not good else None,
                "terminal_event_ordinal": self.events if self.terminal else None,
                "coverage": ("successful response reader receives; may be read-ahead; consumption/forwarding unproven"
                             if self.boundary == "officialreceived" else "qualifier forwarded post-Gateway bytes; caller consumption unproven")}
        self.capture.guard(finalize)
        if self.boundary not in self.capture.boundaries:
            self.capture.boundaries[self.boundary] = {"state": "incomplete", "complete": False, "leaves": self.rows, "cause": "unknown"}


class DownstreamTap:
    def __init__(self, capture, official_payload=None):
        from sse_events import SseEventAssembler
        self.capture = capture
        self.parser = SseEventAssembler(max_frame_bytes=MAX_JSON_BYTES)
        self.delta = BoundedValue()
        self.completed = BoundedValue()
        self.terminal = False
        self.total = self.events = 0
        self.official = None
        if official_payload is not None:
            def select():
                self.official = OfficialTap(capture, official_message_names(official_payload), "downstreamOfficialmessages", "collaboration")
            capture.guard(select)

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
            if self.official:
                self.capture.guard(self.official.accept, value)
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
        if self.official:
            self.official.finish(complete, framed=good and termination.discarded_bytes == 0)


def peer_ticket(root, plan, downstream_socket):
    try:
        host, port = downstream_socket.getpeername()[:2]
        if host != "127.0.0.1":
            return None
        path = root / f"peer-{port}.json"
        if path.stat().st_size > 4096:
            return None
        with path.open("rb") as source:
            data = source.read(4097)
        ticket = bounded_json(data, 4096)
        if (ticket["run_id"] != plan["run_id"] or ticket["case"] != plan["case"]
                or type(ticket["epoch"]) is not int or ticket["epoch"] not in (0, 1)
                or not isinstance(ticket["request_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", ticket["request_id"])):
            return None
        return {key: ticket[key] for key in ("run_id", "case", "epoch", "request_id")}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def install_gateway_observation(root, plan):
    """Qualification startup only; use the public owning exchange/socket seam."""
    require_private_storage()
    import subscription_exchange
    import gateway_exchange_adapters
    original = subscription_exchange.open_subscription
    @contextmanager
    def observed(request, *, provider_id, timeout, backend=None, downstream_socket=None):
        ticket = None
        if provider_id == "cursor-subscription" and backend is None and downstream_socket is not None:
            ticket = peer_ticket(root, plan, downstream_socket)
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
    original_open = gateway_exchange_adapters.LiveTransport.open

    def official_open(transport, opening):
        # Actual immutable route selection, independent of requested model expectation.
        if opening.upstream_name != "official":
            return original_open(transport, opening)
        ticket = peer_ticket(root, plan, getattr(transport._handler, "connection", None))
        if ticket is None:
            return original_open(transport, opening)
        capture = FixtureCapture(plan, ticket)
        capture.association = {"actual_route": "official", "opening_association": {}}
        for name in ("adaptedhistory", "servedhistoryblob", "nativefield", "canonicalchunks"):
            capture.boundaries[name] = {"state": "not-applicable", "complete": False}
        tap = OfficialTap(capture, set())
        def prepare():
            context = opening.event_context or {}
            capture.association["opening_association"] = {
                "gateway_request_sha256": identity(context.get("request_id")),
                "route_attempt_index": context.get("route_attempt_index") if type(context.get("route_attempt_index")) is int else None,
                "native_boundary": "successful response readline bytes; may include reader read-ahead; consumption/forwarding unproven"}
            body = opening.request.data or b""
            payload = request_payload(body, opening.request.get_header("Content-encoding", ""), budget=tap)
            tap.names = official_message_names(payload)
            tap.history(payload)
        capture.guard(prepare)

        @contextmanager
        def context_view():
            succeeded = yielded = False
            try:
                with original_open(transport, opening) as response:
                    supported = (200 <= (getattr(response, "status", None) or response.getcode()) < 300
                                 and opening.upstream_format == "responses"
                                 and "text/event-stream" in response.headers.get("Content-Type", ""))
                    class ResponseView:
                        def __getattr__(self, name):
                            return getattr(response, name)
                        def readline(self, *args, **kwargs):
                            data = response.readline(*args, **kwargs)
                            if supported:
                                capture.guard(tap.feed, data)
                            return data  # Same object; owner errors/close/release stay delegated.
                    yield ResponseView()
                    yielded = True
                succeeded = yielded and supported
            finally:
                # Observation/persistence cannot replace an opening/read/owner exception.
                try:
                    tap.finish(succeeded)
                    capture.persist(root / (ticket["request_id"] + ".json"))
                except Exception:
                    pass
        return context_view()
    gateway_exchange_adapters.LiveTransport.open = official_open


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", action="store_true", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--tickets", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--observe-cursor-phases", action="store_true")
    args = parser.parse_args(argv)
    require_private_storage()
    if args.plan.stat().st_size > 65536:
        raise ValueError("fixture-plan-limit")
    plan = validate_plan(json.loads(args.plan.read_bytes()))
    install_gateway_observation(args.tickets, plan)
    phase_observer = install_cursor_phase_observation(args.tickets) if args.observe_cursor_phases else None
    sys.argv = ["codex_proxy.py", "--port", str(args.port)]
    try:
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "src-python/codex_proxy.py"), run_name="__main__")
    finally:
        if phase_observer is not None:
            phase_observer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
