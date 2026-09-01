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
    assert "class GatewayTransport" not in source
    assert "TransportPolicy.OFFICIAL_KEEPALIVE" in source


def test_vision_proxy_does_not_import_facade_or_handler() -> None:
    source = Path(vision_proxy.__file__).read_text(encoding="utf-8")
    for marker in (
        "import codex_proxy",
        "from codex_proxy",
        "BaseHTTPRequestHandler",
        "CodexProxyHandler",
    ):
        assert marker not in source
    assert "class VisionProxyAdapter" not in source
    assert "class VisionFacts" in source
    assert "class VisionProxyHooks" not in source


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


def test_gateway_compat_has_no_adapter_factories_or_host_adapter_calls() -> None:
    """Compatibility must read owning module attributes directly at call time."""
    import ast

    package = Path(__file__).resolve().parents[1] / "src-python" / "gateway_compat"
    forbidden_defs = {"_apply_patch_adapter", "_collaboration_adapter", "_tool_surface_adapter"}
    forbidden_host_calls = forbidden_defs | {
        "_catalog_output_limit",
        "_collaboration_context_with_protocol",
        "_is_collaboration_v2_context",
        "_resolve_collaboration_boundary",
        "_write_adapter_event",
        "_apply_maintained_thinking_controls",
    }
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in forbidden_defs, f"factory returned in {path.name}:{node.lineno}"
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            target = node.func.value
            if (
                isinstance(target, ast.Name)
                and target.id == "host"
                and node.func.attr in forbidden_host_calls
            ):
                raise AssertionError(f"host adapter seam returned in {path.name}:{node.lineno}")


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


def _cross_module_underscore_imports(root: Path) -> list[tuple[str, int, str, str]]:
    """Return (relative path, lineno, imported module, name) for private cross-module imports."""
    import ast

    violations: list[tuple[str, int, str, str]] = []

    def module_name(path: Path) -> str:
        rel = path.relative_to(root)
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    def resolve_from(file_mod: str, node: ast.ImportFrom) -> str | None:
        if node.module is None and not node.level:
            return None
        imported = node.module or ""
        if node.level:
            pkg = file_mod.split(".")
            if node.level > 1:
                pkg = pkg[: -(node.level - 1)] if len(pkg) >= node.level - 1 else []
            elif node.level == 1:
                pkg = pkg[:-1]
            imported = ".".join([*pkg, node.module] if node.module else pkg)
        return imported or None

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        file_mod = module_name(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            owner = resolve_from(file_mod, node)
            if not owner or owner == file_mod:
                continue
            for alias in node.names:
                name = alias.name
                if name.startswith("_") and not name.startswith("__"):
                    violations.append(
                        (str(path.relative_to(root.parent)), node.lineno, owner, name)
                    )
    return violations


def test_no_cross_module_underscore_imports() -> None:
    src_root = Path(__file__).resolve().parents[1] / "src-python"
    allowlist: tuple[tuple[str, str, str], ...] = ()
    violations = [
        item
        for item in _cross_module_underscore_imports(src_root)
        if (item[0], item[2], item[3]) not in allowlist
    ]
    assert violations == [], (
        "cross-module underscore imports must use the owning module's public name:\n"
        + "\n".join(f"  {path}:{lineno} from {owner} import {name}" for path, lineno, owner, name in violations)
    )


def test_underscore_import_gate_flags_a_new_private_import() -> None:
    import ast

    tree = ast.parse("from gateway_sse import sse_payload_bytes, _sse_payload_bytes\n")
    imported = [alias.name for node in tree.body if isinstance(node, ast.ImportFrom) for alias in node.names]
    assert "_sse_payload_bytes" in imported
    assert "sse_payload_bytes" in imported


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
