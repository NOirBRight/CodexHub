"""Maintained Catalog: official model identity, family policy, and thinking payload.

This module is the capability source for Maintained Providers (ADR-0009). It is
not a Gateway owning module. catalog_sync and request-time compat read it at
call time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


THINKING_NONE = "none"
THINKING_ALWAYS_ON = "always_on"
THINKING_TOGGLE = "toggle"

MAINTAINED_PROVIDER_IDS = frozenset(
    {
        "ollama-cloud",
        "volc",
        "minimax-cn",
        "kimi",
        "kimi-cn",
        "commandcode",
        "opencode-go",
    }
)

KIMI_PROVIDER_IDS = frozenset({"kimi", "kimi-cn"})


@dataclass(frozen=True)
class FamilyPolicy:
    levels: tuple[str, ...]
    default_level: str | None
    thinking_mode: str
    input_modalities: tuple[str, ...] = ("text",)


@dataclass(frozen=True)
class MaintainedModel:
    provider_id: str
    id: str
    display_name: str
    family: str
    context_window: int
    max_output_tokens: int
    input_modalities: tuple[str, ...]
    reasoning_levels: tuple[str, ...]
    default_reasoning_level: str | None
    thinking_mode: str
    sort_order: int = 0
    aliases: tuple[str, ...] = ()
    tool_surface_strategy: str | None = None
    native_responses_tool_codec: str | None = None
    multi_agent_version: str | None = None


@dataclass(frozen=True)
class ThinkingPayload:
    drop_reasoning_effort: bool = False
    reasoning_effort: str | None = None
    thinking: Mapping[str, str] | None = None


_GENERIC = FamilyPolicy(
    levels=("low", "medium", "high", "xhigh", "max"),
    default_level=None,
    thinking_mode=THINKING_ALWAYS_ON,
)

FAMILIES: dict[str, FamilyPolicy] = {
    "glm-5.3": FamilyPolicy(("low", "high", "max"), "max", THINKING_ALWAYS_ON),
    "glm-5.3-flash": FamilyPolicy(
        ("low", "high", "max"), "max", THINKING_ALWAYS_ON, ("text", "image")
    ),
    "glm-5.2": FamilyPolicy(("high", "max"), "max", THINKING_ALWAYS_ON),
    "glm-5.1": FamilyPolicy(("high",), "high", THINKING_ALWAYS_ON),
    "kimi-k3": FamilyPolicy(("low", "high", "max"), "max", THINKING_ALWAYS_ON, ("text", "image")),
    "kimi-k2.7": FamilyPolicy((), None, THINKING_ALWAYS_ON, ("text", "image")),
    "kimi-k2.6": FamilyPolicy((), None, THINKING_TOGGLE, ("text", "image")),
    "minimax-m3": FamilyPolicy((), None, THINKING_TOGGLE, ("text", "image")),
    "minimax-m2": FamilyPolicy((), None, THINKING_ALWAYS_ON),
    "deepseek-v4-pro": FamilyPolicy(("low", "high", "max"), "high", THINKING_ALWAYS_ON),
    "deepseek-v4-flash": FamilyPolicy(("low", "high", "max"), "high", THINKING_ALWAYS_ON),
    "qwen3.5": FamilyPolicy(("high",), "high", THINKING_ALWAYS_ON, ("text", "image")),
    "qwen3.8": FamilyPolicy(("low", "medium", "xhigh"), "xhigh", THINKING_ALWAYS_ON, ("text", "image")),
    "claude": FamilyPolicy(("low", "medium", "high", "xhigh", "max"), "high", THINKING_ALWAYS_ON, ("text", "image")),
    "gpt-5": FamilyPolicy(("low", "medium", "high", "xhigh", "max"), "high", THINKING_ALWAYS_ON, ("text", "image")),
    "gemini": FamilyPolicy(("low", "medium", "high"), "high", THINKING_ALWAYS_ON, ("text", "image")),
    "grok": FamilyPolicy(("low", "medium", "high", "xhigh"), "high", THINKING_ALWAYS_ON, ("text", "image")),
    "muse": FamilyPolicy(("low", "medium", "high", "xhigh"), "xhigh", THINKING_ALWAYS_ON, ("text", "image")),
    "mimo": FamilyPolicy(("low", "medium", "xhigh"), "high", THINKING_ALWAYS_ON, ("text", "image")),
    "kimi-k2.5": FamilyPolicy(("low", "high", "max"), "max", THINKING_TOGGLE, ("text", "image")),
    "deepseek-v4-flash-vision": FamilyPolicy(("high", "max"), "high", THINKING_ALWAYS_ON, ("text", "image")),
    "gemma4": FamilyPolicy(("high",), "high", THINKING_ALWAYS_ON, ("text", "image")),
    "gpt-oss": FamilyPolicy(("low", "medium", "high"), "medium", THINKING_ALWAYS_ON),
    "nemotron-3-ultra": FamilyPolicy(("medium", "high"), "medium", THINKING_ALWAYS_ON),
    "nemotron-3-super": FamilyPolicy(("low", "high"), "low", THINKING_ALWAYS_ON),
    "nemotron-3-nano": FamilyPolicy(("low", "high"), "low", THINKING_ALWAYS_ON),
    "mistral-large-3": FamilyPolicy((), None, THINKING_NONE, ("text", "image")),
    "doubao": FamilyPolicy(("high",), "high", THINKING_ALWAYS_ON, ("text", "image")),
    "generic": _GENERIC,
}


def is_maintained_provider(provider_id: str | None) -> bool:
    return str(provider_id or "").strip() in MAINTAINED_PROVIDER_IDS


def ollama_model_basename(model_id: str) -> str:
    value = str(model_id or "").strip()
    slash = value.rfind("/")
    if slash >= 0:
        value = value[slash + 1 :]
    return value


def family_for(model_id: str) -> str:
    """Classify a wire id into a documented family, or generic."""
    identity = ollama_model_basename(model_id).lower()
    if identity.startswith("minimax-m3") or identity == "minimax-m3":
        return "minimax-m3"
    if identity.startswith("minimax-m2") or identity.startswith("minimax-m"):
        if identity.startswith("minimax-m3"):
            return "minimax-m3"
        return "minimax-m2"
    if identity.startswith("kimi-k3"):
        return "kimi-k3"
    if identity.startswith("kimi-k2.7"):
        return "kimi-k2.7"
    if identity.startswith("kimi-k2.6"):
        return "kimi-k2.6"
    if identity.startswith("kimi-k2.5"):
        return "kimi-k2.5"
    if identity.startswith("glm-5.3-flash"):
        return "glm-5.3-flash"
    if identity.startswith("glm-5.3"):
        return "glm-5.3"
    if identity.startswith("glm-5.2"):
        return "glm-5.2"
    if identity.startswith("glm-5.1"):
        return "glm-5.1"
    if identity.startswith("deepseek-v4-pro"):
        return "deepseek-v4-pro"
    if identity.startswith("deepseek-v4-flash"):
        if "vision" in identity:
            return "deepseek-v4-flash-vision"
        return "deepseek-v4-flash"
    if identity.startswith("claude"):
        return "claude"
    if identity.startswith("gpt-5"):
        return "gpt-5"
    if identity.startswith("gemini"):
        return "gemini"
    if identity.startswith("grok-"):
        return "grok"
    if identity.startswith("muse-"):
        return "muse"
    if identity.startswith("mimo-v2-omni") or (
        identity.startswith("mimo-v2.5") and "pro" not in identity
    ):
        return "mimo"
    if identity.startswith("gpt-oss"):
        return "gpt-oss"
    if identity.startswith("gemma4"):
        return "gemma4"
    if identity.startswith("nemotron-3-ultra"):
        return "nemotron-3-ultra"
    if identity.startswith("nemotron-3-super"):
        return "nemotron-3-super"
    if identity.startswith("nemotron-3-nano"):
        return "nemotron-3-nano"
    if identity.startswith("qwen3.8"):
        return "qwen3.8"
    if identity.startswith("qwen3.5"):
        return "qwen3.5"
    if identity.startswith("mistral-large-3"):
        return "mistral-large-3"
    if identity.startswith("doubao-seed") or identity.startswith("ark-code"):
        return "doubao"
    if identity.startswith("minimax"):
        return "minimax-m3" if "m3" in identity else "minimax-m2"
    return "generic"


def family_policy(model_id: str) -> FamilyPolicy:
    return FAMILIES.get(family_for(model_id), _GENERIC)


def _row(
    provider_id: str,
    model_id: str,
    display_name: str,
    context_window: int,
    max_output_tokens: int,
    sort_order: int,
    *,
    aliases: tuple[str, ...] = (),
    tool_surface_strategy: str | None = None,
    native_responses_tool_codec: str | None = None,
    multi_agent_version: str | None = None,
    reasoning_levels: tuple[str, ...] | None = None,
    default_reasoning_level: str | None = None,
    thinking_mode: str | None = None,
    input_modalities: tuple[str, ...] | None = None,
) -> MaintainedModel:
    family = family_for(model_id)
    policy = family_policy(model_id)
    levels = policy.levels if reasoning_levels is None else reasoning_levels
    default = policy.default_level if default_reasoning_level is None else default_reasoning_level
    if default is not None and default not in levels:
        default = levels[0] if levels else None
    return MaintainedModel(
        provider_id=provider_id,
        id=model_id,
        display_name=display_name,
        family=family,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        input_modalities=policy.input_modalities if input_modalities is None else input_modalities,
        reasoning_levels=levels,
        default_reasoning_level=default,
        thinking_mode=policy.thinking_mode if thinking_mode is None else thinking_mode,
        sort_order=sort_order,
        aliases=aliases,
        tool_surface_strategy=tool_surface_strategy,
        native_responses_tool_codec=native_responses_tool_codec,
        multi_agent_version=multi_agent_version,
    )


def _kimi_rows(provider_id: str, prefix: str) -> tuple[MaintainedModel, ...]:
    return (
        _row(provider_id, "kimi-k3", f"{prefix} K3", 1_048_576, 131_072, 1),
        _row(provider_id, "kimi-k2.7-code", f"{prefix} K2.7 Code", 262_144, 32_768, 2),
        _row(
            provider_id,
            "kimi-k2.7-code-highspeed",
            f"{prefix} K2.7 Code Highspeed",
            262_144,
            32_768,
            3,
        ),
        _row(provider_id, "kimi-k2.6", f"{prefix} K2.6", 262_144, 32_768, 4),
    )


_TEXT = ("text",)
_VISION = ("text", "image")
_ALL = ("low", "medium", "high", "xhigh", "max")
_FOUR = ("low", "medium", "high", "xhigh")
_THREE = ("low", "medium", "high")
_HIGH_MAX = ("high", "max")
_LOW_HIGH_MAX = ("low", "high", "max")
_LOW_MEDIUM_XHIGH = ("low", "medium", "xhigh")
_HIGH_XHIGH = ("high", "xhigh")


def _commandcode_rows() -> tuple[MaintainedModel, ...]:
    defaults = {
        "z-ai/glm-5.3-flash": "max",
        "zai-org/glm-5.3": "max",
        "zai-org/glm-5.2": "max",
        "gpt-5.6-sol": "high",
        "gpt-5.6-terra": "xhigh",
        "gpt-5.6-luna": "max",
    }
    specs: tuple[tuple[str, tuple[str, ...], int, int], ...] = (
        ("claude-sonnet-5", _ALL, 1_000_000, 128_000),
        ("claude-sonnet-4-6", _ALL, 1_000_000, 128_000),
        ("claude-fable-5", _ALL, 1_000_000, 128_000),
        ("claude-opus-5", _ALL, 1_000_000, 128_000),
        ("claude-opus-4-8", _ALL, 1_000_000, 128_000),
        ("claude-opus-4-7", _ALL, 1_000_000, 128_000),
        ("claude-haiku-4-5-20251001", _ALL, 200_000, 64_000),
        ("gpt-5.6-sol", _ALL, 1_048_576, 128_000),
        ("gpt-5.6-terra", _ALL, 1_048_576, 128_000),
        ("gpt-5.6-luna", _ALL, 1_050_000, 128_000),
        ("gpt-5.5", _FOUR, 256_000, 128_000),
        ("gpt-5.4", _FOUR, 256_000, 128_000),
        ("gpt-5.3-codex", _FOUR, 256_000, 128_000),
        ("gpt-5.4-mini", _THREE, 256_000, 64_000),
        ("deepseek/deepseek-v4-pro", _HIGH_MAX, 1_000_000, 384_000),
        ("deepseek/deepseek-v4-flash", _HIGH_MAX, 1_000_000, 384_000),
        ("deepseek/deepseek-v4-flash-vision-exp", _HIGH_MAX, 1_000_000, 384_000),
        ("moonshotai/kimi-k2.7-code", _LOW_HIGH_MAX, 262_144, 32_768),
        ("moonshotai/kimi-k2.7-code-highspeed", _LOW_HIGH_MAX, 262_144, 32_768),
        ("moonshotai/kimi-k2.6", _LOW_HIGH_MAX, 262_144, 32_768),
        ("moonshotai/kimi-k2.5", _LOW_HIGH_MAX, 262_144, 65_536),
        ("z-ai/glm-5.3-flash", _LOW_HIGH_MAX, 1_000_000, 131_072),
        ("zai-org/glm-5.3", _LOW_HIGH_MAX, 1_000_000, 131_072),
        ("zai-org/glm-5.2", _HIGH_MAX, 1_000_000, 131_072),
        ("minimax/minimax-m2.7-free", _LOW_MEDIUM_XHIGH, 204_800, 131_072),
        ("minimaxai/minimax-m2.5", _LOW_MEDIUM_XHIGH, 204_800, 131_072),
        ("xiaomi/mimo-v2.5-pro", _LOW_MEDIUM_XHIGH, 1_048_576, 128_000),
        ("xiaomi/mimo-v2.5", _LOW_MEDIUM_XHIGH, 1_000_000, 128_000),
        ("qwen/qwen3.8-max", _LOW_MEDIUM_XHIGH, 1_000_000, 131_072),
        ("qwen/qwen3.8-27b", _LOW_MEDIUM_XHIGH, 262_144, 65_536),
        ("qwen/qwen3.8-flash", _LOW_MEDIUM_XHIGH, 1_000_000, 65_536),
        ("stepfun/step-3.7-flash", _THREE, 256_000, 64_000),
        ("stepfun/step-3.5-flash", _THREE, 256_000, 64_000),
        ("tencent/hy3", _THREE, 256_000, 64_000),
        ("tencent/hy3-paid", _THREE, 256_000, 64_000),
        ("google/gemini-3.7-flash", _THREE, 1_000_000, 65_536),
        ("google/gemini-3.6-flash", _THREE, 1_000_000, 65_536),
        ("google/gemini-3.5-flash", _THREE, 1_000_000, 65_536),
        ("google/gemini-3.5-flash-lite", _THREE, 1_000_000, 65_536),
        ("google/gemini-3.1-flash-lite", _THREE, 1_000_000, 65_536),
        ("sakana/fugu-ultra", _HIGH_XHIGH, 256_000, 64_000),
        ("meta/muse-spark-1.1", _FOUR, 1_048_576, 131_072),
        ("meta/muse-spark-1.2", _FOUR, 1_048_576, 131_072),
        ("meta/muse-spark-1.2-contributor", _FOUR, 1_048_576, 131_072),
        ("xai/grok-4.5", _THREE, 500_000, 500_000),
        ("xai/grok-4.6", _FOUR, 500_000, 500_000),
    )
    rows: list[MaintainedModel] = []
    for index, (model_id, levels, context_window, max_output_tokens) in enumerate(specs, 1):
        leaf = model_id.split("/")[-1]
        default = defaults.get(model_id, levels[-1] if levels else None)
        rows.append(
            _row(
                "commandcode",
                model_id,
                f"Command Code {leaf}",
                context_window,
                max_output_tokens,
                index,
                reasoning_levels=levels,
                default_reasoning_level=default,
            )
        )
    return tuple(rows)


def _opencode_rows() -> tuple[MaintainedModel, ...]:
    specs: tuple[tuple[str, str, int, int, bool, str | None], ...] = (
        ("grok-4.6", "OpenCode Grok 4.6", 500_000, 500_000, True, "high"),
        ("grok-4.5", "OpenCode Grok 4.5", 500_000, 500_000, True, "high"),
        ("gpt-5.6-luna", "OpenCode GPT 5.6 Luna", 1_050_000, 128_000, True, "max"),
        ("muse-spark-1.2-contributor", "OpenCode Muse Spark 1.2 Contributor", 1_048_576, 131_072, True, "xhigh"),
        ("glm-5.3-flash", "OpenCode GLM-5.3 Flash", 1_000_000, 131_072, False, "high"),
        ("glm-5.3", "OpenCode GLM-5.3", 1_000_000, 131_072, False, "max"),
        ("glm-5.2", "OpenCode GLM-5.2", 1_000_000, 131_072, False, "max"),
        ("glm-5.1", "OpenCode GLM-5.1", 202_752, 32_768, False, "high"),
        ("glm-5", "OpenCode GLM-5", 202_752, 131_072, False, "high"),
        ("kimi-k3", "OpenCode Kimi K3", 1_048_576, 131_072, True, "high"),
        ("kimi-k2.7-code", "OpenCode Kimi K2.7 Code", 262_144, 262_144, True, None),
        ("kimi-k2.6", "OpenCode Kimi K2.6", 262_144, 65_536, True, None),
        ("kimi-k2.5", "OpenCode Kimi K2.5", 262_144, 65_536, True, "max"),
        ("longcat-2.0", "OpenCode LongCat 2.0", 1_000_000, 131_072, False, "high"),
        ("deepseek-v4-pro", "OpenCode DeepSeek V4 Pro", 1_000_000, 384_000, False, "max"),
        ("deepseek-v4-flash", "OpenCode DeepSeek V4 Flash", 1_000_000, 384_000, False, "max"),
        ("deepseek-v4-flash-vision-exp", "OpenCode DeepSeek V4 Flash Vision Exp", 1_000_000, 384_000, True, "max"),
        ("mimo-v2.5", "OpenCode MiMo V2.5", 1_000_000, 128_000, True, "high"),
        ("mimo-v2.5-pro", "OpenCode MiMo V2.5 Pro", 1_048_576, 128_000, False, "high"),
        ("mimo-v2-pro", "OpenCode MiMo V2 Pro", 1_048_576, 131_072, False, "high"),
        ("mimo-v2-omni", "OpenCode MiMo V2 Omni", 262_144, 65_536, True, "high"),
        ("hy3", "OpenCode Hy3", 256_000, 64_000, False, "high"),
        ("hy3-preview", "OpenCode Hy3 Preview", 256_000, 64_000, False, "high"),
        ("minimax-m3", "OpenCode MiniMax M3", 1_000_000, 131_072, True, "max"),
        ("minimax-m2.7", "OpenCode MiniMax M2.7", 204_800, 131_072, False, "max"),
        ("minimax-m2.5", "OpenCode MiniMax M2.5", 196_608, 131_072, False, "max"),
        ("qwen3.8-max", "OpenCode Qwen3.8 Max", 1_000_000, 131_072, True, "high"),
        ("qwen3.7-max", "OpenCode Qwen3.7 Max", 1_000_000, 65_536, False, "high"),
        ("qwen3.7-plus", "OpenCode Qwen3.7 Plus", 1_000_000, 65_536, True, "high"),
        ("qwen3.6-plus", "OpenCode Qwen3.6 Plus", 1_000_000, 65_536, True, "high"),
        ("qwen3.5-plus", "OpenCode Qwen3.5 Plus", 1_000_000, 65_536, True, "high"),
    )
    rows: list[MaintainedModel] = []
    for index, (model_id, name, context_window, max_output_tokens, vision, default) in enumerate(specs, 1):
        rows.append(
            _row(
                "opencode-go",
                model_id,
                name,
                context_window,
                max_output_tokens,
                index,
                input_modalities=_VISION if vision else _TEXT,
                default_reasoning_level=default,
            )
        )
    return tuple(rows)


# Endpoint numbers: Ollama Cloud library / current working toml; Volc Coding Plan
# live list (pi-provider/arkcli) when it disagrees with foundation-model cards;
# MiniMax and Kimi official docs.
_OFFICIAL: tuple[MaintainedModel, ...] = (
    # Ollama Cloud
    _row("ollama-cloud", "glm-5.3", "Ollama GLM-5.3", 1_048_576, 131_072, 1),
    _row("ollama-cloud", "glm-5.3-flash", "Ollama GLM-5.3 Flash", 1_000_000, 131_072, 2),
    _row(
        "ollama-cloud",
        "glm-5.2",
        "Ollama GLM-5.2",
        1_000_000,
        131_072,
        3,
        tool_surface_strategy="deferred_core",
        native_responses_tool_codec="strict_apply_patch",
        multi_agent_version="v2",
    ),
    _row("ollama-cloud", "glm-5.1", "Ollama GLM-5.1", 202_752, 131_072, 4),
    _row("ollama-cloud", "kimi-k3", "Ollama Kimi K3", 1_048_576, 131_072, 5),
    _row("ollama-cloud", "kimi-k2.7-code", "Ollama Kimi K2.7 Code", 262_144, 262_144, 6),
    _row("ollama-cloud", "kimi-k2.6", "Ollama Kimi K2.6", 262_144, 262_144, 7),
    _row("ollama-cloud", "minimax-m3", "Ollama MiniMax-M3", 512_000, 524_288, 8),
    _row("ollama-cloud", "minimax-m2.7", "Ollama MiniMax-M2.7", 196_608, 196_608, 9),
    _row("ollama-cloud", "deepseek-v4-pro", "Ollama DeepSeek V4 Pro", 1_048_576, 1_048_576, 10),
    _row("ollama-cloud", "deepseek-v4-flash", "Ollama DeepSeek V4 Flash", 1_048_576, 1_048_576, 11),
    _row("ollama-cloud", "qwen3.5:397b", "Ollama Qwen 3.5 397B", 262_144, 65_536, 12),
    _row("ollama-cloud", "gemma4:31b", "Ollama Gemma 4 31B", 262_144, 262_144, 13),
    _row("ollama-cloud", "gpt-oss:120b", "Ollama GPT-OSS 120B", 131_072, 32_768, 14),
    _row("ollama-cloud", "gpt-oss:20b", "Ollama GPT-OSS 20B", 131_072, 32_768, 15),
    _row("ollama-cloud", "nemotron-3-ultra", "Ollama Nemotron 3 Ultra", 262_144, 128_000, 16),
    _row("ollama-cloud", "nemotron-3-super", "Ollama Nemotron 3 Super", 262_144, 65_536, 17),
    _row("ollama-cloud", "nemotron-3-nano:30b", "Ollama Nemotron 3 Nano 30B", 1_048_576, 131_072, 18),
    _row("ollama-cloud", "mistral-large-3:675b", "Ollama Mistral Large 3", 262_144, 262_144, 19),
    # Volc Coding Plan
    _row("volc", "glm-5.3", "Volc GLM-5.3", 128_000, 32_000, 1),
    _row("volc", "doubao-seed-2.1-turbo", "Volc Doubao Seed 2.1 Turbo", 256_000, 256_000, 2),
    _row("volc", "doubao-seed-evolving", "Volc Doubao Seed Evolving", 1_000_000, 256_000, 3),
    _row("volc", "kimi-k2.7-code", "Volc Kimi K2.7 Code", 256_000, 32_000, 4),
    _row("volc", "minimax-m3", "Volc MiniMax-M3", 512_000, 128_000, 5),
    _row("volc", "deepseek-v4-pro", "Volc DeepSeek V4 Pro", 1_024_000, 384_000, 6),
    _row("volc", "deepseek-v4-flash", "Volc DeepSeek V4 Flash", 1_024_000, 384_000, 7),
    _row("volc", "doubao-seed-2.0-code", "Volc Doubao Seed 2.0 Code", 256_000, 256_000, 8),
    _row("volc", "doubao-seed-2.0-pro", "Volc Doubao Seed 2.0 Pro", 256_000, 256_000, 9),
    _row("volc", "doubao-seed-2.0-lite", "Volc Doubao Seed 2.0 Lite", 256_000, 128_000, 10),
    # MiniMax official
    _row("minimax-cn", "MiniMax-M3", "MiniMax M3", 1_000_000, 524_288, 1, aliases=("minimax-m3",)),
    _row("minimax-cn", "MiniMax-M2.7", "MiniMax M2.7", 204_800, 204_800, 2),
    _row("minimax-cn", "MiniMax-M2.7-highspeed", "MiniMax M2.7 Highspeed", 204_800, 204_800, 3),
    _row("minimax-cn", "MiniMax-M2.5", "MiniMax M2.5", 204_800, 204_800, 4),
    _row("minimax-cn", "MiniMax-M2.5-highspeed", "MiniMax M2.5 Highspeed", 204_800, 204_800, 5),
    _row("minimax-cn", "MiniMax-M2.1", "MiniMax M2.1", 204_800, 204_800, 6),
    _row("minimax-cn", "MiniMax-M2.1-highspeed", "MiniMax M2.1 Highspeed", 204_800, 204_800, 7),
    _row("minimax-cn", "MiniMax-M2", "MiniMax M2", 204_800, 204_800, 8),
    *_kimi_rows("kimi-cn", "Kimi CN"),
    *_kimi_rows("kimi", "Kimi"),
    *_commandcode_rows(),
    *_opencode_rows(),
)

_OFFICIAL_BY_PROVIDER: dict[str, tuple[MaintainedModel, ...]] = {}
_OFFICIAL_INDEX: dict[tuple[str, str], MaintainedModel] = {}
for _model in _OFFICIAL:
    _OFFICIAL_BY_PROVIDER.setdefault(_model.provider_id, ())
    _OFFICIAL_BY_PROVIDER[_model.provider_id] = _OFFICIAL_BY_PROVIDER[_model.provider_id] + (_model,)
    _OFFICIAL_INDEX[(_model.provider_id, _model.id)] = _model
    for _alias in _model.aliases:
        _OFFICIAL_INDEX[(_model.provider_id, _alias)] = _model


def official_models(provider_id: str) -> tuple[MaintainedModel, ...]:
    return _OFFICIAL_BY_PROVIDER.get(str(provider_id or "").strip(), ())


def resolve_model(provider_id: str | None, model_id: str | None) -> MaintainedModel | None:
    """Return the official row, or a family overlay for a maintained provider."""
    provider = str(provider_id or "").strip()
    wire_id = str(model_id or "").strip()
    if not provider or not wire_id:
        return None
    exact = _OFFICIAL_INDEX.get((provider, wire_id))
    if exact is not None:
        return exact
    if not is_maintained_provider(provider):
        return None
    family = family_for(wire_id)
    if family == "generic":
        return None
    policy = family_policy(wire_id)
    default = policy.default_level
    if default is not None and default not in policy.levels:
        default = policy.levels[0] if policy.levels else None
    return MaintainedModel(
        provider_id=provider,
        id=wire_id,
        display_name=wire_id,
        family=family,
        context_window=0,
        max_output_tokens=0,
        input_modalities=policy.input_modalities,
        reasoning_levels=policy.levels,
        default_reasoning_level=default,
        thinking_mode=policy.thinking_mode,
    )


def reasoning_levels_for(provider_id: str | None, model_id: str | None) -> tuple[str, ...]:
    resolved = resolve_model(provider_id, model_id)
    if resolved is None:
        return ()
    return resolved.reasoning_levels


def thinking_payload(
    provider_id: str | None,
    model_id: str | None,
    *,
    effort: str | None = None,
    thinking_enabled: bool | None = None,
) -> ThinkingPayload:
    """Map CodexHub/Codex controls onto the upstream thinking/effort fields."""
    resolved = resolve_model(provider_id, model_id)
    if resolved is None:
        return ThinkingPayload()
    family = resolved.family
    enabled = True if thinking_enabled is None else bool(thinking_enabled)

    if resolved.thinking_mode == THINKING_NONE:
        return ThinkingPayload(drop_reasoning_effort=True)

    if family == "kimi-k2.7":
        return ThinkingPayload(
            drop_reasoning_effort=True,
            thinking={"type": "enabled", "keep": "all"},
        )

    if family == "minimax-m2":
        return ThinkingPayload(drop_reasoning_effort=True)

    if family == "minimax-m3":
        if enabled:
            return ThinkingPayload(drop_reasoning_effort=True, thinking={"type": "adaptive"})
        return ThinkingPayload(drop_reasoning_effort=True, thinking={"type": "disabled"})

    if family == "kimi-k2.6":
        return ThinkingPayload(
            drop_reasoning_effort=True,
            thinking={"type": "enabled" if enabled else "disabled"},
        )

    if resolved.reasoning_levels:
        requested = str(effort or "").strip().lower() or None
        if requested in resolved.reasoning_levels:
            chosen = requested
        else:
            chosen = resolved.default_reasoning_level or resolved.reasoning_levels[0]
        return ThinkingPayload(reasoning_effort=chosen)

    return ThinkingPayload(drop_reasoning_effort=True)


def allowed_provider_slugs() -> tuple[str, ...]:
    """Return provider/model slugs for catalog_policy allowlists."""
    slugs: list[str] = []
    for model in _OFFICIAL:
        slugs.append(f"{model.provider_id}/{model.id}")
        for alias in model.aliases:
            slugs.append(f"{model.provider_id}/{alias}")
    return tuple(slugs)


def allowed_ollama_cloud_model_ids() -> tuple[str, ...]:
    return tuple(model.id for model in official_models("ollama-cloud"))


def display_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for model in _OFFICIAL:
        names[f"{model.provider_id}/{model.id}"] = model.display_name
        if model.provider_id == "ollama-cloud":
            names[model.id] = model.display_name
        for alias in model.aliases:
            names[f"{model.provider_id}/{alias}"] = model.display_name
    return names


def as_provider_model_fields(model: MaintainedModel) -> dict[str, Any]:
    """Fields a providers.toml model row should carry."""
    payload: dict[str, Any] = {
        "id": model.id,
        "display_name": model.display_name,
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "input_modalities": model.input_modalities,
        "supported_reasoning_levels": model.reasoning_levels,
        "default_reasoning_level": model.default_reasoning_level,
        "thinking_mode": model.thinking_mode,
        "sort_order": model.sort_order,
        "enabled": True,
    }
    if model.aliases:
        payload["aliases"] = model.aliases
    if model.tool_surface_strategy:
        payload["tool_surface_strategy"] = model.tool_surface_strategy
    if model.native_responses_tool_codec:
        payload["native_responses_tool_codec"] = model.native_responses_tool_codec
    if model.multi_agent_version:
        payload["multi_agent_version"] = model.multi_agent_version
    return payload
