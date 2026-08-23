"""Guard the Gateway test surface against private ``codex_proxy`` internals."""

from __future__ import annotations

import re
from pathlib import Path

FORBIDDEN = re.compile(
    r"codex_proxy\._[A-Za-z]|CodexProxyHandler\.__new__|CodexProxyHandler\._"
)

# Transport, catalog, and event families now belong to owning modules (#461, #462).
FORBIDDEN_OWNING_MODULE_PATCH = re.compile(
    r"(?:patch(?:\.object)?\(\s*|setattr\(\s*)(?:"
    r"['\"]codex_proxy\.(?:urlopen|official_urlopen|official_pool_manager|"
    r"official_proxy_url|open_upstream_once|transport_failure_phase|"
    r"getproxies_registry|getproxies|proxy_bypass|time\.sleep|time\.monotonic|"
    r"sys\.platform|urllib3|sleep_for_retry_with_gateway_cancellation|"
    r"generated_catalog_by_slug|generated_catalog_slugs|"
    r"resolve_external_model_alias|resolve_ollama_cloud_model|"
    r"existing_generated_catalog_path|published_catalog_model|"
    r"should_include_model|ollama_cloud_alias_upstream_model|"
    r"ollama_cloud_runtime_upstream|choose_upstream|official_upstream|"
    r"current_catalog_data|load_policy|write_proxy_event|GATEWAY_EVENT_WRITER|"
    r"GATEWAY_DIAGNOSTIC_RECORDER|flush_proxy_event_writer|"
    r"compatible_request_body|compatible_response_body|compatible_sse_line|"
    r"official_passthrough_request_body|transparent_request_body|"
    r"normalize_third_party_tool_call|downgrade_invalid_third_party_tool_calls|"
    r"guard_duplicate_multi_agent_spawn_calls|image_proxy_cache_lookup|"
    r"image_proxy_description_for_part|_prepare_runtime_tool_compatibility|"
    r"_filter_tools_for_subagent_coordinator|_inject_explicit_codex_tools|"
    r"_runtime_alias_for_namespace_child|_adapt_apply_patch_custom_tool_history)"
    r"|codex_proxy,\s*['\"](?:OFFICIAL_HTTP_POOLS|_open_upstream_once|"
    r"open_upstream_once|choose_upstream|official_upstream|current_catalog_data|"
    r"write_proxy_event|GATEWAY_EVENT_WRITER|GATEWAY_DIAGNOSTIC_RECORDER|"
    r"flush_proxy_event_writer|compatible_request_body|compatible_response_body|"
    r"compatible_sse_line|normalize_third_party_tool_call|"
    r"downgrade_invalid_third_party_tool_calls|"
    r"guard_duplicate_multi_agent_spawn_calls|_prepare_runtime_tool_compatibility|"
    r"_filter_tools_for_subagent_coordinator|_inject_explicit_codex_tools|"
    r"_runtime_alias_for_namespace_child|_adapt_apply_patch_custom_tool_history)"
    r")"
)

TESTS_ROOT = Path(__file__).resolve().parent


def test_gateway_tests_do_not_touch_codex_proxy_privates() -> None:
    hits: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(TESTS_ROOT)}:{line_no}:{line.strip()}")
    assert hits == []


def test_gateway_tests_do_not_patch_migrated_transport_or_catalog_on_facade() -> None:
    hits: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        collapsed = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
        if FORBIDDEN_OWNING_MODULE_PATCH.search(collapsed):
            hits.append(str(path.relative_to(TESTS_ROOT)))
    assert hits == []
