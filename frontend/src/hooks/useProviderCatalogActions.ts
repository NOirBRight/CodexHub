import { useEffect, useState, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
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
  changedProviderProtocols,
  providerCatalogTransactionFeedback,
} from "../lib/providerCatalogTransaction";
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

export class ProviderCatalogTransactionHandledError extends Error {
  readonly providers: Provider[] | null;

  constructor(message: string, providers: Provider[] | null) {
    super(message);
    this.name = "ProviderCatalogTransactionHandledError";
    this.providers = providers;
  }
}

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
  const [providerCatalogRecoveryPending, setProviderCatalogRecoveryPending] = useState(false);

  useEffect(() => {
    let active = true;
    void api.providerCatalogRecoveryPending()
      .then((pending) => {
        if (active) {
          setProviderCatalogRecoveryPending(pending);
        }
      })
      .catch(() => {
        if (active) {
          setProviderCatalogRecoveryPending(true);
        }
      });
    return () => {
      active = false;
    };
  }, []);

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

  async function updateGatewayAfterCatalog(
    activeSettings?: Settings | null,
    toastId?: string,
    options?: {
      catalogAlreadyPublished?: boolean;
    },
  ) {
    const catalogAlreadyPublished = options?.catalogAlreadyPublished ?? false;
    if (toastId && !catalogAlreadyPublished) {
      updateToast(toastId, {
        action: null,
        text: t("providers.generatingCatalog"),
        tone: "loading",
      });
    }
    if (!catalogAlreadyPublished) {
      await api.generateCatalog();
    }
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
    const protocolSwitches = changedProviderProtocols(providers, next);
    const activeToastId = toastId ?? showToast(
      successMessage ? `${successMessage}...` : t("providers.updateProviderCatalog"),
      "loading",
    );
    const mustRegenerateCatalog = (
      regenerateCatalog
      || protocolSwitches.length > 0
      || providerCatalogRecoveryPending
    );
    let transactionCommitted = false;
    let protocolCommitted = false;
    try {
      let saved: Provider[];
      let catalogAlreadyPublished = false;
      if (mustRegenerateCatalog) {
        const transaction = await api.persistProviderCatalogState(next);
        const feedback = providerCatalogTransactionFeedback(transaction, t);
        saved = transaction.providers;
        setProviderCatalogRecoveryPending(transaction.outcome === "recovery_required");
        if (!feedback.committed) {
          setProviders(saved);
          onProvidersChanged?.(saved);
          updateToast(activeToastId, {
            action: null,
            text: feedback.text,
            tone: feedback.tone,
          });
          setError(feedback.text);
          throw new ProviderCatalogTransactionHandledError(feedback.text, saved);
        }
        transactionCommitted = true;
        protocolCommitted = transaction.protocolChanged;
        catalogAlreadyPublished = true;
      } else {
        saved = await api.saveProviders(next);
      }
      setProviders(saved);
      onProvidersChanged?.(saved);
      let syncResult: GatewayClientSyncSummary | null = null;
      if (mustRegenerateCatalog) {
        try {
          syncResult = await updateGatewayAfterCatalog(
            undefined,
            activeToastId,
            { catalogAlreadyPublished },
          );
        } catch (err) {
          if (!transactionCommitted) {
            throw err;
          }
          const postCommitMessage = t(
            protocolCommitted
              ? "providers.protocolChangeCommittedRefreshFailed"
              : "providers.providerCatalogCommittedRefreshFailed",
            {
              detail: messageFromError(err),
            },
          );
          const committedMessage = `${
            protocolCommitted
              ? t("providers.protocolChangedRestartLongLivedCodex")
              : successMessage ?? t("providers.providerCatalogUpdated")
          } ${postCommitMessage}`;
          updateToast(activeToastId, {
            action: null,
            text: committedMessage,
            tone: "error",
          });
          setError(committedMessage);
          return saved;
        }
      }
      const completedMessage = protocolCommitted
        ? t("providers.protocolChangedRestartLongLivedCodex")
        : successMessage ?? t("providers.providerCatalogUpdated");
      const toastMessage = catalogSyncToastMessage(
        completedMessage,
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
      if (err instanceof ProviderCatalogTransactionHandledError) {
        throw err;
      }
      if (mustRegenerateCatalog && !transactionCommitted) {
        setProviderCatalogRecoveryPending(true);
        const message = t(
          protocolSwitches.length
            ? "providers.protocolChangeOutcomeUnconfirmed"
            : "providers.providerCatalogOutcomeUnconfirmed",
          {
            detail: messageFromError(err),
          },
        );
        updateToast(activeToastId, {
          action: null,
          text: message,
          tone: "error",
        });
        setError(message);
        throw new ProviderCatalogTransactionHandledError(message, null);
      }
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
      if (err instanceof ProviderCatalogTransactionHandledError) {
        setModelDiscoveryError(err.message);
        return;
      }
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
      const refreshResult = await api.refreshOfficialModels();
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
        return;
      }
      const syncResult = await updateGatewayAfterCatalog(undefined, toastId ?? undefined, {
        catalogAlreadyPublished: true,
      });
      const refreshMessage = refreshResult.restart_required
        ? `${t("providers.officialModelsRefreshed")} ${t("providers.officialContextLimitsRestartCodex")}`
        : t("providers.officialModelsRefreshed");
      const toastMessage = catalogSyncToastMessage(refreshMessage, syncResult);
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
    try {
      const detectedFormat = probeDetectedEndpointFormat(result);
      await saveProviders(
        nextProviders,
        true,
        detectedFormat
          ? t("providers.probeCompleted", {
            format: upstreamFormatLabel(detectedFormat, tr),
          })
          : t("providers.probeNoSupportedEndpoint"),
        toastId,
      );
      setError(null);
    } catch (err) {
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
    providerCatalogRecoveryPending,
    refreshOfficialModels,
    refreshProviderModels,
    saveAddProviderForm,
    saveProviders,
    updateGatewayAfterCatalog,
  };
}
