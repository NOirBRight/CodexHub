from __future__ import annotations

import unittest
from unittest.mock import patch

from catalog import CatalogPolicy
from catalog_sync import build_codex_catalog, complete_third_party_reasoning_levels
import maintained_catalog
from maintained_catalog import (
    family_for,
    family_policy,
    is_maintained_provider,
    official_models,
    reasoning_levels_for,
    resolve_model,
    thinking_payload,
)


class MaintainedCatalogTests(unittest.TestCase):
    def test_kimi_k3_defaults_to_max_on_three_levels(self):
        model = resolve_model("kimi", "kimi-k3")
        assert model is not None
        self.assertEqual(model.reasoning_levels, ("low", "high", "max"))
        self.assertEqual(model.default_reasoning_level, "max")
        self.assertEqual(model.thinking_mode, "always_on")
        self.assertEqual(model.input_modalities, ("text", "image"))
        payload = thinking_payload("kimi", "kimi-k3")
        self.assertEqual(payload.reasoning_effort, "max")
        self.assertFalse(payload.drop_reasoning_effort)

    def test_kimi_k27_code_has_no_effort_grades(self):
        self.assertEqual(reasoning_levels_for("ollama-cloud", "kimi-k2.7-code"), ())
        model = resolve_model("kimi-cn", "kimi-k2.7-code-highspeed")
        assert model is not None
        self.assertEqual(model.thinking_mode, "always_on")
        payload = thinking_payload("volc", "kimi-k2.7-code", effort="high")
        self.assertTrue(payload.drop_reasoning_effort)
        self.assertIsNone(payload.reasoning_effort)
        self.assertEqual(payload.thinking, {"type": "enabled", "keep": "all"})

    def test_glm_52_is_high_max_not_five_levels(self):
        self.assertEqual(reasoning_levels_for("ollama-cloud", "glm-5.2"), ("high", "max"))
        self.assertEqual(resolve_model("ollama-cloud", "glm-5.2").default_reasoning_level, "max")

    def test_glm_53_has_low_high_max(self):
        self.assertEqual(reasoning_levels_for("volc", "glm-5.3"), ("low", "high", "max"))
        self.assertEqual(resolve_model("volc", "glm-5.3").default_reasoning_level, "max")

    def test_minimax_m3_is_toggle_without_effort_grades(self):
        model = resolve_model("minimax-cn", "MiniMax-M3")
        assert model is not None
        self.assertEqual(model.reasoning_levels, ())
        self.assertEqual(model.thinking_mode, "toggle")
        on_payload = thinking_payload("minimax-cn", "MiniMax-M3")
        self.assertEqual(on_payload.thinking, {"type": "adaptive"})
        off_payload = thinking_payload("minimax-cn", "MiniMax-M3", thinking_enabled=False)
        self.assertEqual(off_payload.thinking, {"type": "disabled"})

    def test_minimax_m2_family_cannot_disable_thinking(self):
        self.assertEqual(family_for("MiniMax-M2.7-highspeed"), "minimax-m2")
        payload = thinking_payload("minimax-cn", "MiniMax-M2.7", thinking_enabled=False)
        self.assertTrue(payload.drop_reasoning_effort)
        self.assertIsNone(payload.thinking)

    def test_volc_official_list_has_glm_53_not_glm_52(self):
        ids = [model.id for model in official_models("volc")]
        self.assertIn("glm-5.3", ids)
        self.assertNotIn("glm-5.2", ids)
        overlay = resolve_model("volc", "glm-5.2")
        assert overlay is not None
        self.assertEqual(overlay.reasoning_levels, ("high", "max"))

    def test_kimi_dual_presets_share_family_and_differ_by_prefix(self):
        cn = resolve_model("kimi-cn", "kimi-k3")
        global_row = resolve_model("kimi", "kimi-k3")
        assert cn is not None and global_row is not None
        self.assertEqual(cn.reasoning_levels, global_row.reasoning_levels)
        self.assertEqual(cn.display_name, "Kimi CN K3")
        self.assertEqual(global_row.display_name, "Kimi K3")

    def test_commandcode_includes_open_models_and_vision(self):
        ids = [model.id for model in official_models("commandcode")]
        self.assertIn("qwen/qwen3.8-max", ids)
        self.assertIn("qwen/qwen3.8-max-0902", ids)
        self.assertIn("google/gemini-3.7-flash", ids)
        self.assertIn("google/gemini-3.8-flash", ids)
        self.assertIn("moonshotai/kimi-k2.5", ids)
        self.assertIn("moonshotai/kimi-k3", ids)
        self.assertIn("xiaomi/mimo-v2.5", ids)
        self.assertIn("claude-fable-5-1", ids)
        self.assertIn("deepseek/deepseek-v4-flash-fast", ids)
        self.assertIn("tencent/hy4-preview", ids)
        self.assertIn("meta/muse-spark-1.3", ids)
        gpt = resolve_model("commandcode", "gpt-5.6-sol")
        assert gpt is not None
        self.assertEqual(gpt.display_name, "gpt-5.6-sol")
        self.assertFalse(gpt.display_name.startswith("Command Code"))
        self.assertEqual(gpt.input_modalities, ("text", "image"))
        self.assertEqual(gpt.default_reasoning_level, "high")
        glm = resolve_model("commandcode", "zai-org/glm-5.3")
        assert glm is not None
        self.assertEqual(glm.input_modalities, ("text",))
        self.assertEqual(glm.default_reasoning_level, "max")

    def test_opencode_go_uses_official_vision_flags(self):
        ids = [model.id for model in official_models("opencode-go")]
        self.assertIn("mimo-v2.5", ids)
        self.assertIn("qwen3.7-plus", ids)
        self.assertIn("qwen3.8-flash", ids)
        self.assertIn("hy4-preview", ids)
        self.assertIn("muse-spark-1.3-contributor", ids)
        self.assertIn("omen-alpha", ids)
        self.assertIn("deepseek-v4-flash-vision-exp", ids)
        self.assertIn("omen-alpha", ids)
        omen = resolve_model("opencode-go", "omen-alpha")
        assert omen is not None
        self.assertEqual(omen.context_window, 500_000)
        self.assertEqual(omen.max_output_tokens, 128_000)
        self.assertEqual(omen.input_modalities, ("text", "image"))
        self.assertEqual(omen.reasoning_levels, ("low", "high"))
        self.assertEqual(omen.default_reasoning_level, "high")
        flash = resolve_model("opencode-go", "glm-5.3-flash")
        assert flash is not None
        self.assertEqual(flash.input_modalities, ("text", "image"))
        self.assertEqual(flash.default_reasoning_level, "max")
        self.assertEqual(flash.reasoning_levels, ("low", "high", "max"))
        luna = resolve_model("opencode-go", "gpt-5.6-luna")
        assert luna is not None
        self.assertEqual(luna.input_modalities, ("text", "image"))
        self.assertEqual(luna.default_reasoning_level, "max")
        vision = resolve_model("opencode-go", "deepseek-v4-flash-vision-exp")
        assert vision is not None
        self.assertEqual(vision.input_modalities, ("text", "image"))
        glm5 = resolve_model("opencode-go", "glm-5")
        assert glm5 is not None
        self.assertEqual(glm5.family, "glm-5")
        self.assertEqual(glm5.reasoning_levels, ())
        self.assertEqual(glm5.thinking_mode, "toggle")
        longcat = resolve_model("opencode-go", "longcat-2.0")
        assert longcat is not None
        self.assertEqual(longcat.reasoning_levels, ())
        self.assertEqual(longcat.thinking_mode, "toggle")
        qwen37 = resolve_model("opencode-go", "qwen3.7-max")
        assert qwen37 is not None
        self.assertEqual(qwen37.family, "qwen3.7")
        self.assertEqual(qwen37.reasoning_levels, ())
        mimo_pro = resolve_model("opencode-go", "mimo-v2.5-pro")
        assert mimo_pro is not None
        self.assertEqual(mimo_pro.family, "mimo")
        self.assertEqual(mimo_pro.reasoning_levels, ("low", "medium", "xhigh"))
        self.assertEqual(mimo_pro.default_reasoning_level, "xhigh")
        hy3 = resolve_model("opencode-go", "hy3")
        assert hy3 is not None
        self.assertEqual(hy3.reasoning_levels, ("low", "medium", "high"))
        hy4 = resolve_model("opencode-go", "hy4-preview")
        assert hy4 is not None
        self.assertEqual(hy4.reasoning_levels, ("high",))
        qwen38 = resolve_model("opencode-go", "qwen3.8-max")
        assert qwen38 is not None
        self.assertEqual(qwen38.default_reasoning_level, "xhigh")
        five = ("low", "medium", "high", "xhigh", "max")
        for model_id in (
            "glm-5",
            "longcat-2.0",
            "qwen3.7-max",
            "qwen3.7-plus",
            "qwen3.6-plus",
            "mimo-v2.5-pro",
            "mimo-v2-pro",
            "hy3",
            "hy3-preview",
        ):
            model = resolve_model("opencode-go", model_id)
            assert model is not None
            self.assertNotEqual(model.family, "generic", model_id)
            self.assertNotEqual(model.reasoning_levels, five, model_id)

    def test_unknown_model_on_custom_provider_is_not_maintained(self):
        self.assertFalse(is_maintained_provider("custom-lab"))
        self.assertIsNone(resolve_model("custom-lab", "glm-5.2"))

    def test_xai_shares_grok_family_without_being_maintained(self):
        self.assertFalse(is_maintained_provider("xai"))
        grok46 = resolve_model("xai", "grok-4.6")
        grok45 = resolve_model("xai", "grok-4.5")
        assert grok46 is not None and grok45 is not None
        self.assertEqual(family_for("grok-4.6"), "grok")
        self.assertEqual(family_for("grok-4.5"), "grok-4.5")
        self.assertEqual(grok46.reasoning_levels, ("low", "medium", "high", "xhigh"))
        self.assertEqual(grok46.default_reasoning_level, "high")
        self.assertEqual(grok46.input_modalities, ("text", "image"))
        self.assertEqual(grok45.reasoning_levels, ("low", "medium", "high"))
        self.assertEqual(grok45.default_reasoning_level, "high")
        commandcode = resolve_model("commandcode", "xai/grok-4.6")
        assert commandcode is not None
        self.assertEqual(commandcode.default_reasoning_level, "high")
        self.assertEqual(commandcode.input_modalities, ("text", "image"))

    def test_family_defaults_stay_inside_declared_levels(self):
        self.assertEqual(family_for("mimo-v2.5-pro"), "mimo")
        self.assertEqual(family_for("mimo-v2-pro"), "mimo")
        self.assertEqual(family_for("qwen3.7-plus"), "qwen3.7")
        self.assertEqual(family_for("qwen3.6-plus"), "qwen3.6")
        self.assertEqual(family_for("hy4-preview"), "hy4")
        self.assertEqual(family_for("longcat-2.0"), "longcat")
        self.assertEqual(family_for("glm-5"), "glm-5")
        self.assertEqual(family_policy("mimo-v2.5-pro").default_level, "xhigh")
        qwen = resolve_model("opencode-go", "qwen3.8-max")
        assert qwen is not None
        self.assertEqual(qwen.default_reasoning_level, "xhigh")
        payload = thinking_payload("opencode-go", "longcat-2.0")
        self.assertTrue(payload.drop_reasoning_effort)
        self.assertEqual(payload.thinking, {"type": "enabled"})
        off = thinking_payload("opencode-go", "glm-5", thinking_enabled=False)
        self.assertEqual(off.thinking, {"type": "disabled"})

    def test_build_catalog_does_not_pad_declared_grok_levels_to_max(self):
        policy = CatalogPolicy(
            denied_models=set(),
            denied_substrings=set(),
            display_names={},
            official_models=(),
            allowed_ollama_cloud_models=(),
            allowed_provider_models=("xai/grok-4.6", "custom-lab/other"),
        )
        catalog = build_codex_catalog(
            [],
            [],
            policy,
            "0.142.0",
            external_models=[
                {
                    "alias": "xai/grok-4.6",
                    "provider_alias": "xai",
                    "upstream_name": "xai",
                    "display_prefix": "xAI",
                    "base_url": "https://api.x.ai/v1",
                    "api_key": "secret-test-key",
                    "upstream_model": "grok-4.6",
                    "priority_base": 200,
                    "context_window": 500000,
                    "max_output_tokens": 500000,
                    "input_modalities": ("text", "image"),
                    "supported_reasoning_levels": ("low", "medium", "high", "xhigh"),
                    "default_reasoning_level": "high",
                    "context_source": "providers_toml",
                    "max_output_source": "providers_toml",
                },
                {
                    "alias": "custom-lab/other",
                    "provider_alias": "custom-lab",
                    "upstream_name": "custom-lab",
                    "display_prefix": "Custom",
                    "base_url": "https://custom.example.test/v1",
                    "api_key": "secret-test-key",
                    "upstream_model": "other",
                    "priority_base": 300,
                    "context_window": 128000,
                    "max_output_tokens": 4096,
                    "input_modalities": ("text",),
                    "supported_reasoning_levels": ("high",),
                    "default_reasoning_level": "high",
                    "context_source": "providers_toml",
                    "max_output_source": "providers_toml",
                },
            ],
        )
        by_slug = {model["slug"]: model for model in catalog["models"]}
        self.assertEqual(
            [item["effort"] for item in by_slug["xai/grok-4.6"]["supported_reasoning_levels"]],
            ["low", "medium", "high", "xhigh"],
        )
        self.assertEqual(by_slug["xai/grok-4.6"]["default_reasoning_level"], "high")
        self.assertEqual(by_slug["xai/grok-4.6"]["input_modalities"], ["text", "image"])
        self.assertEqual(by_slug["xai/grok-4.6"]["context_window"], 500000)
        self.assertEqual(
            [item["effort"] for item in by_slug["custom-lab/other"]["supported_reasoning_levels"]],
            ["high"],
        )

    def test_build_catalog_applies_grok_family_when_runtime_row_omits_levels(self):
        policy = CatalogPolicy(
            denied_models=set(),
            denied_substrings=set(),
            display_names={},
            official_models=(),
            allowed_ollama_cloud_models=(),
            allowed_provider_models=("xai/grok-4.6",),
        )
        catalog = build_codex_catalog(
            [],
            [],
            policy,
            "0.142.0",
            external_models=[
                {
                    "alias": "xai/grok-4.6",
                    "provider_alias": "xai",
                    "upstream_name": "xai",
                    "display_prefix": "xAI",
                    "base_url": "https://api.x.ai/v1",
                    "api_key": "secret-test-key",
                    "upstream_model": "grok-4.6",
                    "priority_base": 200,
                    "context_window": 500000,
                    "max_output_tokens": 500000,
                    "input_modalities": ("text",),
                    "supported_reasoning_levels": (),
                    "default_reasoning_level": None,
                    "context_source": "providers_toml",
                    "max_output_source": "providers_toml",
                },
            ],
        )
        grok = next(model for model in catalog["models"] if model["slug"] == "xai/grok-4.6")
        self.assertEqual(
            [item["effort"] for item in grok["supported_reasoning_levels"]],
            ["low", "medium", "high", "xhigh"],
        )
        self.assertNotIn("max", [item["effort"] for item in grok["supported_reasoning_levels"]])
        self.assertEqual(grok["default_reasoning_level"], "high")
        self.assertEqual(grok["input_modalities"], ["text", "image"])

    def test_official_rows_have_consistent_defaults(self):
        for provider_id in ("ollama-cloud", "volc", "minimax-cn", "kimi", "kimi-cn"):
            rows = official_models(provider_id)
            self.assertGreater(len(rows), 0, provider_id)
            for model in rows:
                if model.reasoning_levels:
                    self.assertIn(model.default_reasoning_level, model.reasoning_levels, model.id)
                else:
                    self.assertIsNone(model.default_reasoning_level, model.id)
                policy = family_policy(model.id)
                self.assertEqual(model.reasoning_levels, policy.levels, model.id)
                self.assertEqual(model.thinking_mode, policy.thinking_mode, model.id)

    def test_complete_levels_does_not_fill_when_fill_missing_false(self):
        filled = complete_third_party_reasoning_levels(("high", "max"), fill_missing=False)
        self.assertEqual([item["effort"] for item in filled], ["high", "max"])
        empty = complete_third_party_reasoning_levels((), fill_missing=False)
        self.assertEqual(empty, [])

    def test_complete_levels_still_fills_custom_providers(self):
        filled = complete_third_party_reasoning_levels(("high",))
        self.assertEqual([item["effort"] for item in filled], ["low", "medium", "high", "xhigh", "max"])

    def test_build_catalog_uses_maintained_levels_for_volc_glm(self):
        policy = CatalogPolicy(
            denied_models=set(),
            denied_substrings=set(),
            display_names={},
            official_models=(),
            allowed_ollama_cloud_models=(),
            allowed_provider_models=("volc/glm-5.3",),
        )
        catalog = build_codex_catalog(
            [],
            [],
            policy,
            "0.142.0",
            external_models=[
                {
                    "alias": "volc/glm-5.3",
                    "provider_alias": "volc",
                    "upstream_name": "volcengine",
                    "display_prefix": "Volc",
                    "base_url": "https://ark.example.test/v1",
                    "api_key": "secret-test-key",
                    "upstream_model": "glm-5.3",
                    "priority_base": 200,
                    "context_window": 128000,
                    "max_output_tokens": 32000,
                    "input_modalities": ("text",),
                    "context_source": "providers_toml",
                    "max_output_source": "providers_toml",
                }
            ],
        )
        glm = next(model for model in catalog["models"] if model["slug"] == "volc/glm-5.3")
        self.assertEqual([item["effort"] for item in glm["supported_reasoning_levels"]], ["low", "high", "max"])
        self.assertEqual(glm["default_reasoning_level"], "max")

    def test_build_catalog_keeps_empty_levels_for_k27(self):
        policy = CatalogPolicy(
            denied_models=set(),
            denied_substrings=set(),
            display_names={},
            official_models=(),
            allowed_ollama_cloud_models=(),
            allowed_provider_models=("kimi/kimi-k2.7-code",),
        )
        catalog = build_codex_catalog(
            [],
            [],
            policy,
            "0.142.0",
            external_models=[
                {
                    "alias": "kimi/kimi-k2.7-code",
                    "provider_alias": "kimi",
                    "upstream_name": "kimi",
                    "display_prefix": "Kimi",
                    "base_url": "https://api.moonshot.ai/v1",
                    "api_key": "secret-test-key",
                    "upstream_model": "kimi-k2.7-code",
                    "priority_base": 400,
                    "context_window": 262144,
                    "max_output_tokens": 32768,
                    "input_modalities": ("text", "image"),
                    "context_source": "providers_toml",
                    "max_output_source": "providers_toml",
                }
            ],
        )
        kimi = next(model for model in catalog["models"] if model["slug"] == "kimi/kimi-k2.7-code")
        self.assertEqual(kimi["supported_reasoning_levels"], [])
        self.assertIsNone(kimi.get("default_reasoning_level"))


class MaintainedThinkingRequestTests(unittest.TestCase):
    def test_kimi_k3_keeps_reasoning_effort(self):
        from gateway_request import apply_maintained_thinking_controls, reasoning_param_is_unsupported

        self.assertFalse(reasoning_param_is_unsupported("kimi", "kimi-k3", "kimi-k3"))
        payload = {"model": "kimi-k3", "reasoning_effort": "high"}
        changed = apply_maintained_thinking_controls(payload, "kimi", "kimi-k3", "kimi-k3")
        self.assertFalse(changed)
        self.assertEqual(payload["reasoning_effort"], "high")

    def test_kimi_k27_drops_effort_and_pins_thinking(self):
        from gateway_request import apply_maintained_thinking_controls, reasoning_param_is_unsupported

        self.assertTrue(reasoning_param_is_unsupported("kimi", "kimi-k2.7-code", "kimi-k2.7-code"))
        payload = {
            "model": "kimi-k2.7-code",
            "reasoning_effort": "high",
            "reasoning": {"effort": "high"},
        }
        changed = apply_maintained_thinking_controls(payload, "kimi", "kimi-k2.7-code", "kimi-k2.7-code")
        self.assertTrue(changed)
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("reasoning", payload)
        self.assertEqual(payload["thinking"], {"type": "enabled", "keep": "all"})

    def test_minimax_m3_defaults_to_adaptive_thinking(self):
        from gateway_request import apply_maintained_thinking_controls

        payload = {"model": "MiniMax-M3"}
        changed = apply_maintained_thinking_controls(payload, "minimax_cn", "MiniMax-M3", "MiniMax-M3")
        self.assertTrue(changed)
        self.assertEqual(payload["thinking"], {"type": "adaptive"})

    def test_minimax_m3_respects_disabled_thinking(self):
        from gateway_request import apply_maintained_thinking_controls

        payload = {"model": "MiniMax-M3", "thinking": {"type": "disabled"}}
        apply_maintained_thinking_controls(payload, "minimax_cn", "MiniMax-M3", "MiniMax-M3")
        self.assertEqual(payload["thinking"], {"type": "disabled"})


class MaintainedThinkingCompatibilitySeamTests(unittest.TestCase):
    def test_owning_module_exposes_thinking_controls_at_call_time(self):
        import gateway_request

        payload = {"model": "kimi-k3"}
        with patch("gateway_request.apply_maintained_thinking_controls", return_value=True) as controls:
            self.assertTrue(
                gateway_request.apply_maintained_thinking_controls(
                    payload, "kimi", "kimi-k3", "kimi-k3"
                )
            )

        controls.assert_called_once_with(payload, "kimi", "kimi-k3", "kimi-k3")


class BundledMaintainedProvidersTests(unittest.TestCase):
    def test_bundled_catalog_has_kimi_dual_presets_and_volc_glm_53(self):
        from providers_config import DEFAULT_PROVIDERS_PATH, load_providers

        providers = {provider.id: provider for provider in load_providers(DEFAULT_PROVIDERS_PATH)}
        self.assertIn("kimi", providers)
        self.assertIn("kimi-cn", providers)
        self.assertFalse(providers["kimi"].supports_developer_role)
        self.assertFalse(providers["kimi-cn"].supports_developer_role)
        self.assertEqual(providers["kimi"].base_url, "https://api.moonshot.ai/v1")
        self.assertEqual(providers["kimi-cn"].base_url, "https://api.moonshot.cn/v1")
        volc_ids = [model.id for model in providers["volc"].models]
        self.assertIn("glm-5.3", volc_ids)
        self.assertNotIn("glm-5.2", volc_ids)
        kimi_ids = [model.id for model in providers["kimi"].models]
        self.assertEqual(kimi_ids, ["kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"])
        glm = next(model for model in providers["volc"].models if model.id == "glm-5.3")
        self.assertEqual(glm.supported_reasoning_levels, ("low", "high", "max"))
        self.assertEqual(glm.default_reasoning_level, "max")
        self.assertEqual(glm.thinking_mode, "always_on")
        commandcode_grok = next(
            model for model in providers["commandcode"].models if model.id == "xai/grok-4.6"
        )
        self.assertEqual(commandcode_grok.supported_reasoning_levels, ("low", "medium", "high", "xhigh"))
        self.assertEqual(commandcode_grok.default_reasoning_level, "high")
        self.assertEqual(commandcode_grok.input_modalities, ("text", "image"))
        opencode_grok45 = next(
            model for model in providers["opencode-go"].models if model.id == "grok-4.5"
        )
        self.assertEqual(opencode_grok45.supported_reasoning_levels, ("low", "medium", "high"))
        flash = next(
            model for model in providers["opencode-go"].models if model.id == "glm-5.3-flash"
        )
        self.assertEqual(flash.input_modalities, ("text", "image"))
        self.assertEqual(flash.supported_reasoning_levels, ("low", "high", "max"))
        self.assertEqual(flash.default_reasoning_level, "max")
        longcat = next(
            model for model in providers["opencode-go"].models if model.id == "longcat-2.0"
        )
        self.assertEqual(longcat.supported_reasoning_levels, ())
        self.assertEqual(longcat.thinking_mode, "toggle")
        omen = next(model for model in providers["opencode-go"].models if model.id == "omen-alpha")
        self.assertEqual(omen.context_window, 500000)
        self.assertEqual(omen.supported_reasoning_levels, ("low", "high"))
        self.assertIn("claude-fable-5-1", [model.id for model in providers["commandcode"].models])
        self.assertIn("moonshotai/kimi-k3", [model.id for model in providers["commandcode"].models])


if __name__ == "__main__":
    unittest.main()


def test_opencode_muse_13_matches_live_reasoning_contract():
    from maintained_catalog import resolve_model
    model = resolve_model("opencode-go", "muse-spark-1.3-contributor")
    assert model.default_reasoning_level == "xhigh"
    assert model.reasoning_levels == ("low", "medium", "high", "xhigh")
