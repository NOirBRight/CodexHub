import { emptyProvider, type AddProviderForm } from "../providerForm";
import { applyPresetReasoningDefaults, bundledPresetFor, instantiateCatalogProvider } from "../providerCatalog";
import { normalizeModel } from "../providerModel";
import {
  applyProviderProbeResult,
  normalizeEndpointFormats,
  probeSucceeded,
} from "../providerEndpoint";
import { mergeDiscoveredModels, renumberModels, slugify } from "../format";
import {
  filterCodexVisibleOfficialModels,
  refreshedOfficialModelOrder,
  shouldFollowOfficialCatalogOrder,
  sortOfficialModels,
  mergeOfficialModelSources,
} from "../officialModels";
import type { Model, Provider, Settings, UpstreamFormatProbeResult } from "../types";

export const OFFICIAL_ID = "__official__";
export const ADD_ID = "__add__";

export type ProviderDraftState<TDraft> = {
  providerId: string;
  draft: TDraft;
  dirty: boolean;
};

export type PendingProviderNavigation<TDraft, TAddForm> =
  | { kind: "existing"; targetId: string; draft: TDraft }
  | { kind: "add"; targetId: string; form: TAddForm };

/** Read-only view the page renders. */
export type ProviderWorkspaceState = {
  busy: string | null;
  catalogModels: Model[];
  form: AddProviderForm;
  modelDiscoveryError: string | null;
  modelMetadata: Model[];
  officialDisabledModelsDraft: string[];
  officialModelOrderDraft: string[];
  officialModels: Model[];
  pendingNavigation: PendingProviderNavigation<Provider, AddProviderForm> | null;
  pendingNewProvider: Provider | null;
  probeResult: UpstreamFormatProbeResult | null;
  providers: Provider[];
  selectedId: string;
  settings: Settings | null;
  settingsDraft: Settings | null;
};

/** Synchronous edit intents. */
export type ProviderEditIntent =
  | { type: "select"; targetId: string }
  | { type: "trackDraft"; state: ProviderDraftState<Provider> }
  | { type: "updateForm"; form: AddProviderForm }
  | { type: "resetForm" }
  | { type: "stageCatalogPreset"; preset: Provider }
  | { type: "toggleOfficialModel"; modelId: string; enabled: boolean }
  | { type: "reorderOfficialModels"; models: Model[] }
  | { type: "reorderProviders"; providers: Provider[] }
  | { type: "setBusy"; busy: string | null }
  | { type: "setProbeResult"; result: UpstreamFormatProbeResult | null }
  | { type: "setDiscoveryError"; error: string | null }
  | { type: "setSelectedId"; selectedId: string }
  | { type: "clearDirtyDraft"; providerId: string }
  | { type: "clearPendingNewProvider" }
  | { type: "setPendingNewProvider"; provider: Provider | null }
  | { type: "setPendingNavigation"; pending: PendingProviderNavigation<Provider, AddProviderForm> | null }
  | { type: "setOfficialModels"; models: Model[] }
  | { type: "setOfficialModelOrderDraft"; order: string[] }
  | { type: "setOfficialDisabledModelsDraft"; disabled: string[] }
  | { type: "setProviders"; providers: Provider[] }
  | { type: "syncExternal"; providers: Provider[]; settings: Settings | null; catalogModels: Model[]; modelMetadata: Model[]; selectedId?: string };

export type ProviderWorkspaceOutcome =
  | { kind: "ok"; message?: string; form?: AddProviderForm; probeResult?: UpstreamFormatProbeResult; providers?: Provider[] }
  | { kind: "cancelled" }
  | { kind: "blocked"; reason: string }
  | { kind: "error"; message: string };

/** Pure state updates; effects (I/O) happen in the hook. */
export function providerWorkspaceReducer(
  state: ProviderWorkspaceState,
  intent: ProviderEditIntent,
): ProviderWorkspaceState {
  switch (intent.type) {
    case "select":
      return { ...state, selectedId: intent.targetId };
    case "updateForm":
      return { ...state, form: intent.form };
    case "resetForm":
      return { ...state, form: emptyProvider };
    case "setBusy":
      return { ...state, busy: intent.busy };
    case "setProbeResult":
      return { ...state, probeResult: intent.result };
    case "setDiscoveryError":
      return { ...state, modelDiscoveryError: intent.error };
    case "setSelectedId":
      return { ...state, selectedId: intent.selectedId };
    case "setPendingNewProvider":
      return { ...state, pendingNewProvider: intent.provider };
    case "clearPendingNewProvider":
      return { ...state, pendingNewProvider: null };
    case "setPendingNavigation":
      return { ...state, pendingNavigation: intent.pending };
    case "setOfficialModels":
      return { ...state, officialModels: intent.models };
    case "setOfficialModelOrderDraft":
      return { ...state, officialModelOrderDraft: intent.order };
    case "setOfficialDisabledModelsDraft":
      return { ...state, officialDisabledModelsDraft: intent.disabled };
    case "setProviders":
      return { ...state, providers: intent.providers };
    case "toggleOfficialModel": {
      const disabled = new Set(state.officialDisabledModelsDraft);
      if (intent.enabled) {
        disabled.delete(intent.modelId);
      } else {
        disabled.add(intent.modelId);
      }
      return { ...state, officialDisabledModelsDraft: [...disabled] };
    }
    case "reorderOfficialModels": {
      const nextOrder = intent.models.map((model) => model.id);
      return {
        ...state,
        officialModels: intent.models,
        officialModelOrderDraft: nextOrder,
      };
    }
    case "reorderProviders":
      return { ...state, providers: intent.providers };
    case "stageCatalogPreset": {
      const existing = state.providers.find((p) => p.id === intent.preset.id);
      if (existing) {
        return { ...state, pendingNewProvider: null, selectedId: intent.preset.id };
      }
      if (state.pendingNewProvider?.id === intent.preset.id) {
        return { ...state, selectedId: intent.preset.id };
      }
      const nextSortOrder =
        Math.max(0, ...state.providers.map((p) => p.sort_order ?? 0)) + 1;
      return {
        ...state,
        pendingNewProvider: instantiateCatalogProvider(intent.preset, nextSortOrder),
        selectedId: intent.preset.id,
      };
    }
    case "syncExternal": {
      const next = {
        ...state,
        providers: intent.providers,
        settings: intent.settings,
        settingsDraft: intent.settings,
        catalogModels: intent.catalogModels,
        modelMetadata: intent.modelMetadata,
        officialModels: sortOfficialModels(
          mergeOfficialModelSources(intent.catalogModels, intent.modelMetadata),
          intent.settings?.official_model_sort_order ?? [],
        ),
      };
      const selectedId = intent.selectedId ?? state.selectedId;
      if (
        selectedId !== OFFICIAL_ID &&
        selectedId !== ADD_ID &&
        state.pendingNewProvider?.id !== selectedId &&
        !intent.providers.some((p) => p.id === selectedId)
      ) {
        return { ...next, selectedId: intent.providers[0]?.id ?? OFFICIAL_ID };
      }
      return next;
    }
    case "trackDraft":
    case "clearDirtyDraft":
      return state;
  }
}

/** Selectors the page renders. */
export function selectSelectedProvider(state: ProviderWorkspaceState): Provider | null {
  if (state.pendingNewProvider && state.selectedId === state.pendingNewProvider.id) {
    return state.pendingNewProvider;
  }
  return state.providers.find((p) => p.id === state.selectedId) ?? null;
}

export function selectOfficialModelDraftDirty(state: ProviderWorkspaceState): boolean {
  if (!state.settings) {
    return false;
  }
  return (
    JSON.stringify(state.officialDisabledModelsDraft) !==
      JSON.stringify(state.settings.official_disabled_models ?? []) ||
    JSON.stringify(state.officialModelOrderDraft) !==
      JSON.stringify(state.settings.official_model_sort_order ?? [])
  );
}

export function selectOfficialEnabledCount(state: ProviderWorkspaceState): number {
  const disabled = new Set(state.officialDisabledModelsDraft);
  return state.officialModels.filter((model) => !disabled.has(model.id)).length;
}

export function buildNextProviderFromForm(
  providers: Provider[],
  form: AddProviderForm,
  targetId?: string,
): { id: string; provider: Provider | null; error: string | null } {
  const id = form.id.trim() || slugify(form.name);
  if (!id) {
    return { id, provider: null, error: "providers.providerNameRequired" };
  }
  if (providers.some((provider) => provider.id === id)) {
    return { id, provider: null, error: "providers.providerAlreadyExists" };
  }
  const models = renumberModels(form.models.map((model) => normalizeModel(model)));
  const nextSortOrder = Math.max(0, ...providers.map((p) => p.sort_order ?? 0)) + 1;
  const providerName = form.name.trim();
  return {
    id,
    provider: {
      id,
      name: providerName,
      base_url: form.base_url.trim(),
      api_key: form.api_key.trim() || null,
      upstream_format: form.upstream_format,
      available_upstream_formats: normalizeEndpointFormats(form.available_upstream_formats),
      tool_protocol: form.tool_protocol,
      display_prefix: form.display_prefix.trim() || null,
      sort_order: nextSortOrder,
      enabled: true,
      models,
    },
    error: null,
  };
}

export function applyDiscoveredModelsForProvider(
  baseProvider: Provider,
  discovered: Model[],
  preset: Provider | null,
  retainIntersection: boolean,
): { provider: Provider; addedCount: number } {
  const previousModelIds = new Set(baseProvider.models.map((model) => model.id));
  const withDefaults = applyPresetReasoningDefaults(discovered, preset);
  const retained = retainIntersection
    ? baseProvider.models.filter((model) => withDefaults.some((item) => item.id === model.id))
    : baseProvider.models;
  const provider = {
    ...baseProvider,
    models: mergeDiscoveredModels(retained, withDefaults),
  };
  const addedCount = provider.models.filter((model) => !previousModelIds.has(model.id)).length;
  return { provider, addedCount };
}

export function applyProbeToProvider(
  provider: Provider,
  result: UpstreamFormatProbeResult,
): Provider {
  if (!probeSucceeded(result)) {
    return provider;
  }
  return applyProviderProbeResult(provider, result);
}

export function resolveOfficialRefresh(
  currentOrder: string[],
  refreshedModels: Model[],
): { followsAutomatic: boolean; nextOrder: string[]; sortedModels: Model[] } {
  const followsAutomatic = shouldFollowOfficialCatalogOrder(currentOrder);
  const nextOrder = followsAutomatic
    ? currentOrder
    : refreshedOfficialModelOrder(currentOrder, refreshedModels);
  const filtered = filterCodexVisibleOfficialModels(refreshedModels);
  return { followsAutomatic, nextOrder, sortedModels: sortOfficialModels(filtered, nextOrder) };
}

export function providerProbeModelFor(provider: Provider): string | null {
  const model = provider.models.find((item) => item.enabled) ?? provider.models[0];
  return model?.upstream_model?.trim() || model?.id || null;
}

export function formProbeModelFor(form: AddProviderForm): string | null {
  const model = form.models.find((item) => item.enabled) ?? form.models[0];
  return model?.upstream_model?.trim() || model?.id || null;
}
