import type { Model, Provider } from "./types";

export function cx(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(" ");
}

export function formatLimit(value?: number | null) {
  if (!value) {
    return "Unknown";
  }
  return new Intl.NumberFormat("en-US").format(value);
}

export function displayModel(model: Model) {
  return model.display_name?.trim() || model.id;
}

export function slugify(value: string) {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
}

export function renumberProviders(providers: Provider[]) {
  return providers.map((provider, index) => ({
    ...provider,
    sort_order: index + 1,
  }));
}

export function renumberModels(models: Model[]) {
  return models.map((model, index) => ({
    ...model,
    sort_order: index + 1,
  }));
}

export function mergeDiscoveredModels(existing: Model[], discovered: Model[]) {
  const existingById = new Map(existing.map((model) => [model.id, model]));
  const merged: Model[] = discovered.map((model, index) => {
    const previous = existingById.get(model.id);
    return {
      ...model,
      display_name: previous?.display_name ?? model.display_name ?? null,
      upstream_model: previous?.upstream_model ?? model.upstream_model ?? null,
      input_modalities: previous?.input_modalities ?? model.input_modalities ?? null,
      supported_reasoning_levels:
        previous?.supported_reasoning_levels ?? model.supported_reasoning_levels ?? null,
      default_reasoning_level:
        previous?.default_reasoning_level ?? model.default_reasoning_level ?? null,
      source_kind: previous?.source_kind ?? model.source_kind ?? null,
      locked: previous?.locked ?? model.locked ?? false,
      hidden: previous?.hidden ?? model.hidden ?? false,
      codex_enabled: previous?.codex_enabled ?? model.codex_enabled ?? true,
      gateway_exported: previous?.gateway_exported ?? model.gateway_exported ?? true,
      pricing: previous?.pricing ?? model.pricing ?? null,
      metadata_provenance: previous?.metadata_provenance ?? model.metadata_provenance ?? null,
      enabled: previous?.enabled ?? true,
      sort_order: previous?.sort_order ?? index + 1,
    };
  });

  for (const model of existing) {
    if (!discovered.some((item) => item.id === model.id)) {
      merged.push(model);
    }
  }

  return renumberModels(merged);
}
