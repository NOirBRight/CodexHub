from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from route_primitives import (
    CAPACITY_RETRY_CADENCE_SECONDS,
    DEFAULT_CAPACITY_RETRY_ELAPSED_LIMIT_SECONDS,
    DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS,
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    DEFAULT_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS,
    DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS,
    DEFAULT_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS,
    DEFAULT_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS,
    DEFAULT_STREAM_RETRY_ELAPSED_LIMIT_SECONDS,
    DEFAULT_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS,
    DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
    RETRY_FAILURE_PROVIDER_THROTTLE,
    RETRY_FAILURE_QUICK_TRANSIENT,
    RETRY_REQUEST_COMPACT,
    RETRY_REQUEST_IMAGE_PROXY_VISION,
    RETRY_REQUEST_MAIN_GENERATION,
    RETRY_REQUEST_OFFICIAL_CONTROL,
)
from subagent_policy import (
    guidance_enabled as _subagent_policy_guidance_enabled,
    semantic_repair_enabled as _subagent_policy_semantic_repair_enabled,
    subagent_assist_mode as _subagent_policy_assist_mode,
)


def _runtime_proxy_dir() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    root = Path(codex_home) if codex_home else Path.home() / ".codex"
    return root / "proxy"

def upstream_timeout_seconds() -> int:
    settings_value = _runtime_settings_value("gateway_request_timeout_seconds")
    if isinstance(settings_value, int):
        return settings_value if settings_value > 0 else DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    if isinstance(settings_value, str):
        try:
            value = int(settings_value)
        except ValueError:
            value = DEFAULT_UPSTREAM_TIMEOUT_SECONDS
        return value if value > 0 else DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    raw_value = os.environ.get("CODEX_PROXY_UPSTREAM_TIMEOUT_SECONDS")
    if not raw_value:
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_UPSTREAM_TIMEOUT_SECONDS


def sse_keepalive_seconds() -> float:
    raw_value = os.environ.get("CODEX_PROXY_SSE_KEEPALIVE_SECONDS")
    if not raw_value:
        return 15.0
    try:
        value = float(raw_value)
    except ValueError:
        return 15.0
    if value <= 0:
        return 0.0
    return max(0.001, min(value, 60.0))


def _number_setting_or_env(
    *,
    settings_name: str,
    env_name: str,
    default: float,
    fallback_settings_names: tuple[str, ...] = (),
    fallback_env_names: tuple[str, ...] = (),
) -> float:
    def parse_setting(name: str) -> float | None:
        settings_value = _runtime_settings_value(name)
        if isinstance(settings_value, (int, float)) and not isinstance(settings_value, bool):
            return float(settings_value) if settings_value > 0 else 0.0
        if isinstance(settings_value, str):
            try:
                value = float(settings_value)
            except ValueError:
                return None
            return value if value > 0 else 0.0
        return None

    def parse_env(name: str) -> float | None:
        raw_value = os.environ.get(name)
        if raw_value is None:
            return None
        try:
            value = float(raw_value)
        except ValueError:
            return None
        return value if value > 0 else 0.0

    primary_setting = parse_setting(settings_name)
    if primary_setting is not None:
        return primary_setting
    primary_env = parse_env(env_name)
    if primary_env is not None:
        return primary_env
    for name in fallback_settings_names:
        fallback_setting = parse_setting(name)
        if fallback_setting is not None:
            return fallback_setting
    for name in fallback_env_names:
        fallback_env = parse_env(name)
        if fallback_env is not None:
            return fallback_env
    return default


def transport_sse_idle_timeout_seconds() -> float:
    return _number_setting_or_env(
        settings_name="gateway_transport_sse_idle_timeout_seconds",
        env_name="CODEX_PROXY_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS",
        default=DEFAULT_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS,
    )


def model_event_sse_idle_timeout_seconds() -> float:
    return _number_setting_or_env(
        settings_name="gateway_model_event_sse_idle_timeout_seconds",
        env_name="CODEX_PROXY_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS",
        default=DEFAULT_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS,
        fallback_settings_names=(
            "gateway_post_content_sse_idle_timeout_seconds",
            "gateway_pre_output_sse_idle_timeout_seconds",
        ),
        fallback_env_names=(
            "CODEX_PROXY_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS",
            "CODEX_PROXY_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS",
        ),
    )


def pre_output_sse_idle_timeout_seconds() -> float:
    settings_value = _runtime_settings_value("gateway_pre_output_sse_idle_timeout_seconds")
    if isinstance(settings_value, (int, float)) and not isinstance(settings_value, bool):
        return float(settings_value) if settings_value > 0 else 0.0
    if isinstance(settings_value, str):
        try:
            value = float(settings_value)
        except ValueError:
            value = DEFAULT_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS
        return value if value > 0 else 0.0
    raw_value = os.environ.get("CODEX_PROXY_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS")
    if not raw_value:
        return DEFAULT_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS
    try:
        value = float(raw_value)
    except ValueError:
        return DEFAULT_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS
    return value if value > 0 else 0.0


def post_content_sse_idle_timeout_seconds() -> float:
    settings_value = _runtime_settings_value("gateway_post_content_sse_idle_timeout_seconds")
    if isinstance(settings_value, (int, float)) and not isinstance(settings_value, bool):
        return float(settings_value) if settings_value > 0 else 0.0
    if isinstance(settings_value, str):
        try:
            value = float(settings_value)
        except ValueError:
            value = DEFAULT_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS
        return value if value > 0 else 0.0
    raw_value = os.environ.get("CODEX_PROXY_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS")
    if not raw_value:
        return DEFAULT_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS
    try:
        value = float(raw_value)
    except ValueError:
        return DEFAULT_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS
    return value if value > 0 else 0.0


def official_upstream_open_attempts() -> int:
    raw_value = os.environ.get("CODEX_PROXY_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS")
    if not raw_value:
        return DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS
    if value <= 0:
        return DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS
    return min(value, DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS)


def _env_flag(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {"0", "false", "no", "off", ""}


def _runtime_settings_value(name: str) -> Any:
    try:
        with (_runtime_proxy_dir() / "settings.json").open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return payload.get(name)


def gateway_client_key() -> str | None:
    raw_value = os.environ.get("CODEX_PROXY_GATEWAY_CLIENT_KEY")
    if raw_value is None:
        return None
    value = raw_value.strip()
    return value or None


def max_request_body_bytes() -> int:
    raw_value = os.environ.get("CODEX_PROXY_MAX_REQUEST_BODY_BYTES")
    if raw_value is None:
        return DEFAULT_MAX_REQUEST_BODY_BYTES
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_MAX_REQUEST_BODY_BYTES
    if value <= 0:
        return DEFAULT_MAX_REQUEST_BODY_BYTES
    return min(value, 256 * 1024 * 1024)


def _env_or_settings_flag(env_name: str, settings_name: str, default: bool) -> bool:
    settings_value = _runtime_settings_value(settings_name)
    if isinstance(settings_value, bool):
        return settings_value
    if isinstance(settings_value, str):
        return settings_value.strip().lower() not in {"0", "false", "no", "off", ""}
    raw_value = os.environ.get(env_name)
    if raw_value is not None:
        return raw_value.strip().lower() not in {"0", "false", "no", "off", ""}
    return default


def gateway_auto_retry_enabled() -> bool:
    return _env_or_settings_flag(
        "CODEX_PROXY_AUTO_RETRY_ENABLED",
        "gateway_auto_retry_enabled",
        True,
    )


def gateway_official_http_passthrough_enabled() -> bool:
    return _env_or_settings_flag(
        "CODEX_PROXY_OFFICIAL_HTTP_PASSTHROUGH_ENABLED",
        "gateway_official_http_passthrough_enabled",
        True,
    )


def gateway_websocket_recorder_enabled() -> bool:
    return _env_or_settings_flag(
        "CODEX_PROXY_WEBSOCKET_RECORDER_ENABLED",
        "gateway_websocket_recorder_enabled",
        False,
    )


def gateway_websocket_recorder_max_frames() -> int:
    value = _number_setting_or_env(
        settings_name="gateway_websocket_recorder_max_frames",
        env_name="CODEX_PROXY_WEBSOCKET_RECORDER_MAX_FRAMES",
        default=8,
    )
    return max(1, min(int(value), 32))


def gateway_websocket_recorder_idle_timeout_seconds() -> float:
    value = _number_setting_or_env(
        settings_name="gateway_websocket_recorder_idle_timeout_seconds",
        env_name="CODEX_PROXY_WEBSOCKET_RECORDER_IDLE_TIMEOUT_SECONDS",
        default=2.0,
    )
    return max(0.1, min(float(value), 30.0))


def gateway_auto_retry_max_attempts() -> int:
    settings_value = _runtime_settings_value("gateway_auto_retry_max_attempts")
    if isinstance(settings_value, int):
        return max(1, min(settings_value, DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS))
    if isinstance(settings_value, str):
        try:
            value = int(settings_value)
        except ValueError:
            value = DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS
        return max(1, min(value, DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS))
    raw_value = os.environ.get("CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS")
    if not raw_value:
        return DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS
    return max(1, min(value, DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS))


def _request_kind_retry_env_name(request_kind: str) -> str | None:
    if request_kind == RETRY_REQUEST_COMPACT:
        return "CODEX_PROXY_COMPACT_RETRY_MAX_ATTEMPTS"
    if request_kind == RETRY_REQUEST_MAIN_GENERATION:
        return "CODEX_PROXY_MAIN_GENERATION_RETRY_MAX_ATTEMPTS"
    return None


def _request_kind_retry_settings_name(request_kind: str) -> str | None:
    if request_kind == RETRY_REQUEST_COMPACT:
        return "gateway_compact_retry_max_attempts"
    if request_kind == RETRY_REQUEST_MAIN_GENERATION:
        return "gateway_main_generation_retry_max_attempts"
    return None


def _default_retry_attempts_for_request_kind(request_kind: str) -> int:
    if request_kind == RETRY_REQUEST_COMPACT:
        return 3
    if request_kind == RETRY_REQUEST_IMAGE_PROXY_VISION:
        return 3
    if request_kind == RETRY_REQUEST_OFFICIAL_CONTROL:
        return 1
    return 5


def _bounded_retry_attempts(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(1, min(value, DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS))
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return default
        return max(1, min(parsed, DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS))
    return default


def _upstream_retry_attempts(request_kind: str = RETRY_REQUEST_MAIN_GENERATION) -> int:
    if not gateway_auto_retry_enabled():
        return 1
    default = _default_retry_attempts_for_request_kind(request_kind)
    settings_name = _request_kind_retry_settings_name(request_kind)
    if settings_name:
        settings_value = _runtime_settings_value(settings_name)
        if settings_value is not None:
            return _bounded_retry_attempts(settings_value, default)
    env_name = _request_kind_retry_env_name(request_kind)
    if env_name:
        raw_value = os.environ.get(env_name)
        if raw_value is not None:
            return _bounded_retry_attempts(raw_value, default)
    return min(gateway_auto_retry_max_attempts(), default)


def _request_kind_retry_attempts_configured(request_kind: str) -> bool:
    settings_name = _request_kind_retry_settings_name(request_kind)
    if settings_name and _runtime_settings_value(settings_name) is not None:
        return True
    env_name = _request_kind_retry_env_name(request_kind)
    return bool(env_name and os.environ.get(env_name) is not None)


def gateway_capacity_retry_elapsed_limit_seconds() -> float:
    return _number_setting_or_env(
        settings_name="gateway_capacity_retry_elapsed_limit_seconds",
        env_name="CODEX_PROXY_CAPACITY_RETRY_ELAPSED_LIMIT_SECONDS",
        default=DEFAULT_CAPACITY_RETRY_ELAPSED_LIMIT_SECONDS,
    )


def gateway_stream_retry_elapsed_limit_seconds() -> float:
    return _number_setting_or_env(
        settings_name="gateway_stream_retry_elapsed_limit_seconds",
        env_name="CODEX_PROXY_STREAM_RETRY_ELAPSED_LIMIT_SECONDS",
        default=DEFAULT_STREAM_RETRY_ELAPSED_LIMIT_SECONDS,
    )


def gateway_downstream_retry_notice_enabled() -> bool:
    return _env_or_settings_flag(
        "CODEX_PROXY_DOWNSTREAM_RETRY_NOTICE_ENABLED",
        "gateway_downstream_retry_notice_enabled",
        False,
    )


def gateway_capacity_retry_delay_seconds(attempt: int) -> int:
    index = max(1, attempt) - 1
    if index < len(CAPACITY_RETRY_CADENCE_SECONDS):
        return CAPACITY_RETRY_CADENCE_SECONDS[index]
    return CAPACITY_RETRY_CADENCE_SECONDS[-1]


def subagent_assist_mode() -> str:
    return _subagent_policy_assist_mode()


def subagent_guidance_enabled(event_context: Mapping[str, Any] | None) -> bool:
    return _subagent_policy_guidance_enabled(event_context)


def subagent_semantic_repair_enabled(event_context: Mapping[str, Any] | None) -> bool:
    return _subagent_policy_semantic_repair_enabled(event_context)


def lifecycle_empty_final_resample_enabled(
    event_context: Mapping[str, Any] | None,
    request_kind: str,
) -> bool:
    if request_kind != RETRY_REQUEST_MAIN_GENERATION:
        return False
    if not subagent_semantic_repair_enabled(event_context):
        return False
    return bool((event_context or {}).get("subagent_lifecycle_complete"))


def gateway_retry_delay_seconds(
    attempt: int,
    *,
    failure_class: str = RETRY_FAILURE_QUICK_TRANSIENT,
    retry_after_seconds: int | None = None,
) -> int:
    if retry_after_seconds is not None:
        return retry_after_seconds
    if failure_class == RETRY_FAILURE_PROVIDER_THROTTLE:
        return gateway_capacity_retry_delay_seconds(attempt)
    return min(max(1, attempt - 1) * 2, 8)


def gateway_image_proxy_enabled() -> bool:
    return _env_or_settings_flag(
        "CODEX_PROXY_IMAGE_PROXY_ENABLED",
        "gateway_image_proxy_enabled",
        False,
    )


def gateway_image_proxy_model() -> str:
    settings_value = _runtime_settings_value("gateway_image_proxy_model")
    if isinstance(settings_value, str) and settings_value.strip():
        return settings_value.strip()
    return os.environ.get("CODEX_PROXY_IMAGE_PROXY_MODEL", "").strip()
