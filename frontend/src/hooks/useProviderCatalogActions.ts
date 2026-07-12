import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { ToastContextValue } from "../components/PageToast";
import { mergeDiscoveredModels, renumberModels, slugify } from "../lib/format";
import {
  filterCodexVisibleOfficialModels,
  refreshedOfficialModelOrder,
  shouldFollowOfficialCatalogOrder,
  sortOfficialModels,
} from "../lib/officialModels";
import { emptyProvider, type AddProviderForm } from "../lib/providerForm";
import { normalizeModel } from "../lib/providerModel";
import {
  applyProviderProbeResult,
  normalizeEndpointFormats,
  probeDetectedEndpointFormat,
  shortProviderDiscoveryError,
  upstreamFormatLabel,
} from "../lib/providerEndpoint";
import { api, messageFromError } from "../lib/tauri";
import type {
  GatewayClientSyncSummary,
  Model,
  Provider,
  Settings,
  UpstreamFormatProbeResult,
} from "../lib/types";

type Translate = (key: string, options?: Record<string, unknown>) => string;
type SetState<T> = Dispatch<SetStateAction<T>>;

export type SaveProviders = (
  next: Provider[],
  regenerateCatalog?: boolean,
  successMessage?: string,
  toastId?: string,
) => Promise<Provider[]>;

type ProviderCatalogActionOptions = {
  form: AddProviderForm;
  officialModelOrderDraft: string[];
  officialModelRefreshStartedRef: MutableRefObject<boolean>;
  onProvidersChanged?: (providers: Provider[]) => void;
  providers: Provider[];
  refreshGatewayState: () => Promise<void>;
  setBusy: SetState<string | null>;
  setError: (value: string | null) => void;
  setForm: SetState<AddProviderForm>;
  setModelDiscoveryError: SetState<string | null>;
  setOfficialModelOrderDraft: SetState<string[]>;
  setOfficialModels: SetState<Model[]>;
  setProbeResult: SetState<UpstreamFormatProbeResult | null>;
  setProviders: SetState<Provider[]>;
  setSelectedId: SetState<string>;
  settings: Settings | null;
  settingsDraft: Settings | null;
  t: Translate;
  tr: Translate;
  toast: Pick<ToastContextValue, "showToast" | "updateToast">;
  updateToastWithError: (toastId: string, err: unknown) => void;
};

export function useProviderCatalogActions({
  form,
  officialModelOrderDraft,
  officialModelRefreshStartedRef,
  onProvidersChanged,
  providers,
  refreshGatewayState,
  setBusy,
  setError,
  setForm,
  setModelDiscoveryError,
  setOfficialModelOrderDraft,
  setOfficialModels,
  setProbeResult,
  setProviders,
  setSelectedId,
  settings,
  settingsDraft,
  t,
  tr,
  toast,
  updateToastWithError,
}: ProviderCatalogActionOptions) {
  const { showToast, updateToast } = toast;

  function updateProbeToast(toastId: string, result: UpstreamFormatProbeResult) {
    const detectedFormat = probeDetectedEndpointFormat(result);
    updateToast(toastId, {
      action: null,
      text: detectedFormat
        ? t("providers.probeCompleted", { format: upstreamFormatLabel(detectedFormat, tr) })
        : t("providers.probeNoSupportedEndpoint"),
      tone: detectedFormat ? "success" : "error",
    });
  }

  async function updateGatewayAfterCatalog(activeSettings?: Settings | null, toastId?: string) {
    if (toastId) {
      updateToast(toastId, {
        action: null,
        text: t("providers.generatingCatalog"),
        tone: "loading",
      });
    }
    await api.generateCatalog();
    const syncSettings = activeSettings ?? settingsDraft ?? settings;
    let syncResult: GatewayClientSyncSummary | null = null;
    if (syncSettings?.auto_sync_clients) {
      if (toastId) {
        updateToast(toastId, {
          action: null,
          text: t("providers.syncBoundClients"),
          tone: "loading",
        });
      }
      syncResult = await api.syncGatewayClients().catch((err) => ({
        applied: 0,
        skipped: 0,
        failed: 1,
        results: [],
        message: t("providers.clientSyncFailed", { message: messageFromError(err) }),
      }));
    }
    await refreshGatewayState();
    return syncResult;
  }

  function catalogSyncToastMessage(
    baseMessage: string | undefined,
    syncResult: GatewayClientSyncSummary | null,
  ) {
    if (syncResult?.failed) {
      const syncMessage = tr("providers.syncClientsFailed", { count: syncResult.failed });
      return baseMessage ? `${baseMessage}; ${syncMessage}` : syncMessage;
    }
    if (syncResult?.applied) {
      const syncMessage = tr("providers.syncedClients", {
        count: syncResult.applied,
        plural: syncResult.applied === 1 ? "" : "s",
      });
      return baseMessage ? `${baseMessage}; ${syncMessage}` : syncMessage;
    }
    return baseMessage ?? null;
  }

  async function saveProviders(
    next: Provider[],
    regenerateCatalog = true,
    successMessage?: string,
    toastId?: string,
  ) {
    setBusy("save");
    const activeToastId = toastId ?? showToast(
      successMessage ? `${successMessage}...` : t("providers.updateProviderCatalog"),
      "loading",
    );
    try {
      const saved = await api.saveProviders(next);
      setProviders(saved);
      onProvidersChanged?.(saved);
      let syncResult: GatewayClientSyncSummary | null = null;
      if (regenerateCatalog) {
        syncResult = await updateGatewayAfterCatalog(undefined, activeToastId);
      }
      const toastMessage = catalogSyncToastMessage(
        successMessage ?? t("providers.providerCatalogUpdated"),
        syncResult,
      );
      if (syncResult?.failed) {
        updateToast(activeToastId, {
          action: null,
          text: toastMessage ?? t("providers.providerCatalogUpdateFailed"),
          tone: "error",
        });
      } else {
        updateToast(activeToastId, {
          action: null,
          text: toastMessage ?? t("providers.providerCatalogUpdated"),
          tone: "success",
        });
        setError(null);
      }
      return saved;
    } catch (err) {
      updateToastWithError(activeToastId, err);
      throw err;
    } finally {
      setBusy(null);
    }
  }

  async function refreshProviderModels(provider: Provider) {
    setBusy(provider.id);
    const toastId = showToast(t("providers.discoveringProviderModels", { name: provider.name }), "loading");
    try {
      const models = await api.discoverProviderModels(provider.base_url, provider.api_key ?? "");
      const previousModelIds = new Set(provider.models.map((model) => model.id));
      const nextProvider = {
        ...provider,
        models: mergeDiscoveredModels(provider.models, models),
      };
      const nextProviders = providers.map((item) =>
        item.id === provider.id ? nextProvider : item,
      );
      setProviders(nextProviders);
      const addedCount = nextProvider.models.filter((model) => !previousModelIds.has(model.id)).length;
      await saveProviders(
        nextProviders,
        true,
        t("providers.discoveredProviderModels", {
          name: provider.name,
          count: models.length,
          plural: models.length === 1 ? "" : "s",
          addedCount,
        }),
        toastId,
      );
      setModelDiscoveryError(null);
    } catch (err) {
      const discoveryError = shortProviderDiscoveryError(err, tr);
      setModelDiscoveryError(discoveryError);
      updateToast(toastId, {
        action: null,
        text: discoveryError,
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  }

  async function refreshOfficialModels(options?: { quiet?: boolean }) {
    const quiet = options?.quiet ?? false;
    if (!quiet) {
      setBusy("official-refresh");
    }
    const toastId = quiet ? null : showToast(t("providers.refreshingOfficialModels"), "loading");
    try {
      const refreshed = filterCodexVisibleOfficialModels(await api.refreshOfficialModels());
      const followsAutomaticOrder = shouldFollowOfficialCatalogOrder(officialModelOrderDraft);
      const nextOrder = followsAutomaticOrder
        ? officialModelOrderDraft
        : refreshedOfficialModelOrder(officialModelOrderDraft, refreshed);
      if (!followsAutomaticOrder) {
        setOfficialModelOrderDraft(nextOrder);
      }
      setOfficialModels(sortOfficialModels(refreshed, nextOrder));
      if (quiet) {
        await api.generateCatalog();
        await refreshGatewayState();
        setModelDiscoveryError(null);
        return;
      }
      const syncResult = await updateGatewayAfterCatalog(undefined, toastId ?? undefined);
      const toastMessage = catalogSyncToastMessage(t("providers.officialModelsRefreshed"), syncResult);
      if (syncResult?.failed) {
        updateToast(toastId!, {
          action: null,
          text: toastMessage ?? t("providers.officialModelsRefreshedSyncFailed"),
          tone: "error",
        });
      } else {
        updateToast(toastId!, {
          action: null,
          text: toastMessage ?? t("providers.officialModelsRefreshed"),
          tone: "success",
        });
        setError(null);
      }
    } catch (err) {
      if (quiet) {
        officialModelRefreshStartedRef.current = false;
        setModelDiscoveryError(messageFromError(err));
      } else {
        updateToastWithError(toastId!, err);
      }
    } finally {
      if (!quiet) {
        setBusy(null);
      }
    }
  }

  async function discoverForForm() {
    setBusy("discover");
    const toastId = showToast(t("providers.discoveringModels"), "loading");
    try {
      const models = await api.discoverProviderModels(form.base_url, form.api_key);
      setForm((current) => ({
        ...current,
        models: mergeDiscoveredModels(current.models, models),
      }));
      updateToast(toastId, {
        action: null,
        text: t("providers.discoveredModels", { count: models.length, plural: models.length === 1 ? "" : "s" }),
        tone: "success",
      });
      setModelDiscoveryError(null);
    } catch (err) {
      const discoveryError = shortProviderDiscoveryError(err, tr);
      setModelDiscoveryError(discoveryError);
      updateToast(toastId, {
        action: null,
        text: discoveryError,
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  }

  async function probeUpstreamFormat(
    baseUrl: string,
    apiKey: string,
    model?: string | null,
    providerId?: string,
  ) {
    setBusy("probe");
    setProbeResult(null);
    const toastId = showToast(t("providers.endpointSelectionTest"), "loading");
    try {
      const result = await api.probeUpstreamFormat(baseUrl, apiKey, model);
      setProbeResult(result);
      if (providerId) {
        await persistProviderProbeResult(providerId, result, toastId);
      } else {
        updateProbeToast(toastId, result);
        setError(null);
      }
      return result;
    } catch (err) {
      updateToastWithError(toastId, err);
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function persistProviderProbeResult(
    providerId: string,
    result: UpstreamFormatProbeResult,
    toastId: string,
  ) {
    const nextProviders = providers.map((provider) =>
      provider.id === providerId ? applyProviderProbeResult(provider, result) : provider,
    );
    setProviders(nextProviders);
    try {
      const saved = await api.saveProviders(nextProviders);
      setProviders(saved);
      onProvidersChanged?.(saved);
      setError(null);
      updateProbeToast(toastId, result);
    } catch (err) {
      updateToastWithError(toastId, err);
      setError(messageFromError(err));
    }
  }

  function providerProbeModel(provider: Provider) {
    const model = provider.models.find((item) => item.enabled) ?? provider.models[0];
    return model?.upstream_model?.trim() || model?.id || null;
  }

  function formProbeModel() {
    const model = form.models.find((item) => item.enabled) ?? form.models[0];
    return model?.upstream_model?.trim() || model?.id || null;
  }

  async function saveAddProviderForm(nextForm: AddProviderForm, targetId?: string) {
    const id = nextForm.id.trim() || slugify(nextForm.name);
    if (!id) {
      setError(t("providers.providerNameRequired"));
      return null;
    }
    if (providers.some((provider) => provider.id === id)) {
      setError(t("providers.providerAlreadyExists", { name: nextForm.name.trim() }));
      return null;
    }

    const models = renumberModels(nextForm.models.map((model) => normalizeModel(model)));
    const nextSortOrder = Math.max(0, ...providers.map((provider) => provider.sort_order ?? 0)) + 1;
    const providerName = nextForm.name.trim();
    await saveProviders(
      [
        ...providers,
        {
          id,
          name: providerName,
          base_url: nextForm.base_url.trim(),
          api_key: nextForm.api_key.trim() || null,
          upstream_format: nextForm.upstream_format,
          available_upstream_formats: normalizeEndpointFormats(nextForm.available_upstream_formats),
          tool_protocol: nextForm.tool_protocol,
          display_prefix: nextForm.display_prefix.trim() || null,
          sort_order: nextSortOrder,
          enabled: true,
          models,
        },
      ],
      true,
      t("providers.providerAdded", { name: providerName }),
    );
    setSelectedId(targetId ?? id);
    setForm(emptyProvider);
    return id;
  }

  async function addProvider() {
    await saveAddProviderForm(form);
  }

  return {
    addProvider,
    catalogSyncToastMessage,
    discoverForForm,
    formProbeModel,
    persistProviderProbeResult,
    probeUpstreamFormat,
    providerProbeModel,
    refreshOfficialModels,
    refreshProviderModels,
    saveAddProviderForm,
    saveProviders,
    updateGatewayAfterCatalog,
  };
}
