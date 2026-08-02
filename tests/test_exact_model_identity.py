import json
from pathlib import Path
from unittest.mock import patch

import pytest

import codex_proxy
from providers_config import ModelConfig, ProviderConfig, build_external_model_index, build_ollama_cloud_model_index
from test_routing import post_handler


def _catalog_row(slug: str, *, provider: str = "openai", upstream_model: str | None = None):
    return {
        "slug": slug,
        "visibility": "list",
        "supported_in_api": True,
        "codex_proxy_metadata": {
            "provider": provider,
            "upstream_name": "official" if provider == "openai" else provider,
            "upstream_model": upstream_model or slug.removeprefix("openai/"),
        },
    }


def test_official_resolution_rejects_internal_display_name_collision_before_io():
    catalog = {
        "models": [
            {
                **_catalog_row("gpt-5.6-terra"),
                "id": "codex-auto-review",
                "display_name": "GPT-5.6 Terra",
            }
        ]
    }

    with patch("codex_proxy.existing_generated_catalog_path", return_value=Path("missing.json")), patch(
        "codex_proxy.load_catalog_models", return_value=catalog["models"]
    ), patch("codex_proxy.urlopen") as urlopen:
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream("openai/gpt-5.6-terra")

    assert failure.value.classification == "local_resolution_failure"
    assert failure.value.reason == "internal_model"
    urlopen.assert_not_called()


def test_duplicate_canonical_catalog_slug_is_catalog_inconsistency():
    rows = [_catalog_row("gpt-5.6-terra"), _catalog_row("openai/gpt-5.6-terra")]

    with patch("codex_proxy.existing_generated_catalog_path", return_value=Path("missing.json")), patch(
        "codex_proxy.load_catalog_models", return_value=rows
    ):
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream("gpt-5.6-terra")

    assert failure.value.classification == "catalog_inconsistency"
    assert failure.value.reason == "duplicate_canonical_slug"


@pytest.mark.parametrize("malformed_slug", [None, 42, "   "])
def test_malformed_catalog_slug_is_catalog_inconsistency(malformed_slug):
    row = {**_catalog_row("gpt-5.6-terra"), "slug": malformed_slug}

    with patch("codex_proxy.existing_generated_catalog_path", return_value=Path("missing.json")), patch(
        "codex_proxy.load_catalog_models", return_value=[row]
    ):
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream("gpt-5.6-terra")

    assert failure.value.classification == "catalog_inconsistency"
    assert failure.value.reason == "missing_catalog_slug"


def test_malformed_catalog_document_is_catalog_inconsistency():
    with patch(
        "codex_proxy.existing_generated_catalog_path",
        return_value=Path("malformed.json"),
    ), patch(
        "codex_proxy.load_catalog_models",
        side_effect=ValueError("invalid catalog document"),
    ):
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream("gpt-5.6-terra")

    assert failure.value.classification == "catalog_inconsistency"
    assert failure.value.reason == "malformed_catalog"


@pytest.mark.parametrize("document", ["{not-json", {"models": {"slug": "bad"}}])
def test_malformed_catalog_file_is_catalog_inconsistency(tmp_path, document):
    path = tmp_path / "catalog.json"
    path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")

    with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
        codex_proxy.generated_catalog_by_slug(path)

    assert failure.value.classification == "catalog_inconsistency"
    assert failure.value.reason == "malformed_catalog"


def test_catalog_upstream_model_mismatch_is_catalog_inconsistency():
    row = _catalog_row("gpt-5.6-terra", upstream_model="codex-auto-review")

    with patch("codex_proxy.existing_generated_catalog_path", return_value=Path("missing.json")), patch(
        "codex_proxy.load_catalog_models", return_value=[row]
    ):
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream("gpt-5.6-terra")

    assert failure.value.classification == "catalog_inconsistency"
    assert failure.value.reason == "upstream_model_mismatch"


@pytest.mark.parametrize(
    "requested",
    [
        "minimax-cn/minimax-m3",  # configured alias for MiniMax-M3
        "GPT-5.6 Terra",  # display name
        "codex-auto-review",  # internal reviewer identity
        "ollama-cloud/gpt-5.6-terra",  # cross-catalog provider fallback
    ],
)
def test_presentation_or_cross_catalog_identity_never_selects_terra(requested: str):
    with patch("codex_proxy.resolve_external_model_alias", return_value={
        "alias": "minimax-cn/MiniMax-M3",
        "provider_alias": "minimax-cn",
        "upstream_name": "minimax_cn",
        "base_url": "https://example.test/v1",
        "api_key": "test-key",
        "upstream_model": "MiniMax-M3",
    }), patch("codex_proxy.urlopen") as urlopen:
        with pytest.raises(codex_proxy.ModelIdentityResolutionError):
            codex_proxy.choose_upstream(requested)

    urlopen.assert_not_called()


def test_external_provider_alias_is_not_a_routable_model_identity():
    external = {
        "alias": "minimax-cn/MiniMax-M3",
        "provider_alias": "minimax-cn",
        "upstream_name": "minimax_cn",
        "base_url": "https://example.test/v1",
        "api_key": "test-key",
        "upstream_model": "MiniMax-M3",
    }
    with patch("codex_proxy.resolve_external_model_alias", return_value=external):
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream("minimax-cn/minimax-m3")

    assert failure.value.reason == "model_alias_not_routable"
    assert failure.value.classification == "local_resolution_failure"


@pytest.mark.parametrize("requested", ["ollama-cloud/glm-5.2", "glm-5.2"])
def test_ollama_catalog_rejects_unsupported_or_cross_provider_rows_before_io(requested):
    catalog_slug = requested
    catalog = {
        catalog_slug: {
            "slug": catalog_slug,
            "supported_in_api": True,
            "codex_proxy_metadata": {
                "provider": "openai",
                "upstream_name": "official",
                "upstream_model": "codex-auto-review",
            },
        }
    }

    with (
        patch("codex_proxy.generated_catalog_by_slug", return_value=catalog),
        patch("codex_proxy.resolve_ollama_cloud_model", return_value=(False, None)),
        patch("codex_proxy.urlopen") as urlopen,
    ):
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream(requested)

    assert failure.value.classification == "catalog_inconsistency"
    assert failure.value.reason == "provider_mismatch"
    urlopen.assert_not_called()


def test_ollama_catalog_rejects_contradictory_upstream_metadata_before_io():
    catalog = {
        "ollama-cloud/glm-5.2": {
            "slug": "ollama-cloud/glm-5.2",
            "supported_in_api": True,
            "codex_proxy_metadata": {
                "provider": "ollama-cloud",
                "upstream_name": "ollama_cloud",
                "upstream_model": "codex-auto-review",
            },
        }
    }

    with (
        patch("codex_proxy.generated_catalog_by_slug", return_value=catalog),
        patch("codex_proxy.resolve_ollama_cloud_model", return_value=(False, None)),
        patch("codex_proxy.urlopen") as urlopen,
    ):
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream("ollama-cloud/glm-5.2")

    assert failure.value.classification == "catalog_inconsistency"
    assert failure.value.reason == "upstream_model_mismatch"
    urlopen.assert_not_called()


@pytest.mark.parametrize("requested", ["ollama-cloud/glm-5.2", "glm-5.2"])
def test_ollama_catalog_rejects_supported_in_api_false_before_io(requested):
    catalog = {
        requested: {
            "slug": requested,
            "supported_in_api": False,
        }
    }

    with (
        patch("codex_proxy.generated_catalog_by_slug", return_value=catalog),
        patch("codex_proxy.resolve_ollama_cloud_model", return_value=(False, None)),
        patch("codex_proxy.urlopen") as urlopen,
    ):
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream(requested)

    assert failure.value.reason == "unsupported_model"
    urlopen.assert_not_called()


def test_bare_ollama_catalog_fallback_includes_exact_upstream_identity():
    row = _catalog_row("glm-5.2", provider="ollama-cloud", upstream_model="glm-5.2")

    with (
        patch("codex_proxy._published_catalog_model", return_value=row),
        patch("codex_proxy.resolve_ollama_cloud_model", return_value=(False, None)),
        patch("codex_proxy.should_include_model", return_value=True),
    ):
        upstream = codex_proxy.choose_upstream("glm-5.2")

    assert upstream["provider_id"] == "ollama-cloud"
    assert upstream["model_id"] == "glm-5.2"
    assert upstream["upstream_model"] == "glm-5.2"


@pytest.mark.parametrize(
    ("field", "value"),
    [("visibility", "hide"), ("visibility", "future"), ("hidden", True)],
)
def test_ollama_catalog_rejects_non_listable_visibility_before_io(field, value):
    row = _catalog_row(
        "ollama-cloud/glm-5.2",
        provider="ollama-cloud",
        upstream_model="glm-5.2",
    )
    row[field] = value
    catalog = {"ollama-cloud/glm-5.2": row}

    with (
        patch("codex_proxy.generated_catalog_by_slug", return_value=catalog),
        patch("codex_proxy.resolve_ollama_cloud_model", return_value=(False, None)),
        patch("codex_proxy.urlopen") as urlopen,
    ):
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream("ollama-cloud/glm-5.2")

    assert failure.value.classification == "catalog_inconsistency"
    assert failure.value.reason == "unsupported_visibility"
    urlopen.assert_not_called()


def test_ollama_catalog_rejects_malformed_metadata_before_io():
    row = _catalog_row(
        "ollama-cloud/glm-5.2",
        provider="ollama-cloud",
        upstream_model="glm-5.2",
    )
    row["codex_proxy_metadata"] = "not-an-object"

    with (
        patch("codex_proxy.generated_catalog_by_slug", return_value={"ollama-cloud/glm-5.2": row}),
        patch("codex_proxy.resolve_ollama_cloud_model", return_value=(False, None)),
        patch("codex_proxy.urlopen") as urlopen,
    ):
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream("ollama-cloud/glm-5.2")

    assert failure.value.classification == "catalog_inconsistency"
    assert failure.value.reason == "malformed_metadata"
    urlopen.assert_not_called()


@pytest.mark.parametrize(
    ("requested", "field", "value"),
    [
        ("ollama-cloud/codex-auto-review", "alias", "ollama-cloud/codex-auto-review"),
        ("codex-auto-review", "model_id", "codex-auto-review"),
    ],
)
def test_ollama_runtime_rejects_internal_route_identity_before_io(requested, field, value):
    runtime_model = {
        "alias": "ollama-cloud/glm-5.2",
        "provider_alias": "ollama-cloud",
        "upstream_name": "ollama_cloud",
        "base_url": "https://ollama.example.test/v1",
        "api_key": "ollama-runtime-token",
        "upstream_model": "glm-5.2",
        field: value,
    }

    with (
        patch("codex_proxy.resolve_ollama_cloud_model", return_value=(True, runtime_model)),
        patch("codex_proxy.resolve_external_model_alias", return_value=None),
        patch("codex_proxy.urlopen") as urlopen,
    ):
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream(requested)

    assert failure.value.classification == "local_resolution_failure"
    assert failure.value.reason == "internal_model"
    urlopen.assert_not_called()


def test_external_runtime_rejects_provider_qualified_internal_alias_before_io():
    external = {
        "alias": "volc/codex-auto-review",
        "provider_alias": "volc",
        "upstream_name": "volcengine",
        "base_url": "https://example.test/v1",
        "api_key": "test-key",
        "upstream_model": "glm-5.2",
    }

    with (
        patch("codex_proxy.resolve_external_model_alias", return_value=external),
        patch("codex_proxy.urlopen") as urlopen,
    ):
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream("volc/codex-auto-review")

    assert failure.value.classification == "local_resolution_failure"
    assert failure.value.reason == "internal_model"
    urlopen.assert_not_called()


@pytest.mark.parametrize("requested", ["legacy-display", "ollama-cloud/legacy-display"])
def test_ollama_runtime_alias_is_presentation_only(requested):
    providers = [
        ProviderConfig(
            id="ollama-cloud",
            name="Ollama",
            base_url="https://ollama.example.test/v1",
            api_key="test-key",
            models=[ModelConfig(id="glm-5.2", aliases=("legacy-display",))],
        )
    ]
    _, index = build_ollama_cloud_model_index(providers, require_api_key=False)

    with (
        patch("codex_proxy.resolve_ollama_cloud_model", return_value=(True, index[requested])),
        patch("codex_proxy.urlopen") as urlopen,
    ):
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream(requested)

    assert failure.value.classification == "local_resolution_failure"
    assert failure.value.reason == "model_alias_not_routable"
    urlopen.assert_not_called()


def test_provider_model_index_rejects_duplicate_exact_ids_and_alias_collisions():
    providers = [
        ProviderConfig(
            id="volc",
            name="Volc",
            base_url="https://example.test/v1",
            api_key="test-key",
            models=[ModelConfig(id="glm-5.2"), ModelConfig(id="glm-5.2")],
        )
    ]
    with pytest.raises(ValueError, match="duplicate provider model identity"):
        build_external_model_index(providers)

    providers[0].models = [
        ModelConfig(id="glm-5.2", aliases=("shared",)),
        ModelConfig(id="other", aliases=("shared",)),
    ]
    with pytest.raises(ValueError, match="duplicate provider model identity"):
        build_external_model_index(providers)


def test_ollama_cloud_index_rejects_duplicate_exact_ids():
    providers = [
        ProviderConfig(
            id="ollama-cloud",
            name="Ollama",
            base_url="https://example.test/v1",
            api_key="test-key",
            models=[ModelConfig(id="glm-5.2"), ModelConfig(id="glm-5.2")],
        )
    ]
    with pytest.raises(ValueError, match="duplicate provider model identity"):
        build_ollama_cloud_model_index(providers)


def test_external_provider_index_failure_is_catalog_inconsistency_before_io():
    with patch(
        "codex_proxy.resolve_external_model_alias",
        side_effect=ValueError("duplicate provider model identity: foo/model"),
    ), patch("codex_proxy.urlopen") as urlopen:
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream("foo/model")

    assert failure.value.classification == "catalog_inconsistency"
    assert failure.value.reason == "provider_model_index_inconsistency"
    urlopen.assert_not_called()


def test_ollama_provider_index_failure_is_catalog_inconsistency_before_io():
    with patch(
        "codex_proxy.resolve_ollama_cloud_model",
        side_effect=ValueError("duplicate provider model identity: glm-5.2"),
    ), patch("codex_proxy.urlopen") as urlopen:
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.choose_upstream("ollama-cloud/glm-5.2")

    assert failure.value.classification == "catalog_inconsistency"
    assert failure.value.reason == "provider_model_index_inconsistency"
    urlopen.assert_not_called()


def test_route_plan_rejects_missing_upstream_identity_without_substitution():
    with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
        codex_proxy.route_plan_for_request(
            {"name": "official", "auth": "codex_auth"},
            {"client_id": "unknown"},
            inbound_format="responses",
            model_requested="gpt-5.6-terra",
            canonical_route_model="gpt-5.6-terra",
        )

    assert failure.value.classification == "local_resolution_failure"
    assert failure.value.reason == "missing_upstream_model"


def test_route_plan_retains_exact_provider_id_and_model_slug():
    plan = codex_proxy.route_plan_for_request(
        {
            "name": "volcengine",
            "provider_id": "volc",
            "upstream_model": "glm-5.2",
            "auth": "api_key",
            "base_url": "https://example.test/v1",
            "upstream_format": "responses",
        },
        {"client_id": "unknown"},
        inbound_format="responses",
        model_requested="volc/glm-5.2",
        canonical_route_model="volc/glm-5.2",
    )

    assert plan.provider_id == "volc"
    assert plan.model_requested == "volc/glm-5.2"
    assert plan.canonical_model == "volc/glm-5.2"
    assert plan.upstream_model == "glm-5.2"


def test_route_plan_preserves_explicit_provider_id_underscores():
    plan = codex_proxy.route_plan_for_request(
        {
            "name": "custom_transport",
            "provider_id": "foo_bar",
            "upstream_model": "model-v1",
            "auth": "api_key",
            "base_url": "https://example.test/v1",
            "upstream_format": "responses",
        },
        {"client_id": "unknown"},
        inbound_format="responses",
        model_requested="foo_bar/model-v1",
        canonical_route_model="foo_bar/model-v1",
    )

    assert plan.provider_id == "foo_bar"


def test_route_plan_rejects_mismatched_upstream_without_model_id():
    with patch("codex_proxy.urlopen") as urlopen:
        with pytest.raises(codex_proxy.ModelIdentityResolutionError) as failure:
            codex_proxy.route_plan_for_request(
                {
                    "name": "volcengine",
                    "provider_id": "volc",
                    "upstream_model": "codex-auto-review",
                    "auth": "api_key",
                    "base_url": "https://example.test/v1",
                    "upstream_format": "responses",
                },
                {"client_id": "unknown"},
                inbound_format="responses",
                model_requested="volc/glm-5.2",
                canonical_route_model="volc/glm-5.2",
            )

    assert failure.value.classification == "catalog_inconsistency"
    assert failure.value.reason == "configured_model_mismatch"
    urlopen.assert_not_called()


def test_identity_error_payload_distinguishes_catalog_and_local_resolution():
    catalog_error = codex_proxy.ModelIdentityResolutionError(
        "duplicate canonical slug",
        classification="catalog_inconsistency",
        reason="duplicate_canonical_slug",
    )
    local_error = codex_proxy.ModelIdentityResolutionError(
        "model is not supported",
        classification="local_resolution_failure",
        reason="unsupported_model",
    )

    catalog_payload = codex_proxy._codexhub_error_payload(
        source="gateway",
        message=str(catalog_error),
        status=400,
        exc=catalog_error,
        error_type="model_identity_error",
        error="ModelIdentityResolutionError",
    )
    local_payload = codex_proxy._codexhub_error_payload(
        source="gateway",
        message=str(local_error),
        status=400,
        exc=local_error,
        error_type="model_identity_error",
        error="ModelIdentityResolutionError",
    )

    assert catalog_payload["code"] == "catalog.inconsistency"
    assert catalog_payload["details"]["classification"] == "catalog_inconsistency"
    assert local_payload["code"] == "gateway.model_resolution"
    assert local_payload["details"]["classification"] == "local_resolution_failure"
    assert "codex-auto-review" not in json.dumps(catalog_payload)


def test_identity_error_payload_drops_untrusted_provider_and_model_labels():
    failure = codex_proxy.ModelIdentityResolutionError(
        "model identity is invalid",
        classification="local_resolution_failure",
        reason="unsupported_model",
        provider_id="Bearer SECRET",
        model_slug="Bearer SECRET",
    )

    payload = codex_proxy._codexhub_error_payload(
        source="gateway",
        message="model identity is invalid",
        status=400,
        exc=failure,
        error_type="model_identity_error",
        error="ModelIdentityResolutionError",
    )

    rendered = json.dumps(payload)
    assert "SECRET" not in rendered
    assert "provider_id" not in payload["details"]
    assert "model_slug" not in payload["details"]


def test_handler_rejects_identity_before_auth_or_upstream_io():
    handler, fake = post_handler(
        "/v1/responses",
        json.dumps({"model": "openai/codex-auto-review", "input": []}).encode(),
    )
    failure = codex_proxy.ModelIdentityResolutionError(
        "model identity is internal and cannot be routed",
        classification="local_resolution_failure",
        reason="internal_model",
        provider_id="openai",
        model_slug="codex-auto-review",
    )

    with (
        patch("codex_proxy.choose_upstream", side_effect=failure),
        patch("codex_proxy.materialize_operational_authentication") as auth,
        patch("codex_proxy._open_upstream_response") as open_upstream,
    ):
        codex_proxy.CodexProxyHandler._proxy_post_request(handler, inbound_format="responses")

    assert fake.status == 400
    payload = json.loads(fake.wfile.writes[-1].decode())
    assert payload["codexhub_error"]["code"] == "gateway.model_resolution"
    assert payload["codexhub_error"]["details"]["reason"] == "internal_model"
    auth.assert_not_called()
    open_upstream.assert_not_called()


def test_handler_identity_error_detail_uses_bounded_non_secret_reason():
    handler, fake = post_handler(
        "/v1/responses",
        json.dumps({"model": "openai/secret-model", "input": []}).encode(),
    )
    failure = codex_proxy.ModelIdentityResolutionError(
        "Bearer SECRET should never be returned",
        classification="local_resolution_failure",
        reason="unsupported_model",
        provider_id="openai",
        model_slug="secret-model",
    )

    with patch("codex_proxy.choose_upstream", side_effect=failure):
        codex_proxy.CodexProxyHandler._proxy_post_request(handler, inbound_format="responses")

    rendered = fake.wfile.writes[-1].decode()
    assert "SECRET" not in rendered
    assert "Bearer " not in rendered
    assert "unsupported_model" in rendered
