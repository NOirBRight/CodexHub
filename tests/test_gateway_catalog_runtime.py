"""Direct seam tests for Gateway catalog/runtime resolution."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

import gateway_catalog_runtime
from catalog import CatalogPolicy
from gateway_catalog_runtime import CatalogFacts, CatalogRuntime
from gateway_errors import ModelIdentityResolutionError


FORBIDDEN_SOURCE_MARKERS = (
    "import codex_proxy",
    "from codex_proxy",
    "BaseHTTPRequestHandler",
    "CodexProxyHandler",
    "gateway_transport",
    "gateway_sse",
    "route_plan",
    "_proxy_attr",
    "getattr(",
)


def _policy() -> CatalogPolicy:
    return CatalogPolicy(
        denied_models=set(),
        denied_substrings=set(),
        display_names={},
    )


def test_catalog_runtime_source_is_independent_of_facade_handler_transport_and_planning() -> None:
    source = Path(gateway_catalog_runtime.__file__).read_text(encoding="utf-8")
    for marker in FORBIDDEN_SOURCE_MARKERS:
        assert marker not in source
    assert "class CatalogRuntime" in source
    assert "class CatalogFacts" in source


def test_catalog_runtime_hooks_are_frozen():
    runtime = CatalogRuntime()
    with pytest.raises(FrozenInstanceError):
        runtime.facts = CatalogFacts()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        runtime.official_base_url_reader = lambda: "https://changed.test"  # type: ignore[misc]


def test_catalog_facts_are_deeply_immutable() -> None:
    facts = CatalogFacts(
        official_fast_variant_base_models={"fast": "base"},
        official_fast_variant_display_names={"fast": "Fast"},
        upstream_max_output_token_caps={"model": 10},
    )
    with pytest.raises(FrozenInstanceError):
        facts.official_alias_prefix = "changed/"  # type: ignore[misc]
    with pytest.raises(TypeError):
        facts.official_fast_variant_base_models["other"] = "base"  # type: ignore[index]


def test_openai_model_list_stays_openai_shaped() -> None:
    payload = gateway_catalog_runtime.openai_model_list(
        {
            "fetched_at": "2026-08-25T00:00:00Z",
            "models": [
                {"slug": "gpt-5.6-luna", "codex_proxy_metadata": {"provider": "openai"}},
                {"slug": "xai/grok-4", "codex_proxy_metadata": {"provider": "xai"}},
            ],
        }
    )
    assert payload == {
        "object": "list",
        "data": [
            {"id": "gpt-5.6-luna", "object": "model", "owned_by": "openai"},
            {"id": "xai/grok-4", "object": "model", "owned_by": "xai"},
        ],
    }


def test_codex_model_manifest_uses_catalog_rows_for_app_picker() -> None:
    assert gateway_catalog_runtime.wants_codex_model_manifest("") is False
    assert gateway_catalog_runtime.wants_codex_model_manifest("client_version=0.149.1") is True
    assert gateway_catalog_runtime.wants_codex_model_manifest("includeHidden=true") is True
    payload = gateway_catalog_runtime.codex_model_manifest(
        {
            "fetched_at": "2026-08-25T00:00:00Z",
            "client_version": "0.149.1",
            "models": [
                {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6 Luna"},
                {"slug": "gpt-5.6-terra"},
                {"slug": "gpt-5.6-sol"},
            ],
        }
    )
    assert [row["slug"] for row in payload["models"]] == [
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    ]
    assert payload["models"][0]["visibility"] == "list"
    assert payload["models"][0]["hidden"] is False
    assert "data" not in payload


def test_generated_catalog_reader_hook_normalizes_exact_official_identity() -> None:
    requested_path = Path("missing-generated-catalog.json")
    runtime = CatalogRuntime(
        facts=CatalogFacts(generated_catalog_path=requested_path),
        catalog_path_reader=lambda path: path,
        catalog_models_reader=lambda _path: [
            {"slug": "openai/gpt-5.6-sol", "supported_in_api": True},
            {"slug": "provider/model", "supported_in_api": True},
        ],
    )

    assert runtime.generated_catalog_slugs() == {"gpt-5.6-sol", "provider/model"}


def test_xai_oauth_session_routes_grok_without_api_key() -> None:
    from providers_config import ModelConfig, ProviderConfig, build_external_model_index

    providers = [
        ProviderConfig(
            id="xai",
            name="xAI",
            base_url="https://api.x.ai/v1",
            api_key="{env:XAI_API_KEY}",
            models=[ModelConfig(id="grok-4.6")],
        )
    ]
    with (
        patch.dict("os.environ", {}, clear=True),
        patch(
            "providers_config.provider_has_subscription_session",
            side_effect=lambda provider_id: provider_id == "xai",
        ),
    ):
        index = build_external_model_index(providers)

    runtime = CatalogRuntime(
        policy_reader=lambda _path: _policy(),
        external_model_reader=lambda slug: index.get(slug),
        official_fast_variant_reader=lambda _slug, _policy: None,
        official_alias_reader=lambda _slug, _policy: None,
        generated_official_reader=lambda _slug, _policy: None,
        ollama_alias_reader=lambda _slug, _policy: None,
        ollama_runtime_reader=lambda _slug, _policy: None,
    )

    with patch(
        "gateway_catalog_runtime.provider_auth_mode",
        side_effect=lambda provider_id: "xai_oauth" if provider_id == "xai" else None,
    ):
        upstream = runtime.choose_upstream("xai/grok-4.6")

    assert upstream["provider_id"] == "xai"
    assert upstream["model_id"] == "xai/grok-4.6"
    assert upstream["upstream_model"] == "grok-4.6"
    assert upstream["auth"] == "xai_oauth"
    assert upstream["api_key"] is None


def test_xai_without_api_key_or_session_is_unsupported_model() -> None:
    runtime = CatalogRuntime(
        policy_reader=lambda _path: _policy(),
        external_model_reader=lambda _slug: None,
        official_fast_variant_reader=lambda _slug, _policy: None,
        official_alias_reader=lambda _slug, _policy: None,
        generated_official_reader=lambda _slug, _policy: None,
        ollama_alias_reader=lambda _slug, _policy: None,
        ollama_runtime_reader=lambda _slug, _policy: None,
    )

    with pytest.raises(ModelIdentityResolutionError) as failure:
        runtime.choose_upstream("xai/grok-4.6")

    assert failure.value.reason == "unsupported_model"
    assert failure.value.provider_id == "xai"
    assert failure.value.model_slug == "xai/grok-4.6"


def test_external_resolution_returns_ephemeral_provider_model_facts() -> None:
    external = {
        "alias": "volc/glm-5.2",
        "provider_alias": "volc",
        "upstream_name": "volcengine",
        "base_url": "https://example.test/v1",
        "api_key": "test-key",
        "upstream_model": "glm-5.2",
        "input_modalities": ["text"],
    }
    runtime = CatalogRuntime(
        policy_reader=lambda _path: _policy(),
        external_model_reader=lambda slug: external if slug == "volc/glm-5.2" else None,
        official_fast_variant_reader=lambda _slug, _policy: None,
        official_alias_reader=lambda _slug, _policy: None,
        generated_official_reader=lambda _slug, _policy: None,
        ollama_alias_reader=lambda _slug, _policy: None,
        ollama_runtime_reader=lambda _slug, _policy: None,
    )

    upstream = runtime.choose_upstream("volc/glm-5.2")

    assert upstream["provider_id"] == "volc"
    assert upstream["model_id"] == "volc/glm-5.2"
    assert upstream["upstream_model"] == "glm-5.2"
    assert upstream["input_modalities"] == ("text",)


def test_context_guard_uses_only_complete_published_official_budget() -> None:
    budget = {
        "source": "current_direct_official",
        "freshness": "fresh",
        "model_context_window": 1000,
        "effective_context_window_percent": 80,
        "effective_context_window": 800,
        "model_auto_compact_token_limit": 700,
    }
    catalog = {
        "models": [{
            "slug": "gpt-test",
            "context_window": 1200,
            "codex_proxy_metadata": {
                "provider": "openai",
                "upstream_name": "official",
                "upstream_model": "gpt-test",
                "official_context_budget": budget,
            },
        }]
    }

    guarded = CatalogRuntime().catalog_with_openai_context_guard(
        catalog,
        {"gpt-test": budget},
        require_published_snapshot=True,
    )

    assert guarded["models"][0]["context_window"] == 1000
    assert guarded["models"][0]["max_context_window"] == 1000


def test_catalog_factory_keeps_owning_module_reader_patches_live() -> None:
    row = {"slug": "runtime-model", "max_output_tokens": 321}
    with patch("gateway_catalog_runtime.generated_catalog_by_slug", return_value={"runtime-model": row}):
        assert gateway_catalog_runtime.catalog_max_output_tokens("runtime-model") == 321


def test_owning_module_generated_catalog_slug_hook_stays_live():
    with patch("gateway_catalog_runtime.generated_catalog_by_slug", return_value={"hook-only": {"slug": "hook-only"}}):
        assert gateway_catalog_runtime.generated_catalog_slugs() == {"hook-only"}


def test_catalog_types_are_true_facade_aliases() -> None:
    assert gateway_catalog_runtime.CatalogFacts is CatalogFacts
    assert gateway_catalog_runtime.CatalogRuntime is CatalogRuntime
