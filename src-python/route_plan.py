from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit
import hashlib
import logging
import re

from catalog import canonical_model_id
from codex_semantic_adapter import COLLABORATION_V2
from gateway_errors import (
    UnqualifiedRouteProtocolError,
    UnsupportedRouteProtocolError,
    UpstreamProtocolTranslationError,
    _catalog_failure,
    _identity_failure,
)
from gateway_settings import (
    _default_retry_attempts_for_request_kind,
    _request_kind_retry_attempts_configured,
    _upstream_retry_attempts,
    gateway_auto_retry_max_attempts,
    gateway_capacity_retry_elapsed_limit_seconds,
    gateway_downstream_retry_notice_enabled,
    gateway_official_http_passthrough_enabled,
    gateway_stream_retry_elapsed_limit_seconds,
    official_upstream_open_attempts,
    upstream_timeout_seconds,
)
from protocol_translation import (
    PreparedExchange,
    UnsupportedProtocolTranslationError,
    prepare_exchange,
)
from providers_config import (
    NATIVE_RESPONSES_TOOL_CODECS,
    TOOL_PROTOCOLS,
    TOOL_SURFACE_STRATEGIES,
)
from route_primitives import (
    AUTO_UPSTREAM_PROTOCOL_FALLBACK_STATUSES,
    AttemptRequestBodyMode,
    AuthenticationStrategy,
    authentication_strategy as _authentication_strategy,
    BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER,
    BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
    BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
    BEHAVIOR_OFFICIAL_GATEWAY_COMPAT,
    BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
    CAPACITY_RETRY_CADENCE_SECONDS,
    CAPACITY_RETRY_FAILURE_CLASSES,
    CODEX_SEMANTIC_EXTERNAL_ADAPTER,
    CODEX_SEMANTIC_NONE,
    CallerRequestBodyMode,
    CapabilityState,
    CodexCompatibilityPolicy,
    CollaborationBackend,
    DEFAULT_CAPACITY_RETRY_ELAPSED_LIMIT_SECONDS,
    DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS,
    DEFAULT_MAIN_GENERATION_PRE_RESPONSE_BUDGET_SECONDS,
    DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS,
    DEFAULT_STREAM_RETRY_ELAPSED_LIMIT_SECONDS,
    DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
    ExecutionOwner,
    FrozenRequestHeaders,
    MutationPolicy,
    REPAIR_CODEX_SUBAGENT,
    REPAIR_NONE,
    REQUEST_KIND_GATEWAY,
    REQUEST_KIND_TRANSPARENT,
    RETRY_FAILURE_PROVIDER_THROTTLE,
    RETRY_FAILURE_QUICK_TRANSIENT,
    RETRY_REQUEST_COMPACT,
    RETRY_REQUEST_MAIN_GENERATION,
    ROUTE_PLAN_SCHEMA_VERSION,
    RetryPolicy,
    RouteMutation,
    RouteProtocol,
    StreamingPolicy,
    ToolExposureMode,
    TransportPolicy,
    UsagePolicy,
    VISION_PROXY_CODEX_APP_ADAPTER,
    VISION_PROXY_DISABLED,
    VISION_PROXY_TRANSPARENT_OVERLAY,
    VisionAction,
    VisionNetworkAction,
    WIRE_CHAT_TO_RESPONSES,
    WIRE_RESPONSES_TO_CHAT,
    WIRE_TRANSPARENT,
)


TOOL_SURFACE_STRATEGY_ERROR_CODE = "invalid_external_tool_surface_strategy"
NATIVE_RESPONSES_TOOL_CODEC_ERROR_CODE = "invalid_native_responses_tool_codec"
RESPONSE_ENDPOINT_SUFFIXES = ("/responses", "/response")
KNOWN_UPSTREAM_ENDPOINT_SUFFIXES = (
    "/chat/completions",
    *RESPONSE_ENDPOINT_SUFFIXES,
    "/messages",
    "/models",
)

_LOGGER = logging.getLogger(__name__)
_planning_event_sink = None


def _emit_planning_event(event: str, **fields: Any) -> None:
    sink = _planning_event_sink
    if sink is not None:
        sink(event, **fields)
        return
    _LOGGER.warning("planning diagnostic %s %s", event, dict(fields))


def _upstream_endpoint_root(base_url: str) -> str:
    parsed = urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    lowered_path = base_path.lower()
    for suffix in KNOWN_UPSTREAM_ENDPOINT_SUFFIXES:
        if lowered_path.endswith(suffix):
            base_path = base_path[: -len(suffix)].rstrip("/")
            break
    return urlunsplit((parsed.scheme, parsed.netloc, base_path, "", parsed.query))


def _upstream_base_path_matches(base_url: str, path: str) -> bool:
    lowered_path = urlsplit(base_url).path.rstrip("/").lower()
    requested_path = path.lower()
    if requested_path == "/responses":
        return any(lowered_path.endswith(suffix) for suffix in RESPONSE_ENDPOINT_SUFFIXES)
    return lowered_path.endswith(requested_path)


def _upstream_base_has_version_suffix(base_url: str) -> bool:
    path = urlsplit(base_url).path.rstrip("/")
    if not path:
        return False
    return bool(re.fullmatch(r"v\d+(?:\.\d+)?", path.rsplit("/", 1)[-1].lower()))


def _upstream_endpoint_url(upstream: Mapping[str, Any], path: str) -> str:
    base = str(upstream["base_url"]).strip()
    base_parts = urlsplit(base)
    base_without_query = urlunsplit((base_parts.scheme, base_parts.netloc, base_parts.path.rstrip("/"), "", ""))
    request_parts = urlsplit(path if path.startswith("/") else "/" + path)
    request_path = request_parts.path or "/"
    if _upstream_base_path_matches(base_without_query, request_path):
        result = base_without_query
    else:
        root = _upstream_endpoint_root(base_without_query)
        if upstream.get("auth") == "codex_auth" or _upstream_base_has_version_suffix(root):
            result = root + request_path
        else:
            result = root + "/v1" + request_path
    queries = [query for query in (base_parts.query, request_parts.query) if query]
    return result + (("?" + "&".join(queries)) if queries else "")


def _responses_url(upstream: Mapping[str, Any], request_path: str) -> str:
    parsed = urlsplit(request_path)
    path = parsed.path
    if path.startswith("/v1/"):
        path = path[3:]
    elif not path.startswith("/"):
        path = "/" + path
    url = _upstream_endpoint_url(upstream, path)
    if parsed.query:
        url += ("&" if "?" in url else "?") + parsed.query
    return url


def _chat_completions_url(upstream: Mapping[str, Any]) -> str:
    return _upstream_endpoint_url(upstream, "/chat/completions")


def _external_tool_protocol(upstream: Mapping[str, Any]) -> str:
    configured = str(upstream.get("tool_protocol") or "auto").strip().lower()
    if configured in TOOL_PROTOCOLS and configured != "auto":
        return configured
    upstream_format = str(upstream.get("upstream_format") or "").strip().lower()
    if upstream_format == "responses":
        return "responses_structured"
    if upstream_format == "chat_completions":
        return "chat_tools"
    return "text_compat"


def _external_tool_surface_strategy(upstream: Mapping[str, Any]) -> str:
    configured = upstream.get("tool_surface_strategy", "eager")
    if isinstance(configured, str) and configured in TOOL_SURFACE_STRATEGIES:
        return configured
    _emit_planning_event("external_tool_surface_rejected", reason="invalid_tool_surface_strategy")
    raise UpstreamProtocolTranslationError(
        UnsupportedProtocolTranslationError(
            TOOL_SURFACE_STRATEGY_ERROR_CODE,
            "External tool surface strategy is invalid.",
        )
    )


def _external_native_responses_tool_codec(upstream: Mapping[str, Any]) -> str:
    configured = upstream.get("native_responses_tool_codec", "none")
    if isinstance(configured, str) and configured in NATIVE_RESPONSES_TOOL_CODECS:
        return configured
    _emit_planning_event("native_responses_tool_codec_rejected", reason="invalid_codec")
    raise UpstreamProtocolTranslationError(
        UnsupportedProtocolTranslationError(
            NATIVE_RESPONSES_TOOL_CODEC_ERROR_CODE,
            "External native Responses tool codec is invalid.",
        )
    )


OFFICIAL_PASSTHROUGH_FIRST_EVENT_ATTEMPTS = 2

def _is_codex_app_context(request_context: Mapping[str, str]) -> bool:
    return request_context.get("client_id") == "codex-app"


def _has_explicit_third_party_client_identity(request_context: Mapping[str, str]) -> bool:
    client_id = str(request_context.get("client_id") or "").strip().lower()
    return bool(client_id and client_id not in {"unknown", "codex-app"})


@dataclass(frozen=True)
class ToolExposurePolicy:
    requested_mode: ToolExposureMode
    effective_mode: ToolExposureMode
    capability_state: CapabilityState
    supports_search_tool: bool | None
    proven_tool_subset: tuple[str, ...]
    gateway_schema_injection: bool
    strip_caller_tools: bool


@dataclass(frozen=True)
class VisionPlan:
    policy: str
    action: VisionAction
    network_action: VisionNetworkAction
    input_has_image: bool
    target_accepts_images: bool
    image_proxy_enabled: bool


@dataclass(frozen=True)
class RouteRuntimeFacts:
    """Runtime settings captured once before the pure route decision is built."""

    request_timeout_seconds: int
    request_kind_base_attempts: int
    request_kind_attempts_configured: bool
    failure_expansion_attempts: int
    official_open_attempts: int
    capacity_elapsed_limit_seconds: float
    stream_elapsed_limit_seconds: float
    downstream_retry_notice_enabled: bool
    pre_response_budget_seconds: float


@dataclass(frozen=True)
class RetryExecutionPlan:
    eligibility: CapabilityState
    policy: RetryPolicy
    request_kind: str
    request_timeout_seconds: int
    base_open_attempts: int
    base_relay_attempts: int
    failure_expansion_attempts: int
    request_kind_attempts_configured: bool
    retry_http_errors: bool
    open_attempt_budget: int | None
    capacity_elapsed_limit_seconds: float
    stream_elapsed_limit_seconds: float
    emit_downstream_retry_notice: bool
    pre_response_budget_seconds: float | None
    lifecycle_final_retry_eligible: bool
    empty_completed_max_attempts: int = 2

    def _attempts_for_failure_class(
        self,
        base_attempts: int,
        failure_class: str,
        *,
        stream_failure: bool,
    ) -> int:
        if (
            base_attempts <= 1
            or self.request_kind_attempts_configured
        ):
            return base_attempts
        if failure_class in CAPACITY_RETRY_FAILURE_CLASSES:
            return max(base_attempts, self.failure_expansion_attempts)
        if stream_failure and failure_class == RETRY_FAILURE_QUICK_TRANSIENT:
            return max(base_attempts, self.failure_expansion_attempts)
        return base_attempts

    def open_attempts_for_failure_class(self, failure_class: str) -> int:
        if self.open_attempt_budget is not None:
            return self.base_open_attempts
        return self._attempts_for_failure_class(
            self.base_open_attempts,
            failure_class,
            stream_failure=False,
        )

    def relay_attempts_for_failure_class(
        self,
        failure_class: str,
        *,
        stream_failure: bool,
    ) -> int:
        return self._attempts_for_failure_class(
            self.base_relay_attempts,
            failure_class,
            stream_failure=stream_failure,
        )

    def capacity_elapsed_limit_allows(
        self,
        elapsed_seconds: float,
        delay_seconds: int | float,
    ) -> bool:
        return (
            self.capacity_elapsed_limit_seconds <= 0
            or elapsed_seconds + delay_seconds
            <= self.capacity_elapsed_limit_seconds
        )

    def stream_elapsed_limit_allows(
        self,
        elapsed_seconds: float,
        delay_seconds: int | float,
    ) -> bool:
        return (
            self.stream_elapsed_limit_seconds <= 0
            or elapsed_seconds + delay_seconds
            <= self.stream_elapsed_limit_seconds
        )

    def lifecycle_final_extra_attempts(
        self,
        event_context: Mapping[str, Any] | None,
    ) -> int:
        return int(
            self.lifecycle_final_retry_eligible
            and bool((event_context or {}).get("subagent_lifecycle_complete"))
        )

    def retry_delay_seconds(
        self,
        attempt: int,
        *,
        failure_class: str,
        retry_after_seconds: int | None = None,
    ) -> int:
        if retry_after_seconds is not None:
            return retry_after_seconds
        if failure_class == RETRY_FAILURE_PROVIDER_THROTTLE:
            index = max(1, attempt) - 1
            if index < len(CAPACITY_RETRY_CADENCE_SECONDS):
                return CAPACITY_RETRY_CADENCE_SECONDS[index]
            return CAPACITY_RETRY_CADENCE_SECONDS[-1]
        return min(max(1, attempt - 1) * 2, 8)

    def pre_response_deadline(self, started_at: float) -> float | None:
        if self.pre_response_budget_seconds is None:
            return None
        return started_at + self.pre_response_budget_seconds

    def new_open_attempt_budget(self) -> dict[str, int] | None:
        if self.open_attempt_budget is None:
            return None
        return {
            "max_attempts": self.open_attempt_budget,
            "attempts_started": 0,
        }

    def telemetry_snapshot(self) -> dict[str, Any]:
        return {
            "eligibility": self.eligibility.value,
            "policy": self.policy.value,
            "request_kind": self.request_kind,
            "request_timeout_seconds": self.request_timeout_seconds,
            "base_open_attempts": self.base_open_attempts,
            "base_relay_attempts": self.base_relay_attempts,
            "failure_expansion_attempts": self.failure_expansion_attempts,
            "retry_http_errors": self.retry_http_errors,
            "open_attempt_budget": self.open_attempt_budget,
            "capacity_elapsed_limit_seconds": (
                self.capacity_elapsed_limit_seconds
            ),
            "stream_elapsed_limit_seconds": (
                self.stream_elapsed_limit_seconds
            ),
            "emit_downstream_retry_notice": (
                self.emit_downstream_retry_notice
            ),
            "pre_response_budget_seconds": (
                self.pre_response_budget_seconds
            ),
            "lifecycle_final_retry_eligible": (
                self.lifecycle_final_retry_eligible
            ),
        }


@dataclass(frozen=True)
class RelayExecutionPlan:
    selected_upstream_format: str
    request_kind: str
    streaming_policy: StreamingPolicy
    usage_policy: UsagePolicy
    response_mutation_policy: MutationPolicy
    sse_mutation_policy: MutationPolicy
    verify_cross_protocol_source: bool
    lifecycle_final_retry_enabled: bool


@dataclass(frozen=True)
class RouteAttemptPlan:
    index: int
    upstream_protocol: RouteProtocol
    selected_upstream_format: str
    endpoint_url: str
    wire_format_adapter: str
    request_conversion_steps: tuple[str, ...]
    request_body_mode: AttemptRequestBodyMode
    authentication_strategy: AuthenticationStrategy
    request_headers: FrozenRequestHeaders
    streaming_policy: StreamingPolicy
    usage_policy: UsagePolicy
    transport_policy: TransportPolicy
    request_mutation_policy: MutationPolicy
    response_mutation_policy: MutationPolicy
    sse_mutation_policy: MutationPolicy
    verify_cross_protocol_source: bool
    retry: RetryExecutionPlan
    tool_protocol: str
    tool_surface_strategy: str
    native_responses_tool_codec: str
    named_mutations: frozenset[RouteMutation]
    fallback_http_statuses: frozenset[int]

    @property
    def mutation_summary(self) -> tuple[RouteMutation, ...]:
        return tuple(sorted(self.named_mutations, key=lambda mutation: mutation.value))

    def allows_protocol_fallback_status(self, status: int | None) -> bool:
        return isinstance(status, int) and status in self.fallback_http_statuses

    def relay_execution_plan(
        self,
        *,
        lifecycle_final_retry_enabled: bool,
    ) -> RelayExecutionPlan:
        return RelayExecutionPlan(
            selected_upstream_format=self.selected_upstream_format,
            request_kind=self.retry.request_kind,
            streaming_policy=self.streaming_policy,
            usage_policy=self.usage_policy,
            response_mutation_policy=self.response_mutation_policy,
            sse_mutation_policy=self.sse_mutation_policy,
            verify_cross_protocol_source=(
                self.verify_cross_protocol_source
            ),
            lifecycle_final_retry_enabled=lifecycle_final_retry_enabled,
        )

    def telemetry_snapshot(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "upstream_protocol": self.upstream_protocol.value,
            "endpoint_url": _safe_route_endpoint_url(self.endpoint_url),
            "wire_format_adapter": self.wire_format_adapter,
            "request_conversion_steps": list(
                self.request_conversion_steps
            ),
            "request_body_mode": self.request_body_mode.value,
            "authentication_strategy": (
                self.authentication_strategy.value
            ),
            "streaming_policy": self.streaming_policy.value,
            "usage_policy": self.usage_policy.value,
            "transport_policy": self.transport_policy.value,
            "request_mutation_policy": (
                self.request_mutation_policy.value
            ),
            "response_mutation_policy": (
                self.response_mutation_policy.value
            ),
            "sse_mutation_policy": self.sse_mutation_policy.value,
            "verify_cross_protocol_source": (
                self.verify_cross_protocol_source
            ),
            "tool_protocol": self.tool_protocol,
            "tool_surface_strategy": self.tool_surface_strategy,
            "native_responses_tool_codec": (
                self.native_responses_tool_codec
            ),
            "retry": self.retry.telemetry_snapshot(),
            "fallback_http_statuses": sorted(
                self.fallback_http_statuses
            ),
            "mutation_summary": [
                mutation.value for mutation in self.mutation_summary
            ],
        }

    def _exchange_formats(self) -> tuple[str, str]:
        if (
            self.request_body_mode
            == AttemptRequestBodyMode.CONVERT_RESPONSES_TO_CHAT
        ):
            return (
                RouteProtocol.RESPONSES.value,
                RouteProtocol.CHAT_COMPLETIONS.value,
            )
        if (
            self.request_body_mode
            == AttemptRequestBodyMode.CONVERT_CHAT_TO_RESPONSES
        ):
            return (
                RouteProtocol.CHAT_COMPLETIONS.value,
                RouteProtocol.RESPONSES.value,
            )
        if self.request_body_mode == AttemptRequestBodyMode.PREPARED_DIRECT:
            protocol = self.upstream_protocol.value
            return protocol, protocol
        raise UnsupportedRouteProtocolError(
            f"unsupported planned request body mode: {self.request_body_mode}"
        )

    def prepare_body(self, request_body: bytes) -> PreparedExchange:
        inbound_format, outbound_format = self._exchange_formats()
        return prepare_exchange(
            request_body,
            inbound_format=inbound_format,
            outbound_format=outbound_format,
        )

    def request_body(self, prepared_body: bytes) -> bytes:
        return self.prepare_body(prepared_body).upstream_body


@dataclass(frozen=True)
class RouteFailureObservation:
    """Plan-wide facts used only when no executable attempt exists."""

    upstream_protocol: RouteProtocol
    wire_format_adapter: str
    authentication_strategy: AuthenticationStrategy
    request_kind: str
    retry_policy: RetryPolicy
    retry_eligibility: CapabilityState
    usage_policy: UsagePolicy
    streaming_policy: StreamingPolicy
    transport_policy: TransportPolicy
    request_mutation_policy: MutationPolicy
    response_mutation_policy: MutationPolicy
    sse_mutation_policy: MutationPolicy


@dataclass(frozen=True)
class RouteCapabilityBinding:
    source_present: bool
    schema_version: int | None
    provider_id: str | None
    model_id: str | None
    configured_upstream_protocol: RouteProtocol
    binding_upstream_protocol: RouteProtocol
    route_scope_state: CapabilityState
    route_scope_failure_reason: str | None


@dataclass(frozen=True)
class RoutePlan:
    schema_version: str
    provider_id: str
    model_requested: str | None
    canonical_model: str | None
    upstream_model: str | None
    inbound_protocol: RouteProtocol
    configured_upstream_protocol_name: str
    configured_upstream_protocol: RouteProtocol
    protocol_capability_state: CapabilityState
    protocol_failure_reason: str | None
    attempts: tuple[RouteAttemptPlan, ...]
    failure_observation: RouteFailureObservation | None
    capability_manifest_version: str | None
    capability_manifest_hash: str | None
    capability_manifest_state: CapabilityState
    capability_binding: RouteCapabilityBinding
    behavior_profile: str
    caller_request_body_mode: CallerRequestBodyMode
    prepared_request_protocol: RouteProtocol
    codex_semantic_adapter: str
    raw_provider_probe: bool
    request_kind_policy: str
    repair_policy: str
    vision: VisionPlan
    tool_exposure: ToolExposurePolicy
    codex_compatibility_policy: CodexCompatibilityPolicy
    collaboration_backend: CollaborationBackend
    execution_owner: ExecutionOwner
    named_mutations: frozenset[RouteMutation]
    official_http_passthrough: bool
    transparent_metered: bool
    transparent_same_format: bool
    transparent_lightweight_fallback: bool
    transparent_tool_loop_guard: bool

    def __post_init__(self) -> None:
        if bool(self.attempts) == (self.failure_observation is not None):
            raise ValueError(
                "route plan must contain either attempts or one failure observation"
            )

    def _required_failure_observation(self) -> RouteFailureObservation:
        if self.failure_observation is None:
            raise RuntimeError("route plan has neither an attempt nor failure facts")
        return self.failure_observation

    @property
    def mutation_summary(self) -> tuple[RouteMutation, ...]:
        return tuple(sorted(self.named_mutations, key=lambda mutation: mutation.value))

    @property
    def vision_proxy_policy(self) -> str:
        return self.vision.policy

    @property
    def primary_attempt(self) -> RouteAttemptPlan | None:
        return self.attempts[0] if self.attempts else None

    @property
    def request_kind(self) -> str:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.retry.request_kind
            if primary_attempt is not None
            else self._required_failure_observation().request_kind
        )

    @property
    def authentication_strategy(self) -> AuthenticationStrategy:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.authentication_strategy
            if primary_attempt is not None
            else self._required_failure_observation().authentication_strategy
        )

    @property
    def upstream_protocol(self) -> RouteProtocol:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.upstream_protocol
            if primary_attempt is not None
            else self._required_failure_observation().upstream_protocol
        )

    @property
    def selected_upstream_format(self) -> str:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.selected_upstream_format
            if primary_attempt is not None
            else self.configured_upstream_protocol_name
        )

    @property
    def wire_format_adapter(self) -> str:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.wire_format_adapter
            if primary_attempt is not None
            else self._required_failure_observation().wire_format_adapter
        )

    @property
    def retry_policy(self) -> RetryPolicy:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.retry.policy
            if primary_attempt is not None
            else self._required_failure_observation().retry_policy
        )

    @property
    def retry_eligibility(self) -> CapabilityState:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.retry.eligibility
            if primary_attempt is not None
            else self._required_failure_observation().retry_eligibility
        )

    @property
    def usage_policy(self) -> UsagePolicy:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.usage_policy
            if primary_attempt is not None
            else self._required_failure_observation().usage_policy
        )

    @property
    def streaming_policy(self) -> StreamingPolicy:
        primary_attempt = self.primary_attempt
        if primary_attempt is not None:
            return primary_attempt.streaming_policy
        return self._required_failure_observation().streaming_policy

    @property
    def transport_policy(self) -> TransportPolicy:
        primary_attempt = self.primary_attempt
        if primary_attempt is not None:
            return primary_attempt.transport_policy
        return self._required_failure_observation().transport_policy

    @property
    def request_mutation_policy(self) -> MutationPolicy:
        primary_attempt = self.primary_attempt
        if primary_attempt is not None:
            return primary_attempt.request_mutation_policy
        return self._required_failure_observation().request_mutation_policy

    @property
    def response_mutation_policy(self) -> MutationPolicy:
        primary_attempt = self.primary_attempt
        if primary_attempt is not None:
            return primary_attempt.response_mutation_policy
        return self._required_failure_observation().response_mutation_policy

    @property
    def sse_mutation_policy(self) -> MutationPolicy:
        primary_attempt = self.primary_attempt
        if primary_attempt is not None:
            return primary_attempt.sse_mutation_policy
        return self._required_failure_observation().sse_mutation_policy


def behavior_profile_for_request(
    upstream: Mapping[str, Any],
    request_context: Mapping[str, str],
    *,
    inbound_format: str,
    official_http_passthrough_enabled: bool | None = None,
) -> str:
    if str(upstream.get("name")) != "official":
        return BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY
    if official_http_passthrough_enabled is None:
        official_http_passthrough_enabled = gateway_official_http_passthrough_enabled()
    if (
        official_http_passthrough_enabled
        and inbound_format == "responses"
        and _is_codex_app_context(request_context)
    ):
        return BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
    return BEHAVIOR_OFFICIAL_GATEWAY_COMPAT


def _wire_format_adapter(inbound_format: str, upstream_format: str) -> str:
    if inbound_format == upstream_format:
        return WIRE_TRANSPARENT
    if inbound_format == "responses" and upstream_format == "chat_completions":
        return WIRE_RESPONSES_TO_CHAT
    if inbound_format == "chat_completions" and upstream_format == "responses":
        return WIRE_CHAT_TO_RESPONSES
    return WIRE_TRANSPARENT


def _route_protocol(value: Any) -> RouteProtocol:
    try:
        return RouteProtocol(str(value))
    except ValueError:
        return RouteProtocol.UNKNOWN


def _route_provider_id(upstream: Mapping[str, Any], requested_provider: str | None = None) -> str:
    """Return the stable exported provider ID, never the transport name."""

    configured = upstream.get("provider_id") or upstream.get("provider_alias")
    if isinstance(configured, str) and configured.strip():
        return canonical_model_id(configured.strip())
    if requested_provider:
        return canonical_model_id(requested_provider)
    return {
        "official": "openai",
        "ollama_cloud": "ollama-cloud",
        "ollama-cloud": "ollama-cloud",
        "volcengine": "volc",
        "minimax_cn": "minimax-cn",
    }.get(str(upstream.get("name") or ""), canonical_model_id(str(upstream.get("name") or "")))


def _validate_route_identity(
    upstream: Mapping[str, Any],
    *,
    requested_provider: str,
    requested_model: str,
    requested_model_id: str,
) -> tuple[str, str]:
    """Validate that route metadata binds exactly to the requested pair."""

    provider_id = _route_provider_id(upstream, requested_provider or None)
    if requested_provider and provider_id != _route_provider_id({"provider_id": requested_provider}, requested_provider):
        raise _identity_failure(
            "resolved upstream provider does not match the requested provider",
            reason="provider_mismatch",
            provider_id=requested_provider,
            model_slug=requested_model_id,
        )
    upstream_model_value = upstream.get("upstream_model")
    if not isinstance(upstream_model_value, str) or not upstream_model_value.strip():
        raise _identity_failure(
            "resolved upstream is missing an exact upstream_model binding",
            reason="missing_upstream_model",
            provider_id=provider_id or requested_provider or None,
            model_slug=requested_model_id,
        )
    upstream_model = canonical_model_id(upstream_model_value.strip())
    if not upstream_model:
        raise _identity_failure(
            "resolved upstream has an empty upstream_model binding",
            reason="missing_upstream_model",
            provider_id=provider_id or requested_provider or None,
            model_slug=requested_model_id,
        )
    if upstream.get("model_id") is not None:
        configured_model_id = canonical_model_id(str(upstream.get("model_id")))
        if configured_model_id != requested_model_id:
            raise _catalog_failure(
                "resolved upstream model_id contradicts the requested model slug",
                reason="configured_model_mismatch",
                provider_id=provider_id,
                model_slug=requested_model_id,
            )
    elif upstream_model != canonical_model_id(requested_model if requested_provider else requested_model_id):
        raise _catalog_failure(
            "resolved upstream upstream_model contradicts the requested model slug",
            reason="configured_model_mismatch",
            provider_id=provider_id,
            model_slug=requested_model_id,
        )
    return provider_id, upstream_model

def _route_endpoint_url(
    upstream: Mapping[str, Any],
    protocol: RouteProtocol,
) -> str:
    base_url = upstream.get("base_url")
    if protocol == RouteProtocol.RESPONSES:
        return (
            _responses_url(upstream, "/v1/responses")
            if isinstance(base_url, str) and base_url
            else "/responses"
        )
    if protocol == RouteProtocol.CHAT_COMPLETIONS:
        return (
            _chat_completions_url(upstream)
            if isinstance(base_url, str) and base_url
            else "/chat/completions"
        )
    raise UnsupportedRouteProtocolError(
        f"planned attempt has no executable upstream protocol: {protocol.value}"
    )


def _safe_route_endpoint_url(endpoint_url: str) -> str:
    """Return endpoint identity without credentials or query secrets.

    Route diagnostics need to distinguish configured endpoints, but failure
    telemetry must never copy userinfo, query parameters, or fragments from a
    provider URL.  The route planner only creates HTTP(S) endpoint URLs (or a
    relative fixture path), so preserve the scheme/host/path and bound the
    result for event payloads.
    """

    value = str(endpoint_url or "")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    def safe_path(path: str) -> str:
        # A provider may legitimately put a credential or signed token in its
        # base URL path.  Keep the common fixed API paths readable, but never
        # persist an arbitrary path segment verbatim in telemetry.
        safe_paths = {
            "/v1",
            "/v2",
            "/responses",
            "/response",
            "/chat/completions",
            "/v1/responses",
            "/v1/response",
            "/v1/chat/completions",
        }
        if path in safe_paths:
            return path
        if not path:
            return ""
        segments = path.split("/")
        return "/".join(
            "" if index == 0 and segment == "" else (
                "" if segment == "" else f"sha256:{hashlib.sha256(segment.encode('utf-8')).hexdigest()[:16]}"
            )
            for index, segment in enumerate(segments)
        )

    # ``urlsplit`` accepts schemeless network-path references such as
    # ``//user:secret@example.test/v1``.  They still have a usable hostname,
    # so sanitize them through the same host/port path below.  For malformed
    # values such as ``user:secret@example.test/v1`` there is no trustworthy
    # authority boundary; never echo the raw value because it can contain
    # credentials.
    if not parsed.netloc:
        path = parsed.path if value.startswith("/") and not value.startswith("//") else ""
        return safe_path(path.split("?", 1)[0].split("#", 1)[0])[:300]
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
    except ValueError:
        pass
    return urlunsplit((parsed.scheme, netloc, safe_path(parsed.path), "", ""))[:300]


def _capability_state(value: Any, *, default: CapabilityState) -> CapabilityState:
    try:
        return CapabilityState(str(value))
    except ValueError:
        return default


CAPABILITY_MANIFEST_VERSION_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?"
)
SUPPORTED_CAPABILITY_MANIFEST_VERSIONS = frozenset(
    {"provider-capabilities.v3"}
)
CAPABILITY_MANIFEST_HASH_LENGTHS = {
    "sha256": 64,
    "sha384": 96,
    "sha512": 128,
}


def _valid_capability_manifest_version(value: str) -> bool:
    return (
        CAPABILITY_MANIFEST_VERSION_RE.fullmatch(value) is not None
        and value in SUPPORTED_CAPABILITY_MANIFEST_VERSIONS
    )


def _valid_capability_manifest_hash(value: str) -> bool:
    algorithm, separator, digest = value.partition(":")
    expected_length = CAPABILITY_MANIFEST_HASH_LENGTHS.get(algorithm.lower())
    return bool(
        separator
        and expected_length is not None
        and len(digest) == expected_length
        and re.fullmatch(r"[0-9a-fA-F]+", digest)
    )


def _capability_manifest_identity(
    upstream: Mapping[str, Any],
) -> tuple[str | None, str | None, CapabilityState]:
    version_value = upstream.get("capability_manifest_version")
    version = (
        version_value.strip()
        if isinstance(version_value, str) and version_value.strip()
        else None
    )
    hash_value = upstream.get("capability_manifest_hash")
    manifest_hash = (
        hash_value.strip()
        if isinstance(hash_value, str) and hash_value.strip()
        else None
    )
    if not (
        version is not None
        and manifest_hash is not None
        and _valid_capability_manifest_version(version)
        and _valid_capability_manifest_hash(manifest_hash)
    ):
        return None, None, CapabilityState.UNQUALIFIED
    return (
        version,
        manifest_hash,
        _capability_state(
            upstream.get("capability_manifest_state"),
            default=CapabilityState.UNQUALIFIED,
        ),
    )


CAPABILITY_BINDING_SCHEMA_VERSION = 1


def _binding_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _canonical_binding_provider(value: str | None) -> str | None:
    if value is None:
        return None
    return canonical_model_id(value).replace("_", "-").lower()


def _canonical_binding_model(value: str | None) -> str | None:
    if value is None:
        return None
    return canonical_model_id(value).lower()


def _route_capability_binding(
    upstream: Mapping[str, Any],
    *,
    configured_upstream_protocol: RouteProtocol,
    provider_id: str,
    model_id: str | None,
) -> RouteCapabilityBinding:
    raw_binding = upstream.get("capability_binding")
    if not isinstance(raw_binding, Mapping):
        return RouteCapabilityBinding(
            source_present=raw_binding is not None,
            schema_version=None,
            provider_id=None,
            model_id=None,
            configured_upstream_protocol=configured_upstream_protocol,
            binding_upstream_protocol=RouteProtocol.UNKNOWN,
            route_scope_state=CapabilityState.UNQUALIFIED,
            route_scope_failure_reason=(
                "malformed_capability_binding"
                if raw_binding is not None
                else "missing_capability_binding"
            ),
        )

    schema_value = raw_binding.get("schema_version")
    schema_version = (
        schema_value
        if isinstance(schema_value, int) and not isinstance(schema_value, bool)
        else None
    )
    binding_provider = _binding_text(raw_binding.get("provider"))
    binding_model = _binding_text(raw_binding.get("model"))
    binding_protocol = _route_protocol(
        raw_binding.get("upstream_protocol")
    )

    route_scope_failure_reason: str | None = None
    if schema_version != CAPABILITY_BINDING_SCHEMA_VERSION:
        route_scope_failure_reason = "unsupported_binding_schema_version"
    elif (
        _canonical_binding_provider(binding_provider)
        != _canonical_binding_provider(provider_id)
    ):
        route_scope_failure_reason = "binding_provider_mismatch"
    elif (
        model_id is not None
        and _canonical_binding_model(binding_model)
        != _canonical_binding_model(model_id)
    ):
        route_scope_failure_reason = "binding_model_mismatch"
    elif binding_protocol != configured_upstream_protocol:
        route_scope_failure_reason = (
            "binding_upstream_protocol_mismatch"
        )

    route_scope_state = (
        CapabilityState.SUPPORTED
        if route_scope_failure_reason is None
        else CapabilityState.UNQUALIFIED
    )
    return RouteCapabilityBinding(
        source_present=True,
        schema_version=schema_version,
        provider_id=binding_provider,
        model_id=binding_model,
        configured_upstream_protocol=configured_upstream_protocol,
        binding_upstream_protocol=binding_protocol,
        route_scope_state=route_scope_state,
        route_scope_failure_reason=route_scope_failure_reason,
    )


def _default_route_runtime_facts(request_kind: str) -> RouteRuntimeFacts:
    return RouteRuntimeFacts(
        request_timeout_seconds=DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
        request_kind_base_attempts=_default_retry_attempts_for_request_kind(
            request_kind
        ),
        request_kind_attempts_configured=False,
        failure_expansion_attempts=DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS,
        official_open_attempts=DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS,
        capacity_elapsed_limit_seconds=(
            DEFAULT_CAPACITY_RETRY_ELAPSED_LIMIT_SECONDS
        ),
        stream_elapsed_limit_seconds=DEFAULT_STREAM_RETRY_ELAPSED_LIMIT_SECONDS,
        downstream_retry_notice_enabled=False,
        pre_response_budget_seconds=(
            DEFAULT_MAIN_GENERATION_PRE_RESPONSE_BUDGET_SECONDS
        ),
    )


def _route_runtime_facts(request_kind: str) -> RouteRuntimeFacts:
    return RouteRuntimeFacts(
        request_timeout_seconds=upstream_timeout_seconds(),
        request_kind_base_attempts=_upstream_retry_attempts(request_kind),
        request_kind_attempts_configured=(
            _request_kind_retry_attempts_configured(request_kind)
        ),
        failure_expansion_attempts=gateway_auto_retry_max_attempts(),
        official_open_attempts=official_upstream_open_attempts(),
        capacity_elapsed_limit_seconds=(
            gateway_capacity_retry_elapsed_limit_seconds()
        ),
        stream_elapsed_limit_seconds=(
            gateway_stream_retry_elapsed_limit_seconds()
        ),
        downstream_retry_notice_enabled=(
            gateway_downstream_retry_notice_enabled()
        ),
        pre_response_budget_seconds=(
            DEFAULT_MAIN_GENERATION_PRE_RESPONSE_BUDGET_SECONDS
        ),
    )


def _vision_plan_for_route(
    *,
    codex_app_external: bool,
    input_has_image: bool,
    target_accepts_images: bool,
    image_proxy_enabled: bool,
) -> VisionPlan:
    if not input_has_image or target_accepts_images:
        return VisionPlan(
            policy=VISION_PROXY_DISABLED,
            action=VisionAction.PASS_THROUGH,
            network_action=VisionNetworkAction.NONE,
            input_has_image=input_has_image,
            target_accepts_images=target_accepts_images,
            image_proxy_enabled=image_proxy_enabled,
        )
    if image_proxy_enabled:
        return VisionPlan(
            policy=(
                VISION_PROXY_CODEX_APP_ADAPTER
                if codex_app_external
                else VISION_PROXY_TRANSPARENT_OVERLAY
            ),
            action=VisionAction.PROXY,
            network_action=VisionNetworkAction.IMAGE_PROXY,
            input_has_image=True,
            target_accepts_images=False,
            image_proxy_enabled=True,
        )
    return VisionPlan(
        policy=VISION_PROXY_DISABLED,
        action=VisionAction.REJECT,
        network_action=VisionNetworkAction.NONE,
        input_has_image=True,
        target_accepts_images=False,
        image_proxy_enabled=False,
    )


def _route_supports_transparent_metering(
    *,
    upstream_name: str,
    configured_upstream_format: str,
    selected_upstream_format: str,
    inbound_format: str,
    wire_format_adapter: str,
    request_context: Mapping[str, str],
    provider_hint: str | None,
    collaboration_protocol: str | None = None,
    raw_provider_probe: bool = False,
) -> bool:
    # Collaboration V2 is a Gateway-owned compatibility surface for
    # provider-scoped third-party routes.  It must not be sent through the
    # transparent client-runtime path, otherwise the provider sees the native
    # namespace and no runtime tool adapter can translate calls back.
    if (
        not raw_provider_probe
        and collaboration_protocol == COLLABORATION_V2
        and upstream_name != "official"
        and provider_hint is not None
    ):
        return False
    if _is_codex_app_context(request_context):
        return False
    explicit_client = _has_explicit_third_party_client_identity(request_context)
    provider_scoped = provider_hint is not None
    standard_external = provider_hint is None and upstream_name != "official" and explicit_client
    official_responses = provider_hint is None and upstream_name == "official" and explicit_client
    if not (provider_scoped or standard_external or official_responses):
        return False

    if official_responses:
        return selected_upstream_format == "responses" and (
            (inbound_format == "responses" and wire_format_adapter == WIRE_TRANSPARENT)
            or (inbound_format == "chat_completions" and wire_format_adapter == WIRE_CHAT_TO_RESPONSES)
        )

    return (
        (
            inbound_format == "chat_completions"
            and selected_upstream_format == "chat_completions"
            and wire_format_adapter == WIRE_TRANSPARENT
        )
        or (
            inbound_format == "chat_completions"
            and configured_upstream_format == "responses"
            and selected_upstream_format == "responses"
            and wire_format_adapter == WIRE_CHAT_TO_RESPONSES
        )
        or (
            inbound_format == "responses"
            and selected_upstream_format == "responses"
            and wire_format_adapter == WIRE_TRANSPARENT
        )
        or (
            inbound_format == "responses"
            and configured_upstream_format == "chat_completions"
            and selected_upstream_format == "chat_completions"
            and wire_format_adapter == WIRE_RESPONSES_TO_CHAT
        )
    )


def _tool_exposure_policy_for_route(
    upstream: Mapping[str, Any],
    *,
    upstream_name: str,
    behavior_profile: str,
    request_kind: str,
    raw_provider_probe: bool,
) -> ToolExposurePolicy:
    tool_protocol = _external_tool_protocol(upstream)
    raw_requested_mode = upstream.get("tool_exposure_mode")
    if raw_requested_mode is None:
        if behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH:
            requested_mode = ToolExposureMode.OFFICIAL_NATIVE
        elif behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED:
            requested_mode = ToolExposureMode.UNKNOWN
        else:
            requested_mode = ToolExposureMode.CURRENT_COMPATIBILITY
    else:
        try:
            requested_mode = ToolExposureMode(str(raw_requested_mode))
        except ValueError:
            requested_mode = ToolExposureMode.UNKNOWN

    raw_state = upstream.get("tool_capability_state")
    if raw_state is None:
        capability_state = (
            CapabilityState.SUPPORTED
            if requested_mode
            in {
                ToolExposureMode.CURRENT_COMPATIBILITY,
                ToolExposureMode.OFFICIAL_NATIVE,
            }
            else CapabilityState.UNQUALIFIED
        )
    else:
        try:
            capability_state = CapabilityState(str(raw_state))
        except ValueError:
            capability_state = CapabilityState.UNQUALIFIED
    if requested_mode in {
        ToolExposureMode.NATIVE_DEFERRED_SEARCH_CANDIDATE,
        ToolExposureMode.NATIVE_NO_SEARCH_CANDIDATE,
        ToolExposureMode.UNKNOWN,
    }:
        capability_state = CapabilityState.UNQUALIFIED
    elif requested_mode == ToolExposureMode.UNSUPPORTED:
        capability_state = CapabilityState.UNSUPPORTED
    if tool_protocol == "none":
        capability_state = CapabilityState.UNSUPPORTED

    raw_subset = upstream.get("proven_tool_subset")
    proven_tool_subset = (
        tuple(item for item in raw_subset if isinstance(item, str) and item)
        if isinstance(raw_subset, (list, tuple))
        else ()
    )
    supports_search_tool = upstream.get("supports_search_tool")
    if not isinstance(supports_search_tool, bool):
        supports_search_tool = None

    if behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH:
        effective_mode = ToolExposureMode.OFFICIAL_NATIVE
    elif behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED:
        effective_mode = ToolExposureMode.UNKNOWN
    else:
        # #57/#58 have not authorized either native candidate. Keeping their
        # evidence visible while executing the compatibility mode is the
        # behavior-preserving fail-closed boundary.
        effective_mode = ToolExposureMode.CURRENT_COMPATIBILITY
    if tool_protocol == "none":
        effective_mode = ToolExposureMode.UNSUPPORTED
    gateway_schema_injection = (
        upstream_name != "official"
        and effective_mode == ToolExposureMode.CURRENT_COMPATIBILITY
        and request_kind != RETRY_REQUEST_COMPACT
        and not raw_provider_probe
    )
    strip_caller_tools = (
        request_kind == RETRY_REQUEST_COMPACT
        and behavior_profile
        not in {
            BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
            BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
        }
    )
    return ToolExposurePolicy(
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        capability_state=capability_state,
        supports_search_tool=supports_search_tool,
        proven_tool_subset=proven_tool_subset,
        gateway_schema_injection=gateway_schema_injection,
        strip_caller_tools=strip_caller_tools,
    )


def route_plan_for_request(
    upstream: Mapping[str, Any],
    request_context: Mapping[str, str],
    *,
    inbound_format: str,
    provider_hint: str | None = None,
    collaboration_protocol: str | None = None,
    model_requested: str | None = None,
    canonical_route_model: str | None = None,
    request_kind: str = RETRY_REQUEST_MAIN_GENERATION,
    raw_provider_probe: bool = False,
    input_has_image: bool = False,
    target_accepts_images: bool = True,
    image_proxy_enabled: bool = False,
    official_http_passthrough_enabled: bool = True,
    caller_stream: bool = True,
    runtime_facts: (
        RouteRuntimeFacts
        | Mapping[str, RouteRuntimeFacts]
        | None
    ) = None,
) -> RoutePlan:
    upstream_name = str(upstream.get("name") or "")
    configured_upstream_format = str(upstream.get("upstream_format") or "responses")
    configured_upstream_protocol = _route_protocol(configured_upstream_format)
    if configured_upstream_protocol == RouteProtocol.AUTO:
        attempt_protocols = (
            RouteProtocol.RESPONSES,
            RouteProtocol.CHAT_COMPLETIONS,
        )
    elif configured_upstream_protocol in {
        RouteProtocol.RESPONSES,
        RouteProtocol.CHAT_COMPLETIONS,
    }:
        attempt_protocols = (configured_upstream_protocol,)
    else:
        attempt_protocols = ()
    requested_model_id = (
        canonical_model_id(canonical_route_model or model_requested)
        if canonical_route_model or model_requested
        else ""
    )
    requested_provider, separator, requested_model = (
        requested_model_id.partition("/")
    )
    resolved_provider_id = _route_provider_id(upstream, requested_provider if separator else None)
    resolved_upstream_model: str | None = None
    if requested_model_id:
        resolved_provider_id, resolved_upstream_model = _validate_route_identity(
            upstream,
            requested_provider=requested_provider if separator else resolved_provider_id,
            requested_model=requested_model if separator else requested_model_id,
            requested_model_id=requested_model_id,
        )
    binding_provider_id = str(
        resolved_provider_id
    )
    binding_model_id = str(
        (requested_model if separator else requested_model_id)
        or resolved_upstream_model
        or upstream.get("upstream_model")
        or ""
    )
    capability_binding = _route_capability_binding(
        upstream,
        configured_upstream_protocol=configured_upstream_protocol,
        provider_id=binding_provider_id,
        model_id=binding_model_id or None,
    )
    binding_scope_failed = (
        capability_binding.source_present
        and capability_binding.route_scope_state
        != CapabilityState.SUPPORTED
    )
    if binding_scope_failed:
        attempt_protocols = ()
    if binding_scope_failed:
        protocol_capability_state = CapabilityState.UNQUALIFIED
    elif attempt_protocols:
        protocol_capability_state = CapabilityState.SUPPORTED
    elif configured_upstream_protocol == RouteProtocol.ANTHROPIC_MESSAGES:
        protocol_capability_state = CapabilityState.UNSUPPORTED
    else:
        protocol_capability_state = CapabilityState.UNQUALIFIED
    selected_upstream_format = (
        attempt_protocols[0].value
        if attempt_protocols
        else configured_upstream_format
    )
    wire_adapter = _wire_format_adapter(inbound_format, selected_upstream_format)
    codex_app_external = upstream_name != "official" and _is_codex_app_context(request_context)
    compatibility_external = (
        upstream_name != "official"
        and provider_hint is None
        and not _is_codex_app_context(request_context)
        and not _has_explicit_third_party_client_identity(request_context)
    )
    transparent_metered = _route_supports_transparent_metering(
        upstream_name=upstream_name,
        configured_upstream_format=configured_upstream_format,
        selected_upstream_format=selected_upstream_format,
        inbound_format=inbound_format,
        wire_format_adapter=wire_adapter,
        request_context=request_context,
        provider_hint=provider_hint,
        collaboration_protocol=collaboration_protocol,
        raw_provider_probe=raw_provider_probe,
    )
    if codex_app_external:
        behavior_profile = BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER
    elif compatibility_external:
        behavior_profile = BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY
    elif transparent_metered:
        behavior_profile = BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
    else:
        behavior_profile = behavior_profile_for_request(
            upstream,
            request_context,
            inbound_format=inbound_format,
            official_http_passthrough_enabled=official_http_passthrough_enabled,
        )

    official_http_passthrough = (
        behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
    )
    transparent_same_format = transparent_metered and wire_adapter == WIRE_TRANSPARENT
    transparent_lightweight_fallback = transparent_metered and wire_adapter in {
        WIRE_CHAT_TO_RESPONSES,
        WIRE_RESPONSES_TO_CHAT,
    }
    codex_semantic_adapter = (
        CODEX_SEMANTIC_EXTERNAL_ADAPTER
        if codex_app_external or compatibility_external
        else CODEX_SEMANTIC_NONE
    )
    effective_request_kind = (
        RETRY_REQUEST_MAIN_GENERATION if transparent_metered else request_kind
    )
    if isinstance(runtime_facts, RouteRuntimeFacts):
        selected_runtime_facts = runtime_facts
    elif isinstance(runtime_facts, Mapping):
        selected_runtime_facts = runtime_facts.get(
            effective_request_kind,
            _default_route_runtime_facts(effective_request_kind),
        )
    else:
        selected_runtime_facts = _default_route_runtime_facts(
            effective_request_kind
        )
    request_kind_policy = (
        REQUEST_KIND_TRANSPARENT if transparent_metered else REQUEST_KIND_GATEWAY
    )
    retry_policy = (
        RetryPolicy.CONSERVATIVE_PRE_OUTPUT
        if transparent_metered
        else RetryPolicy.GATEWAY_FULL
    )
    usage_policy = (
        UsagePolicy.ASYNC_TAP
        if transparent_metered
        else UsagePolicy.SYNC_CAPTURE
    )
    repair_policy = (
        REPAIR_CODEX_SUBAGENT
        if codex_app_external and not raw_provider_probe
        else REPAIR_NONE
    )
    vision = _vision_plan_for_route(
        codex_app_external=codex_app_external,
        input_has_image=input_has_image,
        target_accepts_images=target_accepts_images,
        image_proxy_enabled=image_proxy_enabled,
    )

    tool_exposure = _tool_exposure_policy_for_route(
        upstream,
        upstream_name=upstream_name,
        behavior_profile=behavior_profile,
        request_kind=request_kind,
        raw_provider_probe=raw_provider_probe,
    )
    if official_http_passthrough:
        codex_compatibility_policy = CodexCompatibilityPolicy.OFFICIAL_NATIVE
        collaboration_backend = CollaborationBackend.CODEX_RUNTIME
        streaming_policy = StreamingPolicy.OFFICIAL_PASSTHROUGH
        mutation_policy = MutationPolicy.OFFICIAL_PASSTHROUGH
    elif transparent_metered:
        codex_compatibility_policy = CodexCompatibilityPolicy.NONE
        collaboration_backend = CollaborationBackend.CLIENT_RUNTIME
        streaming_policy = (
            StreamingPolicy.TRANSPARENT
            if transparent_same_format
            else StreamingPolicy.TRANSPARENT_CONVERTED
        )
        mutation_policy = MutationPolicy.TRANSPARENT
    else:
        codex_compatibility_policy = CodexCompatibilityPolicy.CURRENT_COMPATIBILITY
        collaboration_backend = CollaborationBackend.GATEWAY_COMPATIBILITY
        streaming_policy = StreamingPolicy.GATEWAY_ADAPTED
        mutation_policy = MutationPolicy.GATEWAY_COMPATIBILITY

    authentication_strategy = _authentication_strategy(
        upstream.get("auth") or "unknown"
    )
    request_headers = FrozenRequestHeaders.unmaterialized()
    transport_policy = (
        TransportPolicy.OFFICIAL_KEEPALIVE
        if upstream_name == "official"
        else TransportPolicy.STANDARD
    )
    caller_request_body_mode = CallerRequestBodyMode.PRESERVE_CALLER
    base_named_mutations = {RouteMutation.MODEL_ALIAS}
    if tool_exposure.gateway_schema_injection:
        base_named_mutations.add(RouteMutation.HARD_CODED_SCHEMA_INJECTION)
    if codex_semantic_adapter == CODEX_SEMANTIC_EXTERNAL_ADAPTER:
        base_named_mutations.add(RouteMutation.NAMESPACE_FLATTENING)
    if repair_policy != REPAIR_NONE:
        base_named_mutations.add(RouteMutation.SEMANTIC_REPAIR)
    if official_http_passthrough:
        base_named_mutations.add(RouteMutation.OFFICIAL_TOOL_SEARCH_PRESERVATION)
    elif not transparent_metered:
        base_named_mutations.add(RouteMutation.SYNTHETIC_TERMINAL_FAILURE)
    if vision.action == VisionAction.PROXY:
        base_named_mutations.add(RouteMutation.IMAGE_CONTENT_REPLACEMENT)
    elif vision.action == VisionAction.REJECT:
        base_named_mutations.add(RouteMutation.IMAGE_UNSUPPORTED_REJECTION)
    if tool_exposure.strip_caller_tools:
        base_named_mutations.add(RouteMutation.CALLER_TOOL_STRIPPING)
    if not attempt_protocols:
        base_named_mutations.add(RouteMutation.UNSUPPORTED_PROTOCOL_REJECTION)

    attempts: list[RouteAttemptPlan] = []
    for index, attempt_protocol in enumerate(attempt_protocols):
        attempt_wire_adapter = _wire_format_adapter(
            inbound_format,
            attempt_protocol.value,
        )
        if attempt_protocol.value == inbound_format:
            request_body_mode = AttemptRequestBodyMode.PREPARED_DIRECT
        elif (
            inbound_format == RouteProtocol.CHAT_COMPLETIONS.value
            and attempt_protocol == RouteProtocol.RESPONSES
        ):
            request_body_mode = AttemptRequestBodyMode.CONVERT_CHAT_TO_RESPONSES
        elif (
            inbound_format == RouteProtocol.RESPONSES.value
            and attempt_protocol == RouteProtocol.CHAT_COMPLETIONS
        ):
            request_body_mode = AttemptRequestBodyMode.CONVERT_RESPONSES_TO_CHAT
        else:
            request_body_mode = AttemptRequestBodyMode.PREPARED_DIRECT
        if (
            request_body_mode
            == AttemptRequestBodyMode.CONVERT_CHAT_TO_RESPONSES
        ):
            request_conversion_steps: tuple[str, ...] = (WIRE_CHAT_TO_RESPONSES,)
        elif (
            request_body_mode
            == AttemptRequestBodyMode.CONVERT_RESPONSES_TO_CHAT
        ):
            request_conversion_steps = (WIRE_RESPONSES_TO_CHAT,)
        else:
            request_conversion_steps = ()
        attempt_mutations = set(base_named_mutations)
        if request_conversion_steps:
            attempt_mutations.add(RouteMutation.WIRE_CONVERSION)
        if official_http_passthrough:
            base_open_attempts = selected_runtime_facts.official_open_attempts
            base_relay_attempts = OFFICIAL_PASSTHROUGH_FIRST_EVENT_ATTEMPTS
            retry_http_errors = False
            open_attempt_budget = selected_runtime_facts.official_open_attempts
        else:
            base_open_attempts = (
                selected_runtime_facts.request_kind_base_attempts
            )
            base_relay_attempts = (
                selected_runtime_facts.request_kind_base_attempts
            )
            retry_http_errors = True
            open_attempt_budget = None
        retry_execution = RetryExecutionPlan(
            eligibility=protocol_capability_state,
            policy=retry_policy,
            request_kind=effective_request_kind,
            request_timeout_seconds=(
                selected_runtime_facts.request_timeout_seconds
            ),
            base_open_attempts=base_open_attempts,
            base_relay_attempts=base_relay_attempts,
            failure_expansion_attempts=(
                selected_runtime_facts.failure_expansion_attempts
            ),
            request_kind_attempts_configured=(
                selected_runtime_facts.request_kind_attempts_configured
            ),
            retry_http_errors=retry_http_errors,
            open_attempt_budget=open_attempt_budget,
            capacity_elapsed_limit_seconds=(
                selected_runtime_facts.capacity_elapsed_limit_seconds
            ),
            stream_elapsed_limit_seconds=(
                selected_runtime_facts.stream_elapsed_limit_seconds
            ),
            emit_downstream_retry_notice=(
                not official_http_passthrough
                and not transparent_metered
                and caller_stream
                and inbound_format == RouteProtocol.RESPONSES.value
                and selected_runtime_facts.downstream_retry_notice_enabled
            ),
            pre_response_budget_seconds=(
                selected_runtime_facts.pre_response_budget_seconds
                if effective_request_kind
                == RETRY_REQUEST_MAIN_GENERATION
                else None
            ),
            lifecycle_final_retry_eligible=(
                not official_http_passthrough
                and repair_policy != REPAIR_NONE
                and effective_request_kind
                == RETRY_REQUEST_MAIN_GENERATION
            ),
        )
        attempt_upstream = {
            **upstream,
            "upstream_format": attempt_protocol.value,
        }
        attempts.append(
            RouteAttemptPlan(
                index=index,
                upstream_protocol=attempt_protocol,
                selected_upstream_format=attempt_protocol.value,
                endpoint_url=_route_endpoint_url(upstream, attempt_protocol),
                wire_format_adapter=attempt_wire_adapter,
                request_conversion_steps=request_conversion_steps,
                request_body_mode=request_body_mode,
                authentication_strategy=authentication_strategy,
                request_headers=request_headers,
                streaming_policy=streaming_policy,
                usage_policy=usage_policy,
                transport_policy=transport_policy,
                request_mutation_policy=mutation_policy,
                response_mutation_policy=mutation_policy,
                sse_mutation_policy=mutation_policy,
                verify_cross_protocol_source=(
                    attempt_protocol.value != inbound_format
                    and behavior_profile
                    in {
                        BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
                        BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
                        BEHAVIOR_OFFICIAL_GATEWAY_COMPAT,
                    }
                ),
                retry=retry_execution,
                tool_protocol=_external_tool_protocol(attempt_upstream),
                tool_surface_strategy=(
                    _external_tool_surface_strategy(attempt_upstream)
                    if upstream_name != "official"
                    else "eager"
                ),
                native_responses_tool_codec=(
                    _external_native_responses_tool_codec(attempt_upstream)
                    if upstream_name != "official"
                    else "none"
                ),
                named_mutations=frozenset(attempt_mutations),
                fallback_http_statuses=(
                    frozenset(AUTO_UPSTREAM_PROTOCOL_FALLBACK_STATUSES)
                    if (
                        configured_upstream_protocol == RouteProtocol.AUTO
                        and index == 0
                        and len(attempt_protocols) > 1
                    )
                    else frozenset()
                ),
            )
        )
    named_mutations = frozenset(
        set(base_named_mutations).union(
            *(attempt.named_mutations for attempt in attempts)
        )
    )

    canonical_model = (
        canonical_model_id(canonical_route_model or model_requested)
        if canonical_route_model or model_requested
        else None
    )
    upstream_model = resolved_upstream_model
    (
        manifest_version,
        manifest_hash,
        manifest_state,
    ) = _capability_manifest_identity(
        upstream
    )
    return RoutePlan(
        schema_version=ROUTE_PLAN_SCHEMA_VERSION,
        provider_id=resolved_provider_id,
        model_requested=model_requested,
        canonical_model=canonical_model,
        upstream_model=upstream_model,
        inbound_protocol=_route_protocol(inbound_format),
        configured_upstream_protocol_name=configured_upstream_format,
        configured_upstream_protocol=configured_upstream_protocol,
        protocol_capability_state=protocol_capability_state,
        protocol_failure_reason=(
            None
            if attempts
            else (
                (
                    "unqualified route capability binding: "
                    f"{capability_binding.route_scope_failure_reason}"
                )
                if binding_scope_failed
                else (
                    f"unsupported upstream protocol: {configured_upstream_format}"
                    if protocol_capability_state == CapabilityState.UNSUPPORTED
                    else f"unqualified upstream protocol: {configured_upstream_format}"
                )
            )
        ),
        attempts=tuple(attempts),
        failure_observation=(
            None
            if attempts
            else RouteFailureObservation(
                upstream_protocol=configured_upstream_protocol,
                wire_format_adapter=wire_adapter,
                authentication_strategy=authentication_strategy,
                request_kind=effective_request_kind,
                retry_policy=retry_policy,
                retry_eligibility=protocol_capability_state,
                usage_policy=usage_policy,
                streaming_policy=streaming_policy,
                transport_policy=transport_policy,
                request_mutation_policy=mutation_policy,
                response_mutation_policy=mutation_policy,
                sse_mutation_policy=mutation_policy,
            )
        ),
        capability_manifest_version=manifest_version,
        capability_manifest_hash=manifest_hash,
        capability_manifest_state=manifest_state,
        capability_binding=capability_binding,
        behavior_profile=behavior_profile,
        caller_request_body_mode=caller_request_body_mode,
        prepared_request_protocol=_route_protocol(inbound_format),
        codex_semantic_adapter=codex_semantic_adapter,
        raw_provider_probe=raw_provider_probe,
        request_kind_policy=request_kind_policy,
        repair_policy=repair_policy,
        vision=vision,
        tool_exposure=tool_exposure,
        codex_compatibility_policy=codex_compatibility_policy,
        collaboration_backend=collaboration_backend,
        execution_owner=ExecutionOwner.CODEX_CLIENT,
        named_mutations=frozenset(named_mutations),
        official_http_passthrough=official_http_passthrough,
        transparent_metered=transparent_metered,
        transparent_same_format=transparent_same_format,
        transparent_lightweight_fallback=transparent_lightweight_fallback,
        transparent_tool_loop_guard=(
            transparent_same_format and upstream_name != "official"
        ),
    )


def _route_plan_event_fields(plan: RoutePlan) -> dict[str, Any]:
    primary_attempt = plan.primary_attempt
    return {
        "route_plan_schema_version": plan.schema_version,
        "route_plan_summary_scope": "planned",
        "route_provider_id": plan.provider_id,
        "route_model_requested": plan.model_requested,
        "route_model_canonical": plan.canonical_model,
        "route_upstream_model": plan.upstream_model,
        "route_endpoint_url": (
            _safe_route_endpoint_url(primary_attempt.endpoint_url)
            if primary_attempt is not None
            else None
        ),
        "configured_upstream_protocol_name": plan.configured_upstream_protocol_name,
        "configured_upstream_protocol": plan.configured_upstream_protocol.value,
        "protocol_capability_state": plan.protocol_capability_state.value,
        "protocol_failure_reason": plan.protocol_failure_reason,
        "route_attempts": [
            attempt.telemetry_snapshot() for attempt in plan.attempts
        ],
        "wire_format_adapter": (
            primary_attempt.wire_format_adapter
            if primary_attempt is not None
            else plan.wire_format_adapter
        ),
        "route_plan_primary_wire_format_adapter": (
            primary_attempt.wire_format_adapter
            if primary_attempt is not None
            else plan.wire_format_adapter
        ),
        "caller_request_body_mode": plan.caller_request_body_mode.value,
        "prepared_request_protocol": plan.prepared_request_protocol.value,
        "codex_semantic_adapter": plan.codex_semantic_adapter,
        "request_kind": plan.request_kind,
        "raw_provider_probe": plan.raw_provider_probe,
        "request_kind_policy": plan.request_kind_policy,
        "retry_policy": (
            primary_attempt.retry.policy.value
            if primary_attempt is not None
            else plan.retry_policy.value
        ),
        "retry_eligibility": (
            primary_attempt.retry.eligibility.value
            if primary_attempt is not None
            else plan.retry_eligibility.value
        ),
        "usage_policy": (
            primary_attempt.usage_policy.value
            if primary_attempt is not None
            else plan.usage_policy.value
        ),
        "repair_policy": plan.repair_policy,
        "capability_manifest_version": plan.capability_manifest_version,
        "capability_manifest_hash": plan.capability_manifest_hash,
        "capability_manifest_state": plan.capability_manifest_state.value,
        "capability_binding_source_present": (
            plan.capability_binding.source_present
        ),
        "capability_binding_schema_version": (
            plan.capability_binding.schema_version
        ),
        "capability_binding_provider_id": (
            plan.capability_binding.provider_id
        ),
        "capability_binding_model_id": plan.capability_binding.model_id,
        "capability_binding_configured_upstream_protocol": (
            plan.capability_binding.configured_upstream_protocol.value
        ),
        "capability_binding_upstream_protocol": (
            plan.capability_binding.binding_upstream_protocol.value
        ),
        "capability_binding_route_scope_state": (
            plan.capability_binding.route_scope_state.value
        ),
        "capability_binding_route_scope_failure_reason": (
            plan.capability_binding.route_scope_failure_reason
        ),
        "authentication_strategy": (
            primary_attempt.authentication_strategy.value
            if primary_attempt is not None
            else plan.authentication_strategy.value
        ),
        "tool_requested_exposure_mode": plan.tool_exposure.requested_mode.value,
        "tool_exposure_mode": plan.tool_exposure.effective_mode.value,
        "tool_capability_state": plan.tool_exposure.capability_state.value,
        "gateway_schema_injection": plan.tool_exposure.gateway_schema_injection,
        "strip_caller_tools": plan.tool_exposure.strip_caller_tools,
        "codex_compatibility_policy": plan.codex_compatibility_policy.value,
        "collaboration_backend": plan.collaboration_backend.value,
        "execution_owner": plan.execution_owner.value,
        "streaming_policy": (
            primary_attempt.streaming_policy.value
            if primary_attempt is not None
            else plan.streaming_policy.value
        ),
        "transport_policy": (
            primary_attempt.transport_policy.value
            if primary_attempt is not None
            else plan.transport_policy.value
        ),
        "request_mutation_policy": (
            primary_attempt.request_mutation_policy.value
            if primary_attempt is not None
            else plan.request_mutation_policy.value
        ),
        "response_mutation_policy": (
            primary_attempt.response_mutation_policy.value
            if primary_attempt is not None
            else plan.response_mutation_policy.value
        ),
        "sse_mutation_policy": (
            primary_attempt.sse_mutation_policy.value
            if primary_attempt is not None
            else plan.sse_mutation_policy.value
        ),
        "vision_proxy_policy": plan.vision.policy,
        "vision_action": plan.vision.action.value,
        "vision_network_action": plan.vision.network_action.value,
        "vision_input_has_image": plan.vision.input_has_image,
        "vision_target_accepts_images": plan.vision.target_accepts_images,
        "transparent_tool_loop_guard": plan.transparent_tool_loop_guard,
        "mutation_summary_scope": "planned_union",
        "planned_mutation_summary": [
            mutation.value for mutation in plan.mutation_summary
        ],
        "mutation_summary": [
            mutation.value for mutation in plan.mutation_summary
        ],
    }


def _route_attempt_event_fields(
    attempt: RouteAttemptPlan,
    *,
    provider_id: str | None = None,
    model_requested: str | None = None,
    model_canonical: str | None = None,
    upstream_model: str | None = None,
) -> dict[str, Any]:
    snapshot = attempt.telemetry_snapshot()
    retry = snapshot["retry"]
    return {
        "execution_summary_scope": "selected_attempt_plan",
        "route_attempt_provider_id": provider_id,
        "route_attempt_model_requested": model_requested,
        "route_attempt_model_canonical": model_canonical,
        "route_attempt_upstream_model": upstream_model,
        "route_attempt_endpoint_url": snapshot["endpoint_url"],
        "executed_upstream_protocol": snapshot["upstream_protocol"],
        "executed_wire_format_adapter": snapshot["wire_format_adapter"],
        "executed_request_conversion_steps": snapshot[
            "request_conversion_steps"
        ],
        "executed_attempt_mutation_summary": snapshot[
            "mutation_summary"
        ],
        "route_attempt_index": snapshot["index"],
        "route_attempt_protocol": snapshot["upstream_protocol"],
        "route_attempt_wire_format_adapter": snapshot[
            "wire_format_adapter"
        ],
        "route_attempt_request_conversion_steps": snapshot[
            "request_conversion_steps"
        ],
        "route_attempt_request_body_mode": snapshot["request_body_mode"],
        "route_attempt_mutation_summary_scope": "attempt_plan",
        "route_attempt_authentication_strategy": snapshot[
            "authentication_strategy"
        ],
        "route_attempt_streaming_policy": snapshot["streaming_policy"],
        "route_attempt_usage_policy": snapshot["usage_policy"],
        "route_attempt_transport_policy": snapshot["transport_policy"],
        "route_attempt_retry_policy": retry["policy"],
        "route_attempt_retry_eligibility": retry["eligibility"],
        "route_attempt_base_open_attempts": retry["base_open_attempts"],
        "route_attempt_base_relay_attempts": retry[
            "base_relay_attempts"
        ],
        "route_attempt_open_attempt_budget": retry[
            "open_attempt_budget"
        ],
        "route_attempt_request_timeout_seconds": retry[
            "request_timeout_seconds"
        ],
        "route_attempt_request_mutation_policy": snapshot[
            "request_mutation_policy"
        ],
        "route_attempt_response_mutation_policy": snapshot[
            "response_mutation_policy"
        ],
        "route_attempt_sse_mutation_policy": snapshot[
            "sse_mutation_policy"
        ],
        "route_attempt_verify_cross_protocol_source": snapshot[
            "verify_cross_protocol_source"
        ],
        "route_attempt_tool_protocol": snapshot["tool_protocol"],
        "route_attempt_tool_surface_strategy": snapshot[
            "tool_surface_strategy"
        ],
        "route_attempt_native_responses_tool_codec": snapshot[
            "native_responses_tool_codec"
        ],
        "route_attempt_fallback_http_statuses": snapshot[
            "fallback_http_statuses"
        ],
        "route_attempt_mutation_summary": snapshot["mutation_summary"],
    }


