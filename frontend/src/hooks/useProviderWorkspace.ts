import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { useToasts } from "../components/PageToast";
import { publishCatalog } from "../lib/catalogPublish";
import { mergeDiscoveredModels } from "../lib/format";
import { applyProviderProbeResult, probeSucceeded, shortProviderDiscoveryError } from "../lib/providerEndpoint";
import { api, isBackendDisconnectedMessage, messageFromError } from "../lib/tauri";
import type { GatewayClientSyncSummary, Model, Provider, Settings, UpstreamFormatProbeResult } from "../lib/types";
import type { AddProviderForm } from "../lib/providerForm";
import {
  ADD_ID,
  OFFICIAL_ID,
  applyDiscoveredModelsForProvider,
  buildOfficialRefreshIntent,
  buildNextProviderFromForm,
  providerWorkspaceReducer,
  resolveOfficialRefresh,
  selectSelectedProvider,
  formProbeModelFor,
  providerProbeModelFor,
  type PendingProviderNavigation,
  type ProviderDraftState,
  type ProviderEditIntent,
  type ProviderRefreshOptions,
  type ProviderWorkspaceOutcome,
  type ProviderWorkspaceState,
} from "../lib/providerWorkspace/core";
import { catalogOverrideToastMessage } from "../lib/providerWorkspace/feedback";
import { mergeOfficialModelSources, sortOfficialModels } from "../lib/officialModels";

type Translate = (key: string, options?: Record<string, unknown>) => string;

function isBackendDisconnected(error: unknown): boolean {
  return isBackendDisconnectedMessage(messageFromError(error));
}

export type WorkspaceSource = {
  catalogModels: Model[];
  modelMetadata: Model[];
  providers: Provider[];
  settings: Settings | null;
};

export type ProviderWorkspaceHandle = {
  /** Read-only view model. */
  state: Readonly<ProviderWorkspaceState>;
  /** Workspace-owned staging operations. Reducer intents stay private. */
  stageProviders: (providers: Provider[]) => void;
  stageSettings: (settings: Settings | null | ((current: Settings | null) => Settings | null)) => void;
  updateForm: (form: AddProviderForm) => void;
  setProbeResult: (result: UpstreamFormatProbeResult | null) => void;
  setDiscoveryError: (error: string | null) => void;
  setOperationBusy: (busy: string | null) => void;
  setOfficialModels: (models: Model[] | ((current: Model[]) => Model[])) => void;
  setOfficialDisabledModels: (disabled: string[]) => void;
  setOfficialModelOrder: (order: string[]) => void;
  stageCatalogPreset: (preset: Provider) => void;
  stagePendingProvider: (provider: Provider | null) => void;
  setSelectedProviderId: (id: string) => void;
  saveProviders: (providers: Provider[], options?: {
    successMessage?: string;
    toastId?: string;
    skipPublish?: boolean;
  }) => Promise<ProviderWorkspaceOutcome>;
  saveSettings: (settings: Settings, options?: {
    regenerateCatalog?: boolean;
    successMessage?: string;
    toastId?: string;
  }) => Promise<ProviderWorkspaceOutcome>;
  saveProvider: (provider: Provider, options?: { successMessage?: string; skipPublish?: boolean }) => Promise<ProviderWorkspaceOutcome>;
  saveAddForm: (form: AddProviderForm, targetId?: string) => Promise<ProviderWorkspaceOutcome>;
  discoverProviderModels: (providerId: string) => Promise<ProviderWorkspaceOutcome>;
  discoverForForm: (form?: AddProviderForm) => Promise<ProviderWorkspaceOutcome>;
  probeProvider: (input: {
    baseUrl: string;
    apiKey: string;
    model?: string | null;
    providerId?: string;
    toastId?: string;
  }) => Promise<ProviderWorkspaceOutcome>;
  refreshOfficialModels: (options?: ProviderRefreshOptions) => Promise<ProviderWorkspaceOutcome>;
  deleteProvider: (providerId: string) => Promise<ProviderWorkspaceOutcome>;
  /** The single dirty-navigation entry point. */
  navigation: {
    pending: PendingProviderNavigation<Provider, import("../lib/providerForm").AddProviderForm> | null;
    resolve: (mode: "save" | "discard" | "cancel") => Promise<ProviderWorkspaceOutcome>;
  };
  /** Selected provider projection. */
  selectedProvider: Provider | null;
  selectProvider: (id: string) => void;
  trackProviderDraft: (draft: ProviderDraftState<Provider>) => void;
};

type ProviderActionIntent =
  | { type: "saveProviders"; providers: Provider[]; successMessage?: string; toastId?: string; skipPublish?: boolean }
  | { type: "saveSettings"; settings: Settings; regenerateCatalog?: boolean; successMessage?: string; toastId?: string }
  | { type: "saveProvider"; provider: Provider; successMessage?: string; skipPublish?: boolean }
  | { type: "saveAddForm"; form: import("../lib/providerForm").AddProviderForm; targetId?: string }
  | { type: "discoverProviderModels"; providerId: string }
  | { type: "discoverForForm"; form?: import("../lib/providerForm").AddProviderForm }
  | { type: "probe"; baseUrl: string; apiKey: string; model?: string | null; providerId?: string; toastId?: string }
  | { type: "refreshOfficialModels"; quiet?: boolean; throwOnError?: boolean }
  | { type: "deleteProvider"; providerId: string };

function initialState(source: WorkspaceSource): ProviderWorkspaceState {
  return {
    busy: null,
    catalogModels: source.catalogModels,
    form: {
      id: "",
      name: "",
      base_url: "",
      api_key: "",
      upstream_format: "auto",
      available_upstream_formats: [],
      tool_protocol: "auto",
      display_prefix: "",
      models: [],
    },
    modelDiscoveryError: null,
    modelMetadata: source.modelMetadata,
    officialDisabledModelsDraft: source.settings?.official_disabled_models ?? [],
    officialModelOrderDraft: source.settings?.official_model_sort_order ?? [],
    officialModelSnapshot: null,
    officialModels: sortOfficialModels(
      mergeOfficialModelSources(source.catalogModels, source.modelMetadata),
      source.settings?.official_model_sort_order ?? [],
    ),
    pendingNavigation: null,
    pendingNewProvider: null,
    probeResult: null,
    providers: source.providers,
    selectedId: OFFICIAL_ID,
    settings: source.settings,
    settingsDraft: source.settings,
  };
}

export function useProviderWorkspace(options: {
  getSource: () => WorkspaceSource;
  onProvidersChanged?: (providers: Provider[]) => void;
  onSettingsChanged?: (settings: Settings) => void;
  refreshGatewayState: () => Promise<void>;
  authorizeCodexRestart?: () => Promise<boolean | null>;
  toast: ReturnType<typeof useToasts>;
  t: Translate;
  tr: Translate;
}): ProviderWorkspaceHandle {
  const { getSource, onProvidersChanged, onSettingsChanged, refreshGatewayState, toast, t, tr } = options;
  const { showToast, updateToast } = toast;
  const externalSource = getSource();
  const [state, dispatch] = useReducer(providerWorkspaceReducer, externalSource, initialState);
  const dirtyDraftRef = useRef<ProviderDraftState<Provider> | null>(null);
  const sourceRef = useRef(externalSource);
  sourceRef.current = externalSource;

  const authorizeCodexRestart = useCallback(async () => {
    if (options.authorizeCodexRestart) {
      return options.authorizeCodexRestart();
    }
    return window.confirm(tr("providers.authorizeCodexRestart") ?? "Restart Codex App?");
  }, [options.authorizeCodexRestart, tr]);

  const publishAndSync = useCallback(async (
    restartCodex: boolean,
    settings: Settings | null,
    updateText: (text: string, tone: "loading" | "success" | "error") => void,
    catalogAlreadyPublished = false,
  ): Promise<GatewayClientSyncSummary | null> => {
    if (!catalogAlreadyPublished) {
      updateText("providers.generatingCatalog", "loading");
    }
    const published = await publishCatalog(
      {
        reason: "provider-catalog",
        persist: !catalogAlreadyPublished,
        syncClients: Boolean(settings?.auto_sync_clients),
      },
      {
        generate: () => api.generateCatalog(restartCodex),
        sync: async () => {
          updateText("providers.syncBoundClients", "loading");
          return api.syncGatewayClients().catch((err) => ({
            applied: 0,
            skipped: 0,
            failed: 1,
            results: [],
            message: messageFromError(err),
          }));
        },
      },
    );
    let syncResult = published.syncResult;
    await refreshGatewayState();
    const diagnostics = await api.catalogOverrideDiagnostics().catch(() => null);
    if (syncResult) {
      syncResult = { ...syncResult, catalog_override_diagnostics: diagnostics };
    } else if (diagnostics) {
      syncResult = { applied: 0, skipped: 0, failed: 0, results: [], message: "", catalog_override_diagnostics: diagnostics };
    }
    return syncResult;
  }, [refreshGatewayState]);

  const edit = useCallback((intent: ProviderEditIntent) => {
    dispatch(intent);
  }, []);

  const syncExternal = useCallback(() => {
    const src = sourceRef.current;
    dispatch({ type: "syncExternal", ...src });
  }, []);

  useEffect(() => {
    syncExternal();
  }, [externalSource.catalogModels, externalSource.modelMetadata, externalSource.providers, externalSource.settings, syncExternal]);

  const updateToastWithError = useCallback(
    (toastId: string, err: unknown) => {
      if (isBackendDisconnected(err)) {
        updateToast(toastId, {
          action: null,
          text: tr("runtime.backendDisconnected"),
          tone: "error",
        });
        return;
      }
      updateToast(toastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
    },
    [tr, updateToast],
  );

  const catalogSyncToastMessage = useCallback(
    (baseMessage: string | undefined, syncResult: { failed?: number; applied?: number; catalog_override_diagnostics?: unknown } | null) => {
      const overrideDiagnostics = (syncResult as { catalog_override_diagnostics?: import("../lib/types").CatalogOverrideDiagnostics | null })?.catalog_override_diagnostics;
      const overrideMessage = overrideDiagnostics
        ? catalogOverrideToastMessage(overrideDiagnostics, t)
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
    },
    [t, tr],
  );

  const updateProbeToast = useCallback(
    (toastId: string, result: UpstreamFormatProbeResult) => {
      if (result.model_required) {
        updateToast(toastId, { action: null, text: t("providers.probeModelRequired"), tone: "error" });
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
        updateToast(toastId, { action: null, text: t(messageKey), tone: "error" });
        return;
      }
      updateToast(toastId, {
        action: null,
        text: t("providers.probeCompleted", { format: result.recommended_format }),
        tone: "success",
      });
    },
    [t, updateToast],
  );

  const saveProvidersCore = useCallback(
    async (next: Provider[], opts: { successMessage?: string; toastId?: string; skipPublish?: boolean }) => {
      const restartCodex = opts.skipPublish ? false : await authorizeCodexRestart();
      if (restartCodex === null) {
        return { kind: "cancelled" as const };
      }
      dispatch({ type: "setBusy", busy: "save" });
      const toastId = opts.toastId ?? showToast(t("providers.updateProviderCatalog"), "loading");
      try {
        const saved = await api.saveProviders(next);
        dispatch({ type: "setProviders", providers: saved });
        onProvidersChanged?.(saved);
        if (!opts.skipPublish) {
          const syncResult = await publishAndSync(
            restartCodex,
            sourceRef.current.settings,
            (text, tone) => updateToast(toastId, { action: null, text: t(text), tone }),
          );
          const toastMessage = catalogSyncToastMessage(opts.successMessage ?? t("providers.providerCatalogUpdated"), syncResult);
          updateToast(toastId, {
            action: null,
            text: toastMessage ?? t("providers.providerCatalogUpdated"),
            tone: "success",
          });
        } else {
          updateToast(toastId, { action: null, text: opts.successMessage ?? t("providers.providerSaved"), tone: "success" });
        }
        return { kind: "ok" as const, message: opts.successMessage, providers: saved };
      } catch (err) {
        updateToastWithError(toastId, err);
        return { kind: "error" as const, message: messageFromError(err) };
      } finally {
        dispatch({ type: "setBusy", busy: null });
      }
    },
    [authorizeCodexRestart, catalogSyncToastMessage, onProvidersChanged, publishAndSync, showToast, t, updateToast, updateToastWithError],
  );

  const runAction = useCallback(
    async (intent: ProviderActionIntent): Promise<ProviderWorkspaceOutcome> => {
      switch (intent.type) {
        case "saveProviders":
          return saveProvidersCore(intent.providers, {
            successMessage: intent.successMessage,
            toastId: intent.toastId,
            skipPublish: intent.skipPublish,
          });
        case "saveSettings": {
          const restartCodex = intent.regenerateCatalog ? await authorizeCodexRestart() : false;
          if (restartCodex === null) {
            return { kind: "cancelled" };
          }
          dispatch({ type: "setBusy", busy: "settings" });
          const toastId = intent.toastId ?? showToast(intent.successMessage ? `${intent.successMessage}...` : t("settings.savingSettings"), "loading");
          try {
            const saved = await api.saveSettings(intent.settings);
            dispatch({ type: "setSettings", settings: saved });
            onSettingsChanged?.(saved);
            const syncResult = intent.regenerateCatalog
              ? await publishAndSync(
                  restartCodex,
                  saved,
                  (text, tone) => updateToast(toastId, { action: null, text: t(text), tone }),
                )
              : null;
            const message = catalogSyncToastMessage(intent.successMessage ?? t("settings.settingsSaved"), syncResult);
            updateToast(toastId, {
              action: null,
              text: message ?? t("settings.settingsSaved"),
              tone: syncResult?.failed ? "error" : "success",
            });
            return { kind: "ok", message: message ?? undefined };
          } catch (err) {
            updateToastWithError(toastId, err);
            return { kind: "error", message: messageFromError(err) };
          } finally {
            dispatch({ type: "setBusy", busy: null });
          }
        }
        case "saveProvider":
          return saveProvidersCore([...state.providers.map((p) => (p.id === intent.provider.id ? intent.provider : p))], {
            successMessage: intent.successMessage,
            skipPublish: intent.skipPublish,
          });
        case "saveAddForm": {
          dispatch({ type: "setBusy", busy: "save" });
          const toastId = showToast(t("providers.savingProvider"), "loading");
          try {
            const restartCodex = await authorizeCodexRestart();
            if (restartCodex === null) {
              updateToast(toastId, { action: null, text: t("providers.saveCancelled"), tone: "info" });
              return { kind: "cancelled" };
            }
            const currentProviders = sourceRef.current.providers;
            const built = buildNextProviderFromForm(currentProviders, intent.form);
            if (!built.provider) {
              const message = t(built.error ?? "providers.providerNameRequired", {
                name: intent.form.name.trim(),
              });
              updateToast(toastId, { action: null, text: message, tone: "error" });
              return { kind: "error", message };
            }
            const nextProviders = [...currentProviders, built.provider];
            const saved = await api.saveProviders(nextProviders);
            dispatch({ type: "setProviders", providers: saved });
            dispatch({ type: "setSelectedId", selectedId: intent.targetId ?? built.id });
            dispatch({ type: "resetForm" });
            dispatch({ type: "clearPendingNewProvider" });
            onProvidersChanged?.(saved);
            const syncResult = await publishAndSync(
              restartCodex,
              sourceRef.current.settings,
              (text, tone) => updateToast(toastId, { action: null, text: t(text), tone }),
            );
            const msg = catalogSyncToastMessage(t("providers.providerAdded", { name: built.provider.name }), syncResult);
            updateToast(toastId, { action: null, text: msg ?? t("providers.providerAdded", { name: built.provider.name }), tone: "success" });
            return { kind: "ok", message: msg ?? undefined, providers: saved };
          } catch (err) {
            updateToastWithError(toastId, err);
            return { kind: "error", message: messageFromError(err) };
          } finally {
            dispatch({ type: "setBusy", busy: null });
          }
        }
        case "discoverProviderModels": {
          const currentProviders = sourceRef.current.providers;
          const pending = state.pendingNewProvider;
          const provider =
            pending?.id === intent.providerId
              ? pending
              : currentProviders.find((p) => p.id === intent.providerId);
          if (!provider) {
            return { kind: "error", message: "provider not found" };
          }
          dispatch({ type: "setBusy", busy: intent.providerId });
          const toastId = showToast(t("providers.discoveringProviderModels", { name: provider.name }), "loading");
          try {
            const models = await api.discoverProviderModels(provider.base_url, provider.api_key ?? "", provider.id);
            const bundled = await api.getBundledProviders().catch(() => [] as Provider[]);
            const preset = bundled.find((b) => b.id === provider.id) ?? null;
            const isPending = pending?.id === provider.id;
            const baseProvider = isPending ? state.pendingNewProvider : provider;
            const retainIntersection = preset?.discovery_policy === "retain-intersection";
            const result = applyDiscoveredModelsForProvider(
              baseProvider ?? provider,
              models,
              preset,
              retainIntersection,
            );
            const msg = t("providers.discoveredProviderModels", {
              name: provider.name,
              count: models.length,
              plural: models.length === 1 ? "" : "s",
              addedCount: result.addedCount,
            });
            if (isPending) {
              dispatch({ type: "setPendingNewProvider", provider: result.provider });
              updateToast(toastId, { action: null, text: msg, tone: "success" });
              return { kind: "ok", message: msg };
            }
            const nextProviders = sourceRef.current.providers.map((p) =>
              p.id === provider.id ? result.provider : p,
            );
            dispatch({ type: "setProviders", providers: nextProviders });
            return saveProvidersCore(nextProviders, { successMessage: msg, toastId });
          } catch (err) {
            const discoveryError = shortProviderDiscoveryError(err, tr);
            dispatch({ type: "setDiscoveryError", error: discoveryError });
            updateToast(toastId, { action: null, text: discoveryError, tone: "error" });
            return { kind: "error", message: discoveryError };
          } finally {
            dispatch({ type: "setBusy", busy: null });
          }
        }
        case "discoverForForm": {
          dispatch({ type: "setBusy", busy: "discover" });
          const toastId = showToast(t("providers.discoveringModels"), "loading");
          try {
            const form = intent.form ?? state.form;
            const models = await api.discoverProviderModels(form.base_url, form.api_key, form.id.trim() || null);
            const nextForm = {
              ...form,
              models: mergeDiscoveredModels(form.models, models),
            };
            dispatch({ type: "updateForm", form: nextForm });
            dispatch({ type: "setDiscoveryError", error: null });
            updateToast(toastId, {
              action: null,
              text: t("providers.discoveredModels", { count: models.length, plural: models.length === 1 ? "" : "s" }),
              tone: "success",
            });
            return { kind: "ok", form: nextForm };
          } catch (err) {
            const discoveryError = shortProviderDiscoveryError(err, tr);
            dispatch({ type: "setDiscoveryError", error: discoveryError });
            updateToast(toastId, { action: null, text: discoveryError, tone: "error" });
            return { kind: "error", message: discoveryError };
          } finally {
            dispatch({ type: "setBusy", busy: null });
          }
        }
        case "probe": {
          dispatch({ type: "setBusy", busy: "probe" });
          dispatch({ type: "setProbeResult", result: null });
          const toastId = intent.toastId ?? showToast(t("providers.endpointSelectionTest"), "loading");
          try {
            const result = await api.probeUpstreamFormat(intent.baseUrl, intent.apiKey, intent.model);
            dispatch({ type: "setProbeResult", result });
            let providers: Provider[] | undefined;
            if (intent.providerId && probeSucceeded(result)) {
              if (state.pendingNewProvider?.id === intent.providerId) {
                dispatch({
                  type: "setPendingNewProvider",
                  provider: applyProviderProbeResult(state.pendingNewProvider, result),
                });
                updateProbeToast(toastId, result);
                return { kind: "ok", probeResult: result };
              }
              const currentProviders = sourceRef.current.providers;
              const provider = currentProviders.find((p) => p.id === intent.providerId);
              if (provider) {
                const nextProviders = currentProviders.map((p) =>
                  p.id === intent.providerId ? applyProviderProbeResult(p, result) : p,
                );
                dispatch({ type: "setProviders", providers: nextProviders });
                const saved = await api.saveProviders(nextProviders);
                dispatch({ type: "setProviders", providers: saved });
                onProvidersChanged?.(saved);
                providers = saved;
              }
            }
            updateProbeToast(toastId, result);
            return { kind: "ok", probeResult: result, providers };
          } catch (err) {
            updateToastWithError(toastId, err);
            return { kind: "error", message: messageFromError(err) };
          } finally {
            dispatch({ type: "setBusy", busy: null });
          }
        }
        case "refreshOfficialModels": {
          const quiet = intent.quiet ?? false;
          let toastId: string | null = null;
          try {
            if (!quiet) {
              dispatch({ type: "setBusy", busy: "official-refresh" });
              toastId = showToast(t("providers.refreshingOfficialModels"), "loading");
            }
            // Refresh reads the current Official model list. Applying a new
            // Codex overlay is a separate, explicitly authorized mutation.
            const refreshResult = await api.refreshOfficialModels(false);
            const resolved = resolveOfficialRefresh(state.officialModelOrderDraft, refreshResult.models);
            if (!resolved.followsAutomatic) {
              dispatch({ type: "setOfficialModelOrderDraft", order: resolved.nextOrder });
            }
            dispatch({ type: "setOfficialModels", models: resolved.sortedModels });
            if (quiet) {
              await refreshGatewayState();
              dispatch({ type: "setDiscoveryError", error: null });
              return { kind: "ok" };
            }
            // A read-only refresh does not publish a catalog. The coordinated
            // path reports a restart result when it did publish while Codex
            // was stopped, so only that path syncs bound clients.
            const syncResult = refreshResult.codex_restart_result
              ? await publishAndSync(
                  false,
                  sourceRef.current.settings,
                  (text, tone) => updateToast(toastId!, { action: null, text: t(text), tone }),
                  true,
                )
              : null;
            const refreshMessage = refreshResult.codex_restart_result === "restarted"
              ? t("providers.officialModelsRefreshedCodexRestarted")
              : refreshResult.codex_restart_result === "switched_relaunch_failed"
                ? t("providers.officialModelsRefreshedCodexRelaunchFailed")
                : refreshResult.restart_required
                  ? t("providers.officialModelsRefreshed") + " " + t("providers.officialContextLimitsRestartCodex")
                  : t("providers.officialModelsRefreshed");
            const refreshFeedback = refreshResult.warning?.trim()
              ? t("providers.officialModelsRefreshedWithWarning", {
                  message: refreshResult.warning.trim(),
                  status: refreshMessage,
                })
              : refreshMessage;
            const toastMessage = catalogSyncToastMessage(refreshFeedback, syncResult);
            updateToast(toastId!, {
              action: null,
              text: toastMessage ?? t("providers.officialModelsRefreshed"),
              tone: refreshResult.warning?.trim() ? "error" : "success",
            });
            return { kind: "ok" };
          } catch (err) {
            if (quiet) {
              dispatch({ type: "setDiscoveryError", error: messageFromError(err) });
              if (intent.throwOnError) {
                throw err;
              }
              return { kind: "error", message: messageFromError(err) };
            }
            if (toastId) {
              updateToastWithError(toastId, err);
            } else {
              showToast(messageFromError(err), "error");
            }
            return { kind: "error", message: messageFromError(err) };
          } finally {
            if (!quiet) {
              dispatch({ type: "setBusy", busy: null });
            }
          }
        }
        case "deleteProvider": {
          const nextProviders = state.providers.filter((p) => p.id !== intent.providerId);
          const toastId = showToast(t("providers.deletingProvider"), "loading");
          try {
            const saved = await api.saveProviders(nextProviders);
            dispatch({ type: "setProviders", providers: saved });
            onProvidersChanged?.(saved);
            updateToast(toastId, { action: null, text: t("providers.providerDeleted"), tone: "success" });
            return { kind: "ok" };
          } catch (err) {
            updateToastWithError(toastId, err);
            return { kind: "error", message: messageFromError(err) };
          }
        }
      }
    },
    [authorizeCodexRestart, catalogSyncToastMessage, onProvidersChanged, onSettingsChanged, publishAndSync, refreshGatewayState, saveProvidersCore, showToast, state, t, tr, updateProbeToast, updateToast, updateToastWithError],
  );

  const stageProviders = useCallback((providers: Provider[]) => {
    edit({ type: "setProviders", providers });
  }, [edit]);

  const stageSettings = useCallback(
    (value: Settings | null | ((current: Settings | null) => Settings | null)) => {
      const next = typeof value === "function" ? value(state.settings) : value;
      if (next) {
        edit({ type: "setSettings", settings: next });
      }
    },
    [edit, state.settings],
  );

  const updateForm = useCallback((form: AddProviderForm) => {
    edit({ type: "updateForm", form });
  }, [edit]);

  const setProbeResult = useCallback((result: UpstreamFormatProbeResult | null) => {
    edit({ type: "setProbeResult", result });
  }, [edit]);

  const setDiscoveryError = useCallback((error: string | null) => {
    edit({ type: "setDiscoveryError", error });
  }, [edit]);

  const setOperationBusy = useCallback((busy: string | null) => {
    edit({ type: "setBusy", busy });
  }, [edit]);

  const setOfficialModels = useCallback(
    (models: Model[] | ((current: Model[]) => Model[])) => {
      const next = typeof models === "function" ? models(state.officialModels) : models;
      edit({ type: "setOfficialModels", models: next });
    },
    [edit, state.officialModels],
  );

  const setOfficialDisabledModels = useCallback((disabled: string[]) => {
    edit({ type: "setOfficialDisabledModelsDraft", disabled });
  }, [edit]);

  const setOfficialModelOrder = useCallback((order: string[]) => {
    edit({ type: "setOfficialModelOrderDraft", order });
  }, [edit]);

  const stageCatalogPreset = useCallback((preset: Provider) => {
    edit({ type: "stageCatalogPreset", preset });
  }, [edit]);

  const stagePendingProvider = useCallback((provider: Provider | null) => {
    edit(provider
      ? { type: "setPendingNewProvider", provider }
      : { type: "clearPendingNewProvider" });
  }, [edit]);

  const setSelectedProviderId = useCallback((id: string) => {
    edit({ type: "setSelectedId", selectedId: id });
  }, [edit]);

  const saveProviders = useCallback(
    (providers: Provider[], options?: { successMessage?: string; toastId?: string; skipPublish?: boolean }) =>
      runAction({ type: "saveProviders", providers, ...options }),
    [runAction],
  );

  const saveSettings = useCallback(
    (settings: Settings, options?: { regenerateCatalog?: boolean; successMessage?: string; toastId?: string }) =>
      runAction({ type: "saveSettings", settings, ...options }),
    [runAction],
  );

  const saveProvider = useCallback(
    (provider: Provider, options?: { successMessage?: string; skipPublish?: boolean }) =>
      runAction({ type: "saveProvider", provider, ...options }),
    [runAction],
  );

  const saveAddForm = useCallback(
    (form: AddProviderForm, targetId?: string) =>
      runAction({ type: "saveAddForm", form, targetId }),
    [runAction],
  );

  const discoverProviderModels = useCallback(
    (providerId: string) => runAction({ type: "discoverProviderModels", providerId }),
    [runAction],
  );

  const discoverForForm = useCallback(
    (form?: AddProviderForm) => runAction({ type: "discoverForForm", form }),
    [runAction],
  );

  const probeProvider = useCallback(
    (input: { baseUrl: string; apiKey: string; model?: string | null; providerId?: string; toastId?: string }) =>
      runAction({ type: "probe", ...input }),
    [runAction],
  );

  const refreshOfficialModels = useCallback(
    (options?: ProviderRefreshOptions) =>
      runAction(buildOfficialRefreshIntent(options)),
    [runAction],
  );

  const deleteProvider = useCallback(
    (providerId: string) => runAction({ type: "deleteProvider", providerId }),
    [runAction],
  );

  const navigation = useMemo(
    () => ({
      pending: state.pendingNavigation,
      resolve: async (mode: "save" | "discard" | "cancel"): Promise<ProviderWorkspaceOutcome> => {
        const pending = state.pendingNavigation;
        if (!pending) {
          return { kind: "ok" };
        }
        if (mode === "cancel") {
          dispatch({ type: "setPendingNavigation", pending: null });
          return { kind: "cancelled" };
        }
        if (mode === "discard") {
          dirtyDraftRef.current = null;
          dispatch({ type: "setPendingNavigation", pending: null });
          dispatch({ type: "setSelectedId", selectedId: pending.targetId });
          if (pending.kind === "add") {
            dispatch({ type: "resetForm" });
            dispatch({ type: "clearPendingNewProvider" });
          }
          return { kind: "ok" };
        }
        if (pending.kind === "existing") {
          await runAction({ type: "saveProvider", provider: pending.draft, skipPublish: true });
        } else {
          const result = await runAction({ type: "saveAddForm", form: pending.form, targetId: pending.targetId });
          if (result.kind === "error") {
            return result;
          }
        }
        dirtyDraftRef.current = null;
        dispatch({ type: "setPendingNavigation", pending: null });
        dispatch({ type: "setSelectedId", selectedId: pending.targetId });
        return { kind: "ok" };
      },
    }),
    [runAction, state.pendingNavigation],
  );

  const selectedProvider = useMemo(() => selectSelectedProvider(state), [state]);
  const selectedIdRef = useRef(state.selectedId);
  const formRef = useRef(state.form);
  selectedIdRef.current = state.selectedId;
  formRef.current = state.form;

  const trackProviderDraft = useCallback((draft: ProviderDraftState<Provider>) => {
    if (!draft.dirty) {
      if (dirtyDraftRef.current?.providerId === draft.providerId) {
        dirtyDraftRef.current = null;
      }
      return;
    }
    dirtyDraftRef.current = draft;
  }, []);

  const selectProvider = useCallback((id: string) => {
    const selectedId = selectedIdRef.current;
    if (id === selectedId) {
      return;
    }
    if (selectedId === ADD_ID) {
      const form = formRef.current;
      if (form.name.trim()) {
        dispatch({
          type: "setPendingNavigation",
          pending: { kind: "add", targetId: id, form },
        });
        return;
      }
      dispatch({ type: "resetForm" });
      dispatch({ type: "setSelectedId", selectedId: id });
      return;
    }
    const dirtyDraft = dirtyDraftRef.current;
    if (dirtyDraft?.dirty && dirtyDraft.providerId === selectedId) {
      dispatch({
        type: "setPendingNavigation",
        pending: { kind: "existing", targetId: id, draft: dirtyDraft.draft },
      });
      return;
    }
    dispatch({ type: "setSelectedId", selectedId: id });
  }, []);

  return {
    state,
    stageProviders,
    stageSettings,
    updateForm,
    setProbeResult,
    setDiscoveryError,
    setOperationBusy,
    setOfficialModels,
    setOfficialDisabledModels,
    setOfficialModelOrder,
    stageCatalogPreset,
    stagePendingProvider,
    setSelectedProviderId,
    saveProviders,
    saveSettings,
    saveProvider,
    saveAddForm,
    discoverProviderModels,
    discoverForForm,
    probeProvider,
    refreshOfficialModels,
    deleteProvider,
    navigation,
    selectedProvider,
    selectProvider,
    trackProviderDraft,
  };
}

// Re-export constants used by the page.
export { ADD_ID, OFFICIAL_ID };
export { formProbeModelFor, providerProbeModelFor };
export type { PendingProviderNavigation, ProviderDraftState };
