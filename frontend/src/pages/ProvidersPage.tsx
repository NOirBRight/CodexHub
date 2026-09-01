import {
  Copy,
  ExternalLink,
  Link2,
  Link2Off,
  Plus,
  RefreshCcw,
  Save,
} from "lucide-react";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useConfirmDialog } from "../components/ConfirmDialog";
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
import { ProviderCatalogPicker } from "../components/providers/ProviderCatalogPicker";
import { providerLogoSrc } from "../lib/providerLogos";
import { HeaderRow } from "../components/providers/ProviderFormControls";
import {
  isOfficialModelDisabled,
  ModelSection,
  modelIdMatches,
  SwitchControl,
} from "../components/providers/ProviderModelSection";
import {
  ADD_ID,
  formProbeModelFor,
  OFFICIAL_ID,
  providerProbeModelFor,
  useProviderWorkspace,
  type PendingProviderNavigation,
} from "../hooks/useProviderWorkspace";
import { useVerticalOverflow } from "../hooks/useVerticalOverflow";
import { cx, displayModel, renumberModels } from "../lib/format";
import { emptyProvider, type AddProviderForm } from "../lib/providerForm";
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

type ProviderNavItem =
  { id: string; sort_order: number; provider: Provider };
type CodexAuthState = "authorized" | "missing" | "unknown";
type ConnectionMode = "official" | "custom";
export type CodexSwitchRequest = { id: number; mode: ConnectionMode };
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
  onStartProxy?: () => Promise<AppStatus | null>;
  onStatusChanged?: (status: AppStatus) => void;
  codexSwitchRequest?: CodexSwitchRequest | null;
  onCodexSwitchRequestHandled?: (id: number) => void;
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
  codexSwitchRequest,
  onCodexSwitchRequestHandled,
  providers: providersSnapshot,
  settings: settingsSnapshot,
}: ProvidersPageProps) {
  const { t } = useTranslation();
  const { confirm: confirmAction, dialog: confirmDialog } = useConfirmDialog();
  const tr = t as Translate;
  const toast = useToasts();
  const { showToast, updateToast } = toast;
  const authorizeCodexRestartRef = useRef<() => Promise<boolean | null>>(async () => null);
  const initialOfficialUsageSnapshot = useMemo(() => readStoredOfficialOpenAIUsageSnapshot(), []);
  const normalizedSettingsSnapshot = useMemo(
    () => settingsSnapshot ? withDefaultFastVariants(settingsSnapshot) : null,
    [settingsSnapshot],
  );
  const [codexAuthPreviewState, setCodexAuthPreviewState] = useState<CodexAuthState | null>(() => readCodexAuthPreviewState());
  const workspace = useProviderWorkspace({
    getSource: () => ({
      catalogModels,
      modelMetadata,
      providers: providersSnapshot,
      settings: normalizedSettingsSnapshot,
    }),
    onProvidersChanged,
    onSettingsChanged,
    refreshGatewayState: async () => {
      await onGatewayChanged?.();
    },
    authorizeCodexRestart: () => authorizeCodexRestartRef.current(),
    toast,
    t,
    tr,
  });
  const {
    busy,
    form,
    modelDiscoveryError,
    officialDisabledModelsDraft,
    officialModelOrderDraft,
    probeResult,
    providers,
    settings,
    settingsDraft,
  } = workspace.state;
  const setProviders = (value: Provider[]) => workspace.edit({ type: "setProviders", providers: value });
  const setSettings = (value: Settings | null | ((current: Settings | null) => Settings | null)) => {
    const next = typeof value === "function" ? value(workspace.state.settings) : value;
    if (next) workspace.edit({ type: "setSettings", settings: next });
  };
  const setSettingsDraft = setSettings;
  const setOfficialDisabledModelsDraft = (disabled: string[]) =>
    workspace.edit({ type: "setOfficialDisabledModelsDraft", disabled });
  const setOfficialModelOrderDraft = (order: string[]) =>
    workspace.edit({ type: "setOfficialModelOrderDraft", order });
  const setForm = (next: AddProviderForm) => workspace.edit({ type: "updateForm", form: next });
  const setProbeResult = (result: UpstreamFormatProbeResult | null) =>
    workspace.edit({ type: "setProbeResult", result });
  const setBusy = (next: string | null) => workspace.edit({ type: "setBusy", busy: next });
  const setModelDiscoveryError = (error: string | null) =>
    workspace.edit({ type: "setDiscoveryError", error });
  const handledCodexSwitchRequestRef = useRef<number | null>(null);
  const [codexStatus, setCodexStatus] = useState<AppStatus | null>(appStatusSnapshot);
  const [connectionPendingMode, setConnectionPendingMode] = useState<ConnectionMode | null>(null);
  const [codexTargetOwnerOverride, setCodexTargetOwnerOverride] =
    useState<AppFlavorInfo["codex_target_owner"] | undefined>(undefined);
  const [loadedGatewayStatus, setLoadedGatewayStatus] = useState<GatewayStatus | null>(gatewayStatusSnapshot ?? null);
  const [codexAuthState, setCodexAuthState] = useState<CodexAuthState>(() => codexAuthPreviewState ?? "unknown");
  const [officialCollaborationOverrides, setOfficialCollaborationOverrides] = useState<
    Record<string, "v1" | "v2">
  >({});
  const [officialCollaborationBaselines, setOfficialCollaborationBaselines] = useState<
    Readonly<Record<string, "v1" | "v2">>
  >({});
  const [officialUsageSnapshot, setOfficialUsageSnapshot] = useState<OpenAIUsageSnapshot | null>(initialOfficialUsageSnapshot);
  const [officialUsageBusy, setOfficialUsageBusy] = useState(false);
  const [officialUsageError, setOfficialUsageError] = useState<string | null>(null);
  const [officialUsageHidden, setOfficialUsageHidden] = useState(false);
  const officialUsageSnapshotRef = useRef<OpenAIUsageSnapshot | null>(null);
  const [catalogPickerOpen, setCatalogPickerOpen] = useState(false);
  const [catalogPresets, setCatalogPresets] = useState<Provider[] | null>(null);
  const selectedId = workspace.state.selectedId;
  const officialModels = workspace.state.officialModels;
  const setOfficialModels = (value: Model[] | ((current: Model[]) => Model[])) => {
    const next = typeof value === "function" ? value(workspace.state.officialModels) : value;
    workspace.edit({ type: "setOfficialModels", models: next });
  };
  const { selectProvider, trackProviderDraft } = workspace;
  const setSelectedId = (value: string | ((current: string) => string)) => {
    const next = typeof value === "function" ? value(workspace.state.selectedId) : value;
    workspace.setSelectedId(next);
  };
  const pendingNewProvider = workspace.state.pendingNewProvider;
  const setPendingNewProvider = (value: Provider | null | ((current: Provider | null) => Provider | null)) => {
    const next = typeof value === "function" ? value(workspace.state.pendingNewProvider) : value;
    if (next) {
      workspace.edit({ type: "setPendingNewProvider", provider: next });
    } else {
      workspace.edit({ type: "clearPendingNewProvider" });
    }
  };
  function addCatalogProvider(preset: Provider) {
    workspace.edit({ type: "setProviders", providers });
    workspace.edit({ type: "stageCatalogPreset", preset });
  }
  const pendingProviderNavigation = workspace.navigation.pending;
  const cancelPendingProviderNavigation = () => {
    void workspace.navigation.resolve("cancel");
  };
  const discardPendingProviderNavigation = () => {
    setForm(emptyProvider);
    void workspace.navigation.resolve("discard");
  };
  const savePendingProviderNavigation = () => workspace.navigation.resolve("save");
  const editWorkspace = workspace.edit;
  useEffect(() => {
    editWorkspace({ type: "updateForm", form });
  }, [editWorkspace, form]);
  async function saveProviders(
    next: Provider[],
    regenerateCatalog = true,
    successMessage?: string,
    toastId?: string,
  ): Promise<Provider[]> {
    const result = await workspace.act({
      type: "saveProviders",
      providers: next,
      successMessage,
      toastId,
      skipPublish: !regenerateCatalog,
    });
    if (result.kind === "ok") {
      return result.providers ?? next;
    }
    if (result.kind === "error") {
      throw new Error(result.message);
    }
    return providers;
  }
  async function discoverForForm() {
    setBusy("discover");
    try {
      const result = await workspace.act({ type: "discoverForForm", form });
      if (result.kind === "ok" && result.form) {
        setForm(result.form);
      }
      setModelDiscoveryError(result.kind === "error" ? result.message : null);
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
    try {
      const result = await workspace.act({
        type: "probe",
        baseUrl,
        apiKey,
        model,
        providerId,
      });
      if (result.kind === "ok" && result.probeResult) {
        setProbeResult(result.probeResult);
        if (result.providers) {
          setProviders(result.providers);
        }
        setError(null);
        return result.probeResult;
      }
      return null;
    } finally {
      setBusy(null);
    }
  }
  async function addProvider() {
    setBusy("save");
    try {
      const result = await workspace.act({ type: "saveAddForm", form });
      if (result.kind === "ok") {
        if (result.providers) {
          setProviders(result.providers);
        }
        setForm(emptyProvider);
        setError(null);
        return;
      }
      if (result.kind === "error") {
        setError(result.message);
      }
    } finally {
      setBusy(null);
    }
  }
  async function refreshProviderModels(provider: Provider) {
    setBusy(provider.id);
    try {
      const result = await workspace.act({ type: "discoverProviderModels", providerId: provider.id });
      if (result.kind === "ok") {
        if (result.providers) {
          setProviders(result.providers);
        }
        setModelDiscoveryError(null);
        return;
      }
      if (result.kind === "error") {
        setModelDiscoveryError(result.message);
      }
    } finally {
      setBusy(null);
    }
  }

  useEffect(() => {
    let active = true;
    void Promise.all([
      api.listOfficialMultiAgentOverrides(),
      api.listOfficialMultiAgentBaselines(),
    ])
      .then(([overrides, baselines]) => {
        if (active) {
          setOfficialCollaborationOverrides(overrides);
          setOfficialCollaborationBaselines(baselines);
        }
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [catalogModels]);

  useEffect(() => {
    setCodexStatus(appStatusSnapshot);
  }, [appStatusSnapshot]);

  useEffect(() => {
    officialUsageSnapshotRef.current = officialUsageSnapshot;
  }, [officialUsageSnapshot]);

  useEffect(() => {
    if (selectedId !== OFFICIAL_ID || codexAuthState !== "authorized") {
      return;
    }
    void primeOfficialOpenAIUsage();
    const usageRefreshTimer = window.setInterval(
      () => void loadOfficialOpenAIUsage(false, false, undefined, { showBusy: false }),
      OPENAI_USAGE_REFRESH_INTERVAL_MS,
    );
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

  const selectedProvider = workspace.selectedProvider;
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
    const items = providers
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
    if (pendingNewProvider && !items.some((item) => item.id === pendingNewProvider.id)) {
      items.push({
        id: pendingNewProvider.id,
        sort_order: pendingNewProvider.sort_order ?? items.length + 1,
        provider: pendingNewProvider,
      });
    }
    return items;
  }, [pendingNewProvider, providers]);
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

  async function primeOfficialOpenAIUsage() {
    if (!officialUsageSnapshotRef.current) {
      await loadOfficialOpenAIUsage(false, false, undefined, { showBusy: false });
    }
    void loadOfficialOpenAIUsage(false, false, undefined, { showBusy: false });
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
    const result = await workspace.act({
      type: "saveSettings",
      settings: next,
      regenerateCatalog,
      successMessage,
      toastId,
    });
    if (result.kind === "ok") {
      setSettings(next);
      setSettingsDraft(next);
      setError(null);
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
    if (pendingNewProvider && next.id === pendingNewProvider.id) {
      await saveProviders([...providers, next], true, successMessage);
      setPendingNewProvider(null);
      return;
    }
    await saveProviders(
      providers.map((provider) => (provider.id === next.id ? next : provider)),
      true,
      successMessage,
    );
  }

  function toggleProviderEnabled(providerId: string, enabled: boolean) {
    if (pendingNewProvider?.id === providerId) {
      setPendingNewProvider({ ...pendingNewProvider, enabled });
      return;
    }
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
    const persistedItems = pendingNewProvider
      ? items.filter((item) => item.id !== pendingNewProvider.id)
      : items;
    const nextProviders = providers.map((provider) => provider);

    persistedItems.forEach((item, index) => {
      const sortOrder = index + 1;
      const providerIndex = nextProviders.findIndex((provider) => provider.id === item.id);
      if (providerIndex >= 0) {
        nextProviders[providerIndex] = { ...nextProviders[providerIndex], sort_order: sortOrder };
      }
    });
    if (pendingNewProvider) {
      const pendingIndex = items.findIndex((item) => item.id === pendingNewProvider.id);
      if (pendingIndex >= 0) {
        setPendingNewProvider({ ...pendingNewProvider, sort_order: pendingIndex + 1 });
      }
    }

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

  async function authorizeCodexRestart(): Promise<boolean | null> {
    const desktopStatus = await api.getCodexDesktopStatus();
    if (!desktopStatus.running) {
      return false;
    }
    if (!desktopStatus.restart_supported) {
      throw new Error(t("providers.codexRestartUnsupported"));
    }
    const confirmed = await confirmAction({
      cancelLabel: t("common.cancel"),
      confirmLabel: t("providers.restartCodexAndContinue"),
      message: t("providers.codexRestartConfirmation"),
      title: t("providers.codexRestartTitle"),
    });
    return confirmed ? true : null;
  }
  authorizeCodexRestartRef.current = authorizeCodexRestart;

  async function applyCodexHubConnection(nextMode: ConnectionMode, forceTakeover: boolean) {
    let restartCodex: boolean | null;
    try {
      restartCodex = await authorizeCodexRestart();
    } catch (err) {
      setError(messageFromError(err));
      return;
    }
    if (restartCodex === null) {
      return;
    }
    const actionLabel = nextMode === "custom" ? t("providers.connectingToHub") : t("providers.disconnectingFromHub");
    setConnectionPendingMode(nextMode);
    setBusy("route");
    const toastId = showToast(`${actionLabel}...`, "loading");
    try {
      let status = forceTakeover
        ? await api.switchMode(nextMode, false, true, restartCodex)
        : await api.switchMode(nextMode, false, false, restartCodex);
      const historySyncStatus = status.history_sync_status;
      const historySyncMessage = status.history_sync_message;
      const codexRestartResult = status.codex_restart_result;
      if (codexRestartResult === "switch_failed_reopened") {
        setConnectionPendingMode(null);
        updateToast(toastId, {
          action: null,
          text: status.message,
          tone: "error",
        });
        return;
      }
      if (nextMode === "custom" && !status.proxy_running) {
        updateToast(toastId, {
          action: null,
          text: t("gateway.startingBackend"),
          tone: "loading",
        });
        const refreshedStatus = await startProxyForHubConnection();
        status = refreshedStatus
          ? { ...refreshedStatus, codex_restart_result: codexRestartResult }
          : status;
      }
      setCodexStatus(status);
      setCodexTargetOwnerOverride(nextMode === "custom" ? appFlavor?.routing_owner ?? null : "official");
      onStatusChanged?.(status);
      setConnectionPendingMode(null);
      setError(null);
      if (
        historySyncStatus &&
        historySyncStatus !== "clean" &&
        historySyncStatus !== "repaired"
      ) {
        const issueMessage = t("providers.codexRouteChangedHistoryIssueRestart", {
          status: codexHubConnectionSuccessMessage(nextMode, tr),
          message: historySyncMessage?.trim() || t("settings.historyUnexpectedFailure"),
        });
        setError(issueMessage);
        updateToast(toastId, {
          action: null,
          text: issueMessage,
          tone: "error",
        });
        return;
      }
      updateToast(toastId, {
        action: null,
        text: status.codex_restart_result === "restarted"
          ? t("providers.codexRouteChangedRestarted", {
              status: codexHubConnectionSuccessMessage(nextMode, tr),
            })
          : status.codex_restart_result === "switched_relaunch_failed"
            ? t("providers.codexRouteChangedRelaunchFailed", {
                status: codexHubConnectionSuccessMessage(nextMode, tr),
              })
            : t("providers.codexRouteChangedRestart", {
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

  useEffect(() => {
    if (
      !codexSwitchRequest
      || handledCodexSwitchRequestRef.current === codexSwitchRequest.id
    ) {
      return;
    }
    handledCodexSwitchRequestRef.current = codexSwitchRequest.id;
    void applyCodexHubConnection(
      codexSwitchRequest.mode,
      Boolean(appFlavor?.codex_takeover_required),
    ).finally(() => onCodexSwitchRequestHandled?.(codexSwitchRequest.id));
  }, [codexSwitchRequest]);

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

  async function refreshOfficialModelsAndCollaborationState(
    options?: { quiet?: boolean; throwOnError?: boolean },
  ) {
    const refreshed = await workspace.act({
      type: "refreshOfficialModels",
      quiet: options?.quiet,
      throwOnError: options?.throwOnError,
    });
    try {
      const [overrides, baselines] = await Promise.all([
        api.listOfficialMultiAgentOverrides(),
        api.listOfficialMultiAgentBaselines(),
      ]);
      setOfficialCollaborationOverrides(overrides);
      setOfficialCollaborationBaselines(baselines);
    } catch {
      // The catalog refresh remains usable when the optional selector readback
      // is unavailable; the next page refresh will retry it.
    }
    return refreshed.kind === "ok";
  }

  async function deleteProvider(providerId: string) {
    if (pendingNewProvider?.id === providerId) {
      if (!(await confirmAction({
        cancelLabel: t("common.cancel"),
        confirmLabel: t("providers.deleteProvider"),
        message: t("providers.deleteProviderConfirm", { name: pendingNewProvider.name }),
        title: t("providers.deleteProvider"),
        tone: "danger",
      }))) {
        return;
      }
      setPendingNewProvider(null);
      setSelectedId(OFFICIAL_ID);
      setProbeResult(null);
      setModelDiscoveryError(null);
      setError(null);
      return;
    }
    const target = providers.find((provider) => provider.id === providerId);
    if (!target) {
      setError(t("providers.providerNotFound", { providerId }));
      return;
    }
    if (!(await confirmAction({
      cancelLabel: t("common.cancel"),
      confirmLabel: t("providers.deleteProvider"),
      message: t("providers.deleteProviderConfirm", { name: target.name }),
      title: t("providers.deleteProvider"),
      tone: "danger",
    }))) {
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

  async function openCatalogPicker() {
    setCatalogPickerOpen(true);
    try {
      setCatalogPresets(await api.getBundledProviders());
    } catch {
      setCatalogPresets([]);
    }
  }

  return (
    <>
    <main className="relative grid h-full min-h-0 min-w-0 grid-cols-[minmax(240px,32%)_minmax(0,1fr)] gap-3 overflow-hidden">
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
          onAdd={() => void openCatalogPicker()}
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
                  probeUpstreamFormat(form.base_url, form.api_key, formProbeModelFor(form))
                }
              />
            ) : selectedId === OFFICIAL_ID ? (
              <OfficialDetail
                authState={codexAuthState}
                busy={busy}
                gatewayContextById={gatewayContextById}
                models={officialModels}
                officialCollaborationBaselines={officialCollaborationBaselines}
                officialCollaborationOverrides={officialCollaborationOverrides}
                onOfficialCollaborationOverridesChanged={setOfficialCollaborationOverrides}
                officialDisabledModels={officialDisabledModels}
                officialIncluded={settings?.include_official_models ?? false}
                authIssue={gatewayStatus?.codex_auth?.issue ?? null}
                onCopyLoginCommand={() => void copyCodexLoginCommand()}
                onAuthorizeCodexRestart={authorizeCodexRestart}
                onContextGuardChanged={reflectContextGuardSetting}
                onOpenCodexApp={() => void openCodexAppForLogin()}
                onRefresh={(options) => refreshOfficialModelsAndCollaborationState(options)}
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
                onChange={(provider, successMessage) => void updateProvider(provider, successMessage)}
                onDelete={() => void deleteProvider(selectedProvider.id)}
                unsaved={pendingNewProvider?.id === selectedProvider.id}
                onDraftStateChange={trackProviderDraft}
                onProbe={(provider) =>
                  probeUpstreamFormat(
                    provider.base_url,
                    provider.api_key ?? "",
                    providerProbeModelFor(provider),
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
    {confirmDialog}
    {pendingProviderNavigation && (
      <UnsavedProviderChangesDialog
        busy={busy === "save"}
        providerName={pendingProviderName(pendingProviderNavigation, tr)}
        onCancel={cancelPendingProviderNavigation}
        onDiscard={() => {
          if (pendingNewProvider && selectedId === pendingNewProvider.id) {
            setPendingNewProvider(null);
          }
          discardPendingProviderNavigation();
        }}
        onSave={() => void savePendingProviderNavigation()}
      />
    )}
    {catalogPickerOpen && (
      <ProviderCatalogPicker
        existingIds={new Set(providers.map((provider) => provider.id))}
        loading={catalogPresets === null}
        presets={catalogPresets ?? []}
        onClose={() => setCatalogPickerOpen(false)}
        onSelectCustom={() => {
          setCatalogPickerOpen(false);
          selectProvider(ADD_ID);
        }}
        onSelectPreset={(preset) => {
          setCatalogPickerOpen(false);
          addCatalogProvider(preset);
        }}
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
      <div className="grid w-full max-w-[420px] gap-4 rounded-overlay border border-line bg-white p-5 shadow-overlay">
        <div className="min-w-0">
          <h3 className="truncate text-base font-semibold">{t("providers.saveProviderChanges")}</h3>
          <p className="mt-1 text-sm leading-5 text-slate-600">
            {t("providers.unsavedChanges", { name: fallbackName })}
          </p>
        </div>
        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center rounded-control border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100"
            disabled={busy}
            onClick={onCancel}
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center rounded-control border border-line bg-white px-3 text-sm font-semibold hover:bg-slate-100"
            disabled={busy}
            onClick={onDiscard}
          >
            {t("common.discard")}
          </button>
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-control bg-action px-3 text-sm font-semibold text-white disabled:bg-slate-300"
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
    <section className="relative grid gap-3 overflow-hidden rounded-inner border border-line bg-surface p-3 transition-[background-color,border-color,box-shadow] duration-150 ease-out">
      <div className="rounded-inner text-left">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold">{t("providers.codexDesktop")}</h2>
            <p className="mt-1 truncate text-xs text-slate-500">{t("providers.codexAppAuth")}</p>
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
      <p className="truncate px-1 text-[11px] leading-4 text-slate-500" title={t("providers.openaiExportHint")}>
        {t("providers.openaiExportHint")}
      </p>
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
          "focus-ring flex h-11 min-w-0 items-center justify-center gap-2 overflow-hidden whitespace-nowrap rounded-full px-4 text-sm font-semibold shadow-control transition-[box-shadow,background-color,color,transform] duration-200 ease-out active:scale-[0.97] disabled:opacity-100",
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
        <span className="shrink-0">{icon}</span>
        <span className="min-w-0 truncate">{label}</span>
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
        "relative grid h-full min-h-0 grid-rows-[auto_auto_minmax(0,1fr)_auto] gap-3 rounded-inner border px-3 pt-3 transition-[background-color,border-color,box-shadow]",
        connected
          ? "border-emerald-300/70 bg-emerald-50/55"
          : "border-line bg-surface",
        "pb-3",
      )}
    >
      {connected && <ConnectedSurfaceFlow />}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold">{t("common.codexHub")}</h2>
          <p className="mt-1 truncate text-xs text-slate-500" title={t("providers.externalProviderCatalog")}>
            {t("providers.externalProviderCatalog")}
          </p>
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
                logoSrc={providerLogoSrc(item.provider.id)}
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
  logoSrc,
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
  logoSrc?: string | null;
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
        <span className="flex min-w-0 items-center gap-2">
          {logoSrc ? (
            <span className="grid h-6 w-6 shrink-0 place-items-center overflow-hidden">
              <img src={logoSrc} alt="" className="max-h-5 max-w-5 object-contain" aria-hidden="true" />
            </span>
          ) : null}
          <span className="block truncate font-semibold">{label}</span>
        </span>
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
  officialCollaborationBaselines,
  officialCollaborationOverrides,
  onOfficialCollaborationOverridesChanged,
  officialDisabledModels,
  officialIncluded,
  onCopyLoginCommand,
  onAuthorizeCodexRestart,
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
  officialCollaborationBaselines: Readonly<Record<string, "v1" | "v2">>;
  officialCollaborationOverrides: Readonly<Record<string, "v1" | "v2">>;
  onOfficialCollaborationOverridesChanged: (overrides: Record<string, "v1" | "v2">) => void;
  officialDisabledModels: string[];
  officialIncluded: boolean;
  onCopyLoginCommand: () => void;
  onAuthorizeCodexRestart: () => Promise<boolean | null>;
  onContextGuardChanged: (enabled: boolean) => void;
  onOpenCodexApp: () => void;
  onRefresh: (options?: { quiet?: boolean; throwOnError?: boolean }) => Promise<boolean>;
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
  // Gateway publishes the authoritative per-model window from the Official
  // catalog.  A user-owned Codex top-level override is Desktop-only and must
  // not shrink the Gateway display when the conflict diagnostic is active.
  const displayedGatewayContextById = gatewayContextById;

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
    let restartCodex: boolean | null;
    try {
      restartCodex = await onAuthorizeCodexRestart();
    } catch (err) {
      showToast(t("providers.contextGuardUpdateFailed", { message: messageFromError(err) }), "error");
      return;
    }
    if (restartCodex === null) {
      return;
    }
    setContextGuardBusy(true);
    const toastId = showToast(
      enabled ? t("providers.enablingContextGuard") : t("providers.disablingContextGuard"),
      "loading",
    );
    try {
      const status = await api.setCodexContextGuard(enabled, restartCodex);
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
      const restartMessage = status.codex_restart_result === "restarted"
        ? t("providers.contextGuardCodexRestarted")
        : status.codex_restart_result === "switched_relaunch_failed"
          ? t("providers.contextGuardCodexRelaunchFailed")
          : enabled
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

  async function changeOfficialCollaborationVersion(modelId: string, version: "v1" | "v2" | null) {
    let restartCodex: boolean | null;
    try {
      restartCodex = await onAuthorizeCodexRestart();
    } catch (err) {
      showToast(t("providers.collaborationVersionSaveFailed", { message: messageFromError(err) }), "error");
      return;
    }
    if (restartCodex === null) {
      return;
    }
    const toastId = showToast(
      t("providers.savingCollaborationVersion", { version: version ?? t("providers.catalogBaseline") }),
      "loading",
    );
    try {
      const result = await api.saveOfficialMultiAgentVersion(modelId, version, restartCodex);
      const canonical = normalizeOfficialModelId(modelId) ?? modelId;
      const next = { ...officialCollaborationOverrides };
      if (version === null) {
        delete next[canonical];
      } else {
        next[canonical] = version;
      }
      onOfficialCollaborationOverridesChanged(next);
      const savedMessage = result.codex_restart_result === "restarted"
        ? t("providers.collaborationVersionSavedCodexRestarted")
        : result.codex_restart_result === "switched_relaunch_failed"
          ? t("providers.collaborationVersionSavedCodexRelaunchFailed")
          : t("providers.collaborationVersionSaved");
      updateToast(toastId, {
        action: null,
        text: result.warning?.trim()
          ? t("providers.collaborationVersionSavedWithWarning", {
              message: result.warning.trim(),
              status: savedMessage,
            })
          : savedMessage,
        tone: result.warning?.trim() ? "error" : "success",
      });
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: t("providers.collaborationVersionSaveFailed", { message: messageFromError(err) }),
        tone: "error",
      });
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
              label={t("providers.contextGuardShort")}
              onChange={(enabled) => void toggleContextGuard(enabled)}
            />
            <div
              id="context-guard-tooltip"
              role="tooltip"
              className="pointer-events-none absolute bottom-full right-0 z-30 mb-2 hidden w-80 whitespace-normal rounded-inner bg-ink px-3 py-2 text-left text-xs font-medium leading-5 text-white shadow-floating group-hover:block group-focus-within:block"
            >
              {t("providers.contextGuardTooltip")}
            </div>
            {contextGuardStatus?.global_override_conflict && (
              <div
                role="status"
                className="mt-1 max-w-80 text-right text-[11px] font-medium leading-4 text-amber-700"
              >
                {t("providers.contextGuardGlobalOverrideConflict")}
              </div>
            )}
          </div>
        }
        interactionDisabled={authState !== "authorized"}
        models={models}
        officialCollaborationBaselines={officialCollaborationBaselines}
        officialCollaborationOverrides={officialCollaborationOverrides}
        officialDisabledModels={officialDisabledModels}
        onRefresh={onRefresh}
        onReorder={onReorder}
        onTestModel={testOfficialModel}
        refreshBusy={busy === "official-refresh"}
        onToggleOfficialModel={onToggleModel}
        onOfficialCollaborationVersionChange={(modelId, version) =>
          void changeOfficialCollaborationVersion(modelId, version)
        }
        modelTestDisabled={authState !== "authorized"}
      />
      <div className="flex items-center justify-end border-t border-line px-5 py-3">
        <button
          type="button"
          className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-control bg-action px-3 text-sm font-semibold text-white disabled:bg-slate-300"
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
