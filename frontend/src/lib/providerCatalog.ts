import type { Model, Provider } from "./types";

export function bundledPresetFor(providerId: string, presets: Provider[]): Provider | undefined {
  return presets.find((preset) => preset.id === providerId);
}

export function usesSubscriptionAuth(preset: Provider | null | undefined): boolean {
  return (preset?.auth_capabilities ?? []).some((capability) => capability.startsWith("subscription:"));
}

export function instantiateCatalogProvider(preset: Provider, sortOrder: number): Provider {
  return {
    ...preset,
    api_key: usesSubscriptionAuth(preset) ? null : preset.api_key ?? null,
    sort_order: sortOrder,
    enabled: true,
  };
}

export function applyPresetReasoningDefaults(models: Model[], preset: Provider | null | undefined): Model[] {
  const template = preset?.models.find((model) => (model.supported_reasoning_levels ?? []).length > 0);
  if (!template) {
    return models;
  }
  return models.map((model) => {
    if ((model.supported_reasoning_levels ?? []).length > 0) {
      return model;
    }
    return {
      ...model,
      supported_reasoning_levels: template.supported_reasoning_levels,
      default_reasoning_level: model.default_reasoning_level ?? template.default_reasoning_level ?? null,
      input_modalities: model.input_modalities?.length
        ? model.input_modalities
        : template.input_modalities ?? model.input_modalities,
    };
  });
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
  const needsModels = includeModels && existing.models.length === 0 && preset.models.length > 0;
  const needsFormats =
    !(existing.available_upstream_formats && existing.available_upstream_formats.length > 0) &&
    Boolean(preset.available_upstream_formats && preset.available_upstream_formats.length > 0);
  const needsPrefix = !existing.display_prefix && Boolean(preset.display_prefix);
  const needsCachedFlag =
    existing.reports_cached_input_tokens == null && preset.reports_cached_input_tokens != null;
  const needsAuthCapabilities =
    !(existing.auth_capabilities && existing.auth_capabilities.length > 0) &&
    Boolean(preset.auth_capabilities && preset.auth_capabilities.length > 0);
  const needsDiscoveryPolicy = !existing.discovery_policy && Boolean(preset.discovery_policy);
  if (
    !needsBaseUrl &&
    !needsModels &&
    !needsFormats &&
    !needsPrefix &&
    !needsCachedFlag &&
    !needsAuthCapabilities &&
    !needsDiscoveryPolicy
  ) {
    return existing;
  }
  return {
    ...existing,
    base_url: needsBaseUrl ? preset.base_url : existing.base_url,
    display_prefix: needsPrefix ? preset.display_prefix : existing.display_prefix,
    upstream_format: existing.upstream_format ?? preset.upstream_format,
    available_upstream_formats: needsFormats
      ? preset.available_upstream_formats
      : existing.available_upstream_formats,
    reports_cached_input_tokens: needsCachedFlag
      ? preset.reports_cached_input_tokens
      : existing.reports_cached_input_tokens,
    auth_capabilities: needsAuthCapabilities ? preset.auth_capabilities : existing.auth_capabilities,
    discovery_policy: needsDiscoveryPolicy ? preset.discovery_policy : existing.discovery_policy,
    models: needsModels ? preset.models.map((model) => ({ ...model })) : existing.models,
  };
}
