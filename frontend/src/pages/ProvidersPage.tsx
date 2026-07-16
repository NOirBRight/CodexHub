import {
  Cable,
  Check,
  Copy,
  Eye,
  EyeOff,
  ExternalLink,
  Link2,
  Link2Off,
  Plus,
  RefreshCcw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { BACKEND_DISCONNECTED_TOAST_KEY, useToasts } from "../components/PageToast";
import { SortableList } from "../components/SortableList";
import {
  OPENAI_USAGE_REFRESH_INTERVAL_MS,
  OfficialOpenAIUsageLimitBars,
  OfficialOpenAIUsagePanel,
  defaultOfficialOpenAIUsageWindow,
  readStoredOfficialOpenAIUsageSnapshot,
  storeOfficialOpenAIUsageSnapshot,
} from "../components/providers/OfficialOpenAIUsagePanel";
import { AddProviderPanel, ProviderDetail } from "../components/providers/ProviderEditor";
import { HeaderRow } from "../components/providers/ProviderFormControls";
import {
  isOfficialModelDisabled,
  ModelSection,
  modelIdMatches,
  SwitchControl,
} from "../components/providers/ProviderModelSection";
import { useProviderNavigationGuard } from "../hooks/useProviderNavigationGuard";
import type { PendingProviderNavigation } from "../hooks/useProviderNavigationGuard";
import { useProviderCatalogActions } from "../hooks/useProviderCatalogActions";
import { useVerticalOverflow } from "../hooks/useVerticalOverflow";
import { cx, displayModel, renumberModels } from "../lib/format";
import { emptyProvider, type AddProviderForm } from "../lib/providerForm";
import {
  filterCodexVisibleOfficialModels,
  mergeOfficialModelSources,
  refreshedOfficialModelOrder,
  shouldFollowOfficialCatalogOrder,
  sortOfficialModels,
} from "../lib/officialModels";
import { upstreamFormatLabel } from "../lib/providerEndpoint";
import { normalizeOfficialModelId, normalizeSettings } from "../lib/settings";
import { api, isBackendDisconnectedMessage, messageFromError } from "../lib/tauri";
import type {
  AppFlavorInfo,
  AppStatus,
  CodexContextGuardStatus,
  GatewayStatus,
  GatewayClientSyncSummary,
  Model,
  OpenAIUsageSnapshot,
  Provider,
  Settings,
  UpstreamFormatProbeResult,
} from "../lib/types";

const OFFICIAL_ID = "__official__";
const ADD_ID = "__add__";
type ProviderNavItem =
  { id: string; sort_order: number; provider: Provider };
type CodexAuthState = "authorized" | "missing" | "unknown";
type ConnectionMode = "official" | "custom";
type Translate = (key: string, options?: Record<string, unknown>) => string;

type ProvidersPageProps = {
  appFlavor?: AppFlavorInfo | null;
  appStatus: AppStatus | null;
  catalogModels: Model[];
  gatewayStatus?: GatewayStatus | null;
  modelMetadata: Model[];
  providers: Provider[];
  settings: Settings | null;
  onGatewayChanged?: () => Promise<GatewayStatus | null | void>;
  onRefreshClients?: () => Promise<void>;
  onProvidersChanged?: (providers: Provider[]) => void;
  onSettingsChanged?: (settings: Settings) => void;
  onStartProxy?: () => Promise<void>;
  onStatusChanged?: (status: AppStatus) => void;
};

function ProvidersPageImpl({
  appFlavor,
  appStatus: appStatusSnapshot,
  catalogModels,
  gatewayStatus: gatewayStatusSnapshot,
  modelMetadata,
  onGatewayChanged,
  onRefreshClients,
  onProvidersChanged,
  onSettingsChanged,
  onStartProxy,
  onStatusChanged,
  providers: providersSnapshot,
  settings: settingsSnapshot,
}: ProvidersPageProps) {
  const { t } = useTranslation();
  const tr = t as Translate;
  const { showToast, updateToast } = useToasts();
  const initialOfficialUsageSnapshot = useMemo(() => readStoredOfficialOpenAIUsageSnapshot(), []);
  const [codexAuthPreviewState, setCodexAuthPreviewState] = useState<CodexAuthState | null>(() => readCodexAuthPreviewState());
  const [providers, setProviders] = useState<Provider[]>(() => providersSnapshot);
  const [settings, setSettings] = useState<Settings | null>(() => (
    settingsSnapshot ? withDefaultFastVariants(settingsSnapshot) : null
  ));
  const [settingsDraft, setSettingsDraft] = useState<Settings | null>(() => (
    settingsSnapshot ? withDefaultFastVariants(settingsSnapshot) : null
  ));
  const [officialDisabledModelsDraft, setOfficialDisabledModelsDraft] = useState<string[]>(() => (
    settingsSnapshot ? withDefaultFastVariants(settingsSnapshot).official_disabled_models : []
  ));
  const [officialModelOrderDraft, setOfficialModelOrderDraft] = useState<string[]>(() => (
    settingsSnapshot ? withDefaultFastVariants(settingsSnapshot).official_model_sort_order : []
  ));
  const persistedOfficialSettingsRef = useRef({
    disabledModels: settingsSnapshot
      ? withDefaultFastVariants(settingsSnapshot).official_disabled_models
      : [],
    modelOrder: settingsSnapshot
      ? withDefaultFastVariants(settingsSnapshot).official_model_sort_order
      : [],
  });
  const [codexStatus, setCodexStatus] = useState<AppStatus | null>(appStatusSnapshot);
  const [connectionPendingMode, setConnectionPendingMode] = useState<ConnectionMode | null>(null);
  const [codexTargetOwnerOverride, setCodexTargetOwnerOverride] =
    useState<AppFlavorInfo["codex_target_owner"] | undefined>(undefined);
  const [loadedGatewayStatus, setLoadedGatewayStatus] = useState<GatewayStatus | null>(gatewayStatusSnapshot ?? null);
  const [codexAuthState, setCodexAuthState] = useState<CodexAuthState>(() => codexAuthPreviewState ?? "unknown");
  const [officialModels, setOfficialModels] = useState<Model[]>(() => {
    const normalizedSettings = settingsSnapshot ? withDefaultFastVariants(settingsSnapshot) : null;
    return sortOfficialModels(
      mergeOfficialModelSources(catalogModels, modelMetadata),
      normalizedSettings?.official_model_sort_order ?? [],
    );
  });
  const [officialUsageSnapshot, setOfficialUsageSnapshot] = useState<OpenAIUsageSnapshot | null>(initialOfficialUsageSnapshot);
  const [officialUsageBusy, setOfficialUsageBusy] = useState(false);
  const [officialUsageError, setOfficialUsageError] = useState<string | null>(null);
  const [officialUsageHidden, setOfficialUsageHidden] = useState(false);
  const officialUsageSnapshotRef = useRef<OpenAIUsageSnapshot | null>(null);
  const officialModelRefreshStartedRef = useRef(false);
  const [form, setForm] = useState(emptyProvider);
  const [probeResult, setProbeResult] = useState<UpstreamFormatProbeResult | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [modelDiscoveryError, setModelDiscoveryError] = useState<string | null>(null);
  const {
    cancelPendingProviderNavigation,
    discardPendingProviderNavigation,
    pendingProviderNavigation,
    savePendingProviderNavigation,
    selectedId,
    selectProvider,
    setSelectedId,
    trackProviderDraft,
  } = useProviderNavigationGuard<Provider, AddProviderForm>({
    addId: ADD_ID,
    form,
    initialSelectedId: OFFICIAL_ID,
    isAddFormDirty: isAddProviderFormDirty,
    resetAddForm: () => setForm(emptyProvider),
    saveAddForm: async (nextForm, targetId) => Boolean(await saveAddProviderForm(nextForm, targetId)),
    saveExistingDraft: (draft) => updateProvider(draft, t("providers.providerSaved", { name: draft.name })),
  });
  const {
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
  } = useProviderCatalogActions({
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
    toast: { showToast, updateToast },
    updateToastWithError,
  });

  useEffect(() => {
    const normalizedSettings = settingsSnapshot ? withDefaultFastVariants(settingsSnapshot) : null;
    const disabledModels = normalizedSettings?.official_disabled_models ?? [];
    const modelOrder = normalizedSettings?.official_model_sort_order ?? [];
    const persistedOfficialSettings = persistedOfficialSettingsRef.current;
    const officialSettingsChanged =
      JSON.stringify(disabledModels) !== JSON.stringify(persistedOfficialSettings.disabledModels) ||
      JSON.stringify(modelOrder) !== JSON.stringify(persistedOfficialSettings.modelOrder);
    setSettings(normalizedSettings);
    setSettingsDraft(normalizedSettings);
    if (officialSettingsChanged) {
      setOfficialDisabledModelsDraft(disabledModels);
      setOfficialModelOrderDraft(modelOrder);
      persistedOfficialSettingsRef.current = { disabledModels, modelOrder };
    }
  }, [settingsSnapshot]);

  useEffect(() => {
    setProviders(providersSnapshot);
    if (
      selectedId !== OFFICIAL_ID &&
      selectedId !== ADD_ID &&
      !providersSnapshot.some((provider) => provider.id === selectedId)
    ) {
      setSelectedId(providersSnapshot[0]?.id ?? OFFICIAL_ID);
    }
  }, [providersSnapshot, selectedId]);

  useEffect(() => {
    setCodexStatus(appStatusSnapshot);
  }, [appStatusSnapshot]);

  useEffect(() => {
    const normalizedSettings = settingsSnapshot ? withDefaultFastVariants(settingsSnapshot) : null;
    setOfficialModels(
      sortOfficialModels(
        mergeOfficialModelSources(catalogModels, modelMetadata),
        normalizedSettings?.official_model_sort_order ?? [],
      ),
    );
  }, [catalogModels, modelMetadata, settingsSnapshot]);

  useEffect(() => {
    officialUsageSnapshotRef.current = officialUsageSnapshot;
  }, [officialUsageSnapshot]);

  useEffect(() => {
    if (selectedId !== OFFICIAL_ID || codexAuthState !== "authorized") {
      return;
    }
    void primeOfficialModels();
    void primeOfficialOpenAIUsage();
    const usageRefreshTimer = window.setInterval(() => void loadOfficialOpenAIUsage(true), OPENAI_USAGE_REFRESH_INTERVAL_MS);
    return () => window.clearInterval(usageRefreshTimer);
  }, [codexAuthState, selectedId]);

  useEffect(() => {
    if (gatewayStatusSnapshot !== undefined) {
      setLoadedGatewayStatus(gatewayStatusSnapshot ?? null);
      setCodexAuthState(codexAuthPreviewState ?? codexAuthStateFromGatewayStatus(gatewayStatusSnapshot ?? null));
    }
  }, [codexAuthPreviewState, gatewayStatusSnapshot]);

  useEffect(() => {
    setProbeResult(null);
    setModelDiscoveryError(null);
  }, [selectedId]);

  const selectedProvider = useMemo(
    () => providers.find((provider) => provider.id === selectedId) ?? null,
    [providers, selectedId],
  );
  const providerModelCount = useMemo(
    () =>
      providers.reduce(
        (total, provider) => total + provider.models.length,
        0,
      ),
    [providers],
  );
  const enabledProviderModels = useMemo(
    () =>
      providers.reduce(
        (total, provider) =>
          total +
          provider.models.filter(
            (model) =>
              provider.enabled &&
              model.enabled &&
              model.gateway_exported,
          ).length,
        0,
      ),
    [providers],
  );
  const officialDisabledModels = officialDisabledModelsDraft;
  const officialModelDraftDirty = Boolean(
    settings &&
    (
      JSON.stringify(officialDisabledModelsDraft) !== JSON.stringify(settings.official_disabled_models ?? []) ||
      JSON.stringify(officialModelOrderDraft) !== JSON.stringify(settings.official_model_sort_order ?? [])
    ),
  );
  const officialEnabledCount = officialModels.filter(
    (model) => !isOfficialModelDisabled(officialDisabledModels, model.id),
  ).length;
  const providerNavItems = useMemo<ProviderNavItem[]>(() => {
    return providers
      .map((provider) => ({
        id: provider.id,
        sort_order: provider.sort_order ?? 0,
        provider,
      }))
      .sort((left, right) => {
        if (left.sort_order !== right.sort_order) {
          return left.sort_order - right.sort_order;
        }
        return left.id.localeCompare(right.id);
      });
  }, [providers]);
  const canAdd = Boolean(form.name.trim());
  const gatewayStatus = gatewayStatusSnapshot ?? loadedGatewayStatus;
  const realCodexConnected = codexStatus?.mode === "custom" && codexStatus.proxy_running === true;
  const effectiveCodexTargetOwner = codexTargetOwnerOverride === undefined
    ? appFlavor?.codex_target_owner ?? null
    : codexTargetOwnerOverride;
  const codexOwnedByOtherApp = Boolean(
    !realCodexConnected &&
      effectiveCodexTargetOwner !== null &&
      effectiveCodexTargetOwner !== "official" &&
      effectiveCodexTargetOwner !== appFlavor?.routing_owner,
  );
  const codexConnected = realCodexConnected || codexOwnedByOtherApp;
  const codexRouteOwnerLabel = realCodexConnected
    ? codexTakeoverOwnerLabel(appFlavor?.routing_owner ?? null, tr)
    : codexOwnedByOtherApp
      ? codexTakeoverOwnerLabel(effectiveCodexTargetOwner, tr)
      : null;
  const gatewayContextById = useMemo(() => {
    return new Map((gatewayStatus?.official_models ?? []).map((model) => [model.id, model.context_window]));
  }, [gatewayStatus]);

  function setMessage(value: string | null) {
    if (value) {
      showToast(value, "message");
    }
  }

  function setError(value: string | null) {
    if (value) {
      if (isBackendDisconnectedMessage(value)) {
        showBackendDisconnectedToast();
        return;
      }
      showToast(value, "error");
    }
  }

  function showBackendDisconnectedToast() {
    let toastId = "";
    toastId = showToast({
      dedupeKey: BACKEND_DISCONNECTED_TOAST_KEY,
      text: t("gateway.backendNotConnected"),
      tone: "error",
      action: {
        label: t("gateway.startBackend"),
        onClick: () => void startBackendFromToast(toastId),
      },
    });
  }

  function updateToastWithError(toastId: string, err: unknown) {
    const text = messageFromError(err);
    if (isBackendDisconnectedMessage(text)) {
      updateToast(toastId, {
        action: {
          label: t("gateway.startBackend"),
          onClick: () => void startBackendFromToast(toastId),
        },
        text: t("gateway.backendNotConnected"),
        tone: "error",
      });
      return;
    }
    updateToast(toastId, {
      action: null,
      text,
      tone: "error",
    });
  }

  async function startProxyForHubConnection(): Promise<AppStatus | null> {
    if (onStartProxy) {
      await onStartProxy();
      return api.getStatus().catch(() => null);
    }
    return api.startProxy();
  }

  async function startBackendFromToast(toastId?: string) {
    setBusy("start");
    const activeToastId = toastId ?? showToast(t("gateway.startingBackend"), "loading");
    updateToast(activeToastId, {
      action: null,
      text: t("gateway.startingBackend"),
      tone: "loading",
    });
    try {
      if (onStartProxy) {
        await onStartProxy();
      } else {
        await api.startProxy();
      }
      const nextCodexStatus = await api.getStatus().catch(() => null);
      if (nextCodexStatus) {
        setCodexStatus(nextCodexStatus);
        onStatusChanged?.(nextCodexStatus);
      }
      await refreshGatewayState();
      updateToast(activeToastId, {
        action: null,
        text: t("gateway.backendStarted"),
        tone: "success",
      });
    } catch (err) {
      updateToastWithError(activeToastId, err);
    } finally {
      setBusy(null);
    }
  }

  async function refreshGatewayState() {
    try {
      const gatewayStatus = await onGatewayChanged?.();
      if (gatewayStatus !== undefined) {
        setLoadedGatewayStatus(gatewayStatus ?? null);
        setCodexAuthState(codexAuthPreviewState ?? codexAuthStateFromGatewayStatus(gatewayStatus ?? null));
      }
    } catch {
      // Refresh failures are surfaced by the owning runtime loader.
    }
  }

  async function primeOfficialModels() {
    if (officialModelRefreshStartedRef.current) {
      return;
    }
    officialModelRefreshStartedRef.current = true;
    await refreshOfficialModels({ quiet: true });
  }

  async function primeOfficialOpenAIUsage() {
    if (!officialUsageSnapshotRef.current) {
      await loadOfficialOpenAIUsage(false, false, undefined, { showBusy: false });
    }
    void loadOfficialOpenAIUsage(true);
  }

  async function loadOfficialOpenAIUsage(
    forceRefresh = true,
    notify = false,
    toastId?: string,
    options?: { showBusy?: boolean },
  ) {
    const showBusy = options?.showBusy ?? true;
    const activeToastId = toastId ?? (notify ? showToast(t("providers.refreshingOpenAIUsage"), "loading") : null);
    if (showBusy) {
      setOfficialUsageBusy(true);
    }
    try {
      const snapshot = await api.openaiUsageCompletions({
        ...defaultOfficialOpenAIUsageWindow(),
        forceRefresh,
      });
      officialUsageSnapshotRef.current = snapshot;
      setOfficialUsageSnapshot(snapshot);
      storeOfficialOpenAIUsageSnapshot(snapshot);
      setOfficialUsageError(null);
      setOfficialUsageHidden(false);
      if (activeToastId) {
        updateToast(activeToastId, {
          action: null,
          text: t("providers.openaiUsageRefreshed"),
          tone: "success",
        });
      }
    } catch (err) {
      if (activeToastId) {
        updateToastWithError(activeToastId, err);
      }
      if (officialUsageSnapshotRef.current) {
        setOfficialUsageError(null);
        setOfficialUsageHidden(false);
        return;
      }
      setOfficialUsageError(messageFromError(err));
      setOfficialUsageHidden(false);
    } finally {
      if (showBusy) {
        setOfficialUsageBusy(false);
      }
    }
  }

  async function openCodexAppForLogin() {
    const toastId = showToast(t("providers.openingCodexApp"), "loading");
    try {
      await api.openCodexApp();
      updateToast(toastId, {
        action: null,
        text: t("providers.codexAppOpened"),
        tone: "success",
      });
    } catch (err) {
      const message = messageFromError(err);
      if (isUnknownCodexHubCommand(message, "open_codex_app")) {
        try {
          await navigator.clipboard.writeText("codex login");
          updateToast(toastId, {
            action: null,
            text: t("providers.openCodexAppUnsupportedCopied"),
            tone: "message",
          });
        } catch {
          updateToast(toastId, {
            action: null,
            text: t("providers.openCodexAppUnsupported"),
            tone: "error",
          });
        }
        return;
      }
      updateToast(toastId, {
        action: null,
        text: message,
        tone: "error",
      });
    }
  }

  async function copyCodexLoginCommand() {
    try {
      await navigator.clipboard.writeText("codex login");
      showToast(t("providers.codexLoginCommandCopied"), "message");
    } catch (err) {
      showToast(t("gateway.copyFailed", { message: messageFromError(err) }), "error");
    }
  }

  async function refreshCodexAuthStatus() {
    setBusy("auth-refresh");
    try {
      const gatewayStatus = await api.gatewayStatus();
      const authState = codexAuthStateFromGatewayStatus(gatewayStatus);
      setCodexAuthPreviewState(null);
      clearCodexAuthPreviewParam();
      setLoadedGatewayStatus(gatewayStatus);
      setCodexAuthState(authState);
      await refreshGatewayState();
      setError(null);
      showToast(t("providers.codexAuthRefreshed"), "message");
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function saveSettings(next: Settings, regenerateCatalog = false, successMessage?: string, toastId?: string) {
    setBusy("settings");
    const activeToastId = toastId ?? showToast(successMessage ? `${successMessage}...` : t("settings.savingSettings"), "loading");
    try {
      const saved = await api.saveSettings(next);
      setSettings(saved);
      setSettingsDraft(saved);
      onSettingsChanged?.(saved);
      let syncResult: GatewayClientSyncSummary | null = null;
      if (regenerateCatalog) {
        syncResult = await updateGatewayAfterCatalog(saved, activeToastId);
      }
      const toastMessage = catalogSyncToastMessage(successMessage ?? t("settings.settingsSaved"), syncResult);
      if (syncResult?.failed) {
        updateToast(activeToastId, {
          action: null,
          text: toastMessage ?? t("providers.settingsSavedSyncFailed"),
          tone: "error",
        });
      } else {
        updateToast(activeToastId, {
          action: null,
          text: toastMessage ?? t("settings.settingsSaved"),
          tone: "success",
        });
        setError(null);
      }
    } catch (err) {
      updateToastWithError(activeToastId, err);
    } finally {
      setBusy(null);
    }
  }

  async function toggleAutostart(enabled: boolean) {
    if (!settingsDraft) {
      return;
    }
    setBusy("autostart");
    const toastId = showToast(enabled ? t("providers.enablingAutoStart") : t("providers.disablingAutoStart"), "loading");
    try {
      if (enabled) {
        await api.setAutostart(true);
      } else {
        await api.removeAutostart();
      }
      await saveSettings(
        { ...settingsDraft, auto_start_software: enabled },
        false,
        enabled ? t("providers.autoStartEnabled") : t("providers.autoStartDisabled"),
        toastId,
      );
    } catch (err) {
      updateToastWithError(toastId, err);
      setBusy(null);
    }
  }

  function reflectContextGuardSetting(enabled: boolean) {
    setSettings((current) => {
      if (!current) {
        return current;
      }
      const next = { ...current, openai_context_guard_enabled: enabled };
      onSettingsChanged?.(next);
      return next;
    });
    setSettingsDraft((current) => (
      current ? { ...current, openai_context_guard_enabled: enabled } : current
    ));
  }

  async function updateProvider(next: Provider, successMessage?: string) {
    await saveProviders(
      providers.map((provider) => (provider.id === next.id ? next : provider)),
      true,
      successMessage,
    );
  }

  function toggleProviderEnabled(providerId: string, enabled: boolean) {
    const providerName = providers.find((provider) => provider.id === providerId)?.name ?? providerId;
    const toastId = showToast(
      enabled
        ? t("providers.enablingProvider", { name: providerName })
        : t("providers.disablingProvider", { name: providerName }),
      "loading",
    );
    const nextProviders = providers.map((provider) =>
      provider.id === providerId ? { ...provider, enabled } : provider,
    );
    setProviders(nextProviders);
    void saveProviders(
      nextProviders,
      true,
      enabled
        ? t("providers.providerEnabledNamed", { name: providerName })
        : t("providers.providerDisabledNamed", { name: providerName }),
      toastId,
    );
  }

  async function reorderHubProviders(items: ProviderNavItem[]) {
    const nextProviders = providers.map((provider) => provider);

    items.forEach((item, index) => {
      const sortOrder = index + 1;
      const providerIndex = nextProviders.findIndex((provider) => provider.id === item.id);
      if (providerIndex >= 0) {
        nextProviders[providerIndex] = { ...nextProviders[providerIndex], sort_order: sortOrder };
      }
    });

    setProviders(nextProviders);
    await saveProviders(nextProviders, true, t("providers.providerOrderSaved"));
  }

  function toggleOfficialInclude(value: boolean) {
    if (!settingsDraft) {
      return;
    }
    const toastId = showToast(value ? t("providers.includingOfficialModels") : t("providers.excludingOfficialModels"), "loading");
    void saveSettings(
      { ...settingsDraft, include_official_models: value },
      true,
      value ? t("providers.officialModelsIncluded") : t("providers.officialModelsExcluded"),
      toastId,
    );
  }

  function toggleOfficialModel(modelId: string, enabled: boolean) {
    const current = officialDisabledModelsDraft;
    const nextDisabled = enabled
      ? current.filter((item) => !modelIdMatches(item, modelId))
      : [...new Set([...current, modelId])];
    setOfficialDisabledModelsDraft(nextDisabled);
    setOfficialModels((currentModels) =>
      currentModels.map((model) => (modelIdMatches(model.id, modelId) ? { ...model, enabled } : model)),
    );
  }

  async function toggleCodexHubConnection() {
    const nextMode: ConnectionMode = realCodexConnected ? "official" : "custom";
    await applyCodexHubConnection(nextMode, Boolean(appFlavor?.codex_takeover_required));
  }

  async function applyCodexHubConnection(nextMode: ConnectionMode, forceTakeover: boolean) {
    const actionLabel = nextMode === "custom" ? t("providers.connectingToHub") : t("providers.disconnectingFromHub");
    setConnectionPendingMode(nextMode);
    setBusy("route");
    const toastId = showToast(`${actionLabel}...`, "loading");
    try {
      let status = forceTakeover
        ? await api.switchMode(nextMode, false, true)
        : await api.switchMode(nextMode, false);
      if (nextMode === "custom" && !status.proxy_running) {
        updateToast(toastId, {
          action: null,
          text: t("gateway.startingBackend"),
          tone: "loading",
        });
        const refreshedStatus = await startProxyForHubConnection();
        status = refreshedStatus ?? status;
      }
      setCodexStatus(status);
      setCodexTargetOwnerOverride(nextMode === "custom" ? appFlavor?.routing_owner ?? null : "official");
      onStatusChanged?.(status);
      setConnectionPendingMode(null);
      setError(null);
      updateToast(toastId, {
        action: null,
        text: t("providers.codexRouteChangedRestart", {
          status: codexHubConnectionSuccessMessage(nextMode, tr),
        }),
        tone: "success",
      });
    } catch (err) {
      const message = messageFromError(err);
      if (isBackendDisconnectedMessage(message)) {
        setConnectionPendingMode(null);
        updateToastWithError(toastId, err);
        return;
      }
      if (!settingsDraft) {
        setConnectionPendingMode(null);
        updateToastWithError(toastId, err);
        return;
      }
      setConnectionPendingMode(null);
      const errorMessage = codexHubConnectionErrorMessage(err, tr);
      setError(errorMessage);
      updateToast(toastId, {
        action: null,
        text: errorMessage,
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  }

  async function reorderOfficialModels(models: Model[]) {
    const nextModels = renumberModels(models);
    setOfficialModels(nextModels);
    setOfficialModelOrderDraft(nextModels.map((model) => model.id));
  }

  async function saveOfficialModels() {
    if (!settingsDraft) {
      return;
    }
    await saveSettings(
      {
        ...settingsDraft,
        official_disabled_models: officialDisabledModelsDraft,
        official_model_sort_order: officialModelOrderDraft,
      },
      true,
      t("providers.officialModelsSaved"),
    );
  }

  async function deleteProvider(providerId: string) {
    const target = providers.find((provider) => provider.id === providerId);
    if (!target) {
      setError(t("providers.providerNotFound", { providerId }));
      return;
    }
    if (!window.confirm(t("providers.deleteProviderConfirm", { name: target.name }))) {
      return;
    }
    const previousProviders = providers;
    const previousSelectedId = selectedId;
    const next = providers.filter((provider) => provider.id !== providerId);
    setSelectedId(next[0]?.id ?? OFFICIAL_ID);
    setProviders(next);
    try {
      const saved = await saveProviders(next, true, t("providers.providerDeleted", { name: target.name }));
      if (saved.some((provider) => provider.id === providerId)) {
        setProviders(saved);
        setSelectedId(providerId);
        setError(t("providers.providerDeleteDidNotPersist", { name: target.name }));
        return;
      }
    } catch {
      setProviders(previousProviders);
      setSelectedId(previousSelectedId);
      return;
    }
    setProbeResult(null);
    setModelDiscoveryError(null);
    setError(null);
  }

  return (
    <>
    <main className="relative grid h-full min-h-0 min-w-[972px] grid-cols-[430px_minmax(0,1fr)] gap-4 overflow-hidden">
      <aside className="min-h-0 min-w-0 overflow-hidden rounded-panel bg-surface shadow-card">
        <ProviderSourceSidebar
          codexAuthState={codexAuthState}
          codexConnected={codexConnected}
          codexForeignOwner={codexOwnedByOtherApp}
          codexOwnerLabel={codexRouteOwnerLabel}
          connectionPendingMode={connectionPendingMode}
          gatewayStatus={gatewayStatus}
          busy={busy}
          enabledProviderModels={enabledProviderModels}
          officialEnabledCount={officialEnabledCount}
          officialIncluded={settings?.include_official_models ?? false}
          officialCount={officialModels.length}
          providerModelCount={providerModelCount}
          onAdd={() => selectProvider(ADD_ID)}
          items={providerNavItems}
          onReorder={(items) => void reorderHubProviders(items)}
          onSelect={selectProvider}
          onToggleOfficialInclude={toggleOfficialInclude}
          onToggleProvider={toggleProviderEnabled}
          onToggleConnection={() => void toggleCodexHubConnection()}
          selectedId={selectedId}
        />
      </aside>

      <section className="min-h-0 min-w-0 overflow-hidden rounded-panel bg-surface shadow-card">
        <div className="grid h-full min-h-0 grid-rows-[minmax(0,1fr)_auto]">
          <div className="min-h-0 overflow-hidden">
            {selectedId === ADD_ID ? (
              <AddProviderPanel
                busy={busy}
                canAdd={Boolean(canAdd)}
                discoverError={modelDiscoveryError}
                form={form}
                probeResult={probeResult}
                onAdd={() => void addProvider()}
                onDiscover={() => void discoverForForm()}
                onFormChange={setForm}
                onProbe={() =>
                  probeUpstreamFormat(form.base_url, form.api_key, formProbeModel())
                }
              />
            ) : selectedId === OFFICIAL_ID ? (
              <OfficialDetail
                authState={codexAuthState}
                busy={busy}
                gatewayContextById={gatewayContextById}
                models={officialModels}
                officialDisabledModels={officialDisabledModels}
                officialIncluded={settings?.include_official_models ?? false}
                authIssue={gatewayStatus?.codex_auth?.issue ?? null}
                onCopyLoginCommand={() => void copyCodexLoginCommand()}
                onContextGuardChanged={reflectContextGuardSetting}
                onOpenCodexApp={() => void openCodexAppForLogin()}
                onRefresh={() => void refreshOfficialModels()}
                onRefreshClients={onRefreshClients}
                onRefreshAuth={() => void refreshCodexAuthStatus()}
                onRefreshUsage={() => void loadOfficialOpenAIUsage(true, true)}
                onReorder={(models) => void reorderOfficialModels(models)}
                onSave={() => void saveOfficialModels()}
                onToggleModel={toggleOfficialModel}
                dirty={officialModelDraftDirty}
                saveBusy={busy === "settings"}
                syncBoundClients={settings?.auto_sync_clients ?? true}
                usageBusy={officialUsageBusy}
                usageError={officialUsageError}
                usageHidden={officialUsageHidden}
                usageSnapshot={officialUsageSnapshot}
              />
            ) : selectedProvider ? (
              <ProviderDetail
                busy={busy}
                discoverError={modelDiscoveryError}
                probeResult={probeResult}
                provider={selectedProvider}
                onChange={(provider) => void updateProvider(provider)}
                onDelete={() => void deleteProvider(selectedProvider.id)}
                onDraftStateChange={trackProviderDraft}
                onProbe={(provider) =>
                  probeUpstreamFormat(
                    provider.base_url,
                    provider.api_key ?? "",
                    providerProbeModel(provider),
                    provider.id,
                  )
                }
                onRefresh={(provider) => void refreshProviderModels(provider)}
              />
            ) : (
              <div className="p-6 text-sm text-slate-500">{t("providers.selectProvider")}</div>
            )}
          </div>

        </div>
      </section>
    </main>
    {pendingProviderNavigation && (
      <UnsavedProviderChangesDialog
        busy={busy === "save"}
        providerName={pendingProviderName(pendingProviderNavigation, tr)}
        onCancel={cancelPendingProviderNavigation}
        onDiscard={discardPendingProviderNavigation}
        onSave={() => void savePendingProviderNavigation()}
      />
    )}
    </>
  );
}
export const ProvidersPage = memo(ProvidersPageImpl);

function UnsavedProviderChangesDialog({
  busy,
  onCancel,
  onDiscard,
  onSave,
  providerName,
}: {
  busy: boolean;
  onCancel: () => void;
  onDiscard: () => void;
  onSave: () => void;
  providerName: string;
}) {
  const { t } = useTranslation();
  const fallbackName = providerName || t("providers.thisProvider");
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/20 p-6">
      <div className="grid w-full max-w-[420px] gap-4 rounded-md border border-line bg-white p-5 shadow-xl">
        <div className="min-w-0">
          <h3 className="truncate text-base font-semibold">{t("providers.saveProviderChanges")}</h3>
          <p className="mt-1 text-sm leading-5 text-slate-600">
            {t("providers.unsavedChanges", { name: fallbackName })}
          </p>
        </div>
        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100"
            disabled={busy}
            onClick={onCancel}
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center rounded-md border border-line bg-white px-3 text-sm font-semibold hover:bg-slate-100"
            disabled={busy}
            onClick={onDiscard}
          >
            {t("common.discard")}
          </button>
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-action px-3 text-sm font-semibold text-white disabled:bg-slate-300"
            disabled={busy}
            onClick={onSave}
          >
            <Save size={16} />
            {t("common.save")}
          </button>
        </div>
      </div>
    </div>
  );
}
function ProviderSourceSidebar({
  busy,
  codexAuthState,
  codexConnected,
  codexForeignOwner,
  codexOwnerLabel,
  connectionPendingMode,
  enabledProviderModels,
  gatewayStatus,
  items,
  officialEnabledCount,
  officialIncluded,
  officialCount,
  providerModelCount,
  onAdd,
  onReorder,
  onSelect,
  onToggleOfficialInclude,
  onToggleProvider,
  onToggleConnection,
  selectedId,
}: {
  busy: string | null;
  codexAuthState: CodexAuthState;
  codexConnected: boolean;
  codexForeignOwner: boolean;
  codexOwnerLabel: string | null;
  connectionPendingMode: ConnectionMode | null;
  enabledProviderModels: number;
  gatewayStatus: GatewayStatus | null;
  items: ProviderNavItem[];
  officialEnabledCount: number;
  officialIncluded: boolean;
  officialCount: number;
  providerModelCount: number;
  onAdd: () => void;
  onReorder: (items: ProviderNavItem[]) => void;
  onSelect: (id: string) => void;
  onToggleOfficialInclude: (included: boolean) => void;
  onToggleProvider: (providerId: string, enabled: boolean) => void;
  onToggleConnection: () => void;
  selectedId: string;
}) {
  const { t } = useTranslation();
  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_auto_minmax(0,1fr)] gap-2 p-3">
      <OfficialOpenAICard
        authState={codexAuthState}
        active={selectedId === OFFICIAL_ID}
        enabledModelCount={officialEnabledCount}
        included={officialIncluded}
        modelCount={officialCount}
        onSelect={() => onSelect(OFFICIAL_ID)}
        onToggleInclude={onToggleOfficialInclude}
        toggleDisabled={codexAuthState !== "authorized"}
      />
      <HubConnectionBridge
        connected={codexConnected}
        foreignOwner={codexForeignOwner}
        ownerLabel={codexOwnerLabel}
        pendingMode={connectionPendingMode}
        disabled={busy === "route" || Boolean(connectionPendingMode)}
        onToggle={onToggleConnection}
      />
      <CodexHubProviderCard
        activeAdd={selectedId === ADD_ID}
        connected={codexConnected}
        enabledModelCount={enabledProviderModels}
        gatewayStatus={gatewayStatus}
        items={items}
        modelCount={providerModelCount}
        selectedId={selectedId}
        onAdd={onAdd}
        onReorder={onReorder}
        onSelect={onSelect}
        onToggleProvider={onToggleProvider}
      />
    </div>
  );
}

function ConnectionLink({ connected }: { connected: boolean }) {
  return (
    <div
      className="pointer-events-none relative flex h-full min-h-[52px] items-center justify-center"
      aria-hidden="true"
    >
      {connected ? (
        <span className="absolute left-1/2 top-[-14px] bottom-[-14px] w-[3px] -translate-x-1/2 overflow-hidden rounded-full bg-gradient-to-t from-emerald-400/60 via-emerald-500/75 to-emerald-400/60">
          <span className="codexhub-flow-beam absolute left-1/2 top-0 h-12 w-[7px] [--flow-distance:92px]" />
          <span className="codexhub-flow-beam codexhub-flow-beam-delay absolute left-1/2 top-0 h-12 w-[7px] [--flow-distance:92px]" />
        </span>
      ) : (
        <>
          <span className="absolute left-1/2 top-[-14px] h-[calc(50%-8px)] w-[3px] -translate-x-1/2 rounded-full bg-slate-300/80" />
          <span className="absolute left-1/2 bottom-[-14px] h-[calc(50%-8px)] w-[3px] -translate-x-1/2 rounded-full bg-slate-300/80" />
        </>
      )}
      <span
        className={cx(
          "relative z-10 grid h-4 w-4 place-items-center rounded-full border transition-[background-color,border-color,box-shadow] duration-200 ease-out",
          connected
            ? "border-emerald-500 bg-emerald-500 shadow-[0_0_0_4px_rgba(16,185,129,0.16)]"
            : "border-slate-300 bg-surface",
        )}
      >
        <span className={cx("h-1.5 w-1.5 rounded-full", connected ? "bg-white" : "bg-slate-300")} />
      </span>
    </div>
  );
}

function ConnectedSurfaceFlow() {
  return (
    <span className="pointer-events-none absolute inset-0 overflow-hidden rounded-[inherit]" aria-hidden="true">
      <span className="codexhub-card-flow absolute left-0 top-0 h-px w-1/2" />
      <span className="codexhub-card-flow codexhub-card-flow-delay absolute bottom-0 left-0 h-px w-1/2" />
    </span>
  );
}

function OfficialOpenAICard({
  active,
  authState,
  enabledModelCount,
  included,
  modelCount,
  onSelect,
  onToggleInclude,
  toggleDisabled,
}: {
  active: boolean;
  authState: CodexAuthState;
  enabledModelCount: number;
  included: boolean;
  modelCount: number;
  onSelect: () => void;
  onToggleInclude: (included: boolean) => void;
  toggleDisabled: boolean;
}) {
  const { t } = useTranslation();
  const authChip = codexAuthChip(authState, t as Translate);

  return (
    <section className="relative grid gap-3 overflow-hidden rounded-panel border border-line bg-surface p-3 shadow-card transition-[background-color,border-color,box-shadow] duration-150 ease-out">
      <div className="rounded-inner text-left">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold">{t("providers.codexDesktop")}</h2>
            <p className="mt-1 text-xs text-slate-500">{t("providers.codexAppAuth")}</p>
          </div>
          <SourceStatusChip {...authChip} />
        </div>
      </div>

      <div className="rounded-inner bg-surface shadow-control">
        <ProviderNavButton
          active={active}
          activeTone="neutral"
          enabled={included}
          label="OpenAI"
          meta={t("providers.modelCount", { enabled: enabledModelCount, total: modelCount })}
          onClick={onSelect}
          onToggle={onToggleInclude}
          toggleDisabled={toggleDisabled}
          toggleLabel={included ? t("providers.openaiSourceIncluded") : t("providers.openaiSourceExcluded")}
        />
      </div>
      <p className="px-1 text-[11px] leading-4 text-slate-500">{t("providers.openaiExportHint")}</p>
    </section>
  );
}

function HubConnectionBridge({
  connected,
  disabled,
  foreignOwner,
  onToggle,
  ownerLabel,
  pendingMode,
}: {
  connected: boolean;
  disabled: boolean;
  foreignOwner: boolean;
  onToggle: () => void;
  ownerLabel: string | null;
  pendingMode: ConnectionMode | null;
}) {
  const { t } = useTranslation();
  const label = pendingMode === "custom"
    ? t("providers.connecting")
    : pendingMode === "official"
      ? t("providers.disconnecting")
    : connected
      ? ownerLabel
        ? t("providers.connectedToHubChannel", { channel: ownerLabel })
        : t("providers.connectedToHub")
      : t("providers.connectToHub");
  const icon = pendingMode === "official" || (!pendingMode && !connected)
    ? <Link2Off size={15} className={pendingMode ? "opacity-70" : undefined} />
    : <Link2 size={15} className={pendingMode ? "opacity-70" : undefined} />;

  return (
    <div className="relative grid grid-cols-[44px_minmax(0,1fr)] items-center gap-2.5 px-1 py-1.5">
      <ConnectionLink connected={connected} />
      <button
        type="button"
        className={cx(
          "focus-ring flex h-11 min-w-0 items-center justify-center gap-2 rounded-full px-4 text-sm font-semibold shadow-control transition-[box-shadow,background-color,color,transform] duration-200 ease-out active:scale-[0.97] disabled:opacity-100",
          pendingMode && "animate-pulse bg-slate-200/85 text-slate-600",
          !pendingMode && foreignOwner
            ? "border border-emerald-200 bg-emerald-100 text-emerald-700 hover:bg-emerald-200 hover:shadow-raised"
            : !pendingMode && connected
              ? "bg-emerald-600 text-white hover:bg-emerald-700 hover:shadow-raised"
            : !pendingMode && "bg-ink text-white hover:bg-slate-800 hover:shadow-raised",
        )}
        disabled={disabled}
        onClick={onToggle}
        title={
          pendingMode === "custom"
            ? t("providers.connectingToHub")
            : pendingMode === "official"
              ? t("providers.disconnectingFromHub")
               : foreignOwner
                 ? t("providers.takeOverFromChannelTitle", { channel: ownerLabel ?? t("common.unknown") })
               : connected
                 ? t("providers.disconnectFromHubTitle")
                : t("providers.connectToHubTitle")
        }
      >
        {icon}
        {label}
      </button>
    </div>
  );
}

function CodexHubProviderCard({
  activeAdd,
  connected,
  enabledModelCount,
  gatewayStatus,
  items,
  modelCount,
  onAdd,
  onReorder,
  onSelect,
  onToggleProvider,
  selectedId,
}: {
  activeAdd: boolean;
  connected: boolean;
  enabledModelCount: number;
  gatewayStatus: GatewayStatus | null;
  items: ProviderNavItem[];
  modelCount: number;
  onAdd: () => void;
  onReorder: (items: ProviderNavItem[]) => void;
  onSelect: (id: string) => void;
  onToggleProvider: (providerId: string, enabled: boolean) => void;
  selectedId: string;
}) {
  const [providerListRef, providerListHasOverflow] = useVerticalOverflow<HTMLDivElement>([
    activeAdd,
    connected,
    items.length,
    selectedId,
  ]);
  const { t } = useTranslation();
  return (
    <section
      className={cx(
        "relative grid h-full min-h-0 grid-rows-[auto_auto_minmax(0,1fr)_auto] gap-3 overflow-hidden rounded-panel border px-3 pt-3 shadow-card transition-[background-color,border-color,box-shadow]",
        connected
          ? "border-emerald-300/70 bg-emerald-50/55 shadow-[0_0_0_1px_rgba(16,185,129,0.08),0_18px_40px_rgba(15,118,110,0.10)]"
          : "border-transparent bg-surface",
        "pb-3",
      )}
    >
      {connected && <ConnectedSurfaceFlow />}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold">{t("common.codexHub")}</h2>
          <p className="mt-1 truncate text-xs text-slate-500">{t("providers.externalProviderCatalog")}</p>
          <p className="mt-1 truncate whitespace-nowrap text-xs leading-4 text-slate-500">
            {t("providers.appsMaySortModels")}
          </p>
        </div>
        <SourceStatusChip {...gatewayStatusChip(gatewayStatus, t as Translate)} />
      </div>

      <div className="grid gap-2">
        <div className="grid grid-cols-2 gap-2 px-px text-xs">
          <SourceMetric label={t("common.models")} value={String(modelCount)} />
          <SourceMetric label={t("common.enabled")} value={String(enabledModelCount)} />
        </div>
      </div>

      <div
        ref={providerListRef}
        className={cx("min-h-0 overflow-auto", providerListHasOverflow && "-mr-3 pr-1")}
      >
        {items.length ? (
          <SortableList
            className="space-y-2"
            items={items}
            getId={(item) => item.id}
            onReorder={onReorder}
            renderItem={(item) => (
              <ProviderNavButton
                active={selectedId === item.provider.id}
                enabled={item.provider.enabled}
                label={item.provider.name}
                meta={t("providers.modelCount", {
                  enabled: item.provider.models.filter((model) => model.enabled).length,
                  total: item.provider.models.length,
                })}
                onClick={() => onSelect(item.provider.id)}
                onToggle={(enabled) => onToggleProvider(item.provider.id, enabled)}
                highlightShape="right"
              />
            )}
          />
        ) : (
          <div className="grid min-h-[96px] place-items-center rounded-inner bg-panel-soft px-3 text-center text-xs text-slate-500 shadow-hairline">
            {t("providers.addHubProviderEmpty")}
          </div>
        )}
      </div>

      <button
        type="button"
        className={cx(
          "focus-ring flex h-10 w-full items-center justify-center gap-2 rounded-control text-sm font-medium shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out active:scale-[0.96]",
          activeAdd ? "bg-action/10 text-action" : "bg-panel-soft text-slate-600 hover:bg-white hover:shadow-raised",
        )}
        onClick={onAdd}
      >
        <Plus size={15} />
        {t("providers.addProvider")}
      </button>
    </section>
  );
}

function gatewayStatusChip(status: GatewayStatus | null, t: Translate): { label: string; tone: "ok" | "muted" | "pending" } {
  if (!status) {
    return { label: t("common.unknown"), tone: "pending" };
  }
  return status.proxy_running
    ? { label: t("runtime.running"), tone: "ok" }
    : { label: t("runtime.stopped"), tone: "muted" };
}

function codexAuthChip(authState: CodexAuthState, t: Translate): { label: string; tone: "ok" | "muted" | "pending" } {
  if (authState === "authorized") {
    return { label: t("providers.authorized"), tone: "ok" };
  }
  if (authState === "missing") {
    return { label: t("providers.authMissing"), tone: "pending" };
  }
  return { label: t("providers.authUnknown"), tone: "muted" };
}

function SourceStatusChip({ label, tone }: { label: string; tone: "ok" | "muted" | "pending" }) {
  return (
    <span
      className={cx(
        "inline-flex h-6 max-w-[112px] items-center rounded-full border px-2 text-[11px] font-semibold leading-none",
        tone === "ok" && "border-emerald-200 bg-emerald-50 text-emerald-700",
        tone === "muted" && "border-slate-200 bg-white text-slate-500",
        tone === "pending" && "border-amber-200 bg-amber-50 text-amber-700",
      )}
    >
      <span className="truncate whitespace-nowrap">{label}</span>
    </span>
  );
}

function SourceMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid min-w-0 place-items-center rounded-inner bg-surface px-2 py-1.5 text-center shadow-control">
      <div className="text-[9px] font-semibold uppercase leading-3 text-slate-500">{label}</div>
      <div className="mt-0.5 font-semibold leading-4 text-ink">{value}</div>
    </div>
  );
}

function ProviderNavButton({
  active,
  activeTone = "default",
  enabled,
  highlightShape = "full",
  label,
  meta,
  onClick,
  onToggle,
  toggleDisabled = false,
  toggleLabel,
}: {
  active: boolean;
  activeTone?: "default" | "neutral";
  enabled: boolean;
  highlightShape?: "full" | "right";
  label: string;
  meta: string;
  onClick: () => void;
  onToggle: (enabled: boolean) => void;
  toggleDisabled?: boolean;
  toggleLabel?: string;
}) {
  const { t } = useTranslation();
  return (
    <div
      className={cx(
        "grid min-h-[58px] w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-3 py-2 text-sm transition-[box-shadow,background-color] duration-150 ease-out",
        highlightShape === "right" ? "rounded-r-inner" : "rounded-inner",
        toggleDisabled
          ? "bg-slate-50 text-slate-400 shadow-control ring-1 ring-slate-200"
          : active
          ? activeTone === "neutral"
            ? "bg-panel-soft text-ink shadow-raised ring-1 ring-line"
            : "bg-blue-50 text-action shadow-raised"
          : "hover:bg-panel hover:shadow-control",
      )}
    >
      <button type="button" className="focus-ring min-w-0 text-left" onClick={onClick}>
        <span className="block truncate font-semibold">{label}</span>
        <span className="block truncate text-xs text-slate-500">{meta}</span>
      </button>
      <SwitchControl
        checked={enabled}
        label={toggleLabel ?? (enabled ? t("providers.providerEnabled") : t("providers.providerDisabled"))}
        disabled={toggleDisabled}
        showLabel={false}
        onChange={onToggle}
      />
    </div>
  );
}

function OfficialDetail({
  authIssue,
  authState,
  busy,
  dirty,
  gatewayContextById,
  models,
  officialDisabledModels,
  officialIncluded,
  onCopyLoginCommand,
  onContextGuardChanged,
  onOpenCodexApp,
  onRefresh,
  onRefreshClients,
  onRefreshAuth,
  onRefreshUsage,
  onReorder,
  onSave,
  onToggleModel,
  saveBusy,
  syncBoundClients,
  usageBusy,
  usageError,
  usageHidden,
  usageSnapshot,
}: {
  authIssue: string | null;
  authState: CodexAuthState;
  busy: string | null;
  dirty: boolean;
  gatewayContextById: Map<string, number>;
  models: Model[];
  officialDisabledModels: string[];
  officialIncluded: boolean;
  onCopyLoginCommand: () => void;
  onContextGuardChanged: (enabled: boolean) => void;
  onOpenCodexApp: () => void;
  onRefresh: () => void;
  onRefreshClients?: () => Promise<void>;
  onRefreshAuth: () => void;
  onRefreshUsage: () => void;
  onReorder: (models: Model[]) => void;
  onSave: () => void;
  onToggleModel: (modelId: string, enabled: boolean) => void;
  saveBusy: boolean;
  syncBoundClients: boolean;
  usageBusy: boolean;
  usageError: string | null;
  usageHidden: boolean;
  usageSnapshot: OpenAIUsageSnapshot | null;
}) {
  const { t } = useTranslation();
  const { showToast, updateToast } = useToasts();
  const authorized = authState === "authorized";
  const authRefreshBusy = busy === "auth-refresh";
  const [contextGuardStatus, setContextGuardStatus] = useState<CodexContextGuardStatus | null>(null);
  const [contextGuardBusy, setContextGuardBusy] = useState(false);
  const displayedGatewayContextById = useMemo(() => {
    if (!contextGuardStatus?.gateway_enabled) {
      return gatewayContextById;
    }
    return new Map(
      Array.from(gatewayContextById, ([modelId, contextWindow]) => [
        modelId,
        Math.min(
          contextWindow,
          contextGuardStatus.model_context_window ?? contextWindow,
        ),
      ]),
    );
  }, [
    contextGuardStatus?.gateway_enabled,
    contextGuardStatus?.model_context_window,
    gatewayContextById,
  ]);

  useEffect(() => {
    let active = true;
    void api.getCodexContextGuardStatus()
      .then((status) => {
        if (active) {
          setContextGuardStatus(status);
        }
      })
      .catch((err) => {
        if (active) {
          showToast(t("providers.contextGuardStatusFailed", { message: messageFromError(err) }), "error");
        }
      });
    return () => {
      active = false;
    };
  }, [showToast, t]);

  async function toggleContextGuard(enabled: boolean) {
    if (contextGuardBusy) {
      return;
    }
    setContextGuardBusy(true);
    const toastId = showToast(
      enabled ? t("providers.enablingContextGuard") : t("providers.disablingContextGuard"),
      "loading",
    );
    try {
      const status = await api.setCodexContextGuard(enabled);
      setContextGuardStatus(status);
      onContextGuardChanged(status.gateway_enabled);
      let syncResult: GatewayClientSyncSummary | null = null;
      let syncResultUncertain = false;
      if (syncBoundClients) {
        updateToast(toastId, {
          action: null,
          text: t("providers.syncBoundClients"),
          tone: "loading",
        });
        try {
          syncResult = await api.syncGatewayClients();
        } catch {
          syncResultUncertain = true;
        }
        await onRefreshClients?.().catch(() => undefined);
      }
      const restartMessage = enabled
        ? t("providers.contextGuardEnabledRestartCodex")
        : t("providers.contextGuardDisabledRestartCodex");
      const appliedClientCount = syncResult?.applied ?? 0;
      const failedClientCount = syncResult?.failed ?? 0;
      let clientSyncFeedback: { text: string; tone: "error" | "success" };
      if (!syncBoundClients) {
        clientSyncFeedback = {
          text: t("providers.contextGuardClientsAutoSyncDisabled", { restartMessage }),
          tone: "success",
        };
      } else if (syncResultUncertain) {
        clientSyncFeedback = {
          text: t("providers.contextGuardClientSyncError", { restartMessage }),
          tone: "error",
        };
      } else if (failedClientCount > 0 && appliedClientCount > 0) {
        clientSyncFeedback = {
          text: t("providers.contextGuardClientsPartiallySyncedRestart", { restartMessage }),
          tone: "error",
        };
      } else if (failedClientCount > 0) {
        clientSyncFeedback = {
          text: t("providers.contextGuardClientsSyncFailed", { restartMessage }),
          tone: "error",
        };
      } else if (appliedClientCount > 0) {
        clientSyncFeedback = {
          text: t("providers.contextGuardClientsSyncedRestart", { restartMessage }),
          tone: "success",
        };
      } else {
        clientSyncFeedback = {
          text: t("providers.contextGuardClientsNotUpdated", { restartMessage }),
          tone: "success",
        };
      }
      updateToast(toastId, {
        action: null,
        ...clientSyncFeedback,
      });
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: t("providers.contextGuardUpdateFailed", { message: messageFromError(err) }),
        tone: "error",
      });
    } finally {
      setContextGuardBusy(false);
    }
  }

  async function testOfficialModel(model: Model) {
    const label = displayModel(model);
    const endpointLabel = upstreamFormatLabel("responses", t as Translate);
    const toastId = showToast(t("providers.testingModel", { label, endpoint: endpointLabel }), "loading");
    try {
      const result = await api.gatewayTestRequest("responses_stream", model.id);
      if (!result.ok) {
        throw new Error(result.error || result.sanitized_body || `HTTP ${result.status ?? "unknown"}`);
      }
      updateToast(toastId, {
        action: null,
        text: t("gateway.connectedHttp", { label, endpoint: endpointLabel, status: result.status }),
        tone: "success",
      });
      return true;
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: t("gateway.connectionFailed", { label, endpoint: endpointLabel, message: messageFromError(err) }),
        tone: "error",
      });
      return false;
    }
  }

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)_auto]">
      <div className="grid gap-3 border-b border-line p-4">
        <HeaderRow
          title={t("common.codex")}
          subtitle={t("providers.openaiSubscriptionCatalog")}
          titleAccessory={
            <SourceStatusChip {...codexAuthChip(authState, t as Translate)} />
          }
          actions={
            authorized && (
              <>
                <OfficialOpenAIUsageLimitBars busy={usageBusy} limits={usageSnapshot?.limits ?? []} />
                <button
                  type="button"
                  className="focus-ring grid h-7 w-7 place-items-center rounded-control bg-surface text-slate-600 shadow-control hover:bg-white disabled:text-slate-300"
                  disabled={usageBusy}
                  aria-label={t("providers.refreshOpenAIUsage")}
                  title={t("providers.refreshOpenAIUsage")}
                  onClick={onRefreshUsage}
                >
                  <RefreshCcw size={14} className={usageBusy ? "animate-spin" : undefined} />
                </button>
              </>
            )
          }
        />
        {!officialIncluded && (
          <div className="rounded-inner border border-amber-200 bg-amber-50 px-3 py-2 text-xs font-medium leading-5 text-amber-800 shadow-hairline">
            {t("providers.openaiSourceExcludedDetail")}
          </div>
        )}
        {authorized ? (
          <OfficialOpenAIUsagePanel
            busy={usageBusy}
            error={usageError}
            snapshot={usageSnapshot}
            usageHidden={usageHidden}
          />
        ) : (
          <CodexAuthPrompt
            authIssue={authIssue}
            authState={authState}
            busy={authRefreshBusy}
            onCopyLoginCommand={onCopyLoginCommand}
            onOpenCodexApp={onOpenCodexApp}
            onRefreshAuth={onRefreshAuth}
          />
        )}
      </div>
      <ModelSection
        contextById={displayedGatewayContextById}
        disabled
        headerControl={
          <div className="group relative">
            <SwitchControl
              ariaDescribedBy="context-guard-tooltip"
              checked={contextGuardStatus?.enabled ?? false}
              className="h-7"
              disabled={contextGuardBusy || !contextGuardStatus}
              label={t("providers.contextGuard")}
              onChange={(enabled) => void toggleContextGuard(enabled)}
            />
            <div
              id="context-guard-tooltip"
              role="tooltip"
              className="pointer-events-none absolute bottom-full right-0 z-30 mb-2 hidden w-80 whitespace-normal rounded-inner bg-ink px-3 py-2 text-left text-xs font-medium leading-5 text-white shadow-floating group-hover:block group-focus-within:block"
            >
              {t("providers.contextGuardTooltip")}
            </div>
          </div>
        }
        interactionDisabled={authState !== "authorized"}
        models={models}
        officialDisabledModels={officialDisabledModels}
        onRefresh={onRefresh}
        onReorder={onReorder}
        onTestModel={testOfficialModel}
        refreshBusy={busy === "official-refresh"}
        onToggleOfficialModel={onToggleModel}
        modelTestDisabled={authState !== "authorized"}
      />
      <div className="flex items-center justify-end border-t border-line px-5 py-3">
        <button
          type="button"
          className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-action px-3 text-sm font-semibold text-white disabled:bg-slate-300"
          disabled={!dirty || saveBusy}
          onClick={onSave}
        >
          <Save size={16} />
          {t("common.save")}
        </button>
      </div>
    </div>
  );
}

function CodexAuthPrompt({
  authIssue,
  authState,
  busy,
  onCopyLoginCommand,
  onOpenCodexApp,
  onRefreshAuth,
}: {
  authIssue: string | null;
  authState: CodexAuthState;
  busy: boolean;
  onCopyLoginCommand: () => void;
  onOpenCodexApp: () => void;
  onRefreshAuth: () => void;
}) {
  const { t } = useTranslation();
  const title = authState === "unknown"
    ? t("providers.codexAuthUnknownTitle")
    : t("providers.codexAuthRequiredTitle");

  return (
    <section className="grid gap-3 rounded-inner bg-amber-50/70 p-3 text-sm shadow-hairline">
      <div className="min-w-0">
        <h3 className="truncate text-sm font-semibold text-ink">{title}</h3>
        <p className="mt-1 text-xs leading-5 text-slate-700">{t("providers.codexAuthRequiredBody")}</p>
        {authIssue && (
          <p className="mt-1 truncate text-xs text-slate-500" title={authIssue}>
            {authIssue}
          </p>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="focus-ring flex h-9 min-w-0 items-center gap-2 rounded-control bg-ink px-3 text-xs font-semibold text-white shadow-control hover:bg-slate-800"
          onClick={onOpenCodexApp}
        >
          <ExternalLink size={15} />
          <span className="truncate">{t("providers.openCodexApp")}</span>
        </button>
        <button
          type="button"
          className="focus-ring flex h-9 min-w-0 items-center gap-2 rounded-control bg-surface px-3 text-xs font-semibold text-slate-700 shadow-control hover:bg-white"
          onClick={onCopyLoginCommand}
        >
          <Copy size={15} />
          <span className="truncate">{t("providers.copyCodexLoginCommand")}</span>
        </button>
        <button
          type="button"
          className="focus-ring flex h-9 min-w-0 items-center gap-2 rounded-control bg-surface px-3 text-xs font-semibold text-slate-700 shadow-control hover:bg-white disabled:text-slate-300"
          disabled={busy}
          onClick={onRefreshAuth}
        >
          <RefreshCcw size={15} className={busy ? "animate-spin" : undefined} />
          <span className="truncate">{t("providers.refreshCodexAuth")}</span>
        </button>
      </div>
    </section>
  );
}

function withDefaultFastVariants(settings: Settings): Settings {
  return normalizeSettings(settings);
}

function isAddProviderFormDirty(form: AddProviderForm) {
  return Boolean(form.name.trim());
}

function pendingProviderName(
  pending: PendingProviderNavigation<Provider, AddProviderForm>,
  t: Translate,
) {
  if (pending.kind === "existing") {
    return pending.draft.name;
  }
  return pending.form.name.trim() || t("providers.newProvider");
}

function codexHubConnectionErrorMessage(err: unknown, t: Translate) {
  const message = messageFromError(err);

  if (message.includes("route.takeover_required")) {
    return t("providers.betaTakeoverRequired");
  }
  if (message.includes("route.owner_mismatch")) {
    return t("providers.betaOwnerConflict");
  }

  return t("providers.codexHubConnectionFailed", { message });
}

function codexTakeoverOwnerLabel(owner: AppFlavorInfo["codex_target_owner"], t: Translate) {
  if (owner === null) return t("providers.betaTakeoverUnowned");
  if (owner === "official") return t("common.official");
  if (owner === "release") return t("gateway.ownerRelease");
  if (owner === "beta") return t("gateway.ownerBeta");
  return t("gateway.ownerExternal");
}

function codexHubConnectionSuccessMessage(mode: string, t: Translate) {
  return mode === "custom" ? t("providers.connectedToHub") : t("providers.disconnectedFromHub");
}

function readCodexAuthPreviewState(): CodexAuthState | null {
  if (typeof window === "undefined" || (!import.meta.env.DEV && !isLocalHttpPreviewLocation(window.location))) {
    return null;
  }
  const value = new URLSearchParams(window.location.search).get("codexAuth");
  return value === "authorized" || value === "missing" || value === "unknown" ? value : null;
}

function clearCodexAuthPreviewParam() {
  if (typeof window === "undefined" || !window.location.search.includes("codexAuth=")) {
    return;
  }
  const url = new URL(window.location.href);
  url.searchParams.delete("codexAuth");
  window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
}

function isLocalHttpPreviewLocation(location: Location) {
  return (
    location.protocol === "http:" &&
    (location.hostname === "127.0.0.1" || location.hostname === "localhost" || location.hostname === "::1")
  );
}

function isUnknownCodexHubCommand(message: string, command: string) {
  return message.toLowerCase().includes(`unknown codexhub command: ${command}`.toLowerCase());
}

function codexAuthStateFromGatewayStatus(status: GatewayStatus | null): CodexAuthState {
  if (!status) {
    return "unknown";
  }
  const auth = status.codex_auth;
  if (auth.logged_in || auth.access_token_present || auth.account_id_present) {
    return "authorized";
  }
  return "missing";
}

function Toggle({
  checked,
  label,
  onChange,
}: {
  checked: boolean;
  label: string;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex h-9 items-center justify-between gap-3 rounded-md border border-line bg-panel px-3 text-sm font-medium">
      <span className="truncate">{label}</span>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    </label>
  );
}
