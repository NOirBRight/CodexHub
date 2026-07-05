import {
  Brain,
  Cable,
  Check,
  ChevronDown,
  Copy,
  Eye,
  EyeOff,
  FlaskConical,
  Link2,
  Link2Off,
  Plus,
  RefreshCcw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useToasts } from "../components/PageToast";
import { SortableList } from "../components/SortableList";
import i18n from "../i18n";
import { cx, displayModel, mergeDiscoveredModels, renumberModels, slugify } from "../lib/format";
import { api, isBackendDisconnectedMessage, messageFromError } from "../lib/tauri";
import type {
  AppStatus,
  GatewayStatus,
  GatewayClientSyncSummary,
  Model,
  Provider,
  Settings,
  UpstreamFormat,
  UpstreamFormatProbeResult,
} from "../lib/types";

const OFFICIAL_ID = "__official__";
const ADD_ID = "__add__";
const DEFAULT_FAST_MODEL_VARIANTS = ["openai/gpt-5.5", "openai/gpt-5.4"];
const DEFAULT_OFFICIAL_MODEL_ORDER = [
  "openai/gpt-5.5",
  "openai/gpt-5.4",
  "openai/gpt-5.4-mini",
  "openai/gpt-5.3-codex-spark",
];

function useVerticalOverflow<T extends HTMLElement>(dependencies: ReadonlyArray<unknown>) {
  const ref = useRef<T | null>(null);
  const [hasOverflow, setHasOverflow] = useState(false);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) {
      setHasOverflow(false);
      return;
    }

    const update = () => {
      setHasOverflow(element.scrollHeight > element.clientHeight + 1);
    };

    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    Array.from(element.children).forEach((child) => observer.observe(child));
    window.addEventListener("resize", update);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", update);
    };
  }, dependencies);

  return [ref, hasOverflow] as const;
}

const emptyProvider = {
  id: "",
  name: "",
  base_url: "",
  api_key: "",
  upstream_format: "responses" as UpstreamFormat,
  available_upstream_formats: [] as UpstreamFormat[],
  display_prefix: "",
  models: [] as Model[],
};

const reasoningLevelOptions = ["low", "medium", "high", "xhigh", "max"];
const endpointSelectionOptions: Array<{ value: UpstreamFormat; labelKey: string }> = [
  { value: "responses", labelKey: "providers.upstreamFormats.responses" },
  { value: "chat_completions", labelKey: "providers.upstreamFormats.chatCompletions" },
  { value: "anthropic_messages", labelKey: "providers.upstreamFormats.anthropicMessages" },
];

type ProviderNavItem =
  { id: string; sort_order: number; provider: Provider };
type CodexAuthState = "authorized" | "missing" | "unknown";
type ConnectionMode = "official" | "custom";
type ProviderDraftState = {
  providerId: string;
  draft: Provider;
  dirty: boolean;
};
type AddProviderForm = typeof emptyProvider;
type PendingProviderNavigation =
  | {
      kind: "existing";
      targetId: string;
      draft: Provider;
    }
  | {
      kind: "add";
      targetId: string;
      form: AddProviderForm;
    };
type InlineTestState = "idle" | "testing" | "success" | "error";
type Translate = (key: string, options?: Record<string, unknown>) => string;

export function ProvidersPage({
  gatewayStatus: gatewayStatusSnapshot,
  onGatewayChanged,
  onStartProxy,
}: {
  gatewayStatus?: GatewayStatus | null;
  onGatewayChanged?: () => Promise<void>;
  onStartProxy?: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const tr = t as Translate;
  const { showToast, updateToast } = useToasts();
  const [providers, setProviders] = useState<Provider[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [settingsDraft, setSettingsDraft] = useState<Settings | null>(null);
  const [codexStatus, setCodexStatus] = useState<AppStatus | null>(null);
  const [connectionPendingMode, setConnectionPendingMode] = useState<ConnectionMode | null>(null);
  const [loadedGatewayStatus, setLoadedGatewayStatus] = useState<GatewayStatus | null>(null);
  const [codexAuthState, setCodexAuthState] = useState<CodexAuthState>("unknown");
  const [officialModels, setOfficialModels] = useState<Model[]>([]);
  const [selectedId, setSelectedId] = useState<string>(OFFICIAL_ID);
  const [form, setForm] = useState(emptyProvider);
  const [probeResult, setProbeResult] = useState<UpstreamFormatProbeResult | null>(null);
  const [busy, setBusy] = useState<string | null>("load");
  const [modelDiscoveryError, setModelDiscoveryError] = useState<string | null>(null);
  const dirtyProviderDraftRef = useRef<ProviderDraftState | null>(null);
  const [pendingProviderNavigation, setPendingProviderNavigation] =
    useState<PendingProviderNavigation | null>(null);

  const trackProviderDraft = useCallback((state: ProviderDraftState) => {
    if (!state.dirty) {
      if (dirtyProviderDraftRef.current?.providerId === state.providerId) {
        dirtyProviderDraftRef.current = null;
      }
      return;
    }
    dirtyProviderDraftRef.current = state;
  }, []);

  useEffect(() => {
    void load();
  }, []);

  useEffect(() => {
    if (gatewayStatusSnapshot !== undefined) {
      setCodexAuthState(codexAuthStateFromGatewayStatus(gatewayStatusSnapshot ?? null));
    }
  }, [gatewayStatusSnapshot]);

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
  const officialDisabledModels = settings?.official_disabled_models ?? [];
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
  const realCodexConnected = codexStatus?.mode === "custom";
  const codexConnected = realCodexConnected;
  const gatewayContextById = useMemo(() => {
    return new Map((gatewayStatus?.official_models ?? []).map((model) => [model.id, model.context_window]));
  }, [gatewayStatus]);

  function selectProvider(id: string) {
    if (id === selectedId) {
      return;
    }
    if (selectedId === ADD_ID) {
      if (isAddProviderFormDirty(form)) {
        setPendingProviderNavigation({ kind: "add", targetId: id, form });
        return;
      }
      setForm(emptyProvider);
      setSelectedId(id);
      return;
    }
    const dirtyDraft = dirtyProviderDraftRef.current;
    if (dirtyDraft?.dirty && dirtyDraft.providerId === selectedId) {
      setPendingProviderNavigation({ kind: "existing", targetId: id, draft: dirtyDraft.draft });
      return;
    }
    setSelectedId(id);
  }

  async function savePendingProviderNavigation() {
    const pending = pendingProviderNavigation;
    if (!pending) {
      return;
    }
    try {
      if (pending.kind === "existing") {
        await updateProvider(pending.draft, t("providers.providerSaved", { name: pending.draft.name }));
        dirtyProviderDraftRef.current = null;
      } else if (pending.kind === "add") {
        const addedId = await saveAddProviderForm(pending.form, pending.targetId);
        if (!addedId) {
          return;
        }
      }
      setPendingProviderNavigation(null);
      setSelectedId(pending.targetId);
    } catch {
      // saveProviders already surfaces the failure.
    }
  }

  function discardPendingProviderNavigation() {
    const pending = pendingProviderNavigation;
    if (!pending) {
      return;
    }
    dirtyProviderDraftRef.current = null;
    if (pending.kind === "add") {
      setForm(emptyProvider);
    }
    setPendingProviderNavigation(null);
    setSelectedId(pending.targetId);
  }

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
      await load();
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
      await onGatewayChanged?.();
    } catch {
      // Refresh failures are surfaced by the owning runtime loader.
    }
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
      const syncMessage = tr("providers.syncedClients", { count: syncResult.applied, plural: syncResult.applied === 1 ? "" : "s" });
      return baseMessage ? `${baseMessage}; ${syncMessage}` : syncMessage;
    }
    return baseMessage ?? null;
  }

  async function load() {
    setBusy("load");
    try {
      const [nextSettings, nextProviders, catalog, modelMetadata, nextCodexStatus, gatewayStatus] = await Promise.all([
        api.getSettings(),
        api.getProviders(),
        api.listModels(),
        api.listModelMetadata().catch(() => []),
        api.getStatus().catch(() => null),
        api.gatewayStatus().catch(() => null),
      ]);
      const normalizedSettings = withDefaultFastVariants(nextSettings);
      setSettings(normalizedSettings);
      setSettingsDraft(normalizedSettings);
      setCodexStatus(nextCodexStatus);
      setConnectionPendingMode(null);
      setLoadedGatewayStatus(gatewayStatus);
      setCodexAuthState(codexAuthStateFromGatewayStatus(gatewayStatus));
      setProviders(nextProviders);
      setOfficialModels(
        sortOfficialModels(
          mergeOfficialModelSources(catalog, modelMetadata),
          normalizedSettings.official_model_sort_order,
        ),
      );
      if (selectedId !== OFFICIAL_ID && selectedId !== ADD_ID && !nextProviders.some((provider) => provider.id === selectedId)) {
        setSelectedId(nextProviders[0]?.id ?? OFFICIAL_ID);
      }
      setError(null);
      setModelDiscoveryError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function saveProviders(
    next: Provider[],
    regenerateCatalog = true,
    successMessage?: string,
    toastId?: string,
  ) {
    setBusy("save");
    const activeToastId = toastId ?? showToast(successMessage ? `${successMessage}...` : t("providers.updateProviderCatalog"), "loading");
    try {
      const saved = await api.saveProviders(next);
      setProviders(saved);
      let syncResult: GatewayClientSyncSummary | null = null;
      if (regenerateCatalog) {
        syncResult = await updateGatewayAfterCatalog(undefined, activeToastId);
      }
      const toastMessage = catalogSyncToastMessage(successMessage ?? t("providers.providerCatalogUpdated"), syncResult);
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

  async function saveSettings(next: Settings, regenerateCatalog = false, successMessage?: string, toastId?: string) {
    setBusy("settings");
    const activeToastId = toastId ?? showToast(successMessage ? `${successMessage}...` : t("settings.savingSettings"), "loading");
    try {
      const saved = await api.saveSettings(next);
      setSettings(saved);
      setSettingsDraft(saved);
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
        { ...settingsDraft, auto_start_proxy: enabled },
        false,
        enabled ? t("providers.autoStartEnabled") : t("providers.autoStartDisabled"),
        toastId,
      );
    } catch (err) {
      updateToastWithError(toastId, err);
      setBusy(null);
    }
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
    if (!settingsDraft) {
      return;
    }
    const current = settingsDraft.official_disabled_models ?? [];
    const nextDisabled = enabled
      ? current.filter((item) => !modelIdMatches(item, modelId))
      : [...new Set([...current, modelId])];
    const nextSettings = { ...settingsDraft, official_disabled_models: nextDisabled };
    setSettings(nextSettings);
    setSettingsDraft(nextSettings);
    setOfficialModels((currentModels) =>
      currentModels.map((model) => (modelIdMatches(model.id, modelId) ? { ...model, enabled } : model)),
    );
    const toastId = showToast(
      enabled ? t("providers.enablingModel", { modelId }) : t("providers.disablingModel", { modelId }),
      "loading",
    );
    void saveSettings(
      nextSettings,
      true,
      enabled ? t("providers.modelEnabledNamed", { modelId }) : t("providers.modelDisabledNamed", { modelId }),
      toastId,
    );
  }

  async function toggleCodexHubConnection() {
    const nextMode: ConnectionMode = realCodexConnected ? "official" : "custom";
    const actionLabel = nextMode === "custom" ? t("providers.connectingToHub") : t("providers.disconnectingFromHub");
    setConnectionPendingMode(nextMode);
    setBusy("route");
    const toastId = showToast(`${actionLabel}...`, "loading");
    try {
      const status = await api.switchMode(nextMode, false);
      setCodexStatus(status);
      setConnectionPendingMode(null);
      setError(null);
      const targetProvider =
        nextMode === "custom" || (settingsDraft?.unified_codex_history ?? true) ? "custom" : "openai";
      void repairUnifiedHistoryInBackground(targetProvider, toastId, codexHubConnectionSuccessMessage(nextMode, tr));
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

  async function repairUnifiedHistoryInBackground(
    targetProvider: "custom" | "openai",
    toastId?: string,
    prefix?: string,
  ) {
    const activeToastId = toastId ?? showToast(t("settings.repairingHistoryBucket"), "loading");
    updateToast(activeToastId, {
      action: null,
      text: prefix ? `${prefix}; ${t("settings.repairingHistoryBucket")}` : t("settings.repairingHistoryBucket"),
      tone: "loading",
    });
    try {
      const message = await api.syncHistory(targetProvider);
      updateToast(activeToastId, {
        action: null,
        text: prefix ? `${prefix}; ${message}` : message,
        tone: "success",
      });
    } catch (err) {
      updateToast(activeToastId, {
        action: null,
        text: prefix
          ? `${prefix}; ${t("providers.historyRepairFailed", { message: messageFromError(err) })}`
          : t("providers.historyRepairFailed", { message: messageFromError(err) }),
        tone: "error",
      });
    }
  }

  async function reorderOfficialModels(models: Model[]) {
    const nextModels = renumberModels(models);
    setOfficialModels(nextModels);
    if (!settingsDraft) {
      return;
    }
    await saveSettings(
      {
        ...settingsDraft,
        official_model_sort_order: nextModels.map((model) => model.id),
      },
      true,
      t("providers.officialModelOrderSaved"),
    );
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

  async function refreshOfficialModels() {
    setBusy("official-refresh");
    const toastId = showToast(t("providers.refreshingOfficialModels"), "loading");
    try {
      const refreshed = filterCodexVisibleOfficialModels(await api.refreshOfficialModels());
      setOfficialModels(sortOfficialModels(refreshed, settingsDraft?.official_model_sort_order ?? []));
      const syncResult = await updateGatewayAfterCatalog(undefined, toastId);
      const toastMessage = catalogSyncToastMessage(t("providers.officialModelsRefreshed"), syncResult);
      if (syncResult?.failed) {
        updateToast(toastId, {
          action: null,
          text: toastMessage ?? t("providers.officialModelsRefreshedSyncFailed"),
          tone: "error",
        });
      } else {
        updateToast(toastId, {
          action: null,
          text: toastMessage ?? t("providers.officialModelsRefreshed"),
          tone: "success",
        });
        setError(null);
      }
    } catch (err) {
      updateToastWithError(toastId, err);
    } finally {
      setBusy(null);
    }
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
    fallbackFormat?: UpstreamFormat | null,
  ) {
    setBusy("probe");
    setProbeResult(null);
    const toastId = showToast(t("providers.endpointSelectionTest"), "loading");
    try {
      const result = await api.probeUpstreamFormat(baseUrl, apiKey, model);
      setProbeResult(result);
      updateToast(toastId, {
        action: null,
        text: t("providers.probeCompleted", {
          format: upstreamFormatLabel(probeRecommendedEndpointFormat(result, fallbackFormat), tr),
        }),
        tone: "success",
      });
      setError(null);
      return result;
    } catch (err) {
      updateToastWithError(toastId, err);
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function persistProviderProbeResult(providerId: string, result: UpstreamFormatProbeResult) {
    const nextProviders = providers.map((provider) =>
      provider.id === providerId ? applyProviderProbeResult(provider, result) : provider,
    );
    setProviders(nextProviders);
    try {
      const saved = await api.saveProviders(nextProviders);
      setProviders(saved);
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
    const nextSortOrder =
      Math.max(
        0,
        ...providers.map((provider) => provider.sort_order ?? 0),
      ) + 1;
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

  return (
    <>
    <main className="relative grid h-full min-h-0 min-w-[972px] grid-cols-[430px_minmax(0,1fr)] gap-4">
      <aside className="min-h-0 min-w-0 overflow-hidden rounded-panel bg-surface shadow-card">
        <ProviderSourceSidebar
          codexAuthState={codexAuthState}
          codexConnected={codexConnected}
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
                  probeUpstreamFormat(form.base_url, form.api_key, formProbeModel(), form.upstream_format)
                }
              />
            ) : selectedId === OFFICIAL_ID ? (
              <OfficialDetail
                authState={codexAuthState}
                busy={busy}
                gatewayContextById={gatewayContextById}
                models={officialModels}
                officialDisabledModels={officialDisabledModels}
                onRefresh={() => void refreshOfficialModels()}
                onReorder={(models) => void reorderOfficialModels(models)}
                onToggleModel={toggleOfficialModel}
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
                    provider.upstream_format,
                  ).then((result) => {
                    if (result) {
                      void persistProviderProbeResult(provider.id, result);
                    }
                    return result;
                  })
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
        onCancel={() => setPendingProviderNavigation(null)}
        onDiscard={discardPendingProviderNavigation}
        onSave={() => void savePendingProviderNavigation()}
      />
    )}
    </>
  );
}

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
      />
      <HubConnectionBridge
        connected={codexConnected}
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
}: {
  active: boolean;
  authState: CodexAuthState;
  enabledModelCount: number;
  included: boolean;
  modelCount: number;
  onSelect: () => void;
  onToggleInclude: (included: boolean) => void;
}) {
  const { t } = useTranslation();
  const authChip = codexAuthChip(authState, t as Translate);

  return (
    <section className="relative grid gap-3 overflow-hidden rounded-panel border border-line bg-surface p-3 shadow-card transition-[background-color,border-color,box-shadow] duration-150 ease-out">
      <button type="button" className="focus-ring rounded-inner text-left" onClick={onSelect}>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold">{t("providers.codexDesktop")}</h2>
            <p className="mt-1 text-xs text-slate-500">{t("providers.codexAppAuth")}</p>
          </div>
          <SourceStatusChip {...authChip} />
        </div>
      </button>

      <div className="rounded-inner bg-surface shadow-control">
        <ProviderNavButton
          active={active}
          activeTone="neutral"
          enabled={included}
          label="OpenAI"
          meta={t("providers.modelCount", { enabled: enabledModelCount, total: modelCount })}
          onClick={onSelect}
          onToggle={onToggleInclude}
          toggleLabel={included ? t("providers.openaiSourceIncluded") : t("providers.openaiSourceExcluded")}
        />
      </div>
    </section>
  );
}

function HubConnectionBridge({
  connected,
  disabled,
  onToggle,
  pendingMode,
}: {
  connected: boolean;
  disabled: boolean;
  onToggle: () => void;
  pendingMode: ConnectionMode | null;
}) {
  const { t } = useTranslation();
  const label = pendingMode === "custom"
    ? t("providers.connecting")
    : pendingMode === "official"
      ? t("providers.disconnecting")
    : connected
      ? t("providers.connectedToHub")
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
          !pendingMode && connected
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
    <div className="min-w-0 rounded-inner bg-surface px-2 py-1.5 shadow-control">
      <div className="truncate text-[10px] font-semibold uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-0.5 truncate font-semibold text-ink">{value}</div>
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
  toggleLabel?: string;
}) {
  const { t } = useTranslation();
  return (
    <div
      className={cx(
        "grid min-h-[58px] w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-3 py-2 text-sm transition-[box-shadow,background-color] duration-150 ease-out",
        highlightShape === "right" ? "rounded-r-inner" : "rounded-inner",
        active
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
        showLabel={false}
        onChange={onToggle}
      />
    </div>
  );
}

function OfficialDetail({
  authState,
  busy,
  gatewayContextById,
  models,
  officialDisabledModels,
  onRefresh,
  onReorder,
  onToggleModel,
}: {
  authState: CodexAuthState;
  busy: string | null;
  gatewayContextById: Map<string, number>;
  models: Model[];
  officialDisabledModels: string[];
  onRefresh: () => void;
  onReorder: (models: Model[]) => void;
  onToggleModel: (modelId: string, enabled: boolean) => void;
}) {
  const { t } = useTranslation();
  const { showToast, updateToast } = useToasts();

  async function testOfficialModel(model: Model) {
    const label = displayModel(model);
    const endpointLabel = upstreamFormatLabel("responses", t as Translate);
    const toastId = showToast(t("providers.testingModel", { label, endpoint: endpointLabel }), "loading");
    try {
      const result = await api.testModelEndpoint(
        "https://api.openai.com/v1",
        "{env:OPENAI_API_KEY}",
        officialModelProbeId(model),
        "responses",
      );
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
    <div className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)]">
      <div className="grid gap-4 border-b border-line p-5">
        <HeaderRow
          title={t("common.codex")}
          subtitle={t("providers.openaiSubscriptionCatalog")}
          actions={
            <>
              <SourceStatusChip {...codexAuthChip(authState, t as Translate)} />
            </>
          }
        />
      </div>
      <ModelSection
        contextById={gatewayContextById}
        disabled
        models={models}
        officialDisabledModels={officialDisabledModels}
        onRefresh={onRefresh}
        onReorder={onReorder}
        onTestModel={testOfficialModel}
        reorderable={false}
        refreshBusy={busy === "official-refresh"}
        onToggleOfficialModel={onToggleModel}
        modelTestDisabled={authState !== "authorized"}
      />
    </div>
  );
}

function ProviderDetail({
  busy,
  discoverError,
  onChange,
  onDelete,
  onDraftStateChange,
  onProbe,
  onRefresh,
  probeResult,
  provider,
}: {
  busy: string | null;
  discoverError?: string | null;
  onChange: (provider: Provider, successMessage?: string) => void;
  onDelete: () => void;
  onDraftStateChange: (state: ProviderDraftState) => void;
  onProbe: (provider: Provider) => Promise<UpstreamFormatProbeResult | null>;
  onRefresh: (provider: Provider) => void;
  probeResult: UpstreamFormatProbeResult | null;
  provider: Provider;
}) {
  const { t } = useTranslation();
  const { showToast, updateToast } = useToasts();
  const normalizedProvider = useMemo(() => normalizeProviderEndpointSelection(provider), [provider]);
  const [draft, setDraft] = useState(() => normalizedProvider);
  const [endpointTestState, setEndpointTestState] = useState<InlineTestState>("idle");
  const dirty = JSON.stringify(draft) !== JSON.stringify(normalizedProvider);

  useEffect(() => {
    setDraft(normalizedProvider);
    setEndpointTestState(hasAvailableEndpointFormats(normalizedProvider.available_upstream_formats) ? "success" : "idle");
  }, [provider.id]);

  useEffect(() => {
    const availableFormats = normalizeEndpointFormats(provider.available_upstream_formats);
    const upstreamFormat = normalizedEndpointFormat(provider.upstream_format);
    setDraft((current) =>
      current.id === provider.id
        ? {
            ...current,
            upstream_format: upstreamFormat,
            available_upstream_formats: availableFormats,
          }
        : current,
    );
    setEndpointTestState(availableFormats.length ? "success" : "idle");
  }, [provider.id, provider.upstream_format, provider.available_upstream_formats]);

  useEffect(() => {
    if (probeResult) {
      setEndpointTestState("success");
    }
  }, [probeResult]);

  useEffect(() => {
    onDraftStateChange({ providerId: provider.id, draft, dirty });
    return () => {
      onDraftStateChange({ providerId: provider.id, draft: normalizedProvider, dirty: false });
    };
  }, [dirty, draft, normalizedProvider, onDraftStateChange, provider.id]);

  function updateModel(modelId: string, patch: Partial<Model>) {
    setDraft((current) => ({
      ...current,
      models: current.models.map((model) =>
        model.id === modelId ? normalizeModel({ ...model, ...patch }) : model,
      ),
    }));
  }

  function addModel() {
    const id = uniqueModelId(draft.models);
    setDraft((current) => ({
      ...current,
      models: [...current.models, createDraftModel(id, current.models.length + 1)],
    }));
    return id;
  }

  function removeModel(modelId: string) {
    const next = {
      ...draft,
      models: renumberModels(draft.models.filter((model) => model.id !== modelId)),
    };
    setDraft(next);
    onChange(next, t("providers.modelRemoved"));
  }

  async function runProbe() {
    setEndpointTestState("testing");
    const result = await onProbe(draft);
    if (result) {
      setDraft((current) => applyProviderProbeResult(current, result));
    }
    setEndpointTestState(result ? "success" : "error");
  }

  async function testModel(model: Model) {
    const label = displayModel(model);
    const upstreamFormat = normalizedEndpointFormat(draft.upstream_format);
    const endpointLabel = upstreamFormatLabel(upstreamFormat, t as Translate);
    const toastId = showToast(t("providers.testingModel", { label, endpoint: endpointLabel }), "loading");
    try {
      const result = await api.testModelEndpoint(
        draft.base_url,
        draft.api_key ?? "",
        modelProbeId(model),
        upstreamFormat,
      );
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
      <div className="grid gap-2 border-b border-line p-4">
        <HeaderRow
          title={provider.name}
          actions={
            <IconButton
              title={t("providers.deleteProvider")}
              danger
              disabled={busy === "save"}
              onClick={onDelete}
            >
              <Trash2 size={16} />
            </IconButton>
          }
        />

        <div className="grid grid-cols-2 gap-2">
          <Field label={t("common.name")}>
            <input
              className="field field-compact"
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            />
          </Field>
          <Field label={t("common.apiKey")}>
            <ApiKeyInput
              value={draft.api_key ?? ""}
              onChange={(apiKey) => setDraft({ ...draft, api_key: apiKey || null })}
            />
          </Field>
          <Field label={t("common.baseUrl")} className="col-span-2">
            <input
              className="field field-compact"
              value={draft.base_url}
              onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
            />
          </Field>
          <div className="col-span-2">
            <EndpointSelectionPanel
              value={draft.upstream_format ?? "auto"}
              result={probeResult}
              availableFormats={draft.available_upstream_formats}
              probeDisabled={busy === "probe" || !draft.base_url.trim()}
              testState={endpointTestState}
              onChange={(upstreamFormat) => setDraft({ ...draft, upstream_format: upstreamFormat })}
              onProbe={() => void runProbe()}
            />
          </div>
        </div>
      </div>

      <ModelSection
        discoverDisabled={!draft.base_url.trim()}
        discoverBusy={busy === draft.id}
        discoverError={discoverError}
        models={draft.models}
        providerId={draft.id}
        onAdd={addModel}
        onDiscover={() => onRefresh(draft)}
        onReorder={(models) => setDraft({ ...draft, models: renumberModels(models) })}
        onRemove={removeModel}
        onTestModel={testModel}
        onToggle={(modelId, enabled) => updateModel(modelId, { enabled })}
        onUpdate={updateModel}
        modelTestDisabled={!draft.base_url.trim()}
      />
      <div className="flex items-center justify-end border-t border-line px-5 py-3">
        <button
          type="button"
          className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-action px-3 text-sm font-semibold text-white disabled:bg-slate-300"
          disabled={!dirty || busy === "save"}
          onClick={() => onChange(draft, t("providers.providerSaved", { name: draft.name }))}
        >
          <Save size={16} />
          {t("common.save")}
        </button>
      </div>
    </div>
  );
}

function ModelSection({
  contextById,
  disabled,
  discoverBusy,
  discoverDisabled,
  discoverError,
  models,
  modelTestDisabled,
  onAdd,
  onDiscover,
  onRefresh,
  onRemove,
  onReorder,
  onTestModel,
  officialDisabledModels,
  providerId,
  reorderable = true,
  refreshBusy,
  onToggleOfficialModel,
  onToggle,
  onUpdate,
}: {
  contextById?: Map<string, number>;
  disabled?: boolean;
  discoverBusy?: boolean;
  discoverDisabled?: boolean;
  discoverError?: string | null;
  models: Model[];
  modelTestDisabled?: boolean;
  onAdd?: () => string | undefined;
  onDiscover?: () => void;
  onRefresh?: () => void;
  onRemove?: (modelId: string) => void;
  onReorder: (models: Model[]) => void;
  onTestModel?: (model: Model) => Promise<boolean>;
  officialDisabledModels?: string[];
  providerId?: string;
  reorderable?: boolean;
  refreshBusy?: boolean;
  onToggleOfficialModel?: (modelId: string, enabled: boolean) => void;
  onToggle?: (modelId: string, enabled: boolean) => void;
  onUpdate?: (modelId: string, patch: Partial<Model>) => void;
}) {
  const { t } = useTranslation();
  const [editingModelId, setEditingModelId] = useState<string | null>(null);
  const [modelTestStates, setModelTestStates] = useState<Record<string, InlineTestState>>({});
  const [testingModelId, setTestingModelId] = useState<string | null>(null);
  const editingModel = editingModelId ? models.find((model) => model.id === editingModelId) ?? null : null;
  const [modelListRef, modelListHasOverflow] = useVerticalOverflow<HTMLDivElement>([
    disabled,
    editingModelId,
    models.length,
    providerId,
    reorderable,
  ]);

  function addAndEdit() {
    const modelId = onAdd?.();
    if (modelId) {
      setEditingModelId(modelId);
    }
  }

  function applyModelUpdate(modelId: string, nextModel: Model) {
    onUpdate?.(modelId, nextModel);
    setEditingModelId(null);
  }

  async function runModelTest(model: Model) {
    if (!onTestModel || testingModelId) {
      return;
    }
    setTestingModelId(model.id);
    setModelTestStates((current) => ({ ...current, [model.id]: "testing" }));
    const ok = await onTestModel(model);
    setModelTestStates((current) => ({ ...current, [model.id]: ok ? "success" : "error" }));
    setTestingModelId(null);
  }

  function renderModelRow(model: Model) {
    const contextWindow = contextById?.get(model.id) ?? model.context_window;
    const modelEnabled = disabled
      ? !isOfficialModelDisabled(officialDisabledModels ?? [], model.id)
      : model.enabled;
    const rowInteractable = !disabled || Boolean(onToggleOfficialModel);
    function activateModelRow() {
      if (disabled && onToggleOfficialModel) {
        onToggleOfficialModel(model.id, !modelEnabled);
        return;
      }
      setEditingModelId(model.id);
    }
    const actions = (
      <div className="flex shrink-0 flex-nowrap items-center justify-end gap-2 whitespace-nowrap text-xs text-slate-500">
        {modelCapabilityTags(model).map((tag) => (
          <ModelCapabilityChip key={tag} tag={tag} />
        ))}
        <CapabilityChip label={formatContextWindow(contextWindow)} />
        {disabled && onToggleOfficialModel && (
          <SwitchControl
            checked={modelEnabled}
            label={modelEnabled ? t("providers.modelEnabled") : t("providers.modelDisabled")}
            showLabel={false}
            onChange={(checked) => onToggleOfficialModel(model.id, checked)}
          />
        )}
        {!disabled && onToggle && (
          <SwitchControl
            checked={modelEnabled}
            label={modelEnabled ? t("providers.modelEnabled") : t("providers.modelDisabled")}
            showLabel={false}
            onChange={(checked) => onToggle(model.id, checked)}
          />
        )}
      </div>
    );
    return (
      <div
        className={cx(
          "grid min-h-[52px] grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-3 py-2",
          rowInteractable && "cursor-pointer",
          !modelEnabled && "opacity-70",
        )}
        role={rowInteractable ? "button" : undefined}
        tabIndex={rowInteractable ? 0 : undefined}
        onClick={rowInteractable ? activateModelRow : undefined}
        onKeyDown={
          rowInteractable
            ? (event) => {
                if (event.target !== event.currentTarget) {
                  return;
                }
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  activateModelRow();
                }
              }
            : undefined
        }
      >
        <ModelIdentity
          model={model}
          providerId={providerId}
          testDisabled={modelTestDisabled || Boolean(testingModelId)}
          testState={modelTestStates[model.id] ?? "idle"}
          onTest={onTestModel ? () => void runModelTest(model) : undefined}
        />
        <div
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
        >
          {actions}
        </div>
      </div>
    );
  }

  return (
    <div className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-3 p-5">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">{t("common.models")}</h3>
          <p className="mt-1 text-xs text-slate-500">{t("providers.configured", { count: models.length })}</p>
          <p className="mt-1 truncate whitespace-nowrap text-xs leading-4 text-slate-500">
            {t("providers.appsMaySortModels")}
          </p>
        </div>
        <div className="flex shrink-0 items-center justify-end gap-2 whitespace-nowrap">
          {discoverError && (
            <span className="max-w-[260px] truncate text-xs font-medium text-danger" title={discoverError}>
              {discoverError}
            </span>
          )}
          {onRefresh && (
            <button
              type="button"
              className="focus-ring inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100 disabled:bg-slate-100"
              disabled={refreshBusy}
              onClick={onRefresh}
            >
              <RefreshCcw size={16} />
              {t("common.refresh")}
            </button>
          )}
          {onDiscover && (
            <button
              type="button"
              className="focus-ring inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100 disabled:bg-slate-100"
              disabled={discoverBusy || discoverDisabled}
              onClick={onDiscover}
            >
              <RefreshCcw size={16} />
              {t("providers.discoverModels")}
            </button>
          )}
          {!disabled && (
            <button
              type="button"
              className="focus-ring inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100"
              onClick={addAndEdit}
            >
              <Plus size={16} />
              {t("providers.addModel")}
            </button>
          )}
        </div>
      </div>
      <div
        ref={modelListRef}
        className={cx("min-h-0 overflow-auto", modelListHasOverflow && "-mr-5 pr-1")}
      >
        {models.length === 0 ? (
          <div className="rounded-inner bg-panel-soft p-4 text-sm text-slate-500 shadow-hairline">
            {t("common.noModels")}
          </div>
        ) : reorderable ? (
          <SortableList
            className="space-y-2"
            items={models}
            getId={(model) => model.id}
            onReorder={onReorder}
            renderItem={renderModelRow}
          />
        ) : (
          <div className="space-y-2">
            {models.map((model) => (
              <div key={model.id} className="rounded-md border border-line bg-white shadow-subtle">
                {renderModelRow(model)}
              </div>
            ))}
          </div>
        )}
      </div>
      {!disabled && editingModel && (
        <ModelEditorOverlay
          model={editingModel}
          onApply={(nextModel) => applyModelUpdate(editingModel.id, nextModel)}
          onClose={() => setEditingModelId(null)}
          onRemove={onRemove ? () => {
            onRemove(editingModel.id);
            setEditingModelId(null);
          } : undefined}
        />
      )}
    </div>
  );
}

function providerQualifiedModelId(providerId: string, modelId: string) {
  const cleanProviderId = providerId.trim();
  const cleanModelId = modelId.trim();
  if (!cleanProviderId || !cleanModelId || cleanModelId.startsWith(`${cleanProviderId}/`)) {
    return cleanModelId;
  }
  return `${cleanProviderId}/${cleanModelId}`;
}

function ModelIdentity({
  model,
  onTest,
  providerId,
  testDisabled,
  testState = "idle",
}: {
  model: Model;
  onTest?: () => void;
  providerId?: string;
  testDisabled?: boolean;
  testState?: InlineTestState;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const copyValue = providerId ? providerQualifiedModelId(providerId, model.id) : model.id;

  async function copyModelId(event: React.MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    try {
      await navigator.clipboard.writeText(copyValue);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  }

  function testCurrentModel(event: React.MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    onTest?.();
  }

  return (
    <div className="min-w-0">
      <span className="block truncate text-sm font-medium">{displayModel(model)}</span>
      <span className="mt-0.5 flex min-w-0 items-center gap-1 text-xs text-slate-500">
        <span className="min-w-0 truncate font-mono">{model.id}</span>
        <button
          type="button"
          className="focus-ring inline-flex h-6 w-6 shrink-0 items-center justify-center rounded border border-transparent text-slate-500 transition-[background-color,border-color,color] duration-150 ease-out hover:border-line hover:bg-panel hover:text-ink"
          onClick={copyModelId}
          title={copied ? t("common.copied") : t("providers.copyModelIdTitle", { id: copyValue })}
          aria-label={copied ? t("providers.copiedModelId", { id: copyValue }) : t("providers.copyModelId", { id: copyValue })}
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
        </button>
        {onTest && (
          <button
            type="button"
            className={cx(
              "focus-ring inline-flex h-6 w-6 shrink-0 items-center justify-center rounded border text-slate-500 transition-[background-color,border-color,color,transform] duration-150 ease-out active:scale-[0.96]",
              testState === "success"
                ? "status-pop border-emerald-200 bg-emerald-50 text-emerald-700"
                : testState === "error"
                  ? "status-pop border-red-200 bg-red-50 text-danger"
                  : "border-transparent text-slate-500 hover:border-line hover:bg-panel hover:text-ink",
            )}
            disabled={testDisabled || testState === "testing"}
            onClick={testCurrentModel}
            title={t("providers.testModelTitle", { id: copyValue })}
            aria-label={t("providers.testModelTitle", { id: copyValue })}
          >
            <ModelTestStateIcon state={testState} size={13} />
          </button>
        )}
      </span>
    </div>
  );
}

function ModelEditorOverlay({
  model,
  onApply,
  onClose,
  onRemove,
}: {
  model: Model;
  onApply: (model: Model) => void;
  onClose: () => void;
  onRemove?: () => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<Model>(() => normalizeModel(model));
  const reasoningEnabled = (draft.supported_reasoning_levels ?? []).length > 0;

  useEffect(() => {
    setDraft(normalizeModel(model));
  }, [model]);

  function setReasoningEnabled(enabled: boolean) {
    setDraft((current) => {
      const levels = current.supported_reasoning_levels?.length
        ? current.supported_reasoning_levels
        : reasoningLevelOptions;
      return {
        ...current,
        supported_reasoning_levels: enabled ? levels : [],
        default_reasoning_level: enabled
          ? current.default_reasoning_level && levels.includes(current.default_reasoning_level)
            ? current.default_reasoning_level
            : "medium"
          : null,
      };
    });
  }

  function setReasoningLevel(level: string, checked: boolean) {
    setDraft((current) => {
      const levels = toggleReasoningLevel(current.supported_reasoning_levels ?? [], level, checked);
      return {
        ...current,
        supported_reasoning_levels: levels,
        default_reasoning_level:
          current.default_reasoning_level && levels.includes(current.default_reasoning_level)
            ? current.default_reasoning_level
            : levels.includes("medium")
              ? "medium"
              : levels[0] ?? null,
      };
    });
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/20 p-6">
      <div className="grid w-full max-w-[760px] overflow-hidden rounded-md border border-line bg-white shadow-xl">
        <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <h3 className="truncate text-base font-semibold">{t("providers.modelSettings")}</h3>
            <p className="mt-1 truncate text-xs text-slate-500">{model.id}</p>
          </div>
          <button
            type="button"
            className="focus-ring grid h-8 w-8 place-items-center rounded-md border border-line bg-panel hover:bg-slate-100"
            onClick={onClose}
            aria-label={t("providers.closeModelSettings")}
          >
            <X size={16} />
          </button>
        </div>

        <div className="grid gap-4 p-5">
          <section className="grid gap-3 rounded-md border border-line bg-panel p-3">
            <div>
              <h4 className="text-sm font-semibold">{t("providers.identity")}</h4>
              <p className="mt-0.5 text-xs text-slate-500">{t("providers.gatewayFacingModelName")}</p>
            </div>
            <Field label={t("common.modelId")}>
              <input
                className="field h-9"
                value={draft.id}
                onChange={(event) => setDraft({ ...draft, id: event.target.value })}
              />
            </Field>
            <Field label={t("common.displayName")}>
              <input
                className="field h-9"
                value={draft.display_name ?? ""}
                onChange={(event) => setDraft({ ...draft, display_name: event.target.value || null })}
              />
            </Field>
            <Field label={t("providers.context")}>
              <input
                className="field h-9"
                min={0}
                type="number"
                value={draft.context_window ?? ""}
                onChange={(event) =>
                  setDraft({ ...draft, context_window: optionalPositiveNumber(event.target.value) })
                }
              />
            </Field>
          </section>

          <section className="grid gap-3 rounded-md border border-line bg-panel p-3">
            <div>
              <div className="text-sm font-semibold">{t("providers.capabilities")}</div>
              <div className="mt-0.5 text-xs text-slate-500">{t("providers.gatewayFacingMetadata")}</div>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="flex h-9 items-center justify-between rounded-md border border-line bg-white px-3 text-sm font-medium">
                <span className="inline-flex items-center gap-2">
                  <Eye size={15} />
                  {t("providers.vision")}
                </span>
                <input
                  type="checkbox"
                  checked={hasVision(draft)}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      input_modalities: event.target.checked ? ["text", "image"] : ["text"],
                    })
                  }
                />
              </label>
              <label className="flex h-9 items-center justify-between rounded-md border border-line bg-white px-3 text-sm font-medium">
                <span className="inline-flex items-center gap-2">
                  <Brain size={15} />
                  {t("providers.thinking")}
                </span>
                <input
                  type="checkbox"
                  checked={reasoningEnabled}
                  onChange={(event) => setReasoningEnabled(event.target.checked)}
                />
              </label>
            </div>
            {reasoningEnabled && (
              <div className="grid gap-3 rounded-md border border-line bg-white p-3 lg:grid-cols-[minmax(0,1fr)_190px]">
                <div className="grid gap-2">
                  <span className="text-xs font-semibold uppercase text-slate-500">{t("providers.reasoningLevels")}</span>
                  <div className="flex flex-wrap gap-2">
                    {reasoningLevelOptions.map((level) => (
                      <label
                        key={level}
                        className="flex h-8 items-center gap-2 rounded-md border border-line bg-white px-2 text-xs font-medium"
                      >
                        <input
                          type="checkbox"
                          checked={(draft.supported_reasoning_levels ?? []).includes(level)}
                          onChange={(event) => setReasoningLevel(level, event.target.checked)}
                        />
                        {level}
                      </label>
                    ))}
                  </div>
                </div>
                <Field label={t("common.defaultReasoning")}>
                  <select
                    className="field h-9"
                    value={draft.default_reasoning_level ?? ""}
                    onChange={(event) =>
                      setDraft({ ...draft, default_reasoning_level: event.target.value || null })
                    }
                  >
                    {(draft.supported_reasoning_levels ?? []).map((level) => (
                      <option key={level} value={level}>
                        {level}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
            )}
          </section>
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-line px-5 py-4">
          {onRemove ? (
            <button
              type="button"
              className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-danger/40 bg-red-50 px-3 text-sm font-semibold text-danger"
              onClick={onRemove}
            >
              <Trash2 size={15} />
              {t("providers.removeModel")}
            </button>
          ) : (
            <span />
          )}
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="focus-ring inline-flex h-9 items-center justify-center rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100"
              onClick={onClose}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="focus-ring inline-flex h-9 items-center justify-center rounded-md bg-action px-3 text-sm font-semibold text-white"
              onClick={() => onApply(normalizeModel(draft))}
            >
              {t("common.apply")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function optionalPositiveNumber(value: string) {
  if (!value.trim()) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function CapabilityChip({ icon, label }: { icon?: React.ReactNode; label: string }) {
  return (
    <span className="inline-flex h-6 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border border-line bg-panel px-2 text-xs font-semibold text-slate-600">
      {icon}
      {label}
    </span>
  );
}

function ModelCapabilityChip({ tag }: { tag: "vision" | "thinking" }) {
  const { t } = useTranslation();
  if (tag === "vision") {
    return <CapabilityChip icon={<Eye size={13} />} label={t("providers.vision")} />;
  }
  return <CapabilityChip icon={<Brain size={13} />} label={t("providers.thinking")} />;
}

function SwitchControl({
  checked,
  label,
  onChange,
  showLabel = true,
}: {
  checked: boolean;
  label: string;
  onChange: (checked: boolean) => void;
  showLabel?: boolean;
}) {
  return (
    <label
      className={cx(
        "inline-flex h-6 shrink-0 items-center gap-2 whitespace-nowrap text-xs font-semibold text-slate-600",
        showLabel && "rounded-full border border-line bg-panel pl-2 pr-1",
      )}
    >
      <span className={showLabel ? "truncate" : "sr-only"}>{label}</span>
      <span className="relative inline-flex h-5 w-9 shrink-0 items-center">
        <input
          type="checkbox"
          className="peer sr-only"
          checked={checked}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span className="absolute inset-0 rounded-full border border-line bg-slate-200 transition-colors peer-checked:border-action peer-checked:bg-action" />
        <span className="absolute left-0.5 h-4 w-4 rounded-full bg-white shadow-sm transition-transform peer-checked:translate-x-4" />
      </span>
    </label>
  );
}

function modelCapabilityTags(model: Model): Array<"vision" | "thinking"> {
  const tags: Array<"vision" | "thinking"> = [];
  if (hasVision(model)) {
    tags.push("vision");
  }
  if ((model.supported_reasoning_levels ?? []).length || model.default_reasoning_level) {
    tags.push("thinking");
  }
  return tags;
}

function isOfficialModelDisabled(disabledModels: string[], modelId: string) {
  return disabledModels.some((item) => modelIdMatches(item, modelId));
}

function modelIdMatches(left: string, right: string) {
  const normalize = (value: string) => value.trim().replace(/^openai\//, "");
  return normalize(left) === normalize(right);
}

function withDefaultFastVariants(settings: Settings): Settings {
  const base = {
    ...settings,
    auto_sync_history: settings.auto_sync_history ?? false,
    unified_codex_history: settings.unified_codex_history ?? true,
    auto_sync_clients: settings.auto_sync_clients ?? settings.auto_sync_catalog ?? true,
    official_disabled_models: settings.official_disabled_models ?? [],
  };
  if (settings.gateway_fast_model_variants?.length) {
    return base;
  }
  return { ...base, gateway_fast_model_variants: DEFAULT_FAST_MODEL_VARIANTS };
}

function formatContextWindow(value?: number | null) {
  if (!value) {
    return i18n.t("common.unknown");
  }
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1)}M`;
  }
  if (value >= 1000) {
    const rounded = Math.round(value / 1000);
    return `${new Intl.NumberFormat(i18n.language || "en-US").format(rounded)}K`;
  }
  return new Intl.NumberFormat(i18n.language || "en-US").format(value);
}

function hasVision(model: Model) {
  return (model.input_modalities ?? ["text"]).includes("image");
}

function toggleReasoningLevel(current: string[], level: string, checked: boolean) {
  const next = checked ? [...new Set([...current, level])] : current.filter((item) => item !== level);
  return reasoningLevelOptions.filter((item) => next.includes(item));
}

function normalizeModel(model: Model): Model {
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

function normalizeProviderEndpointSelection(provider: Provider): Provider {
  return {
    ...provider,
    upstream_format:
      !provider.upstream_format || provider.upstream_format === "auto"
        ? "responses"
        : provider.upstream_format,
    available_upstream_formats: normalizeEndpointFormats(provider.available_upstream_formats),
  };
}

function normalizeEndpointFormats(values?: Array<UpstreamFormat | null | undefined> | null): UpstreamFormat[] {
  if (!values?.length) {
    return [];
  }
  const available = new Set(values.filter((value): value is UpstreamFormat => Boolean(value)));
  return endpointSelectionOptions
    .map((option) => option.value)
    .filter((value) => value !== "auto" && available.has(value));
}

function mergeEndpointFormats(
  ...groups: Array<Array<UpstreamFormat | null | undefined> | null | undefined>
): UpstreamFormat[] {
  return normalizeEndpointFormats(groups.flatMap((group) => group ?? []));
}

function hasAvailableEndpointFormats(values?: Array<UpstreamFormat | null | undefined> | null) {
  return normalizeEndpointFormats(values).length > 0;
}

function probeRecommendedEndpointFormat(
  result: UpstreamFormatProbeResult,
  fallbackFormat?: UpstreamFormat | null,
): UpstreamFormat {
  if (result.recommended_format !== "auto") {
    return result.recommended_format;
  }
  return probeAvailableFormats(result)[0] ?? normalizedEndpointFormat(fallbackFormat);
}

function applyProviderProbeResult(provider: Provider, result: UpstreamFormatProbeResult): Provider {
  return {
    ...provider,
    upstream_format: probeRecommendedEndpointFormat(result, provider.upstream_format),
    available_upstream_formats: probeAvailableFormats(result),
  };
}

function applyAddProviderProbeResult(form: AddProviderForm, result: UpstreamFormatProbeResult): AddProviderForm {
  return {
    ...form,
    upstream_format: probeRecommendedEndpointFormat(result, form.upstream_format),
    available_upstream_formats: probeAvailableFormats(result),
  };
}

function isAddProviderFormDirty(form: AddProviderForm) {
  return Boolean(form.name.trim());
}

function pendingProviderName(pending: PendingProviderNavigation, t: Translate) {
  if (pending.kind === "existing") {
    return pending.draft.name;
  }
  return pending.form.name.trim() || t("providers.newProvider");
}

function sortOfficialModels(models: Model[], sortOrder: string[]) {
  const order = new Map<string, number>();
  const effectiveOrder = sortOrder.length ? sortOrder : DEFAULT_OFFICIAL_MODEL_ORDER;
  effectiveOrder.forEach((id, index) => {
    for (const key of officialModelSortKeys(id)) {
      order.set(key, index);
    }
  });
  return [...models].sort((left, right) => {
    const leftIndex = officialModelSortKeys(left.id).reduce(
      (current, key) => Math.min(current, order.get(key) ?? Number.MAX_SAFE_INTEGER),
      Number.MAX_SAFE_INTEGER,
    );
    const rightIndex = officialModelSortKeys(right.id).reduce(
      (current, key) => Math.min(current, order.get(key) ?? Number.MAX_SAFE_INTEGER),
      Number.MAX_SAFE_INTEGER,
    );
    if (leftIndex !== rightIndex) {
      return leftIndex - rightIndex;
    }
    return (left.sort_order ?? Number.MAX_SAFE_INTEGER) - (right.sort_order ?? Number.MAX_SAFE_INTEGER);
  });
}

function mergeOfficialModelSources(catalog: Model[], metadata: Model[]) {
  const merged = new Map<string, Model>();
  for (const model of metadata.filter(isOfficialModel)) {
    merged.set(model.id, {
      ...model,
      enabled: true,
    });
  }
  for (const model of catalog.filter(isOfficialModel)) {
    const existing = merged.get(model.id);
    merged.set(model.id, {
      ...existing,
      ...model,
      context_window: existing?.context_window ?? model.context_window,
      max_output_tokens: existing?.max_output_tokens ?? model.max_output_tokens,
      input_modalities: existing?.input_modalities ?? model.input_modalities,
      supported_reasoning_levels: existing?.supported_reasoning_levels ?? model.supported_reasoning_levels,
      default_reasoning_level: existing?.default_reasoning_level ?? model.default_reasoning_level,
      enabled: true,
    });
  }
  return filterCodexVisibleOfficialModels(Array.from(merged.values()));
}

function isOfficialModel(model: Model) {
  return model.id.startsWith("openai/") || model.id.startsWith("gpt-");
}

function filterCodexVisibleOfficialModels(models: Model[]) {
  return models.filter((model) => !isOfficialGatewayFastVariant(model));
}

function isOfficialGatewayFastVariant(model: Model) {
  const normalizedId = model.id.trim().replace(/^openai\//, "");
  return normalizedId === "gpt-5.5-fast" || normalizedId === "gpt-5.4-fast";
}

function officialModelSortKeys(id: string) {
  const prefix = "openai/";
  return id.startsWith(prefix) ? [id, id.slice(prefix.length)] : [`${prefix}${id}`, id];
}

function uniqueModelId(models: Model[]) {
  const existing = new Set(models.map((model) => model.id));
  let index = models.length + 1;
  let id = `new-model-${index}`;
  while (existing.has(id)) {
    index += 1;
    id = `new-model-${index}`;
  }
  return id;
}

function createDraftModel(id: string, sortOrder: number): Model {
  return {
    id,
    display_name: "",
    upstream_model: "",
    context_window: 200_000,
    max_output_tokens: null,
    input_modalities: ["text"],
    supported_reasoning_levels: reasoningLevelOptions,
    default_reasoning_level: "medium",
    source_kind: "manual",
    locked: false,
    codex_enabled: true,
    gateway_exported: true,
    sort_order: sortOrder,
    enabled: true,
  };
}

function AddProviderPanel({
  busy,
  canAdd,
  discoverError,
  form,
  onAdd,
  onDiscover,
  onFormChange,
  onProbe,
  probeResult,
}: {
  busy: string | null;
  canAdd: boolean;
  discoverError?: string | null;
  form: typeof emptyProvider;
  onAdd: () => void;
  onDiscover: () => void;
  onFormChange: (form: typeof emptyProvider) => void;
  onProbe: () => Promise<UpstreamFormatProbeResult | null>;
  probeResult: UpstreamFormatProbeResult | null;
}) {
  const { t } = useTranslation();
  const { showToast, updateToast } = useToasts();
  const [endpointTestState, setEndpointTestState] = useState<InlineTestState>("idle");

  useEffect(() => {
    if (probeResult) {
      setEndpointTestState("success");
    }
  }, [probeResult]);

  function updateModel(modelId: string, patch: Partial<Model>) {
    onFormChange({
      ...form,
      models: form.models.map((model) =>
        model.id === modelId ? normalizeModel({ ...model, ...patch }) : model,
      ),
    });
  }

  function addModel() {
    const id = uniqueModelId(form.models);
    onFormChange({
      ...form,
      models: [...form.models, createDraftModel(id, form.models.length + 1)],
    });
    return id;
  }

  async function runProbe() {
    setEndpointTestState("testing");
    const result = await onProbe();
    if (result) {
      onFormChange(applyAddProviderProbeResult(form, result));
    }
    setEndpointTestState(result ? "success" : "error");
  }

  async function testModel(model: Model) {
    const label = displayModel(model);
    const upstreamFormat = normalizedEndpointFormat(form.upstream_format);
    const endpointLabel = upstreamFormatLabel(upstreamFormat, t as Translate);
    const toastId = showToast(t("providers.testingModel", { label, endpoint: endpointLabel }), "loading");
    try {
      const result = await api.testModelEndpoint(
        form.base_url,
        form.api_key,
        modelProbeId(model),
        upstreamFormat,
      );
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
      <div className="grid gap-2 border-b border-line p-4">
        <HeaderRow title={t("providers.addProvider")} />
        <div className="grid grid-cols-2 gap-2">
          <Field label={t("common.name")}>
            <input
              className="field field-compact"
              value={form.name}
              onChange={(event) => onFormChange({ ...form, name: event.target.value })}
            />
          </Field>
          <Field label={t("common.apiKey")}>
            <ApiKeyInput
              value={form.api_key}
              onChange={(apiKey) => onFormChange({ ...form, api_key: apiKey })}
            />
          </Field>
          <Field label={t("common.baseUrl")} className="col-span-2">
            <input
              className="field field-compact"
              value={form.base_url}
              onChange={(event) => onFormChange({ ...form, base_url: event.target.value })}
            />
          </Field>
          <div className="col-span-2">
            <EndpointSelectionPanel
              value={form.upstream_format}
              result={probeResult}
              availableFormats={form.available_upstream_formats}
              probeDisabled={busy === "probe" || !form.base_url.trim()}
              testState={endpointTestState}
              onChange={(upstreamFormat) => onFormChange({ ...form, upstream_format: upstreamFormat })}
              onProbe={() => void runProbe()}
            />
          </div>
        </div>
      </div>

      <ModelSection
        discoverDisabled={!form.base_url.trim()}
        discoverBusy={busy === "discover"}
        discoverError={discoverError}
        models={form.models}
        onAdd={addModel}
        onDiscover={onDiscover}
        onReorder={(models) => onFormChange({ ...form, models: renumberModels(models) })}
        onRemove={(modelId) =>
          onFormChange({ ...form, models: form.models.filter((model) => model.id !== modelId) })
        }
        onTestModel={testModel}
        onToggle={(modelId, enabled) => updateModel(modelId, { enabled })}
        onUpdate={updateModel}
        modelTestDisabled={!form.base_url.trim()}
      />

      <div className="flex items-center justify-end border-t border-line px-5 py-3">
        <button
          type="button"
          className="focus-ring inline-flex h-9 items-center gap-2 rounded-md bg-action px-3 text-sm font-semibold text-white disabled:bg-slate-300"
          disabled={!canAdd || Boolean(busy)}
          onClick={onAdd}
        >
          <Plus size={16} />
          {t("providers.addProvider")}
        </button>
      </div>
    </div>
  );
}

function EndpointSelectionPanel({
  availableFormats,
  onChange,
  onProbe,
  probeDisabled,
  result,
  testState,
  value,
}: {
  availableFormats?: UpstreamFormat[] | null;
  onChange: (value: UpstreamFormat) => void;
  onProbe: () => void;
  probeDisabled: boolean;
  result?: UpstreamFormatProbeResult | null;
  testState: InlineTestState;
  value?: UpstreamFormat | null;
}) {
  const { t } = useTranslation();
  const selected = normalizedEndpointFormat(value);
  const mergedAvailableFormats = mergeEndpointFormats(availableFormats, probeAvailableFormats(result));

  return (
    <div className="grid min-w-0 gap-1 text-sm font-medium text-slate-700">
      <span>{t("common.endpointSelection")}</span>
      <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
        <EndpointFormatSelect availableFormats={mergedAvailableFormats} value={selected} onChange={onChange} />
        <button
          type="button"
          className={cx(
            "mini-button inline-flex h-9 shrink-0 items-center justify-center gap-2 px-3 text-sm font-semibold disabled:bg-slate-100",
            testState === "success" && "status-pop border-emerald-200 bg-emerald-50 text-emerald-700",
            testState === "error" && "status-pop border-red-200 bg-red-50 text-danger",
          )}
          disabled={probeDisabled || testState === "testing"}
          onClick={onProbe}
        >
          <TestStateIcon state={testState} size={16} />
          {t("common.test")}
        </button>
      </div>
    </div>
  );
}

function EndpointFormatSelect({
  availableFormats,
  onChange,
  value,
}: {
  availableFormats: UpstreamFormat[];
  onChange: (value: UpstreamFormat) => void;
  value: UpstreamFormat;
}) {
  const [open, setOpen] = useState(false);
  const selected = endpointSelectionOptions.find((option) => option.value === value) ?? endpointSelectionOptions[0];
  const available = new Set(availableFormats);
  const selectedAvailable = available.has(selected.value);
  const { t } = useTranslation();
  const tr = t as Translate;

  return (
    <div
      className="relative min-w-0"
      onBlur={(event) => {
        const nextTarget = event.relatedTarget as Node | null;
        if (!nextTarget || !event.currentTarget.contains(nextTarget)) {
          setOpen(false);
        }
      }}
    >
      <button
        type="button"
        className="select-trigger h-9 w-full"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate">{upstreamFormatLabel(selected.value, tr)}</span>
          {selectedAvailable && <EndpointAvailableChip />}
        </span>
        <ChevronDown size={15} className="shrink-0 text-slate-500" />
      </button>
      {open && (
        <div className="select-popover absolute left-0 top-[calc(100%+6px)] z-30 w-full min-w-[240px]" role="listbox">
          {endpointSelectionOptions.map((option) => {
            const selectedOption = option.value === value;
            const optionAvailable = available.has(option.value);
            return (
              <button
                key={option.value}
                type="button"
                className="select-option"
                role="option"
                aria-selected={selectedOption}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  onChange(option.value);
                  setOpen(false);
                }}
              >
                <span className="truncate">{upstreamFormatLabel(option.value, tr)}</span>
                <span className="flex shrink-0 items-center gap-2">
                  {optionAvailable && <EndpointAvailableChip />}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function EndpointAvailableChip() {
  const { t } = useTranslation();
  return (
    <span className="shrink-0 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-semibold leading-4 text-emerald-700">
      {t("common.available")}
    </span>
  );
}

function TestStateIcon({ size, state }: { size: number; state: InlineTestState }) {
  if (state === "testing") {
    return <RefreshCcw size={size} className="shrink-0 animate-spin" />;
  }
  if (state === "success") {
    return <Check size={size} className="shrink-0" />;
  }
  if (state === "error") {
    return <X size={size} className="shrink-0" />;
  }
  return <FlaskConical size={size} className="shrink-0" />;
}

function ModelTestStateIcon({ size, state }: { size: number; state: InlineTestState }) {
  if (state === "testing") {
    return <RefreshCcw size={size} className="shrink-0 animate-spin" />;
  }
  if (state === "success") {
    return <Check size={size} className="shrink-0" />;
  }
  if (state === "error") {
    return <X size={size} className="shrink-0" />;
  }
  return <Cable size={size} className="shrink-0" />;
}

function normalizedEndpointFormat(value?: UpstreamFormat | null): UpstreamFormat {
  if (value === "chat_completions" || value === "anthropic_messages") {
    return value;
  }
  return "responses";
}

function upstreamFormatLabel(value?: UpstreamFormat | null, t?: Translate) {
  if (value === "responses") {
    return t?.("providers.upstreamFormats.responses") ?? i18n.t("providers.upstreamFormats.responses");
  }
  if (value === "chat_completions") {
    return t?.("providers.upstreamFormats.chatCompletions") ?? i18n.t("providers.upstreamFormats.chatCompletions");
  }
  if (value === "anthropic_messages") {
    return t?.("providers.upstreamFormats.anthropicMessages") ?? i18n.t("providers.upstreamFormats.anthropicMessages");
  }
  return t?.("providers.upstreamFormats.responses") ?? i18n.t("providers.upstreamFormats.responses");
}

function probeAvailableFormats(result?: UpstreamFormatProbeResult | null): UpstreamFormat[] {
  if (!result) {
    return [];
  }
  const formats: UpstreamFormat[] = [];
  if (result.responses_text_ok || result.responses_tool_ok || result.responses_tool_stream_ok) {
    formats.push("responses");
  }
  if (result.chat_text_ok || result.chat_tool_ok || result.chat_tool_stream_ok) {
    formats.push("chat_completions");
  }
  if (result.anthropic_text_ok) {
    formats.push("anthropic_messages");
  }
  if (result.recommended_format !== "auto" && !formats.includes(result.recommended_format)) {
    formats.push(result.recommended_format);
  }
  return formats;
}

function probeResultSummary(result: UpstreamFormatProbeResult) {
  const availableFormats = probeAvailableFormats(result).map((format) => upstreamFormatLabel(format));
  if (availableFormats.length) {
    return i18n.t("providers.availableFormats", { formats: availableFormats.join(", ") });
  }
  return i18n.t("providers.recommendedFormat", { format: upstreamFormatLabel(result.recommended_format) });
}

function modelProbeId(model: Model) {
  return model.upstream_model?.trim() || model.id;
}

function officialModelProbeId(model: Model) {
  const modelId = modelProbeId(model);
  return modelId.startsWith("openai/") ? modelId.slice("openai/".length) : modelId;
}

function shortProviderDiscoveryError(err: unknown, t: Translate) {
  const message = messageFromError(err);
  const missingEnv = message.match(/\b([A-Z_][A-Z0-9_]*_API_KEY)\b[^.]*\bis not set\b/i);
  if (missingEnv) {
    return t("providers.discoveryFailedNotSet", { env: missingEnv[1] });
  }
  if (/unauthorized|401/i.test(message)) {
    return t("providers.discoveryFailedUnauthorized");
  }
  if (/timeout|timed out/i.test(message)) {
    return t("providers.discoveryTimedOut");
  }
  if (/not found|404/i.test(message)) {
    return t("providers.discoveryFailedMissingEndpoint");
  }
  if (/builder error|invalid/i.test(message)) {
    return t("providers.discoveryFailedInvalid");
  }
  return t("providers.discoveryFailed");
}

function codexHubConnectionErrorMessage(err: unknown, t: Translate) {
  const message = messageFromError(err);

  return t("providers.codexHubConnectionFailed", { message });
}

function codexHubConnectionSuccessMessage(mode: string, t: Translate) {
  return mode === "custom" ? t("providers.connectedToHub") : t("providers.disconnectedFromHub");
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

function HeaderRow({
  actions,
  subtitle,
  title,
}: {
  actions?: React.ReactNode;
  subtitle?: string;
  title: string;
}) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3">
      <div className="min-w-0">
        <h2 className="truncate text-base font-semibold">{title}</h2>
        {subtitle && <p className="mt-1 truncate text-sm text-slate-500">{subtitle}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-nowrap items-center gap-2 whitespace-nowrap">{actions}</div>}
    </div>
  );
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

function Field({
  children,
  className,
  label,
}: {
  children: React.ReactNode;
  className?: string;
  label: string;
}) {
  return (
    <label className={cx("grid gap-1 text-sm font-medium text-slate-700", className)}>
      {label}
      {children}
    </label>
  );
}

function ApiKeyInput({
  onChange,
  value,
}: {
  onChange: (value: string) => void;
  value: string;
}) {
  const { t } = useTranslation();
  const [visible, setVisible] = useState(false);

  return (
    <div className="relative">
      <input
        className="field field-compact pr-10"
        type={visible ? "text" : "password"}
        autoComplete="off"
        spellCheck={false}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      <button
        type="button"
        className="focus-ring absolute right-1 top-1 grid h-7 w-7 place-items-center rounded-md text-slate-500 hover:bg-panel hover:text-ink"
        onClick={() => setVisible((current) => !current)}
        title={visible ? t("common.hideApiKey") : t("common.showApiKey")}
        aria-label={visible ? t("common.hideApiKey") : t("common.showApiKey")}
      >
        {visible ? <EyeOff size={15} /> : <Eye size={15} />}
      </button>
    </div>
  );
}

function IconButton({
  children,
  danger,
  disabled,
  onClick,
  title,
}: {
  children: React.ReactNode;
  danger?: boolean;
  disabled?: boolean;
  onClick: () => void;
  title: string;
}) {
  return (
    <button
      type="button"
      className={cx(
        "focus-ring grid h-9 w-9 place-items-center rounded-md border bg-panel",
        danger ? "border-danger/40 bg-red-50 text-danger" : "border-line text-ink hover:bg-slate-100",
      )}
      disabled={disabled}
      onClick={onClick}
      title={title}
    >
      {children}
    </button>
  );
}
