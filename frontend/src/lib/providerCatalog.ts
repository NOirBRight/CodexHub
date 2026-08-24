import type { Provider } from "./types";

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
  if (!needsBaseUrl && !needsModels && !needsFormats && !needsPrefix && !needsCachedFlag) {
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
    models: needsModels ? preset.models.map((model) => ({ ...model })) : existing.models,
  };
}
