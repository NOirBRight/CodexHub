import { displayModel } from "./format";
import { normalizeOfficialModelId } from "./settings";
import type { Model } from "./types";

export type ModelLabelProvider = {
  id?: string | null;
  name?: string | null;
  display_prefix?: string | null;
};

export function providerDisplayPrefixes(provider: ModelLabelProvider) {
  const prefixes = [
    provider.display_prefix,
    provider.id === "opencode-go" ? "OpenCode" : null,
    provider.id === "commandcode" ? "Command Code" : null,
  ].filter((value): value is string => Boolean(value?.trim()));
  return [...new Set(prefixes)].sort((a, b) => b.length - a.length);
}

export function displayModelName(model: Model, provider?: ModelLabelProvider) {
  let name = displayModel(model);
  if (!provider) {
    return name;
  }
  for (const prefix of providerDisplayPrefixes(provider)) {
    if (
      !name.toLowerCase().startsWith(prefix.toLowerCase()) ||
      !/^[\s/:_-]/.test(name.slice(prefix.length))
    ) {
      continue;
    }
    const stripped = name.slice(prefix.length).replace(/^[\s/:_-]+/, "");
    if (stripped) {
      name = stripped;
      break;
    }
  }
  return name || model.id;
}

export function isDisplayedModelEnabled(
  model: Model,
  officialDisabledModels?: string[],
) {
  if (officialDisabledModels) {
    return !officialDisabledModels.some(
      (item) => normalizeOfficialModelId(item) === normalizeOfficialModelId(model.id),
    );
  }
  return model.enabled !== false;
}

export function enabledPreviewModels(
  models: Model[],
  officialDisabledModels?: string[],
) {
  return models.filter((model) => isDisplayedModelEnabled(model, officialDisabledModels));
}

export function sortModelsEnabledFirst(
  models: Model[],
  officialDisabledModels?: string[],
) {
  return [...models].sort((left, right) => {
    const leftEnabled = isDisplayedModelEnabled(left, officialDisabledModels) ? 0 : 1;
    const rightEnabled = isDisplayedModelEnabled(right, officialDisabledModels) ? 0 : 1;
    return leftEnabled - rightEnabled;
  });
}
