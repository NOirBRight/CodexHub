from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, NoReturn


DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 300
DEFAULT_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS = 600.0
DEFAULT_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS = 300.0
DEFAULT_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS = DEFAULT_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS
DEFAULT_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS = DEFAULT_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS
DEFAULT_MAIN_GENERATION_PRE_RESPONSE_BUDGET_SECONDS = 180.0
DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS = 2
DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS = 30
DEFAULT_CAPACITY_RETRY_ELAPSED_LIMIT_SECONDS = 300.0
DEFAULT_STREAM_RETRY_ELAPSED_LIMIT_SECONDS = 600.0
DEFAULT_MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024
RETRY_REQUEST_MAIN_GENERATION = "main_generation"
RETRY_REQUEST_COMPACT = "compact"
RETRY_REQUEST_IMAGE_PROXY_VISION = "image_proxy_vision"
RETRY_REQUEST_OFFICIAL_CONTROL = "official_control"
BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH = "official_codex_app_http_passthrough"
BEHAVIOR_OFFICIAL_GATEWAY_COMPAT = "official_gateway_compat"
BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY = "external_provider_gateway"
BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER = "codex_app_external_adapter"
BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED = "third_party_app_transparent_metered"

WIRE_TRANSPARENT = "transparent"
WIRE_RESPONSES_TO_CHAT = "responses_to_chat"
WIRE_CHAT_TO_RESPONSES = "chat_to_responses"

CODEX_SEMANTIC_EXTERNAL_ADAPTER = "codex_app_external_adapter"
CODEX_SEMANTIC_NONE = "none"

REQUEST_KIND_GATEWAY = "gateway"
REQUEST_KIND_TRANSPARENT = "transparent"

RETRY_GATEWAY_FULL = "gateway_full"
RETRY_CONSERVATIVE_PRE_OUTPUT = "conservative_pre_output"

USAGE_SYNC_CAPTURE = "sync_capture"
USAGE_ASYNC_TAP = "async_tap"

REPAIR_CODEX_SUBAGENT = "codex_subagent_repair"
REPAIR_NONE = "none"

VISION_PROXY_DISABLED = "disabled"
VISION_PROXY_CODEX_APP_ADAPTER = "codex_app_adapter"
VISION_PROXY_TRANSPARENT_OVERLAY = "transparent_overlay"

ROUTE_PLAN_SCHEMA_VERSION = "codexhub.route-plan.v1"


class CapabilityState(str, Enum):
    SUPPORTED = "Supported"
    UNSUPPORTED = "Unsupported"
    UNQUALIFIED = "Unqualified"


class RouteProtocol(str, Enum):
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    AUTO = "auto"
    UNKNOWN = "unknown"


class AttemptRequestBodyMode(str, Enum):
    PREPARED_DIRECT = "prepared_direct"
    CONVERT_RESPONSES_TO_CHAT = "convert_responses_to_chat"
    CONVERT_CHAT_TO_RESPONSES = "convert_chat_to_responses"


class CallerRequestBodyMode(str, Enum):
    PRESERVE_CALLER = "preserve_caller"
    CONVERT_CHAT_TO_RESPONSES = "convert_chat_to_responses"


class AuthenticationStrategy(str, Enum):
    CODEX_AUTH = "codex_auth"
    API_KEY = "api_key"
    OLLAMA_API_KEY = "ollama_api_key"
    INCOMING = "incoming"
    UNKNOWN = "unknown"


def authentication_strategy(value: Any) -> AuthenticationStrategy:
    try:
        return AuthenticationStrategy(str(value))
    except ValueError:
        return AuthenticationStrategy.UNKNOWN


APPLY_PATCH_FUNCTION_NAME = "apply_patch"
WORKER_REQUESTED_BINDING_FIELD = "_codexhub_worker_requested_binding"


class SensitiveValue:
    """An immutable secret whose representation and equality never expose value."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SensitiveValue(<redacted>)"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SensitiveValue)

    def __hash__(self) -> int:
        return hash(SensitiveValue)

    def __deepcopy__(self, memo: dict[int, Any]) -> SensitiveValue:
        return self


class OperationalAuthentication:
    """Request-scoped auth material captured after route viability is proved."""

    __slots__ = (
        "strategy",
        "_authorization",
        "_account_id",
        "_generated_session_id",
        "_generated_client_request_id",
    )

    def __init__(
        self,
        strategy: AuthenticationStrategy,
        *,
        authorization: str | None,
        account_id: str | None = None,
        generated_session_id: str | None = None,
        generated_client_request_id: str | None = None,
    ):
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(
            self,
            "_authorization",
            SensitiveValue(authorization) if authorization else None,
        )
        object.__setattr__(
            self,
            "_account_id",
            SensitiveValue(account_id) if account_id else None,
        )
        object.__setattr__(
            self,
            "_generated_session_id",
            (
                SensitiveValue(generated_session_id)
                if generated_session_id
                else None
            ),
        )
        object.__setattr__(
            self,
            "_generated_client_request_id",
            (
                SensitiveValue(generated_client_request_id)
                if generated_client_request_id
                else None
            ),
        )

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError(f"{type(self).__name__} is immutable")

    @property
    def authorization(self) -> str | None:
        return (
            self._authorization.reveal()
            if self._authorization is not None
            else None
        )

    @property
    def account_id(self) -> str | None:
        return (
            self._account_id.reveal()
            if self._account_id is not None
            else None
        )

    @property
    def generated_session_id(self) -> str | None:
        return (
            self._generated_session_id.reveal()
            if self._generated_session_id is not None
            else None
        )

    @property
    def generated_client_request_id(self) -> str | None:
        return (
            self._generated_client_request_id.reveal()
            if self._generated_client_request_id is not None
            else None
        )

    def __repr__(self) -> str:
        return (
            "OperationalAuthentication("
            f"strategy={self.strategy!r}, materialized=True)"
        )

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, OperationalAuthentication)
            and self.strategy == other.strategy
        )

    def __hash__(self) -> int:
        return hash((OperationalAuthentication, self.strategy))

    def __deepcopy__(
        self,
        memo: dict[int, Any],
    ) -> OperationalAuthentication:
        return self


class FrozenRequestHeaders:
    """Deeply immutable outbound headers with redacted values."""

    __slots__ = ("_items", "_materialized")

    def __init__(
        self,
        headers: Mapping[str, str] | None = None,
        *,
        materialized: bool,
    ):
        object.__setattr__(
            self,
            "_items",
            tuple(
                (str(name), SensitiveValue(str(value)))
                for name, value in (headers or {}).items()
            ),
        )
        object.__setattr__(self, "_materialized", materialized)

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError(f"{type(self).__name__} is immutable")

    @classmethod
    def unmaterialized(cls) -> FrozenRequestHeaders:
        return cls(materialized=False)

    @property
    def materialized(self) -> bool:
        return self._materialized

    def to_dict(self) -> dict[str, str]:
        if not self._materialized:
            raise RuntimeError(
                "route attempt headers were not materialized before execution"
            )
        return {
            name: sensitive_value.reveal()
            for name, sensitive_value in self._items
        }

    def __repr__(self) -> str:
        return (
            "FrozenRequestHeaders("
            f"materialized={self._materialized}, count={len(self._items)})"
        )

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, FrozenRequestHeaders)
            and self._materialized == other._materialized
            and tuple(name.lower() for name, _value in self._items)
            == tuple(name.lower() for name, _value in other._items)
        )

    def __hash__(self) -> int:
        return hash(
            (
                FrozenRequestHeaders,
                self._materialized,
                tuple(name.lower() for name, _value in self._items),
            )
        )

    def __deepcopy__(
        self,
        memo: dict[int, Any],
    ) -> FrozenRequestHeaders:
        return self


class ToolExposureMode(str, Enum):
    CURRENT_COMPATIBILITY = "current_compatibility"
    OFFICIAL_NATIVE = "official_native"
    NATIVE_DEFERRED_SEARCH_CANDIDATE = "native_deferred_search_candidate"
    NATIVE_NO_SEARCH_CANDIDATE = "native_no_search_candidate"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class VisionAction(str, Enum):
    PASS_THROUGH = "pass_through"
    PROXY = "proxy"
    REJECT = "reject"


class VisionNetworkAction(str, Enum):
    NONE = "none"
    IMAGE_PROXY = "image_proxy"


class CollaborationBackend(str, Enum):
    CODEX_RUNTIME = "codex_runtime"
    CLIENT_RUNTIME = "client_runtime"
    GATEWAY_COMPATIBILITY = "gateway_compatibility"


class CodexCompatibilityPolicy(str, Enum):
    OFFICIAL_NATIVE = "official_native"
    CURRENT_COMPATIBILITY = "current_compatibility"
    NONE = "none"


class ExecutionOwner(str, Enum):
    CODEX_CLIENT = "codex_client"


class StreamingPolicy(str, Enum):
    OFFICIAL_PASSTHROUGH = "official_passthrough"
    TRANSPARENT = "transparent"
    TRANSPARENT_CONVERTED = "transparent_converted"
    GATEWAY_ADAPTED = "gateway_adapted"


class RetryPolicy(str, Enum):
    GATEWAY_FULL = RETRY_GATEWAY_FULL
    CONSERVATIVE_PRE_OUTPUT = RETRY_CONSERVATIVE_PRE_OUTPUT


class UsagePolicy(str, Enum):
    SYNC_CAPTURE = USAGE_SYNC_CAPTURE
    ASYNC_TAP = USAGE_ASYNC_TAP


class TransportPolicy(str, Enum):
    OFFICIAL_KEEPALIVE = "official_keepalive"
    STANDARD = "standard"


class MutationPolicy(str, Enum):
    OFFICIAL_PASSTHROUGH = "official_passthrough"
    TRANSPARENT = "transparent"
    GATEWAY_COMPATIBILITY = "gateway_compatibility"


class RouteMutation(str, Enum):
    MODEL_ALIAS = "model_alias"
    NAMESPACE_FLATTENING = "namespace_flattening"
    WIRE_CONVERSION = "wire_conversion"
    SEMANTIC_REPAIR = "semantic_repair"
    HARD_CODED_SCHEMA_INJECTION = "hard_coded_schema_injection"
    OFFICIAL_TOOL_SEARCH_PRESERVATION = "official_tool_search_preservation"
    SYNTHETIC_TERMINAL_FAILURE = "synthetic_terminal_failure"
    IMAGE_CONTENT_REPLACEMENT = "image_content_replacement"
    IMAGE_UNSUPPORTED_REJECTION = "image_unsupported_rejection"
    CALLER_TOOL_STRIPPING = "caller_tool_stripping"
    UNSUPPORTED_PROTOCOL_REJECTION = "unsupported_protocol_rejection"


RETRY_FAILURE_QUICK_TRANSIENT = "quick_transient"
RETRY_FAILURE_PROVIDER_THROTTLE = "provider_throttle"
RETRY_FAILURE_PROVIDER_OVERLOADED = "provider_overloaded"
RETRY_FAILURE_PERMANENT = "permanent"
CAPACITY_RETRY_FAILURE_CLASSES = {
    RETRY_FAILURE_PROVIDER_THROTTLE,
    RETRY_FAILURE_PROVIDER_OVERLOADED,
}
CAPACITY_RETRY_CADENCE_SECONDS = (10, 20, 30, 60)
TRANSIENT_HTTP_RETRY_STATUSES = {408, 409, 421, 425, 429, 500, 502, 503, 504}
AUTO_UPSTREAM_PROTOCOL_FALLBACK_STATUSES = {404, 405, 415, 422}

RETRY_SAFETY_SAFE_PREWRITE = "safe_prewrite"
RETRY_SAFETY_GUARANTEED_IDEMPOTENT = "guaranteed_idempotent"
RETRY_SAFETY_SUPPRESSED_POST_WRITE = "suppressed_post_write"
RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE = "suppressed_post_exposure"
RETRY_SAFETY_UNKNOWN = "unknown"
PERMANENT_HTTP_ERROR_STATUSES = {
    400,
    401,
    403,
    404,
    405,
    406,
    407,
    410,
    411,
    412,
    413,
    414,
    415,
    416,
    417,
    418,
    422,
    426,
    428,
    431,
    451,
    501,
    505,
}
PERMANENT_UPSTREAM_ERROR_VALUES = {
    "400",
    "401",
    "402",
    "403",
    "404",
    "405",
    "406",
    "410",
    "413",
    "414",
    "415",
    "422",
    "451",
    "10003",
    "10004",
    "10005",
    "10013",
    "10014",
    "10015",
    "10016",
    "10019",
    "10163",
    "10404",
    "10907",
    "10910",
    "11200",
    "11201",
    "11221",
    "access_denied",
    "accessdeniedexception",
    "authentication_error",
    "bad_request",
    "badrequest",
    "billing_hard_limit_reached",
    "billing_not_active",
    "blocked_by_guardrail",
    "content_filter",
    "content_policy_violation",
    "context_length_exceeded",
    "forbidden",
    "guardrail_block",
    "incorrect_api_key",
    "insufficient_quota",
    "insufficient_balance",
    "insufficient_credits",
    "invalid_argument",
    "invalidargument",
    "invalid_api_key",
    "invalid_image",
    "invalid_key",
    "invalid_parameter",
    "invalid_parameters",
    "invalid_request",
    "invalid_request_error",
    "moderation",
    "model_not_found",
    "not_found_error",
    "payment_required",
    "permission_denied",
    "permission_error",
    "safety_violation",
    "unauthorized",
    "unsupported_image",
    "unsupported_parameter",
    "unsupported_country",
    "unsupported_value",
    "validation_error",
    "validationexception",
}
PERMANENT_UPSTREAM_ERROR_NEEDLES = (
    "billing",
    "content policy",
    "context length",
    "context_length",
    "country not supported",
    "incorrect api key",
    "insufficient balance",
    "insufficient credits",
    "insufficient quota",
    "invalid api key",
    "invalid argument",
    "invalid parameter",
    "maximum context",
    "moderation",
    "payment required",
    "permission denied",
    "safety",
    "schema",
    "sensitive",
    "token limit",
    "tokens exceed",
    "too many tokens",
    "unsupported country",
    "validation error",
    "token数量超过上限",
)
PERMANENT_UPSTREAM_AUTH_NEEDLES = (
    "access denied",
    "forbidden",
    "not authorized",
    "unauthorized",
)
PROVIDER_THROTTLE_ERROR_VALUES = {
    "10007",
    "11202",
    "11203",
    "11210",
    "429",
    "rate_limit",
    "rate_limit_error",
    "rate_limit_exceeded",
    "rate_limit_reached",
    "resource_exhausted",
    "request_throttled",
    "throttled",
    "throttling",
    "throttlingexception",
    "too_many_requests",
}
PROVIDER_THROTTLE_ERROR_NEEDLES = (
    "limit_requests",
    "qps",
    "rate limit",
    "rate_limit",
    "request limit",
    "requests per minute",
    "requests rate",
    "resource exhausted",
    "rpm",
    "rps",
    "throttl",
    "tokens per minute",
    "too many requests",
    "tpm",
    "流控",
    "限流",
)
PROVIDER_OVERLOADED_ERROR_VALUES = {
    "10008",
    "10009",
    "10010",
    "10011",
    "10012",
    "10110",
    "10222",
    "10223",
    "503",
    "529",
    "model_overloaded",
    "overloaded_error",
    "provider_unavailable",
    "server_overloaded",
    "service_unavailable",
    "serviceunavailable",
    "serviceunavailableexception",
    "unavailable",
}
PROVIDER_OVERLOADED_ERROR_NEEDLES = (
    "capacity",
    "engine node",
    "engineinternalerror",
    "invalid response",
    "lb",
    "model is down",
    "no available model provider",
    "overload",
    "overloaded",
    "queue",
    "queued",
    "server overloaded",
    "service unavailable",
    "system is busy",
    "temporarily unavailable",
    "引擎节点",
    "排队",
    "服务忙",
)
IMAGE_PROXY_PROMPT_VERSION = "v3"
IMAGE_PROXY_PROMPT = (
    "Describe the image for a downstream text-only coding agent that cannot see it. "
    "Be faithful and evidence-first. Include the scene, important objects, layout, "
    "colors, and spatial relationships. Transcribe all visible text with OCR, including "
    "UI labels, buttons, menus, dialogs, errors, warnings, code, URLs, numbers, and "
    "timestamps. For screenshots, describe UI state, selected items, disabled controls, "
    "notifications, and error messages. For charts or tables, summarize axes, legends, "
    "series, rows, columns, units, and visible trends or outliers. Mark ambiguous or "
    "unreadable details explicitly instead of guessing. Return only compact plain prose; "
    "do not include reasoning, caveats about being a proxy, or meta commentary."
)
IMAGE_PROXY_PROGRESS_TEXT = "Analyzing image...\n\n"

