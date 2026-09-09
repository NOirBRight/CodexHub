import i18n from "../i18n";
import type { Model, Provider } from "./types";

export function cx(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(" ");
}

export function formatLimit(value?: number | null) {
  if (!value) {
    return i18n.t("common.unknown");
  }
  return new Intl.NumberFormat(i18n.language || "en-US").format(value);
}

export function formatContextWindow(value?: number | null) {
  if (!value) {
    return i18n.t("common.unknown");
  }
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1)}M`;
  }
  if (value >= 1000) {
    const rounded = Math.round(value / 1000);
    return `${new Intl.NumberFormat(i18n.language || "en-US").format(rounded)}K`;
  }
  return new Intl.NumberFormat(i18n.language || "en-US").format(value);
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

function positiveLimit(value?: number | null) {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : undefined;
}

export function fillMissingModelLimits(models: Model[], fallback: Model[]) {
  const byId = new Map(fallback.map((model) => [model.id, model]));
  return models.map((model) => {
    const prior = byId.get(model.id);
    if (!prior) {
      return model;
    }
    return {
      ...model,
      context_window: positiveLimit(model.context_window) ?? prior.context_window ?? model.context_window,
      max_context_window:
        positiveLimit(model.max_context_window) ?? prior.max_context_window ?? model.max_context_window,
      max_output_tokens:
        positiveLimit(model.max_output_tokens) ?? prior.max_output_tokens ?? model.max_output_tokens,
    };
  });
}

export function mergeDiscoveredModels(existing: Model[], discovered: Model[]) {
  const existingById = new Map(existing.map((model) => [model.id, model]));
  const merged: Model[] = discovered.map((model, index) => {
    const previous = existingById.get(model.id);
    return {
      ...model,
      display_name: previous?.display_name ?? model.display_name ?? null,
      upstream_model: previous?.upstream_model ?? model.upstream_model ?? null,
      tool_surface_strategy: previous?.tool_surface_strategy ?? model.tool_surface_strategy ?? null,
      input_modalities: previous?.input_modalities ?? model.input_modalities ?? null,
      context_window: positiveLimit(model.context_window) ?? null,
      max_context_window: positiveLimit(model.max_context_window) ?? null,
      max_output_tokens: positiveLimit(model.max_output_tokens) ?? null,
      supported_reasoning_levels:
        nonempty(model.supported_reasoning_levels) ??
        nonempty(previous?.supported_reasoning_levels) ??
        null,
      default_reasoning_level:
        model.default_reasoning_level ?? previous?.default_reasoning_level ?? null,
      source_kind: previous?.source_kind ?? model.source_kind ?? null,
      locked: previous?.locked ?? model.locked ?? false,
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

function nonempty(values?: string[] | null): string[] | null | undefined {
  return values && values.length > 0 ? values : undefined;
}
