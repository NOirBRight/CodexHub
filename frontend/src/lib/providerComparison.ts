import type { Model, Provider } from "./types";
import { normalizeProviderEndpointSelection } from "./providerEndpoint";
import { normalizeModel } from "./providerModel";

/**
 * Normalize a Provider for semantic comparison. Both the persisted baseline
 * and the working draft pass through this boundary so that omitted fields and
 * their persisted default equivalents compare as equal. Model array ordering
 * is preserved because it carries priority.
 */
export function normalizeProviderForComparison(provider: Provider): Provider {
  const endpointNormalized = normalizeProviderEndpointSelection(provider);
  return {
    ...endpointNormalized,
    api_key: endpointNormalized.api_key ?? null,
    tool_surface_strategy: endpointNormalized.tool_surface_strategy ?? null,
    reports_cached_input_tokens: endpointNormalized.reports_cached_input_tokens ?? null,
    display_prefix: endpointNormalized.display_prefix ?? null,
    sort_order: endpointNormalized.sort_order ?? null,
    locked: endpointNormalized.locked ?? false,
    models: endpointNormalized.models.map((model, index) => normalizeModelForComparison(model, index)),
  };
}

/**
 * Returns true when the draft has a material, persistable configuration
 * difference from the persisted baseline. Omitted fields, default
 * equivalents, and normalization side-effects do not produce dirty state.
 * Model ordering is meaningful and a reorder does produce dirty state.
 */
export function isProviderDirty(baseline: Provider, draft: Provider): boolean {
  return serializeProvider(baseline) !== serializeProvider(draft);
}

function normalizeModelForComparison(model: Model, index: number): Model {
  const normalized = normalizeModel(model);
  return {
    ...normalized,
    display_name: normalized.display_name ?? null,
    upstream_model: normalized.upstream_model ?? null,
    tool_surface_strategy: normalized.tool_surface_strategy ?? null,
    source_kind: normalized.source_kind ?? null,
    locked: normalized.locked ?? false,
    codex_enabled: normalized.codex_enabled ?? true,
    gateway_exported: normalized.gateway_exported ?? true,
    max_context_window: normalized.max_context_window ?? null,
    effective_source: normalized.effective_source ?? null,
    max_source: normalized.max_source ?? null,
    confidence: normalized.confidence ?? null,
    verified_at: normalized.verified_at ?? null,
    max_output_tokens: normalized.max_output_tokens ?? null,
    sort_order: index + 1,
    pricing: normalized.pricing ?? null,
    metadata_provenance: normalized.metadata_provenance ?? null,
  };
}

function serializeProvider(provider: Provider): string {
  return JSON.stringify(canonicalizeForSerialization(normalizeProviderForComparison(provider)));
}

/**
 * JSON stringification preserves insertion order for object keys. Canonicalize
 * every object before comparison so equivalent persisted data has one stable
 * representation, while keeping array order because model priority is meaningful.
 */
function canonicalizeForSerialization(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => item === undefined ? null : canonicalizeForSerialization(item));
  }
  if (!value || typeof value !== "object") {
    return value;
  }

  const source = value as Record<string, unknown>;
  const canonical: Record<string, unknown> = {};
  for (const key of Object.keys(source).sort()) {
    const nestedValue = source[key];
    if (nestedValue !== undefined) {
      canonical[key] = canonicalizeForSerialization(nestedValue);
    }
  }
  return canonical;
}
