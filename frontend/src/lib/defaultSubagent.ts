import type { Model, Provider, Settings } from "./types";
import { shortWireDisplayName } from "./wireDisplayName";
import officialFastVariants from "../../../config/official_fast_variants.json";

export const CODEX_SUBAGENT_EFFORTS = [
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
] as const;

export const CLIENT_DEFAULT_SUBAGENT_IDS = ["opencode", "zcode", "omp", "grok"] as const;

export function supportsClientDefaultSubagent(clientId: string) {
  return (CLIENT_DEFAULT_SUBAGENT_IDS as readonly string[]).includes(clientId);
}

export function clientDefaultSubagentFields(settings: Settings, clientId: string) {
  switch (clientId) {
    case "opencode":
      return {
        model: settings.opencode_default_subagent_model,
        effort: settings.opencode_default_subagent_reasoning_effort,
      };
    case "zcode":
      return {
        model: settings.zcode_default_subagent_model,
        effort: settings.zcode_default_subagent_reasoning_effort,
      };
    case "omp":
      return {
        model: settings.omp_default_subagent_model,
        effort: settings.omp_default_subagent_reasoning_effort,
      };
    case "grok":
      return {
        model: settings.grok_default_subagent_model,
        effort: settings.grok_default_subagent_reasoning_effort,
      };
    default:
      return { model: "", effort: "" };
  }
}

export function withClientDefaultSubagent(
  settings: Settings,
  clientId: string,
  model: string,
  effort: string,
): Settings {
  switch (clientId) {
    case "opencode":
      return {
        ...settings,
        opencode_default_subagent_model: model,
        opencode_default_subagent_reasoning_effort: effort,
      };
    case "zcode":
      return {
        ...settings,
        zcode_default_subagent_model: model,
        zcode_default_subagent_reasoning_effort: effort,
      };
    case "omp":
      return {
        ...settings,
        omp_default_subagent_model: model,
        omp_default_subagent_reasoning_effort: effort,
      };
    case "grok":
      return {
        ...settings,
        grok_default_subagent_model: model,
        grok_default_subagent_reasoning_effort: effort,
      };
    default:
      return settings;
  }
}

export type DefaultSubagentOption = {
  id: string;
  label: string;
  efforts: string[];
  defaultEffort: string;
  fast?: boolean;
  speedVariant?: string;
};

const FAST_SUBAGENT_MODELS = new Set(Object.values(officialFastVariants));

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
  includeFastVariants?: boolean;
  officialId: string;
  officialIncluded: boolean;
  officialModels: Model[];
  officialDisabledModels: string[];
  providers: Provider[];
  excludeProviderIds?: string[];
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
      const option: DefaultSubagentOption = {
        id,
        label: shortWireDisplayName(model.display_name, model.id),
        efforts: effortsForModel(model),
        defaultEffort: defaultEffortForModel(model),
      };
      if (
        input.includeFastVariants && FAST_SUBAGENT_MODELS.has(id) &&
        !input.officialDisabledModels.some((item) => officialModelKey(item) === `${id}-fast`)
      ) {
        option.speedVariant = `${id}-fast`;
      }
      push(option);
      if (option.speedVariant) {
        push({
          ...option,
          id: option.speedVariant,
          label: `${option.label} Fast`,
          fast: true,
          speedVariant: id,
        });
      }
    }
  }

  const excluded = new Set(input.excludeProviderIds ?? []);
  for (const provider of input.providers) {
    if (!provider.enabled) continue;
    if (excluded.has(provider.id)) continue;
    for (const model of provider.models) {
      if (model.enabled === false) continue;
      const id = subagentCatalogSlug(provider.id, model.id, input.officialId);
      push({
        id,
        label: `${provider.name} · ${shortWireDisplayName(model.display_name, model.id)}`,
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
