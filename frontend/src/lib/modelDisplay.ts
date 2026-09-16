import { displayModel } from "./format";
import { shortWireDisplayName } from "./wireDisplayName";
import { normalizeOfficialModelId } from "./settings";
import type { Model } from "./types";

export type ModelLabelProvider = {
  id?: string | null;
  name?: string | null;
  display_prefix?: string | null;
};

const PROVIDER_PREFIX_ALIASES: Record<string, readonly string[]> = {
  "opencode-go": ["OpenCode", "OC"],
  commandcode: ["Command Code", "CC"],
};

export function providerDisplayPrefixes(provider: ModelLabelProvider) {
  const prefixes = [
    provider.display_prefix,
    ...(PROVIDER_PREFIX_ALIASES[provider.id ?? ""] ?? []),
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
  return shortWireDisplayName(name, model.id) || model.id;
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

export function partitionDisplayedModels(
  models: Model[],
  officialDisabledModels?: string[],
) {
  const enabled: Model[] = [];
  const disabled: Model[] = [];
  for (const model of models) {
    if (isDisplayedModelEnabled(model, officialDisabledModels)) {
      enabled.push(model);
    } else {
      disabled.push(model);
    }
  }
  return { enabled, disabled };
}

export function stitchDisplayedModelReorder(
  models: Model[],
  reorderedGroup: Model[],
  officialDisabledModels?: string[],
) {
  if (reorderedGroup.length === 0) {
    return [...models];
  }
  const groupEnabled = isDisplayedModelEnabled(reorderedGroup[0], officialDisabledModels);
  const queue = [...reorderedGroup];
  return models.map((model) =>
    isDisplayedModelEnabled(model, officialDisabledModels) === groupEnabled
      ? (queue.shift() ?? model)
      : model,
  );
}
