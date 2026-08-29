import { useState, type Dispatch, type SetStateAction } from "react";
import type { ToastContextValue } from "../components/PageToast";
import { mergeDiscoveredModels, renumberModels, slugify } from "../lib/format";
import {
  filterCodexVisibleOfficialModels,
  refreshedOfficialModelOrder,
  shouldFollowOfficialCatalogOrder,
  sortOfficialModels,
} from "../lib/officialModels";
import { emptyProvider, type AddProviderForm } from "../lib/providerForm";
import {
  applyPresetReasoningDefaults,
  bundledPresetFor,
  instantiateCatalogProvider,
} from "../lib/providerCatalog";
import { normalizeModel } from "../lib/providerModel";
import {
  applyProviderProbeResult,
  normalizeEndpointFormats,
  probeDetectedEndpointFormat,
  probeSucceeded,
  shortProviderDiscoveryError,
  upstreamFormatLabel,
} from "../lib/providerEndpoint";
import { publishCatalog } from "../lib/catalogPublish";
import { api, messageFromError } from "../lib/tauri";
import type {
  CatalogOverrideDiagnostics,
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
  authorizeCodexRestart: () => Promise<boolean | null>;
  form: AddProviderForm;
  officialModelOrderDraft: string[];
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
  authorizeCodexRestart,
  form,
  officialModelOrderDraft,
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
  const [pendingNewProvider, setPendingNewProvider] = useState<Provider | null>(null);

  function updateProbeToast(toastId: string, result: UpstreamFormatProbeResult) {
    if (result.model_required) {
      updateToast(toastId, {
        action: null,
        text: t("providers.probeModelRequired"),
        tone: "error",
      });
      return;
    }
    if (result.inconclusive_reason) {
      const messageKey = {
        authentication_failed: "providers.probeAuthenticationFailed",
        rate_limited: "providers.probeRateLimited",
        network_error: "providers.probeNetworkError",
        upstream_unavailable: "providers.probeUpstreamUnavailable",
        request_failed: "providers.probeRequestFailed",
      }[result.inconclusive_reason];
      updateToast(toastId, {
        action: null,
        text: t(messageKey),
        tone: "error",
      });
      return;
    }
    const detectedFormat = probeDetectedEndpointFormat(result);
    updateToast(toastId, {
      action: null,
      text: detectedFormat
        ? t("providers.probeCompleted", { format: upstreamFormatLabel(detectedFormat, tr) })
        : t("providers.probeNoSupportedEndpoint"),
      tone: detectedFormat ? "success" : "error",
    });
  }

  async function updateGatewayAfterCatalog(
    activeSettings?: Settings | null,
    toastId?: string,
    options?: { catalogAlreadyPublished?: boolean; restartCodex?: boolean },
  ) {
    const catalogAlreadyPublished = options?.catalogAlreadyPublished ?? false;
    if (toastId && !catalogAlreadyPublished) {
      updateToast(toastId, {
        action: null,
        text: t("providers.generatingCatalog"),
        tone: "loading",
      });
    }
    const syncSettings = activeSettings ?? settingsDraft ?? settings;
    const published = await publishCatalog(
      {
        reason: "provider-catalog",
        persist: !catalogAlreadyPublished,
        syncClients: Boolean(syncSettings?.auto_sync_clients),
      },
      {
        generate: () => api.generateCatalog(options?.restartCodex ?? false),
        sync: async () => {
          if (toastId) {
            updateToast(toastId, {
              action: null,
              text: t("providers.syncBoundClients"),
              tone: "loading",
            });
          }
          return api.syncGatewayClients().catch((err) => ({
            applied: 0,
            skipped: 0,
            failed: 1,
            results: [],
            message: t("providers.clientSyncFailed", { message: messageFromError(err) }),
          }));
        },
      },
    );
    let syncResult = published.syncResult;
    await refreshGatewayState();
    const overrideDiagnostics = await api.catalogOverrideDiagnostics().catch(() => null);
    if (syncResult) {
      syncResult = {
        ...syncResult,
        catalog_override_diagnostics: overrideDiagnostics,
      };
    } else if (overrideDiagnostics) {
      syncResult = {
        applied: 0,
        skipped: 0,
        failed: 0,
        results: [],
        message: "",
        catalog_override_diagnostics: overrideDiagnostics,
      };
    }
    return syncResult;
  }

  function catalogSyncToastMessage(
    baseMessage: string | undefined,
    syncResult: GatewayClientSyncSummary | null,
  ) {
    const overrideMessage = syncResult?.catalog_override_diagnostics
      ? catalogOverrideToastMessage(syncResult.catalog_override_diagnostics)
      : null;
    if (syncResult?.failed) {
      const syncMessage = tr("providers.syncClientsFailed", { count: syncResult.failed });
      return [baseMessage, syncMessage, overrideMessage].filter(Boolean).join("; ") || null;
    }
    if (syncResult?.applied) {
      const syncMessage = tr("providers.syncedClients", {
        count: syncResult.applied,
        plural: syncResult.applied === 1 ? "" : "s",
      });
      return [baseMessage, syncMessage, overrideMessage].filter(Boolean).join("; ") || null;
    }
    return [baseMessage, overrideMessage].filter(Boolean).join("; ") || null;
  }

  function catalogOverrideToastMessage(diagnostics: CatalogOverrideDiagnostics): string | null {
    const accepted = Math.max(0, Math.min(100, diagnostics.accepted));
    const rejected = Math.max(0, Math.min(100, diagnostics.rejected));
    const migrated = Math.max(0, Math.min(100, diagnostics.migrated));
    if (accepted === 0 && rejected === 0 && migrated === 0) {
      return null;
    }
    return t("providers.catalogOverrideDiagnostics", {
      accepted,
      rejected,
      migrated,
      restart: t("providers.catalogOverrideRestartCodex"),
    });
  }

  async function saveProviders(
    next: Provider[],
    regenerateCatalog = true,
    successMessage?: string,
    toastId?: string,
  ) {
    const restartCodex = regenerateCatalog ? await authorizeCodexRestart() : false;
    if (restartCodex === null) {
      return providers;
    }
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
        try {
          syncResult = await updateGatewayAfterCatalog(undefined, activeToastId, {
            restartCodex,
          });
        } catch (err) {
          updateToast(activeToastId, {
            action: null,
            text: t("providers.providerSavedCatalogWarning", {
              saved: successMessage ?? t("providers.providerDataSaved"),
              message: messageFromError(err),
            }),
            tone: "success",
          });
          setError(null);
          return saved;
        }
      }
      const toastMessage = catalogSyncToastMessage(
        successMessage ?? t("providers.providerCatalogUpdated"),
        syncResult,
      );
      if (syncResult?.failed) {
        updateToast(activeToastId, {
          action: null,
          text: toastMessage ?? t("providers.providerCatalogUpdateFailed"),
          tone: "success",
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
      const models = await api.discoverProviderModels(
        provider.base_url,
        provider.api_key ?? "",
        provider.id,
      );
      const bundled = await api.getBundledProviders().catch(() => [] as Provider[]);
      const preset = bundledPresetFor(provider.id, bundled);
      const discovered = applyPresetReasoningDefaults(models, preset);
      // Merge against the persisted provider so discovery never drops manual
      // models that are present in saved state but absent from a stale draft.
      const isPending = pendingNewProvider?.id === provider.id;
      const persistedProvider = isPending
        ? pendingNewProvider
        : providers.find((item) => item.id === provider.id) ?? provider;
      const previousModelIds = new Set(persistedProvider.models.map((model) => model.id));
      const retainIntersection = preset?.discovery_policy === "retain-intersection";
      const retainedModels = retainIntersection
        ? persistedProvider.models.filter((model) => discovered.some((item) => item.id === model.id))
        : persistedProvider.models;
      const nextProvider = {
        ...persistedProvider,
        models: mergeDiscoveredModels(retainedModels, discovered),
      };
      if (isPending) {
        setPendingNewProvider(nextProvider);
        const addedCount = nextProvider.models.filter((model) => !previousModelIds.has(model.id)).length;
        updateToast(toastId, {
          action: null,
          text: t("providers.discoveredProviderModels", {
            name: provider.name,
            count: models.length,
            plural: models.length === 1 ? "" : "s",
            addedCount,
          }),
          tone: "success",
        });
        setModelDiscoveryError(null);
        return;
      }
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

  async function refreshOfficialModels(
    options?: { quiet?: boolean; throwOnError?: boolean },
  ): Promise<boolean> {
    const quiet = options?.quiet ?? false;
    let toastId: string | null = null;
    try {
      const restartCodex = quiet ? false : await authorizeCodexRestart();
      if (restartCodex === null) {
        return false;
      }
      if (!quiet) {
        setBusy("official-refresh");
        toastId = showToast(t("providers.refreshingOfficialModels"), "loading");
      }
      const refreshResult = await api.refreshOfficialModels(restartCodex);
      const refreshed = filterCodexVisibleOfficialModels(refreshResult.models);
      const followsAutomaticOrder = shouldFollowOfficialCatalogOrder(officialModelOrderDraft);
      const nextOrder = followsAutomaticOrder
        ? officialModelOrderDraft
        : refreshedOfficialModelOrder(officialModelOrderDraft, refreshed);
      if (!followsAutomaticOrder) {
        setOfficialModelOrderDraft(nextOrder);
      }
      setOfficialModels(sortOfficialModels(refreshed, nextOrder));
      if (quiet) {
        await refreshGatewayState();
        setModelDiscoveryError(null);
        return true;
      }
      const syncResult = await updateGatewayAfterCatalog(undefined, toastId ?? undefined, {
        catalogAlreadyPublished: true,
      });
      const refreshMessage = refreshResult.codex_restart_result === "restarted"
        ? t("providers.officialModelsRefreshedCodexRestarted")
        : refreshResult.codex_restart_result === "switched_relaunch_failed"
          ? t("providers.officialModelsRefreshedCodexRelaunchFailed")
          : refreshResult.restart_required
            ? `${t("providers.officialModelsRefreshed")} ${t("providers.officialContextLimitsRestartCodex")}`
            : t("providers.officialModelsRefreshed");
      const refreshFeedback = refreshResult.warning?.trim()
        ? t("providers.officialModelsRefreshedWithWarning", {
            message: refreshResult.warning.trim(),
            status: refreshMessage,
          })
        : refreshMessage;
      const toastMessage = catalogSyncToastMessage(refreshFeedback, syncResult);
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
          tone: refreshResult.warning?.trim() ? "error" : "success",
        });
        setError(null);
      }
      return !syncResult?.failed;
    } catch (err) {
      if (quiet) {
        setModelDiscoveryError(messageFromError(err));
        if (options?.throwOnError) {
          throw err;
        }
      } else {
        if (toastId) {
          updateToastWithError(toastId, err);
        } else {
          showToast(messageFromError(err), "error");
        }
      }
      return false;
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
      const models = await api.discoverProviderModels(form.base_url, form.api_key, form.id.trim() || null);
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
    if (!probeSucceeded(result)) {
      setError(null);
      updateProbeToast(toastId, result);
      return;
    }
    if (pendingNewProvider?.id === providerId) {
      setPendingNewProvider(applyProviderProbeResult(pendingNewProvider, result));
      setError(null);
      updateProbeToast(toastId, result);
      return;
    }
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

  function addCatalogProvider(preset: Provider) {
    const existing = providers.find((provider) => provider.id === preset.id);
    if (existing) {
      setPendingNewProvider(null);
      setSelectedId(preset.id);
      return preset.id;
    }
    if (pendingNewProvider?.id === preset.id) {
      setSelectedId(preset.id);
      return preset.id;
    }

    const nextSortOrder = Math.max(0, ...providers.map((provider) => provider.sort_order ?? 0)) + 1;
    setPendingNewProvider(instantiateCatalogProvider(preset, nextSortOrder));
    setSelectedId(preset.id);
    return preset.id;
  }

  return {
    addCatalogProvider,
    addProvider,
    pendingNewProvider,
    setPendingNewProvider,
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
