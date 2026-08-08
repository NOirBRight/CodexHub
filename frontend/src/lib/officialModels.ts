import { normalizeOfficialModelId } from "./settings";
import type { Model } from "./types";

export const LEGACY_AUTOMATIC_OFFICIAL_MODEL_ORDER = [
  "gpt-5.5",
  "gpt-5.4",
  "gpt-5.4-mini",
  "gpt-5.3-codex-spark",
];
export const DEFAULT_OFFICIAL_MODEL_ORDER = [
  "gpt-5.6-sol",
  "gpt-5.6-terra",
  "gpt-5.6-luna",
  ...LEGACY_AUTOMATIC_OFFICIAL_MODEL_ORDER,
];


export function sortOfficialModels(models: Model[], sortOrder: string[]) {
  const order = new Map<string, number>();
  const effectiveOrder = shouldFollowOfficialCatalogOrder(sortOrder)
    ? DEFAULT_OFFICIAL_MODEL_ORDER
    : sortOrder;
  effectiveOrder.forEach((id, index) => {
    for (const key of officialModelSortKeys(id)) {
      order.set(key, index);
    }
  });
  return [...models].sort((left, right) => {
    const leftIndex = officialModelSortKeys(left.id).reduce(
      (current, key) => Math.min(current, order.get(key) ?? Number.MAX_SAFE_INTEGER),
      Number.MAX_SAFE_INTEGER,
    );
    const rightIndex = officialModelSortKeys(right.id).reduce(
      (current, key) => Math.min(current, order.get(key) ?? Number.MAX_SAFE_INTEGER),
      Number.MAX_SAFE_INTEGER,
    );
    if (leftIndex !== rightIndex) {
      return leftIndex - rightIndex;
    }
    return (left.sort_order ?? Number.MAX_SAFE_INTEGER) - (right.sort_order ?? Number.MAX_SAFE_INTEGER);
  });
}

export function shouldFollowOfficialCatalogOrder(currentOrder: string[]) {
  if (!currentOrder.length) {
    return true;
  }
  const normalized = currentOrder
    .map((id) => normalizeOfficialModelId(id))
    .filter((id): id is string => Boolean(id));
  let legacyIndex = 0;
  let sawNewModel = false;
  for (const id of normalized) {
    const index = LEGACY_AUTOMATIC_OFFICIAL_MODEL_ORDER.indexOf(id);
    if (index < 0) {
      sawNewModel = true;
      continue;
    }
    if (sawNewModel || index !== legacyIndex) {
      return false;
    }
    legacyIndex += 1;
  }
  return legacyIndex > 0;
}

export function refreshedOfficialModelOrder(currentOrder: string[], refreshedModels: Model[]) {
  const refreshedKeySets = refreshedModels.map((model) => new Set(officialModelSortKeys(model.id)));
  const nextOrder = currentOrder.filter((id) => {
    const keys = officialModelSortKeys(id);
    return refreshedKeySets.some((refreshedKeys) => keys.some((key) => refreshedKeys.has(key)));
  });
  const seen = new Set(nextOrder.flatMap(officialModelSortKeys));
  for (const model of refreshedModels) {
    const keys = officialModelSortKeys(model.id);
    if (keys.some((key) => seen.has(key))) {
      continue;
    }
    nextOrder.push(model.id);
    keys.forEach((key) => seen.add(key));
  }
  return nextOrder;
}

export function mergeOfficialModelSources(catalog: Model[], metadata: Model[]) {
  const knownOfficialIds = officialModelIdSet(catalog, metadata);
  const blockedOfficialIds = new Set<string>();
  for (const model of catalog) {
    if (isCatalogModelListable(model)) {
      continue;
    }
    for (const value of [model.id, model.upstream_model ?? "", ...(model.aliases ?? [])]) {
      const bare = value.trim().toLowerCase().replace(/^openai\//, "");
      if (bare.startsWith("gpt-")) {
        blockedOfficialIds.add(bare);
      }
    }
  }
  const resolvedCatalogLimitFields = [
    "context_window",
    "max_context_window",
    "effective_source",
    "max_source",
    "confidence",
    "verified_at",
    "multi_agent_version",
  ] as const;
  const merged = new Map<string, Model>();
  for (const model of catalog.filter((item) => isOfficialModel(item) && isCatalogModelListable(item))) {
    const canonicalId = normalizeOfficialModelId(model.id, knownOfficialIds);
    if (!canonicalId) {
      continue;
    }
    const existing = merged.get(canonicalId);
    merged.set(canonicalId, {
      ...existing,
      ...model,
      id: canonicalId,
      enabled: existing
        ? (existing.enabled ?? true) || (model.enabled ?? true)
        : model.enabled ?? true,
    });
  }
  const publishedCatalogModels = new Map(merged);
  for (const model of metadata.filter((item) => isOfficialModel(item) && isCatalogModelListable(item))) {
    const canonicalId = normalizeOfficialModelId(model.id, knownOfficialIds);
    if (
      !canonicalId ||
      blockedOfficialIds.has(canonicalId) ||
      !publishedCatalogModels.has(canonicalId)
    ) {
      continue;
    }
    const existing = merged.get(canonicalId);
    const mergedModel: Model = {
      ...existing,
      ...model,
      id: canonicalId,
      enabled: existing
        ? (existing.enabled ?? true) || (model.enabled ?? true)
        : model.enabled ?? true,
    };
    const catalogModel = publishedCatalogModels.get(canonicalId);
    if (catalogModel) {
      for (const field of resolvedCatalogLimitFields) {
        if (Object.prototype.hasOwnProperty.call(catalogModel, field)) {
          Object.assign(mergedModel, { [field]: catalogModel[field] });
        }
      }
    }
    merged.set(canonicalId, mergedModel);
  }
  return filterCodexVisibleOfficialModels(Array.from(merged.values()));
}

type OfficialCollaborationCapability = {
  baseline: "v1" | "v2";
  verdict: "GO" | "PARTIAL" | "NO-GO" | "UNQUALIFIED";
  candidate: string;
};

// This is the checked-in decision table produced by the exact CLI 0.146.1
// capability matrix.  A selector is exposed only for an accepted GO row;
// model/provider names alone never qualify a row.
const OFFICIAL_COLLABORATION_CAPABILITIES: Record<string, OfficialCollaborationCapability> = {
  "gpt-5.6-terra": { baseline: "v2", verdict: "GO", candidate: "adba39866581cd33be813c717a3a0598b15f5858" },
  "gpt-5.6-luna": { baseline: "v1", verdict: "GO", candidate: "adba39866581cd33be813c717a3a0598b15f5858" },
};

export function officialCollaborationVersionOptions(
  model: Model,
  explicitOverrides: Readonly<Record<string, "v1" | "v2">> = {},
) {
  const canonical = normalizeOfficialModelId(model.id);
  const capability = canonical ? OFFICIAL_COLLABORATION_CAPABILITIES[canonical] : undefined;
  if (
    !capability ||
    !canonical ||
    capability.verdict !== "GO" ||
    !isCatalogModelListable(model)
  ) {
    return null;
  }
  const baseline = capability.baseline;
  const explicit = explicitOverrides[canonical];
  // The managed catalog baseline is authoritative until the user selects a
  // model-level override.  A stale generated row must not silently promote a
  // different Collaboration version after restart or catalog refresh.
  const effective = explicit ?? baseline;
  if (effective !== "v1" && effective !== "v2") {
    return null;
  }
  return { baseline, effective, explicit: explicit ?? null, candidate: capability.candidate } as const;
}

export function resolveOfficialModelContextWindow(
  publishedContextWindow: number | null | undefined,
  gatewayContextWindow: number | null | undefined,
) {
  const valid = (value: number | null | undefined) =>
    typeof value === "number" && Number.isFinite(value) && value > 0 ? value : undefined;
  const published = valid(publishedContextWindow);
  const gateway = valid(gatewayContextWindow);
  if (published !== undefined && gateway !== undefined) {
    return Math.min(published, gateway);
  }
  return published ?? gateway;
}

export function officialModelIdSet(...groups: Model[][]) {
  const known = new Set<string>();
  for (const model of groups
    .flatMap((group) => group)
    .filter((item) => isOfficialModel(item) && isCatalogModelListable(item))) {
    const value = model.id.trim();
    const bare = value.startsWith("openai/gpt-") ? value.slice("openai/".length) : value;
    if (bare.startsWith("gpt-")) {
      known.add(bare);
    }
  }
  return known;
}

export function isOfficialModel(model: Model) {
  return model.id.startsWith("openai/") || model.id.startsWith("gpt-");
}

export function filterCodexVisibleOfficialModels(models: Model[]) {
  return models.filter(
    (model) => isCatalogModelListable(model) && !isOfficialGatewayFastVariant(model),
  );
}

export function isCatalogModelListable(model: Model) {
  if (model.visibility !== "list" || (model as Model & { hidden?: unknown }).hidden === true) {
    return false;
  }
  const identities = [model.id, model.upstream_model ?? "", ...(model.aliases ?? [])];
  return !identities.some((value) => {
    const normalized = value
      .trim()
      .toLowerCase()
      .replace(/^openai\//, "")
      .replace(/:cloud$/, "");
    return normalized === "codex-auto-review" || normalized.startsWith("codex-auto-review/");
  });
}

export function isOfficialGatewayFastVariant(model: Model) {
  const normalizedId = model.id.trim().replace(/^openai\//, "");
  return normalizedId === "gpt-5.5-fast" || normalizedId === "gpt-5.4-fast";
}

export function officialModelSortKeys(id: string) {
  const normalized = normalizeOfficialModelId(id);
  return normalized ? [normalized, `openai/${normalized}`] : [id.trim()];
}
