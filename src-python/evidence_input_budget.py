"""Fail-closed inputs and pre-dispatch admission for Claude evidence (#557).

This module deliberately has no transport implementation.  A caller loads one
explicit input contract and calls ``reserve`` immediately before each real
upstream attempt with the exact immutable request bytes it is about to send.
The check and attempt reservation happen under one lock; logging is not part
of enforcement.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import threading
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from protocol_json import AmbiguousJSONError, strict_json_loads

SCHEMA = "codexhub.claude-evidence-input.v1"
GRANT_SCHEMA = "codexhub.claude-evidence-grant.v1"
MAX_ATTEMPTS_PER_PROTOCOL = 20
MAX_OUTPUT_TOKENS = 2_048
MAX_ROUND_SECONDS = 30 * 60
PROTOCOLS = frozenset({"anthropic_messages", "responses", "chat_completions"})
TOKEN_FIELDS = frozenset({"max_tokens", "max_output_tokens", "max_completion_tokens"})
_SECRET_QUERY_KEYS = frozenset({"api_key", "authorization", "password", "secret", "token"})


class EvidenceInputError(ValueError):
    """Raised when an evidence input cannot be used safely."""


class BudgetRefused(EvidenceInputError):
    """Raised before an outbound attempt when admission fails."""


@dataclass(frozen=True, slots=True)
class Reservation:
    """Non-sensitive admission result returned to the outbound caller."""

    protocol: str
    attempt: int
    deadline_remaining: float


@dataclass(frozen=True, slots=True)
class _Credential:
    path: Path
    schema: str
    target_origin: str
    secret_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Leg:
    id: str
    protocol: str
    provider: str
    model: str
    reasoning_effort: str | None
    token_field: str
    endpoint: str
    routes: Mapping[str, frozenset[str]]
    credential: _Credential | None
    decision: str


@dataclass(frozen=True, slots=True)
class _Grant:
    round_id: str
    issued_at: datetime
    expires_at: datetime
    claim_path: Path
    decisions: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Limits:
    max_attempts_per_protocol: int
    max_output_tokens: int
    deadline_seconds: int


class EvidenceInput:
    """One explicit, immutable evidence round and its admission budget."""

    def __init__(
        self,
        *,
        source: Path,
        grant: _Grant,
        legs: tuple[_Leg, ...],
        limits: _Limits,
        monotonic: Callable[[], float],
        wall_clock: Callable[[], datetime],
    ) -> None:
        self.source = source
        self._grant = grant
        self._legs = legs
        self._by_protocol = {leg.protocol: leg for leg in legs}
        self._limits = limits
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._deadline = monotonic() + limits.deadline_seconds
        self._deadline_reached = False
        self._grant_expired = False
        self._counts: dict[str, int] = {}
        self._refusals: list[dict[str, str]] = []
        self._lock = threading.Lock()

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> "EvidenceInput":
        """Load one explicit contract; no auth-store or environment discovery occurs."""
        contract_path = _explicit_file(path, label="evidence input")
        try:
            contract_bytes = contract_path.read_bytes()
            raw = strict_json_loads(contract_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError, AmbiguousJSONError) as exc:
            raise EvidenceInputError("invalid evidence input JSON") from exc
        if not isinstance(raw, Mapping) or raw.get("schema") != SCHEMA:
            raise EvidenceInputError("invalid evidence input schema")

        grant = _parse_grant(raw.get("grant"))
        limits = _parse_limits(raw.get("limits"))
        raw_legs = raw.get("legs")
        if not isinstance(raw_legs, list) or not raw_legs:
            raise EvidenceInputError("evidence input needs a non-empty legs list")

        grant_ids = set(grant.decisions)
        legs: list[_Leg] = []
        seen_ids: set[str] = set()
        seen_protocols: set[str] = set()
        for item in raw_legs:
            leg = _parse_leg(item, grant=grant)
            if leg.id in seen_ids:
                raise EvidenceInputError("duplicate evidence leg id")
            if leg.protocol in seen_protocols:
                raise EvidenceInputError("duplicate protocol binding")
            seen_ids.add(leg.id)
            seen_protocols.add(leg.protocol)
            legs.append(leg)
        if grant_ids != seen_ids:
            raise EvidenceInputError("grant decisions must name exactly the configured legs")

        clock = wall_clock or (lambda: datetime.now(timezone.utc))
        # Validate the clock shape now, but keep expiry enforcement at reserve time.
        _as_utc(clock(), label="wall clock")
        _claim_grant(grant, contract_bytes)
        return cls(
            source=contract_path,
            grant=grant,
            legs=tuple(legs),
            limits=limits,
            monotonic=monotonic,
            wall_clock=clock,
        )

    def reserve(self, protocol: str, method: str, final_url: str, body: bytes) -> Reservation:
        """Atomically validate and reserve one would-be upstream HTTP attempt.

        ``body`` must be the exact immutable bytes passed to the HTTP client
        after this function returns.  This function never rewrites it.
        """
        with self._lock:
            leg = self._by_protocol.get(protocol)
            if leg is None:
                return self._refuse(protocol, "protocol is not configured")
            now = self._monotonic()
            if now >= self._deadline:
                self._deadline_reached = True
            if self._deadline_reached:
                return self._refuse(protocol, "global monotonic deadline reached")
            wall_now = _as_utc(self._wall_clock(), label="wall clock")
            status = self._authorization_status(leg.id, wall_now)
            if status != "approved":
                return self._refuse(protocol, f"authorization is {status}")
            used = self._counts.get(protocol, 0)
            if used >= self._limits.max_attempts_per_protocol:
                return self._refuse(protocol, "per-protocol attempt bound reached")

            method_name = _method(method)
            parts = _safe_url(final_url)
            if _parts_origin(parts) != leg.endpoint:
                return self._refuse(protocol, "final URL or method is outside the fixed binding")
            path = _route_path(parts.path)
            route = leg.routes.get(path)
            if route is None or method_name not in route:
                return self._refuse(protocol, "final URL or method is outside the fixed binding")
            self._validate_body(leg, method_name, path, body)

            attempt = used + 1
            self._counts[protocol] = attempt
            return Reservation(
                protocol=protocol,
                attempt=attempt,
                deadline_remaining=max(0.0, self._deadline - now),
            )

    def redacted_summary(self) -> dict[str, Any]:
        """Return an audit-safe summary with no credential values or paths."""
        with self._lock:
            wall_now = _as_utc(self._wall_clock(), label="wall clock")
            authorization = {
                leg_id: self._authorization_status(leg_id, wall_now)
                for leg_id in sorted(self._grant.decisions)
            }
            return {
                "schema": SCHEMA,
                "source": self.source.name,
                "grant": {"round_id": self._grant.round_id, "authorization": authorization},
                "limits": {
                    "max_attempts_per_protocol": self._limits.max_attempts_per_protocol,
                    "max_output_tokens": self._limits.max_output_tokens,
                    "deadline_seconds": self._limits.deadline_seconds,
                },
                "legs": [
                    {
                        "id": leg.id,
                        "protocol": leg.protocol,
                        "provider": leg.provider,
                        "model": leg.model,
                        "reasoning_effort": leg.reasoning_effort,
                        "token_field": leg.token_field,
                        "endpoint": leg.endpoint,
                        "routes": sorted(leg.routes),
                        "authorization": authorization[leg.id],
                        "credential_present": leg.credential is not None and leg.credential.path.is_file(),
                    }
                    for leg in self._legs
                ],
            }

    def admission_summary(self) -> dict[str, Any]:
        """Return counts and refusal reasons without request bodies or URLs."""
        with self._lock:
            return {
                "counts": dict(self._counts),
                "refusals": [dict(item) for item in self._refusals],
            }

    def _authorization_status(self, leg_id: str, now: datetime) -> str:
        if self._grant_expired:
            return "expired"
        status = _grant_status(self._grant, leg_id, now)
        if status == "expired":
            self._grant_expired = True
        return status

    def _validate_body(self, leg: _Leg, method: str, path: str, body: bytes) -> None:
        if type(body) is not bytes:  # exact immutable carrier, not bytearray/memoryview
            self._refuse(leg.protocol, "outbound body must be immutable bytes")
        if method in {"GET", "HEAD"}:
            if body:
                self._refuse(leg.protocol, "probe body must be empty")
            return
        if not body:
            self._refuse(leg.protocol, "request body is required")
        payload = _json_object(body)
        model = payload.get("model")
        if model != leg.model:
            self._refuse(leg.protocol, "request model does not match the fixed binding")

        unknown_token_fields = {
            key for key in payload if isinstance(key, str) and "token" in key.lower() and key not in TOKEN_FIELDS
        }
        if unknown_token_fields:
            self._refuse(leg.protocol, "request token field is unknown")
        present = TOKEN_FIELDS.intersection(payload)
        if path.endswith("/count_tokens") and leg.protocol != "anthropic_messages":
            self._refuse(leg.protocol, "count_tokens is only valid for Anthropic Messages")
        if path.endswith("/count_tokens"):
            if present and present != {leg.token_field}:
                self._refuse(leg.protocol, "request token field is ambiguous")
        elif present != {leg.token_field}:
            self._refuse(leg.protocol, "request token field is absent, unknown, or ambiguous")
        if present:
            value = payload[leg.token_field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                self._refuse(leg.protocol, "request token limit is not a positive integer")
            if value > self._limits.max_output_tokens:
                self._refuse(leg.protocol, "request token limit exceeds the output bound")
        _validate_reasoning(leg, payload, required=not path.endswith("/count_tokens"))

    def _refuse(self, protocol: str, reason: str) -> Any:
        self._refusals.append({"protocol": protocol, "reason": reason})
        raise BudgetRefused(f"{protocol}: {reason}")


def _require_owner_only_mode() -> None:
    if not callable(getattr(os, "fchmod", None)):
        raise EvidenceInputError("owner-only credential/claim files are unsupported on this platform")


def materialize_credential(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> Path:
    """Copy a supplied credential fixture to a new owner-only file.

    This is intentionally a one-way byte copy: it never refreshes OAuth, writes
    the source, follows source/destination symlinks, or emits the copied value.
    """
    _require_owner_only_mode()
    source_path = _explicit_file(source, label="credential source")
    destination_path = Path(destination)
    if not destination_path.is_absolute():
        raise EvidenceInputError("credential destination must be an absolute path")
    if destination_path.exists() or destination_path.is_symlink():
        raise EvidenceInputError("credential destination exists")
    parent = destination_path.parent
    if not parent.is_dir() or parent.is_symlink() or parent.resolve() != parent:
        raise EvidenceInputError("credential destination parent is not an isolated directory")
    try:
        payload = source_path.read_bytes()
        fd = os.open(destination_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("credential copy made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, ValueError, AttributeError, NotImplementedError) as exc:
        try:
            destination_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise EvidenceInputError("credential materialization failed") from exc
    return destination_path


def _parse_grant(value: Any) -> _Grant:
    if not isinstance(value, Mapping) or value.get("schema") != GRANT_SCHEMA:
        raise EvidenceInputError("invalid or absent user grant")
    round_id = value.get("round_id")
    if not isinstance(round_id, str) or not round_id.strip():
        raise EvidenceInputError("grant round_id is required")
    issued = _parse_datetime(value.get("issued_at"), label="grant issued_at")
    expires = _parse_datetime(value.get("expires_at"), label="grant expires_at")
    if expires <= issued:
        raise EvidenceInputError("grant expires_at must be after issued_at")
    claim_path = _claim_path(value.get("claim_path"))
    raw_legs = value.get("legs")
    if not isinstance(raw_legs, Mapping) or not raw_legs:
        raise EvidenceInputError("grant needs explicit leg decisions")
    decisions: dict[str, str] = {}
    for leg_id, raw_decision in raw_legs.items():
        if not isinstance(leg_id, str) or not leg_id.strip() or not isinstance(raw_decision, Mapping):
            raise EvidenceInputError("invalid grant leg decision")
        decision = raw_decision.get("decision")
        if decision not in {"approved", "refused", "undecided"}:
            raise EvidenceInputError("grant leg decision must be approved, refused, or undecided")
        decisions[leg_id] = decision
    return _Grant(round_id.strip(), issued, expires, claim_path, decisions)


def _claim_path(value: Any) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise EvidenceInputError("grant claim_path must be an explicit absolute path")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise EvidenceInputError("grant claim_path must be an absolute non-symlink path")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink() or parent.resolve() != parent:
        raise EvidenceInputError("grant claim_path parent must be an isolated directory")
    return path


def _claim_grant(grant: _Grant, contract_bytes: bytes) -> None:
    _require_owner_only_mode()
    marker = json.dumps(
        {
            "schema": "codexhub.claude-evidence-grant-claim.v1",
            "round_id": grant.round_id,
            "input_sha256": hashlib.sha256(contract_bytes).hexdigest(),
        },
        separators=(",", ":"),
    ).encode("ascii")
    try:
        fd = os.open(grant.claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise EvidenceInputError("user grant has already been claimed") from exc
    except OSError as exc:
        raise EvidenceInputError("grant claim could not be created") from exc
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, marker)
        os.fsync(fd)
    except (OSError, AttributeError, NotImplementedError) as exc:
        raise EvidenceInputError("grant claim could not be committed") from exc
    finally:
        os.close(fd)


def _parse_limits(value: Any) -> _Limits:
    raw = value if isinstance(value, Mapping) else {}
    attempts = raw.get("max_attempts_per_protocol", MAX_ATTEMPTS_PER_PROTOCOL)
    output = raw.get("max_output_tokens", MAX_OUTPUT_TOKENS)
    deadline = raw.get("deadline_seconds", MAX_ROUND_SECONDS)
    if any(isinstance(item, bool) or not isinstance(item, int) for item in (attempts, output, deadline)):
        raise EvidenceInputError("limits must be integers")
    if not 1 <= attempts <= MAX_ATTEMPTS_PER_PROTOCOL:
        raise EvidenceInputError("max_attempts_per_protocol exceeds the fixed bound")
    if not 1 <= output <= MAX_OUTPUT_TOKENS:
        raise EvidenceInputError("max_output_tokens exceeds the fixed bound")
    if not 1 <= deadline <= MAX_ROUND_SECONDS:
        raise EvidenceInputError("deadline_seconds exceeds the fixed bound")
    return _Limits(attempts, output, deadline)


def _parse_leg(value: Any, *, grant: _Grant) -> _Leg:
    if not isinstance(value, Mapping):
        raise EvidenceInputError("invalid evidence leg")
    leg_id = value.get("id")
    protocol = value.get("protocol")
    provider = value.get("provider")
    model = value.get("model")
    if not all(isinstance(item, str) and item.strip() for item in (leg_id, protocol, provider, model)):
        raise EvidenceInputError("leg id, protocol, provider, and model are required")
    leg_id, protocol, provider, model = (item.strip() for item in (leg_id, protocol, provider, model))
    if protocol not in PROTOCOLS:
        raise EvidenceInputError("unsupported evidence protocol")
    endpoint = _origin(str(value.get("endpoint", "")))
    token_field = value.get("token_field")
    expected_fields = {
        "anthropic_messages": {"max_tokens"},
        "responses": {"max_output_tokens"},
        "chat_completions": {"max_tokens", "max_completion_tokens"},
    }[protocol]
    if token_field not in expected_fields:
        raise EvidenceInputError("token_field is not valid for the selected protocol")
    reasoning = value.get("reasoning_effort")
    if reasoning is not None and (not isinstance(reasoning, str) or not reasoning.strip()):
        raise EvidenceInputError("reasoning_effort must be a non-empty string when present")

    raw_routes = value.get("routes")
    if not isinstance(raw_routes, Mapping) or not raw_routes:
        raise EvidenceInputError("leg needs explicit fixed routes")
    routes: dict[str, frozenset[str]] = {}
    for raw_path, raw_methods in raw_routes.items():
        if not isinstance(raw_path, str) or _route_path(raw_path) != raw_path or "?" in raw_path:
            raise EvidenceInputError("route path must be an explicit absolute path")
        if not isinstance(raw_methods, list) or not raw_methods:
            raise EvidenceInputError("route methods are required")
        methods = frozenset(_method(item) for item in raw_methods)
        routes[raw_path] = methods

    decision = grant.decisions.get(leg_id)
    if decision is None:
        raise EvidenceInputError("leg has no explicit grant decision")
    credential = _parse_credential(value.get("credential"), endpoint=endpoint, required=decision == "approved")
    return _Leg(
        id=leg_id,
        protocol=protocol,
        provider=provider,
        model=model,
        reasoning_effort=reasoning.strip() if isinstance(reasoning, str) else None,
        token_field=token_field,
        endpoint=endpoint,
        routes=routes,
        credential=credential,
        decision=decision,
    )


def _parse_credential(value: Any, *, endpoint: str, required: bool) -> _Credential | None:
    if value is None:
        if required:
            raise EvidenceInputError("approved leg needs an explicit credential path")
        return None
    if not required:
        raise EvidenceInputError("refused or undecided leg must not carry a credential")
    if not isinstance(value, Mapping):
        raise EvidenceInputError("invalid credential binding")
    path = _explicit_file(value.get("path"), label="credential path")
    schema = value.get("schema")
    target_origin = value.get("target_origin")
    raw_fields = value.get("secret_fields")
    if not isinstance(schema, str) or not schema.strip():
        raise EvidenceInputError("credential schema is required")
    if not isinstance(target_origin, str) or _origin(target_origin) != endpoint:
        raise EvidenceInputError("credential target origin does not match the fixed endpoint")
    if not isinstance(raw_fields, list) or not raw_fields or any(
        not isinstance(field, str) or not field.strip() for field in raw_fields
    ):
        raise EvidenceInputError("credential secret_fields are required")
    secret_fields = tuple(field.strip() for field in raw_fields)
    _validate_credential_file(path, schema=schema.strip(), target_origin=endpoint, secret_fields=secret_fields)
    return _Credential(path, schema.strip(), endpoint, secret_fields)


def _validate_credential_file(path: Path, *, schema: str, target_origin: str, secret_fields: tuple[str, ...]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".toml":
            payload = tomllib.loads(text)
        else:
            payload = strict_json_loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError, AmbiguousJSONError, tomllib.TOMLDecodeError) as exc:
        raise EvidenceInputError("invalid explicit credential fixture") from exc
    if not isinstance(payload, Mapping):
        raise EvidenceInputError("credential fixture must be an object")
    declared_schema = payload.get("schema")
    if declared_schema is not None and declared_schema != schema:
        raise EvidenceInputError("credential schema does not match its binding")
    for field in secret_fields:
        value = _lookup(payload, field)
        if not isinstance(value, str) or not value:
            raise EvidenceInputError("credential fixture is missing a declared secret")
    for key in ("target_origin", "endpoint_origin"):
        declared_origin = payload.get(key)
        if declared_origin is not None and (not isinstance(declared_origin, str) or _origin(declared_origin) != target_origin):
            raise EvidenceInputError("credential fixture target origin does not match its binding")


def _lookup(payload: Mapping[str, Any], field: str) -> Any:
    value: Any = payload
    for part in field.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _grant_status(grant: _Grant, leg_id: str, now: datetime) -> str:
    decision = grant.decisions[leg_id]
    if now < grant.issued_at:
        return "not-yet-valid"
    if now >= grant.expires_at:
        return "expired"
    return decision


def _explicit_file(value: Any, *, label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise EvidenceInputError(f"{label} must be an explicit absolute path")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise EvidenceInputError(f"{label} must be an explicit canonical regular file")
    return path


def _parse_datetime(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceInputError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceInputError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise EvidenceInputError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _as_utc(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EvidenceInputError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _origin(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise EvidenceInputError("endpoint must be an absolute HTTP(S) origin")
    if parts.scheme == "http" and parts.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise EvidenceInputError("plaintext endpoints are limited to loopback synthetic fixtures")
    if parts.query or parts.fragment or (parts.path not in {"", "/"}):
        raise EvidenceInputError("endpoint must not include a path, query, or fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise EvidenceInputError("endpoint has an invalid port") from exc
    default = (parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)
    suffix = "" if port is None or default else f":{port}"
    return f"{parts.scheme.lower()}://{parts.hostname.lower()}{suffix}"


def _safe_url(value: str):
    if not isinstance(value, str) or not value:
        raise BudgetRefused("request URL is invalid")
    parts = urlsplit(value)
    try:
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError
        if parts.scheme == "http" and parts.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError
        if parts.fragment:
            raise ValueError
        if any(key.lower() in _SECRET_QUERY_KEYS for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
            raise ValueError
        # Accessing port rejects malformed values.
        _ = parts.port
    except ValueError as exc:
        raise BudgetRefused("request URL is invalid or contains credential material") from exc
    return parts


def _parts_origin(parts: Any) -> str:
    try:
        port = parts.port
    except ValueError as exc:
        raise BudgetRefused("request URL is invalid") from exc
    default = (parts.scheme.lower() == "http" and port == 80) or (
        parts.scheme.lower() == "https" and port == 443
    )
    suffix = "" if port is None or default else f":{port}"
    return f"{parts.scheme.lower()}://{parts.hostname.lower()}{suffix}"


def _route_path(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise EvidenceInputError("route path must start with '/'")
    normalized = posixpath.normpath(value)
    if normalized != value or ".." in value.split("/"):
        raise EvidenceInputError("route path must be canonical")
    return value


def _method(value: str) -> str:
    if not isinstance(value, str) or not value or not value.isascii() or not value.isalpha():
        raise EvidenceInputError("HTTP method is invalid")
    return value.upper()


def _json_object(body: bytes) -> Mapping[str, Any]:
    try:
        payload = strict_json_loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, AmbiguousJSONError) as exc:
        raise BudgetRefused("outbound body is not an unambiguous JSON object") from exc
    if not isinstance(payload, Mapping):
        raise BudgetRefused("outbound body is not a JSON object")
    return payload


def _validate_reasoning(leg: _Leg, payload: Mapping[str, Any], *, required: bool) -> None:
    expected = leg.reasoning_effort
    values: list[str] = []
    if leg.protocol == "responses" and "reasoning" in payload:
        value = payload["reasoning"]
        if not isinstance(value, Mapping) or "effort" not in value or not isinstance(value["effort"], str):
            raise BudgetRefused("reasoning selection is not explicit")
        values.append(value["effort"])
    elif leg.protocol == "chat_completions":
        if "reasoning_effort" in payload:
            value = payload["reasoning_effort"]
            if not isinstance(value, str):
                raise BudgetRefused("reasoning selection is not explicit")
            values.append(value)
        if "reasoning" in payload:
            value = payload["reasoning"]
            if not isinstance(value, Mapping) or not isinstance(value.get("effort"), str):
                raise BudgetRefused("reasoning selection is not explicit")
            values.append(value["effort"])
    elif leg.protocol == "anthropic_messages" and "thinking" in payload:
        value = payload["thinking"]
        if not isinstance(value, Mapping):
            raise BudgetRefused("reasoning selection is not explicit")
        if isinstance(value.get("effort"), str):
            values.append(value["effort"])
        elif expected is not None:
            raise BudgetRefused("reasoning selection is not explicit")
    if len(set(values)) > 1:
        raise BudgetRefused("reasoning selection is ambiguous")
    if expected is not None and required and not values:
        raise BudgetRefused("reasoning selection is absent")
    if expected is not None and values and values[0] != expected:
        raise BudgetRefused("reasoning selection does not match the fixed binding")
