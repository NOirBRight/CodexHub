import type { Model, Provider } from "./types";
import { sortModelsEnabledFirst } from "./modelDisplay";

const CODEX_FIVE_LEVEL_FILL = ["low", "medium", "high", "xhigh", "max"];

function sameLevelSet(left: string[] | null | undefined, right: string[] | null | undefined): boolean {
  const a = [...(left ?? [])].sort();
  const b = [...(right ?? [])].sort();
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

function isCodexFiveLevelFill(levels: string[] | null | undefined): boolean {
  return sameLevelSet(levels, CODEX_FIVE_LEVEL_FILL);
}

export function editorReasoningLevelOptions(catalogLevels?: string[] | null): string[] {
  return catalogLevels && catalogLevels.length > 0 ? catalogLevels : CODEX_FIVE_LEVEL_FILL;
}

export function bundledPresetFor(providerId: string, presets: Provider[]): Provider | undefined {
  return presets.find((preset) => preset.id === providerId);
}

export function presetMissingModel(preset: Provider | null | undefined, modelId: string) {
  return Boolean(preset && !preset.models.some((model) => model.id === modelId));
}

export function modelsMissingFromPreset(models: Model[], preset: Provider | null | undefined) {
  return Boolean(preset && models.some((model) => presetMissingModel(preset, model.id)));
}

export function usesSubscriptionAuth(preset: Provider | null | undefined): boolean {
  return subscriptionAuthAdapter(preset) !== null;
}

export function subscriptionAuthAdapter(preset: Provider | null | undefined): string | null {
  const capability = (preset?.auth_capabilities ?? []).find((candidate) =>
    candidate.startsWith("subscription:"),
  );
  return capability?.slice("subscription:".length) || null;
}

export function instantiateCatalogProvider(preset: Provider, sortOrder: number): Provider {
  return {
    ...preset,
    api_key: usesSubscriptionAuth(preset) ? null : preset.api_key ?? null,
    sort_order: sortOrder,
    enabled: true,
    models: sortModelsEnabledFirst(preset.models),
  };
}

function positiveLimit(value?: number | null) {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : undefined;
}

function keepLimit(current?: number | null, catalog?: number | null) {
  return positiveLimit(current) ?? positiveLimit(catalog) ?? current;
}

function mergeOfficialModelDefaults(model: Model, official: Model): Model {
  const currentLevels = model.supported_reasoning_levels ?? [];
  const catalogLevels = official.supported_reasoning_levels ?? [];
  const replaceGenericFill =
    catalogLevels.length > 0 &&
    isCodexFiveLevelFill(currentLevels) &&
    !isCodexFiveLevelFill(catalogLevels);
  const useCatalogLevels = catalogLevels.length > 0 && (currentLevels.length === 0 || replaceGenericFill);
  const nextLevels = useCatalogLevels ? catalogLevels : currentLevels.length ? currentLevels : catalogLevels;
  return {
    ...model,
    supported_reasoning_levels: nextLevels.length ? nextLevels : model.supported_reasoning_levels,
    default_reasoning_level: useCatalogLevels
      ? official.default_reasoning_level ?? model.default_reasoning_level ?? null
      : model.default_reasoning_level ?? official.default_reasoning_level ?? null,
    thinking_mode: model.thinking_mode ?? official.thinking_mode ?? null,
    input_modalities: (official.input_modalities ?? []).includes("image") &&
      !(model.input_modalities ?? []).includes("image")
      ? official.input_modalities
      : model.input_modalities?.length
        ? model.input_modalities
        : official.input_modalities ?? model.input_modalities,
    context_window: keepLimit(model.context_window, official.context_window),
    max_context_window: keepLimit(model.max_context_window, official.max_context_window),
    max_output_tokens: keepLimit(model.max_output_tokens, official.max_output_tokens),
  };
}

export function applyPresetReasoningDefaults(models: Model[], preset: Provider | null | undefined): Model[] {
  const byId = new Map((preset?.models ?? []).map((model) => [model.id, model]));
  return models.map((model) => {
    const official = byId.get(model.id);
    if (!official) {
      return model;
    }
    return mergeOfficialModelDefaults(model, official);
  });
}

export function applyCatalogModelDefaults(model: Model, catalog: Model | null | undefined): Model {
  if (!catalog) {
    return model;
  }
  return mergeOfficialModelDefaults(model, catalog);
}

export function mergeOfficialPresetModels(
  existing: Model[],
  presetModels: Model[],
  options?: { addMissing?: boolean },
): Model[] {
  const addMissing = options?.addMissing !== false;
  if (existing.length === 0) {
    return addMissing ? presetModels.map((model) => ({ ...model })) : [];
  }
  const seen = new Set(existing.map((model) => model.id));
  const merged = existing.map((model) => {
    const official = presetModels.find((candidate) => candidate.id === model.id);
    if (!official) {
      return model;
    }
    return mergeOfficialModelDefaults(model, official);
  });
  if (addMissing) {
    for (const official of presetModels) {
      if (!seen.has(official.id)) {
        merged.push({ ...official });
      }
    }
  }
  return merged;
}

export function applyCatalogPresetDefaults(
  existing: Provider,
  preset: Provider | null | undefined,
  options?: { includeModels?: boolean },
): Provider {
  if (!preset) {
    return existing;
  }
  const includeModels = options?.includeModels !== false;
  const needsBaseUrl = existing.base_url.trim() === "";
  const mergedModels = includeModels
    ? existing.models.length === 0
      ? sortModelsEnabledFirst(mergeOfficialPresetModels(existing.models, preset.models))
      : mergeOfficialPresetModels(existing.models, preset.models)
    : mergeOfficialPresetModels(existing.models, preset.models, { addMissing: false });
  const needsModels =
    mergedModels.length !== existing.models.length ||
    existing.models.some((model, index) => {
      const next = mergedModels[index];
      return (
        !next ||
        model.id !== next.id ||
        (model.supported_reasoning_levels ?? []).join() !== (next.supported_reasoning_levels ?? []).join() ||
        (model.default_reasoning_level ?? null) !== (next.default_reasoning_level ?? null) ||
        (model.thinking_mode ?? null) !== (next.thinking_mode ?? null) ||
        (model.input_modalities ?? []).join() !== (next.input_modalities ?? []).join() ||
        (model.context_window ?? null) !== (next.context_window ?? null) ||
        (model.max_context_window ?? null) !== (next.max_context_window ?? null) ||
        (model.max_output_tokens ?? null) !== (next.max_output_tokens ?? null)
      );
    });
  const needsFormats =
    !(existing.available_upstream_formats && existing.available_upstream_formats.length > 0) &&
    Boolean(preset.available_upstream_formats && preset.available_upstream_formats.length > 0);
  const needsPrefix = !existing.display_prefix && Boolean(preset.display_prefix);
  const needsCachedFlag =
    existing.reports_cached_input_tokens == null && preset.reports_cached_input_tokens != null;
  const needsAuthCapabilities =
    !(existing.auth_capabilities && existing.auth_capabilities.length > 0) &&
    Boolean(preset.auth_capabilities && preset.auth_capabilities.length > 0);
  const needsOnboardingHint = !existing.onboarding_hint && Boolean(preset.onboarding_hint);
  const needsDiscoveryPolicy = !existing.discovery_policy && Boolean(preset.discovery_policy);
  if (
    !needsBaseUrl &&
    !needsModels &&
    !needsFormats &&
    !needsPrefix &&
    !needsCachedFlag &&
    !needsAuthCapabilities &&
    !needsOnboardingHint &&
    !needsDiscoveryPolicy
  ) {
    return existing;
  }
  return {
    ...existing,
    base_url: needsBaseUrl ? preset.base_url : existing.base_url,
    display_prefix: needsPrefix ? preset.display_prefix : existing.display_prefix,
    upstream_format: existing.upstream_format,
    available_upstream_formats: needsFormats
      ? preset.available_upstream_formats
      : existing.available_upstream_formats,
    reports_cached_input_tokens: needsCachedFlag
      ? preset.reports_cached_input_tokens
      : existing.reports_cached_input_tokens,
    auth_capabilities: needsAuthCapabilities ? preset.auth_capabilities : existing.auth_capabilities,
    onboarding_hint: needsOnboardingHint ? preset.onboarding_hint : existing.onboarding_hint,
    discovery_policy: needsDiscoveryPolicy ? preset.discovery_policy : existing.discovery_policy,
    models: needsModels ? mergedModels : existing.models,
  };
}
