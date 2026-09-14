import type { Model, Provider } from "./types";

export const CODEX_SUBAGENT_EFFORTS = [
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
] as const;

export type DefaultSubagentOption = {
  id: string;
  label: string;
  efforts: string[];
  defaultEffort: string;
};

export function subagentCatalogSlug(
  providerId: string,
  modelId: string,
  officialId: string,
) {
  const normalized = modelId.trim();
  if (!normalized) return "";
  if (providerId === officialId) {
    return normalized.startsWith("openai/")
      ? normalized.slice("openai/".length)
      : normalized;
  }
  if (normalized.startsWith(`${providerId}/`)) return normalized;
  return `${providerId}/${normalized}`;
}

export function listDefaultSubagentOptions(input: {
  officialId: string;
  officialIncluded: boolean;
  officialModels: Model[];
  officialDisabledModels: string[];
  providers: Provider[];
}): DefaultSubagentOption[] {
  const options: DefaultSubagentOption[] = [];
  const seen = new Set<string>();
  const push = (option: DefaultSubagentOption) => {
    if (!option.id || seen.has(option.id)) return;
    seen.add(option.id);
    options.push(option);
  };

  if (input.officialIncluded) {
    for (const model of input.officialModels) {
      if (!isOfficialModelEnabled(model, input.officialDisabledModels)) continue;
      const id = subagentCatalogSlug(input.officialId, model.id, input.officialId);
      push({
        id,
        label: model.display_name?.trim() || model.id,
        efforts: effortsForModel(model),
        defaultEffort: defaultEffortForModel(model),
      });
    }
  }

  for (const provider of input.providers) {
    if (!provider.enabled) continue;
    for (const model of provider.models) {
      if (model.enabled === false) continue;
      const id = subagentCatalogSlug(provider.id, model.id, input.officialId);
      push({
        id,
        label: `${provider.name} · ${model.display_name?.trim() || model.id}`,
        efforts: effortsForModel(model),
        defaultEffort: defaultEffortForModel(model),
      });
    }
  }

  return options;
}

export function resolveSubagentEffort(
  option: DefaultSubagentOption | undefined,
  currentEffort: string,
) {
  const efforts = option?.efforts?.length ? option.efforts : [...CODEX_SUBAGENT_EFFORTS];
  if (currentEffort && efforts.includes(currentEffort)) return currentEffort;
  if (option?.defaultEffort && efforts.includes(option.defaultEffort)) {
    return option.defaultEffort;
  }
  return efforts.includes("medium") ? "medium" : (efforts[0] ?? "medium");
}

export function formatSubagentEffort(effort: string) {
  const value = effort.trim();
  if (!value) return "";
  if (value === "xhigh") return "xHigh";
  return value.charAt(0).toUpperCase() + value.slice(1);
}

export function defaultSubagentSummary(
  label: string | undefined,
  effort: string,
  fallback: string,
) {
  const name = label?.trim() ?? "";
  if (!name) return fallback;
  const effortLabel = formatSubagentEffort(effort);
  return effortLabel ? `${name} · ${effortLabel}` : name;
}

function officialModelKey(value: string) {
  const trimmed = value.trim();
  return trimmed.startsWith("openai/") ? trimmed.slice("openai/".length) : trimmed;
}

function isOfficialModelEnabled(model: Model, disabledModels: string[]) {
  const id = officialModelKey(model.id);
  return !disabledModels.some((item) => officialModelKey(item) === id);
}

function effortsForModel(model: Model) {
  const levels = (model.supported_reasoning_levels ?? []).filter(Boolean);
  return levels.length ? levels : [...CODEX_SUBAGENT_EFFORTS];
}

function defaultEffortForModel(model: Model) {
  const efforts = effortsForModel(model);
  const configured = model.default_reasoning_level?.trim() ?? "";
  if (configured && efforts.includes(configured)) return configured;
  return efforts.includes("medium") ? "medium" : (efforts[0] ?? "medium");
}
