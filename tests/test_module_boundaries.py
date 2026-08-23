"""Extracted Gateway modules must not import the codex_proxy facade."""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

import pytest

import apply_patch_adapter
import collaboration_adapter
import gateway_catalog_runtime
import gateway_errors
import gateway_settings
import gateway_sse
import gateway_transport
import route_plan
import route_primitives
import tool_surface_adapter
import vision_proxy


def test_routing_monolith_and_frozen_surface_are_gone() -> None:
    tests_dir = Path(__file__).parent
    assert not (tests_dir / "test_routing.py").exists()
    assert not (tests_dir / "test_codex_proxy_import_surface.py").exists()


def test_route_plan_source_does_not_import_facade() -> None:
    source = Path(route_plan.__file__).read_text(encoding="utf-8")
    assert "_proxy_attr" not in source
    assert "import codex_proxy" not in source
    assert "_request_header" not in source


def test_gateway_settings_source_does_not_import_facade() -> None:
    source = Path(gateway_settings.__file__).read_text(encoding="utf-8")
    assert "import codex_proxy" not in source
    assert "from codex_proxy" not in source


def test_gateway_catalog_runtime_does_not_import_facade_handler_transport_or_planning() -> None:
    source = Path(gateway_catalog_runtime.__file__).read_text(encoding="utf-8")
    for marker in (
        "import codex_proxy",
        "from codex_proxy",
        "BaseHTTPRequestHandler",
        "CodexProxyHandler",
        "gateway_transport",
        "gateway_sse",
        "route_plan",
        "_proxy_attr",
        "getattr(",
    ):
        assert marker not in source


def test_collaboration_adapter_does_not_import_facade_or_transport() -> None:
    source = Path(collaboration_adapter.__file__).read_text(encoding="utf-8")
    assert "import codex_proxy" not in source
    assert "from codex_proxy" not in source
    assert "gateway_sse" not in source
    assert "transport" not in source


def test_tool_surface_adapter_does_not_import_facade_or_transport() -> None:
    source = Path(tool_surface_adapter.__file__).read_text(encoding="utf-8")
    assert "import codex_proxy" not in source
    assert "from codex_proxy" not in source
    assert "gateway_sse" not in source
    assert "transport" not in source
    assert "BaseHTTPRequestHandler" not in source
    assert "urllib3" not in source
    assert "urlopen" not in source


def test_apply_patch_adapter_does_not_import_facade_or_transport() -> None:
    source = Path(apply_patch_adapter.__file__).read_text(encoding="utf-8")
    assert "import codex_proxy" not in source
    assert "from codex_proxy" not in source
    assert "gateway_sse" not in source
    assert "transport" not in source
    assert "BaseHTTPRequestHandler" not in source
    assert "urllib3" not in source
    assert "urlopen" not in source


def test_gateway_transport_does_not_import_facade_handler_or_sse() -> None:
    source = Path(gateway_transport.__file__).read_text(encoding="utf-8")
    assert "import codex_proxy" not in source
    assert "from codex_proxy" not in source
    assert "gateway_sse" not in source
    assert "BaseHTTPRequestHandler" not in source
    assert "CodexProxyHandler" not in source
    assert "class GatewayTransport" in source
    assert "TransportPolicy.OFFICIAL_KEEPALIVE" in source


def test_vision_proxy_does_not_import_facade_handler_sse_transport_or_catalog() -> None:
    source = Path(vision_proxy.__file__).read_text(encoding="utf-8")
    for marker in (
        "import codex_proxy",
        "from codex_proxy",
        "BaseHTTPRequestHandler",
        "CodexProxyHandler",
        "gateway_sse",
        "gateway_transport",
        "gateway_catalog_runtime",
        "protocol_translation",
        "sse_events",
    ):
        assert marker not in source
    assert "class VisionProxyAdapter" in source
    assert "class VisionFacts" in source
    assert "class VisionProxyHooks" in source


def test_downstream_stream_commit_lives_in_gateway_sse() -> None:
    import codex_proxy

    sse_source = Path(gateway_sse.__file__).read_text(encoding="utf-8")
    facade_source = Path(codex_proxy.__file__).read_text(encoding="utf-8")
    assert "class DownstreamStreamCommit" in sse_source
    assert "class _GatewayDownstreamStreamCommit" not in facade_source
    assert "class DownstreamStreamCommit" not in facade_source
    assert "import codex_proxy" not in sse_source


def test_retry_delay_seconds_rejects_exc_parameter() -> None:
    plan = route_plan.RetryExecutionPlan(
        eligibility=route_primitives.CapabilityState.SUPPORTED,
        policy=route_primitives.RetryPolicy.GATEWAY_FULL,
        request_kind=route_primitives.RETRY_REQUEST_MAIN_GENERATION,
        request_timeout_seconds=30,
        base_open_attempts=2,
        base_relay_attempts=2,
        failure_expansion_attempts=2,
        request_kind_attempts_configured=False,
        retry_http_errors=True,
        open_attempt_budget=None,
        capacity_elapsed_limit_seconds=0.0,
        stream_elapsed_limit_seconds=0.0,
        emit_downstream_retry_notice=False,
        pre_response_budget_seconds=None,
        lifecycle_final_retry_eligible=False,
    )
    assert "exc" not in inspect.signature(plan.retry_delay_seconds).parameters
    with pytest.raises(TypeError):
        plan.retry_delay_seconds(
            1,
            failure_class=route_primitives.RETRY_FAILURE_QUICK_TRANSIENT,
            exc=RuntimeError("leftover Retry-After caller"),
        )
    assert (
        plan.retry_delay_seconds(
            1,
            failure_class=route_primitives.RETRY_FAILURE_QUICK_TRANSIENT,
            retry_after_seconds=7,
        )
        == 7
    )


def test_invalid_codec_still_raises_when_planning_event_sink_is_none(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(route_plan, "_planning_event_sink", None)
    with caplog.at_level(logging.WARNING, logger="route_plan"):
        with pytest.raises(gateway_errors.UpstreamProtocolTranslationError) as raised:
            route_plan._external_native_responses_tool_codec(
                {"native_responses_tool_codec": "not-a-codec"}
            )
    assert raised.value.cause.code == route_plan.NATIVE_RESPONSES_TOOL_CODEC_ERROR_CODE
    assert "native_responses_tool_codec_rejected" in caplog.text


def test_gateway_runtime_is_deleted_and_admission_does_not_import_the_facade() -> None:
    import gateway_admission

    runtime_source = Path(__file__).resolve().parents[1] / "src-python" / "gateway_runtime.py"
    assert not runtime_source.exists(), "gateway_runtime.py must stay deleted (#465)"
    admission_source = Path(gateway_admission.__file__).read_text(encoding="utf-8")
    assert "import codex_proxy" not in admission_source
    assert "from codex_proxy" not in admission_source


def test_gateway_compat_does_not_import_the_facade() -> None:
    package = Path(__file__).resolve().parents[1] / "src-python" / "gateway_compat"
    for path in sorted(package.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "import codex_proxy" not in source
        assert "from codex_proxy" not in source
        assert "import gateway_runtime" not in source
        assert "from gateway_runtime" not in source
        assert "BaseHTTPRequestHandler" not in source
        assert "CodexProxyHandler" not in source
        line_count = source.count("\n") + (0 if source.endswith("\n") else 1)
        assert line_count < 2000, f"{path.name} is {line_count} lines"


def test_gateway_events_does_not_import_the_facade() -> None:
    import gateway_events

    source = Path(gateway_events.__file__).read_text(encoding="utf-8")
    assert "import codex_proxy" not in source
    assert "from codex_proxy" not in source
    assert "import gateway_runtime" not in source
    assert "from gateway_runtime" not in source
    assert "BaseHTTPRequestHandler" not in source
    assert "CodexProxyHandler" not in source


def test_gateway_stream_semantics_does_not_import_the_facade() -> None:
    import gateway_stream_semantics
    import gateway_sse

    stream_source = Path(gateway_stream_semantics.__file__).read_text(encoding="utf-8")
    sse_source = Path(gateway_sse.__file__).read_text(encoding="utf-8")
    for source in (stream_source, sse_source):
        assert "import codex_proxy" not in source
        assert "from codex_proxy" not in source
        assert "import gateway_runtime" not in source
        assert "from gateway_runtime" not in source
        assert "BaseHTTPRequestHandler" not in source
        assert "CodexProxyHandler" not in source
    stream_lines = stream_source.count("\n") + (0 if stream_source.endswith("\n") else 1)
    sse_lines = sse_source.count("\n") + (0 if sse_source.endswith("\n") else 1)
    assert stream_lines < 3000, f"gateway_stream_semantics.py is {stream_lines} lines"
    assert sse_lines < 3000, f"gateway_sse.py is {sse_lines} lines"
    assert "class UpstreamSseSemanticError" in stream_source
    assert "def _chat_stream_chunks_have_terminal" in stream_source


def test_gateway_relay_passthrough_does_not_import_the_facade() -> None:
    import gateway_relay_passthrough

    source = Path(gateway_relay_passthrough.__file__).read_text(encoding="utf-8")
    assert "import codex_proxy" not in source
    assert "from codex_proxy" not in source
    assert "import gateway_runtime" not in source
    assert "from gateway_runtime" not in source
    assert "BaseHTTPRequestHandler" not in source
    assert "CodexProxyHandler" not in source


def test_gateway_relay_pair_imports_cleanly_in_either_order() -> None:
    """gateway_relay imports gateway_relay_passthrough at the bottom of its
    body; the passthrough module must therefore load without importing
    gateway_relay at runtime, or whichever module loads first breaks."""
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src-python")
    for first in ("gateway_relay_passthrough", "gateway_relay"):
        result = subprocess.run(
            [sys.executable, "-c", f"import {first}"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode == 0, f"import {first} first failed:\n{result.stderr}"
