import type { Model } from "./types";

export function normalizeModel(model: Model): Model {
  const levels = model.supported_reasoning_levels ?? [];
  return {
    ...model,
    context_window: model.context_window ?? null,
    input_modalities: model.input_modalities?.length ? model.input_modalities : ["text"],
    supported_reasoning_levels: levels,
    default_reasoning_level:
      model.default_reasoning_level && levels.includes(model.default_reasoning_level)
        ? model.default_reasoning_level
        : null,
  };
}
