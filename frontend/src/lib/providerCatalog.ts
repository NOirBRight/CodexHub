import type { Model, Provider } from "./types";

export function bundledPresetFor(providerId: string, presets: Provider[]): Provider | undefined {
  return presets.find((preset) => preset.id === providerId);
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
  };
}

function mergeOfficialModelDefaults(model: Model, official: Model): Model {
  return {
    ...model,
    supported_reasoning_levels: (model.supported_reasoning_levels ?? []).length
      ? model.supported_reasoning_levels
      : official.supported_reasoning_levels,
    default_reasoning_level: model.default_reasoning_level ?? official.default_reasoning_level ?? null,
    thinking_mode: model.thinking_mode ?? official.thinking_mode ?? null,
    input_modalities: (official.input_modalities ?? []).includes("image") &&
      !(model.input_modalities ?? []).includes("image")
      ? official.input_modalities
      : model.input_modalities?.length
        ? model.input_modalities
        : official.input_modalities ?? model.input_modalities,
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

export function mergeOfficialPresetModels(existing: Model[], presetModels: Model[]): Model[] {
  if (existing.length === 0) {
    return presetModels.map((model) => ({ ...model }));
  }
  const seen = new Set(existing.map((model) => model.id));
  const merged = existing.map((model) => {
    const official = presetModels.find((candidate) => candidate.id === model.id);
    if (!official) {
      return model;
    }
    return mergeOfficialModelDefaults(model, official);
  });
  for (const official of presetModels) {
    if (!seen.has(official.id)) {
      merged.push({ ...official });
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
    ? mergeOfficialPresetModels(existing.models, preset.models)
    : existing.models;
  const needsModels =
    includeModels &&
    (mergedModels.length !== existing.models.length ||
      existing.models.some((model, index) => {
        const next = mergedModels[index];
        return (
          !next ||
          model.id !== next.id ||
          (model.supported_reasoning_levels ?? []).join() !== (next.supported_reasoning_levels ?? []).join() ||
          (model.default_reasoning_level ?? null) !== (next.default_reasoning_level ?? null) ||
          (model.thinking_mode ?? null) !== (next.thinking_mode ?? null) ||
          (model.input_modalities ?? []).join() !== (next.input_modalities ?? []).join()
        );
      }));
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
