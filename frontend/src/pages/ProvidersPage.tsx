import {
  Brain,
  Cable,
  Check,
  ChevronDown,
  Copy,
  Eye,
  EyeOff,
  ExternalLink,
  FlaskConical,
  Link2,
  Link2Off,
  Plus,
  RefreshCcw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import type { FocusEvent as ReactFocusEvent, PointerEvent as ReactPointerEvent } from "react";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { BACKEND_DISCONNECTED_TOAST_KEY, useToasts } from "../components/PageToast";
import { SortableList } from "../components/SortableList";
import i18n from "../i18n";
import { cx, displayModel, mergeDiscoveredModels, renumberModels, slugify } from "../lib/format";
import { normalizeOfficialModelId, normalizeSettings } from "../lib/settings";
import { api, isBackendDisconnectedMessage, messageFromError } from "../lib/tauri";
import type {
  AppFlavorInfo,
  AppStatus,
  CodexContextGuardStatus,
  GatewayStatus,
  GatewayClientSyncSummary,
  Model,
  OpenAIUsageLimit,
  OpenAIUsageSnapshot,
  Provider,
  Settings,
  ToolProtocol,
  UpstreamFormat,
  UpstreamFormatProbeResult,
} from "../lib/types";

const OFFICIAL_ID = "__official__";
const ADD_ID = "__add__";
const LEGACY_AUTOMATIC_OFFICIAL_MODEL_ORDER = [
  "gpt-5.5",
  "gpt-5.4",
  "gpt-5.4-mini",
  "gpt-5.3-codex-spark",
];
const DEFAULT_OFFICIAL_MODEL_ORDER = [
  "gpt-5.6-sol",
  "gpt-5.6-terra",
  "gpt-5.6-luna",
  ...LEGACY_AUTOMATIC_OFFICIAL_MODEL_ORDER,
];
const OPENAI_USAGE_DAY_SECONDS = 86_400;
const OPENAI_USAGE_MIN_WINDOW_DAYS = 365;
const OPENAI_USAGE_QUERY_WINDOW_DAYS = 730;
const OPENAI_USAGE_REFRESH_INTERVAL_MS = 3 * 60 * 1000;
const OPENAI_USAGE_STORAGE_TTL_MS = OPENAI_USAGE_REFRESH_INTERVAL_MS;
const OFFICIAL_OPENAI_USAGE_STORAGE_KEY = "codexhub.officialOpenAIUsageSnapshot.v1";
const OFFICIAL_USAGE_CELL_GAP = 2;
const OFFICIAL_USAGE_CELL_SIZE = 8;
const USAGE_MONTH_LABEL_MIN_GAP_PX = 36;
const OFFICIAL_USAGE_COLOR_STOPS = ["#eff2f5", "#d8ebff", "#acd7ff", "#7cc1ff", "#48a7fb", "#1687e8"];
const OPENAI_USAGE_LIMIT_PLACEHOLDERS: OpenAIUsageLimit[] = [
  { key: "five_hours", name: "5 hours", period: "five_hours" },
  { key: "week", name: "Week", period: "week" },
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

function useElementContentWidth<T extends HTMLElement>(dependencies: ReadonlyArray<unknown> = []) {
  const ref = useRef<T | null>(null);
  const [contentWidth, setContentWidth] = useState(0);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) {
      setContentWidth(0);
      return;
    }

    const update = () => {
      const style = window.getComputedStyle(element);
      const padding =
        (Number.parseFloat(style.paddingLeft) || 0) +
        (Number.parseFloat(style.paddingRight) || 0);
      setContentWidth(Math.max(0, element.clientWidth - padding));
    };

    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    window.addEventListener("resize", update);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", update);
    };
  }, dependencies);

  return [ref, contentWidth] as const;
}

function readStoredOfficialOpenAIUsageSnapshot(): OpenAIUsageSnapshot | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const raw = window.localStorage.getItem(OFFICIAL_OPENAI_USAGE_STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const stored = JSON.parse(raw) as unknown;
    if (!isStoredOpenAIUsageSnapshot(stored)) {
      return null;
    }
    if (Date.now() - stored.stored_at > OPENAI_USAGE_STORAGE_TTL_MS) {
      return null;
    }
    return stored.snapshot;
  } catch {
    return null;
  }
}

function storeOfficialOpenAIUsageSnapshot(snapshot: OpenAIUsageSnapshot) {
  if (typeof window === "undefined") {
    return;
  }
  try {
    const stored = {
      stored_at: Date.now(),
      snapshot,
    };
    window.localStorage.setItem(OFFICIAL_OPENAI_USAGE_STORAGE_KEY, JSON.stringify(stored));
  } catch {
    // Ignore storage quota or privacy-mode failures; backend cache still works.
  }
}

function isStoredOpenAIUsageSnapshot(value: unknown): value is StoredOpenAIUsageSnapshot {
  if (!value || typeof value !== "object") {
    return false;
  }
  const stored = value as Partial<StoredOpenAIUsageSnapshot>;
  return (
    typeof stored.stored_at === "number" &&
    Number.isFinite(stored.stored_at) &&
    isOpenAIUsageSnapshot(stored.snapshot)
  );
}

function isOpenAIUsageSnapshot(value: unknown): value is OpenAIUsageSnapshot {
  if (!value || typeof value !== "object") {
    return false;
  }
  const snapshot = value as Partial<OpenAIUsageSnapshot>;
  return (
    typeof snapshot.start_time === "number" &&
    typeof snapshot.end_time === "number" &&
    typeof snapshot.total_tokens === "number" &&
    Array.isArray(snapshot.buckets) &&
    Array.isArray(snapshot.limits)
  );
}

const emptyProvider = {
  id: "",
  name: "",
  base_url: "",
  api_key: "",
  upstream_format: "responses" as UpstreamFormat,
  available_upstream_formats: [] as UpstreamFormat[],
  tool_protocol: "auto" as ToolProtocol,
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
type StoredOpenAIUsageSnapshot = {
  stored_at: number;
  snapshot: OpenAIUsageSnapshot;
};
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
type OpenAIUsageMode = "day" | "week";
type OfficialOpenAIUsageDay = {
  date: Date;
  dateKey: string;
  inputTokens: number;
  outputTokens: number;
  requests: number;
  startTime: number;
  totalTokens: number;
};
type OfficialOpenAIUsageChartColumn = {
  date: Date;
  days: Array<OfficialOpenAIUsageDay | null>;
  endTime: number;
  inputTokens: number;
  index: number;
  key: string;
  outputTokens: number;
  requests: number;
  startTime: number;
  totalTokens: number;
};
type OfficialOpenAIUsageChartCell = {
  column: OfficialOpenAIUsageChartColumn;
  columnKey: string;
  day: OfficialOpenAIUsageDay | null;
  filled: boolean;
  intensity: number;
  key: string;
  mode: OpenAIUsageMode;
  rowIndex: number;
  selectionKey: string;
  value: number;
};
type OfficialOpenAIUsageTooltipState = {
  cell: OfficialOpenAIUsageChartCell;
  cursorX: number;
  cursorY: number;
  hostWidth: number;
};

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
  const [selectedId, setSelectedId] = useState<string>(OFFICIAL_ID);
  const [form, setForm] = useState(emptyProvider);
  const [probeResult, setProbeResult] = useState<UpstreamFormatProbeResult | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
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
      onProvidersChanged?.(saved);
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
    const toastId = quiet
      ? null
      : showToast(t("providers.refreshingOfficialModels"), "loading");
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
  ) {
    setBusy("probe");
    setProbeResult(null);
    const toastId = showToast(t("providers.endpointSelectionTest"), "loading");
    try {
      const result = await api.probeUpstreamFormat(baseUrl, apiKey, model);
      setProbeResult(result);
      const detectedFormat = probeDetectedEndpointFormat(result);
      updateToast(toastId, {
        action: null,
        text: detectedFormat
          ? t("providers.probeCompleted", {
              format: upstreamFormatLabel(detectedFormat, tr),
            })
          : t("providers.probeNoSupportedEndpoint"),
        tone: detectedFormat ? "success" : "error",
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
      onProvidersChanged?.(saved);
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

function OfficialOpenAIUsageLimitBars({
  busy,
  limits,
}: {
  busy: boolean;
  limits: OpenAIUsageLimit[];
}) {
  const { i18n, t } = useTranslation();
  const locale = resolvedUsageLocale(i18n.language || "en-US");
  const visibleLimits = preferredOpenAIUsageLimits(limits);
  const renderedLimits = visibleLimits.length ? visibleLimits : OPENAI_USAGE_LIMIT_PLACEHOLDERS;
  const usingPlaceholders = !visibleLimits.length;

  return (
    <div className="grid w-[252px] shrink-0 grid-cols-2 gap-2">
      {renderedLimits.map((limit) => {
        const label = usageLimitPeriodLabel(limit, t as Translate);
        const endTime = usingPlaceholders
          ? busy
            ? t("providers.limitRefreshing")
            : t("providers.limitEndUnknown")
          : formatUsageLimitEnd(limit.resets_at, locale, t as Translate);
        const percent = usingPlaceholders ? null : remainingPercent(limit);
        const value =
          percent === null
            ? busy
              ? t("providers.limitRefreshing")
              : t("providers.limitEndUnknown")
            : t("providers.limitRemainingPercent", { percent: Math.round(percent) });
        return (
          <div
            key={limit.key}
            className="min-w-0 rounded-control bg-surface px-2 py-1.5 shadow-control"
            title={`${label} · ${value} · ${endTime}`}
            aria-label={
              percent === null
                ? t("providers.limitPendingAria", { label, endTime })
                : t("providers.limitRemainingAria", {
                    label,
                    percent: Math.round(percent),
                    endTime,
                  })
            }
          >
            <div className="flex min-w-0 items-baseline justify-between gap-2">
              <span className="whitespace-nowrap text-[10px] font-semibold leading-3 text-ink">{label}</span>
              <span
                className={cx(
                  "shrink-0 whitespace-nowrap text-[11px] font-bold leading-3",
                  percent === null ? "text-slate-400" : "text-emerald-700",
                )}
              >
                {value}
              </span>
            </div>
            <div className="mt-0.5 whitespace-nowrap text-[9px] font-medium leading-3 text-slate-400">{endTime}</div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-200">
              <div
                className={cx(
                  "h-full rounded-full transition-[width] duration-200 ease-out",
                  percent === null ? "w-full bg-slate-300/70" : "bg-emerald-500",
                  percent === null && busy && "animate-pulse",
                )}
                style={percent === null ? undefined : { width: `${percent}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function OfficialOpenAIUsagePanel({
  busy,
  error,
  snapshot,
  usageHidden,
}: {
  busy: boolean;
  error: string | null;
  snapshot: OpenAIUsageSnapshot | null;
  usageHidden: boolean;
}) {
  const { i18n, t } = useTranslation();
  const locale = resolvedUsageLocale(i18n.language || "en-US");
  const [mode, setMode] = useState<OpenAIUsageMode>("day");
  const [hoveredUsageCell, setHoveredUsageCell] = useState<OfficialOpenAIUsageTooltipState | null>(null);
  const [selectedUsageCellKey, setSelectedUsageCellKey] = useState<string | null>(null);
  const [chartHostRef, chartContentWidth] = useElementContentWidth<HTMLDivElement>([usageHidden, Boolean(snapshot)]);
  const visibleUsageColumnCount = responsiveUsageColumnCount(chartContentWidth);
  const days = useMemo(
    () => buildOfficialOpenAIUsageDays(snapshot, visibleUsageColumnCount),
    [snapshot, visibleUsageColumnCount],
  );
  const chart = useMemo(
    () => buildOfficialOpenAIUsageChart(days, mode, visibleUsageColumnCount),
    [days, mode, visibleUsageColumnCount],
  );
  const streaks = useMemo(() => usageStreaks(days), [days]);
  const peakTokens = snapshot?.peak_daily_tokens ?? days.reduce((peak, day) => Math.max(peak, day.totalTokens), 0);
  const currentStreak = snapshot?.current_streak_days ?? streaks.current;
  const longestStreak = snapshot?.longest_streak_days ?? streaks.longest;
  const modeOptions: Array<{ label: string; value: OpenAIUsageMode }> = [
    { label: t("usage.day"), value: "day" },
    { label: t("usage.week"), value: "week" },
  ];
  const selectedUsageColumnKey = selectedUsageCellKey?.startsWith("week-") ? selectedUsageCellKey : null;
  const hoveredUsageColumnKey = hoveredUsageCell?.cell.mode === "week" ? hoveredUsageCell.cell.columnKey : null;
  const highlightedUsageCellKey = hoveredUsageCell?.cell.selectionKey ?? selectedUsageCellKey;

  useEffect(() => {
    setHoveredUsageCell(null);
    setSelectedUsageCellKey(null);
  }, [mode, snapshot]);

  function activateUsageCell(event: ReactPointerEvent<HTMLButtonElement>, cell: OfficialOpenAIUsageChartCell) {
    const host = event.currentTarget.closest("[data-openai-usage-chart]");
    if (!(host instanceof HTMLElement)) {
      return;
    }
    const hostRect = host.getBoundingClientRect();
    setHoveredUsageCell({
      cell,
      cursorX: event.clientX - hostRect.left,
      cursorY: event.clientY - hostRect.top,
      hostWidth: hostRect.width,
    });
  }

  function focusUsageCell(event: ReactFocusEvent<HTMLButtonElement>, cell: OfficialOpenAIUsageChartCell) {
    const host = event.currentTarget.closest("[data-openai-usage-chart]");
    if (!(host instanceof HTMLElement)) {
      return;
    }
    const hostRect = host.getBoundingClientRect();
    const cellRect = event.currentTarget.getBoundingClientRect();
    setHoveredUsageCell({
      cell,
      cursorX: cellRect.left - hostRect.left + cellRect.width / 2,
      cursorY: cellRect.top - hostRect.top,
      hostWidth: hostRect.width,
    });
  }

  if (usageHidden) {
    return null;
  }

  return (
    <section className="grid gap-3 rounded-inner bg-panel-soft p-3 shadow-hairline">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <h3 className="truncate text-sm font-semibold text-ink">{t("providers.openaiUsage")}</h3>
        </div>
        <div className="flex shrink-0 rounded-full bg-surface p-0.5 shadow-control">
          {modeOptions.map((option) => (
            <button
              key={option.value}
              type="button"
              className={cx(
                "focus-ring h-6 rounded-full px-2 text-[11px] font-semibold transition-[background-color,color]",
                mode === option.value ? "bg-ink text-white" : "text-slate-500 hover:bg-panel hover:text-ink",
              )}
              onClick={() => setMode(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      {error ? (
        <div className="rounded-inner bg-amber-50 px-3 py-2 text-xs font-medium text-amber-800 shadow-hairline">
          {error}
        </div>
      ) : busy && !snapshot ? (
        <OfficialOpenAIUsageSkeleton label={t("providers.loadingOpenAIUsage")} />
      ) : (
        <>
          <div className="grid grid-cols-5 gap-2 text-xs">
            <SourceMetric
              label={t("gateway.tokens")}
              value={snapshot ? formatUsageNumber(snapshot.total_tokens, locale) : t("common.unknown")}
            />
            <SourceMetric
              label={t("providers.peakDayTokens")}
              value={snapshot ? formatUsageNumber(peakTokens, locale) : t("common.unknown")}
            />
            <SourceMetric
              label={t("providers.longestTaskDuration")}
              value={snapshot ? formatUsageDuration(snapshot.longest_running_turn_sec, locale, t as Translate) : t("common.unknown")}
            />
            <SourceMetric
              label={t("providers.currentStreak")}
              value={snapshot ? t("providers.daysCount", { count: currentStreak }) : t("common.unknown")}
            />
            <SourceMetric
              label={t("providers.longestStreak")}
              value={snapshot ? t("providers.daysCount", { count: longestStreak }) : t("common.unknown")}
            />
          </div>

          <div
            ref={chartHostRef}
            className="relative min-w-0 overflow-visible rounded-inner bg-surface px-3 py-2 shadow-control"
            data-openai-usage-chart
            onPointerLeave={() => setHoveredUsageCell(null)}
          >
            {snapshot && days.length ? (
              <div className="overflow-hidden">
                <div
                  className="grid"
                  role="img"
                  aria-label={t("providers.openaiUsageActivity")}
                  style={{
                    gridAutoFlow: "column",
                    gridTemplateColumns: `repeat(${Math.max(1, chart.columns.length)}, ${OFFICIAL_USAGE_CELL_SIZE}px)`,
                    gridTemplateRows: `repeat(7, ${OFFICIAL_USAGE_CELL_SIZE}px)`,
                    gap: `${OFFICIAL_USAGE_CELL_GAP}px`,
                    height: usageGridHeight(),
                    width: usageGridWidth(chart.columns.length),
                  }}
                >
                  {chart.cells.map((cell, index) => {
                    if (!cell) {
                      return <span key={`empty-${index}`} className="h-full w-full" />;
                    }
                    const highlighted =
                      cell.mode === "week"
                        ? cell.columnKey === (hoveredUsageColumnKey ?? selectedUsageColumnKey)
                        : cell.selectionKey === highlightedUsageCellKey;
                    return (
                      <button
                        key={cell.key}
                        type="button"
                        className={cx(
                          "focus-ring h-full w-full rounded-[3px] border-0 p-0 hover:brightness-[0.97]",
                          highlighted && "ring-1 ring-action/20 brightness-[0.96]",
                        )}
                        style={{ backgroundColor: usageCellColor(cell.intensity, cell.filled) }}
                        aria-label={formatUsageCellLabel(cell, locale, t as Translate)}
                        onPointerEnter={(event) => activateUsageCell(event, cell)}
                        onPointerMove={(event) => activateUsageCell(event, cell)}
                        onFocus={(event) => focusUsageCell(event, cell)}
                        onBlur={() => setHoveredUsageCell(null)}
                        onClick={() => setSelectedUsageCellKey(cell.selectionKey)}
                      />
                    );
                  })}
                </div>
                <div
                  className="relative mt-1 h-4 text-[10px] text-slate-400"
                  style={{ width: usageGridWidth(chart.columns.length) }}
                >
                  {usageMonthLabels(chart.columns, locale, usageGridWidth(chart.columns.length)).map((label) => (
                    <span
                      key={label.key}
                      data-openai-usage-month-label
                      className={cx(
                        "absolute top-0 truncate",
                        label.align === "start" && "translate-x-0",
                        label.align === "center" && "-translate-x-1/2",
                        label.align === "end" && "-translate-x-full",
                      )}
                      style={{ left: `${label.leftPercent}%` }}
                    >
                      {label.label}
                    </span>
                  ))}
                </div>
                <OfficialOpenAIUsageTooltip tooltip={hoveredUsageCell} locale={locale} t={t as Translate} />
              </div>
            ) : (
              <div className="grid min-h-[82px] place-items-center text-xs font-medium text-slate-500">
                {busy ? t("providers.loadingOpenAIUsage") : t("providers.openaiUsageNoData")}
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}

function OfficialOpenAIUsageSkeleton({ label }: { label: string }) {
  const columns = 42;
  const cells = Array.from({ length: columns * 7 }, (_, index) => index);

  return (
    <div className="grid gap-3 animate-pulse" role="status" aria-label={label}>
      <div className="grid grid-cols-5 gap-2 text-xs" aria-hidden="true">
        {Array.from({ length: 5 }, (_, index) => (
          <div
            key={`metric-${index}`}
            className="grid min-w-0 place-items-center rounded-inner bg-surface px-2 py-1.5 shadow-control"
          >
            <span className="h-2 w-10 rounded-full bg-slate-200" />
            <span className={cx("mt-2 h-3 rounded-full bg-slate-200", index === 0 ? "w-12" : "w-9")} />
          </div>
        ))}
      </div>
      <div className="min-w-0 overflow-hidden rounded-inner bg-surface px-3 py-2 shadow-control" aria-hidden="true">
        <div
          className="grid"
          style={{
            gridAutoFlow: "column",
            gridTemplateColumns: `repeat(${columns}, ${OFFICIAL_USAGE_CELL_SIZE}px)`,
            gridTemplateRows: `repeat(7, ${OFFICIAL_USAGE_CELL_SIZE}px)`,
            gap: `${OFFICIAL_USAGE_CELL_GAP}px`,
            height: usageGridHeight(),
            width: usageGridWidth(columns),
          }}
        >
          {cells.map((index) => (
            <span
              key={`cell-${index}`}
              className={cx(
                "h-full w-full rounded-[3px] bg-slate-200",
                index % 11 === 0 && "bg-slate-300/80",
                index % 17 === 0 && "bg-slate-300",
              )}
            />
          ))}
        </div>
        <div className="mt-2 flex gap-5">
          {Array.from({ length: 6 }, (_, index) => (
            <span key={`month-${index}`} className="h-2 w-7 rounded-full bg-slate-200" />
          ))}
        </div>
      </div>
    </div>
  );
}

function OfficialOpenAIUsageTooltip({
  locale,
  t,
  tooltip,
}: {
  locale: string;
  t: Translate;
  tooltip: OfficialOpenAIUsageTooltipState | null;
}) {
  if (!tooltip) {
    return null;
  }
  const { cell } = tooltip;
  const isWeek = cell.mode === "week";
  const tooltipWidth = Math.min(184, Math.max(148, tooltip.hostWidth - 16));
  const left = Math.min(
    Math.max(tooltipWidth / 2 + 8, tooltip.cursorX),
    Math.max(tooltipWidth / 2 + 8, tooltip.hostWidth - tooltipWidth / 2 - 8),
  );
  const top = isWeek ? -8 : tooltip.cursorY - 8;
  const title = isWeek
    ? formatUsageDateRange(cell.column.startTime, cell.column.endTime, locale)
    : formatUsageDate(cell.day?.date ?? cell.column.date, locale);
  const tokens = isWeek ? cell.column.totalTokens : cell.value;

  return (
    <div
      className="pointer-events-none absolute z-20 rounded-inner bg-surface px-2.5 py-1.5 text-center text-xs font-medium text-ink shadow-floating"
      style={{ left, top, width: tooltipWidth, transform: "translate(-50%, -100%)" }}
    >
      <span className="block whitespace-nowrap">
        {t("providers.openaiUsageTooltipCompact", { date: title, tokens: formatUsageNumber(tokens, locale) })}
      </span>
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
      let syncError: string | null = null;
      if (syncBoundClients) {
        updateToast(toastId, {
          action: null,
          text: t("providers.syncBoundClients"),
          tone: "loading",
        });
        try {
          syncResult = await api.syncGatewayClients();
        } catch (err) {
          syncError = messageFromError(err);
        }
        await onRefreshClients?.().catch(() => undefined);
      }
      const restartMessage = enabled
        ? t("providers.contextGuardEnabledRestartCodex")
        : t("providers.contextGuardDisabledRestartCodex");
      const syncedClientNames = syncResult?.results
        .filter((result) => result.applied)
        .map((result) => result.name)
        .join(", ");
      const failedClientNames = syncResult?.results
        .filter((result) => result.status === "failed")
        .map((result) => result.name)
        .join(", ");
      const syncedMessage = syncedClientNames
        ? t("providers.contextGuardClientsSyncedRestart", {
            clientNames: syncedClientNames,
            restartMessage,
          })
        : restartMessage;
      const failedMessage = syncError
        ? t("providers.contextGuardClientSyncError", {
            message: syncError,
            restartMessage,
          })
        : failedClientNames && syncedClientNames
          ? t("providers.contextGuardClientsPartiallySyncedRestart", {
              failedClientNames,
              restartMessage,
              syncedClientNames,
            })
          : failedClientNames
          ? t("providers.contextGuardClientsSyncFailed", {
              clientNames: failedClientNames,
              restartMessage,
            })
          : null;
      updateToast(toastId, {
        action: null,
        text: failedMessage ?? syncedMessage,
        tone: failedMessage ? "error" : "success",
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
    setDraft((current) =>
      current.id === provider.id
        ? {
            ...current,
            available_upstream_formats: availableFormats,
          }
        : current,
    );
    setEndpointTestState(availableFormats.length ? "success" : "idle");
  }, [provider.id, provider.available_upstream_formats]);

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
              toolProtocol={draft.tool_protocol}
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
        onCancelNewModel={(modelId) =>
          setDraft((current) => ({
            ...current,
            models: renumberModels(current.models.filter((model) => model.id !== modelId)),
          }))
        }
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
  headerControl,
  interactionDisabled = false,
  models,
  modelTestDisabled,
  onAdd,
  onCancelNewModel,
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
  headerControl?: React.ReactNode;
  interactionDisabled?: boolean;
  models: Model[];
  modelTestDisabled?: boolean;
  onAdd?: () => string | undefined;
  onCancelNewModel?: (modelId: string) => void;
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
  const [pendingNewModelId, setPendingNewModelId] = useState<string | null>(null);
  const [modelTestStates, setModelTestStates] = useState<Record<string, InlineTestState>>({});
  const [testingModelId, setTestingModelId] = useState<string | null>(null);
  const editingModel = editingModelId ? models.find((model) => model.id === editingModelId) ?? null : null;
  const editingModelIsNew = pendingNewModelId !== null && pendingNewModelId === editingModelId;
  const [modelListRef, modelListHasOverflow] = useVerticalOverflow<HTMLDivElement>([
    disabled,
    editingModelId,
    models.length,
    providerId,
    reorderable,
    interactionDisabled,
  ]);

  function addAndEdit() {
    const modelId = onAdd?.();
    if (modelId) {
      setPendingNewModelId(modelId);
      setEditingModelId(modelId);
    }
  }

  function applyModelUpdate(modelId: string, nextModel: Model) {
    onUpdate?.(modelId, nextModel);
    if (pendingNewModelId === modelId) {
      setPendingNewModelId(null);
    }
    setEditingModelId(null);
  }

  function closeModelEditor() {
    if (editingModelIsNew && pendingNewModelId) {
      onCancelNewModel?.(pendingNewModelId);
      setPendingNewModelId(null);
    }
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
    const rowInteractable = !interactionDisabled && !disabled;
    function activateModelRow() {
      if (interactionDisabled) {
        return;
      }
      setEditingModelId(model.id);
    }
    const actions = (
      <div className="flex shrink-0 flex-nowrap items-center justify-end gap-2 whitespace-nowrap text-xs text-slate-500">
        {modelCapabilityTags(model).map((tag) => (
          <ModelCapabilityChip key={tag} tag={tag} />
        ))}
        <CapabilityChip
          label={formatContextWindow(contextWindow)}
          title={modelLimitDetails(model, contextWindow)}
        />
        {disabled && onToggleOfficialModel && (
          <SwitchControl
            checked={modelEnabled}
            label={modelEnabled ? t("providers.modelEnabled") : t("providers.modelDisabled")}
            disabled={interactionDisabled}
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
          actionsDisabled={interactionDisabled}
          providerId={providerId}
          testDisabled={interactionDisabled || modelTestDisabled || Boolean(testingModelId)}
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
    <div
      className={cx(
        "grid min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-3 p-5",
        interactionDisabled && "text-slate-400",
      )}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">{t("common.models")}</h3>
          <p className="mt-1 text-xs text-slate-500">{t("providers.configured", { count: models.length })}</p>
          <p className="mt-1 truncate whitespace-nowrap text-xs leading-4 text-slate-500">
            {t("providers.appsMaySortModels")}
          </p>
        </div>
        <div className="flex shrink-0 items-center justify-end gap-2 whitespace-nowrap">
          {headerControl}
          {discoverError && (
            <span className="max-w-[260px] truncate text-xs font-medium text-danger" title={discoverError}>
              {discoverError}
            </span>
          )}
          {onRefresh && (
            <button
              type="button"
              className={cx(
                "focus-ring inline-flex shrink-0 items-center justify-center gap-2 border border-line bg-panel px-3 font-semibold hover:bg-slate-100 disabled:bg-slate-100",
                headerControl ? "h-7 rounded-full text-xs" : "h-9 rounded-md text-sm",
              )}
              disabled={interactionDisabled || refreshBusy}
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
        className={cx(
          "min-h-0 overflow-auto",
          interactionDisabled && "opacity-60 grayscale",
          modelListHasOverflow && "-mr-5 pr-1",
        )}
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
          onClose={closeModelEditor}
          onRemove={onRemove ? () => {
            if (pendingNewModelId === editingModel.id) {
              setPendingNewModelId(null);
            }
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
  actionsDisabled = false,
  model,
  onTest,
  providerId,
  testDisabled,
  testState = "idle",
}: {
  actionsDisabled?: boolean;
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
          disabled={actionsDisabled}
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

function CapabilityChip({ icon, label, title }: { icon?: React.ReactNode; label: string; title?: string }) {
  return (
    <span title={title} className="inline-flex h-6 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border border-line bg-panel px-2 text-xs font-semibold text-slate-600">
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
  ariaDescribedBy,
  checked,
  className,
  disabled = false,
  label,
  onChange,
  showLabel = true,
}: {
  ariaDescribedBy?: string;
  checked: boolean;
  className?: string;
  disabled?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
  showLabel?: boolean;
}) {
  return (
    <label
      className={cx(
        "inline-flex h-6 shrink-0 items-center gap-2 whitespace-nowrap text-xs font-semibold text-slate-600",
        showLabel && "rounded-full border border-line bg-panel pl-2 pr-1",
        className,
      )}
      aria-describedby={ariaDescribedBy}
    >
      <span className={showLabel ? "truncate" : "sr-only"}>{label}</span>
      <span className="relative inline-flex h-5 w-9 shrink-0 items-center">
        <input
          type="checkbox"
          className="peer sr-only"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span
          className={cx(
            "absolute inset-0 rounded-full border transition-colors",
            disabled
              ? "border-slate-200 bg-slate-200"
              : "border-line bg-slate-200 peer-checked:border-action peer-checked:bg-action",
          )}
        />
        <span
          className={cx(
            "absolute left-0.5 h-4 w-4 rounded-full shadow-sm transition-transform",
            checked && "translate-x-4",
            disabled ? "bg-slate-100" : "bg-white",
          )}
        />
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
  return normalizeOfficialModelId(left) === normalizeOfficialModelId(right);
}

function withDefaultFastVariants(settings: Settings): Settings {
  return normalizeSettings(settings);
}

function formatContextWindow(value?: number | null) {
  if (!value) {
    return i18n.t("providers.contextDynamic");
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

function modelLimitDetails(model: Model, effective?: number | null) {
  const effectiveLabel = formatContextWindow(effective);
  const maxLabel = model.max_context_window
    ? formatContextWindow(model.max_context_window)
    : i18n.t("common.unknown");
  const verified = model.verified_at ?? i18n.t("common.unknown");
  return i18n.t("providers.contextDetails", {
    effective: effectiveLabel,
    max: maxLabel,
    source: model.effective_source ?? model.max_source ?? i18n.t("common.unknown"),
    verified,
  });
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
    tool_protocol: provider.tool_protocol ?? "auto",
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

function probeDetectedEndpointFormat(result: UpstreamFormatProbeResult): UpstreamFormat | null {
  return normalizedProbeEndpointFormat(result.recommended_format) ?? probeAvailableFormats(result)[0] ?? null;
}

function normalizedProbeEndpointFormat(value?: string | null): UpstreamFormat | null {
  const normalized = value?.trim().toLowerCase().replace(/[-\s]+/g, "_");
  if (!normalized || normalized === "auto") {
    return null;
  }
  if (normalized === "responses" || normalized === "response") {
    return "responses";
  }
  if (normalized === "chat_completions" || normalized === "chat_completion" || normalized === "chat") {
    return "chat_completions";
  }
  if (normalized === "anthropic_messages" || normalized === "anthropic_message" || normalized === "anthropic") {
    return "anthropic_messages";
  }
  return null;
}

function applyProviderProbeResult(provider: Provider, result: UpstreamFormatProbeResult): Provider {
  const detectedFormat = probeDetectedEndpointFormat(result);
  return {
    ...provider,
    upstream_format: detectedFormat ?? provider.upstream_format,
    available_upstream_formats: probeAvailableFormats(result),
    tool_protocol: result.recommended_tool_protocol,
  };
}

function applyProviderProbeAvailability(provider: Provider, result: UpstreamFormatProbeResult): Provider {
  return {
    ...provider,
    available_upstream_formats: probeAvailableFormats(result),
    tool_protocol: result.recommended_tool_protocol,
  };
}

function applyAddProviderProbeResult(form: AddProviderForm, result: UpstreamFormatProbeResult): AddProviderForm {
  const detectedFormat = probeDetectedEndpointFormat(result);
  return {
    ...form,
    upstream_format: detectedFormat ?? form.upstream_format,
    available_upstream_formats: probeAvailableFormats(result),
    tool_protocol: result.recommended_tool_protocol,
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

function preferredOpenAIUsageLimits(limits: OpenAIUsageLimit[]) {
  const usable = limits.filter(limitHasUsageData);
  const fiveHour = usable.find(isFiveHourUsageLimit);
  const weekly = usable.find(isWeeklyUsageLimit);
  const selected = [fiveHour, weekly].filter((limit): limit is OpenAIUsageLimit => Boolean(limit));
  for (const limit of usable) {
    if (selected.length >= 2) {
      break;
    }
    if (!selected.some((item) => item.key === limit.key)) {
      selected.push(limit);
    }
  }
  return selected.slice(0, 2);
}

function limitHasUsageData(limit: OpenAIUsageLimit) {
  return (
    finiteUsageNumber(limit.limit) !== null ||
    finiteUsageNumber(limit.used) !== null ||
    finiteUsageNumber(limit.remaining) !== null ||
    Boolean(limit.resets_at?.trim())
  );
}

function isFiveHourUsageLimit(limit: OpenAIUsageLimit) {
  const value = usageLimitSearchText(limit);
  return (
    /\b5\s*h(?:our)?s?\b/.test(value) ||
    /\bfive[-_\s]?h(?:our)?s?\b/.test(value) ||
    ((value.includes("5") || value.includes("five")) && value.includes("hour")) ||
    /\bprimary\b/.test(value)
  );
}

function isWeeklyUsageLimit(limit: OpenAIUsageLimit) {
  const value = usageLimitSearchText(limit);
  return value.includes("week") || value.includes("weekly") || /\bsecondary\b/.test(value);
}

function usageLimitSearchText(limit: OpenAIUsageLimit) {
  return `${limit.key} ${limit.period} ${limit.name}`.trim().toLowerCase().replace(/_/g, " ");
}

function usageLimitPeriodLabel(limit: OpenAIUsageLimit, t: Translate) {
  if (isFiveHourUsageLimit(limit)) {
    return t("providers.fiveHourLimit");
  }
  if (isWeeklyUsageLimit(limit)) {
    return t("providers.weeklyLimit");
  }
  return limit.name?.trim() || limit.period?.trim() || limit.key;
}

function remainingPercent(limit: OpenAIUsageLimit) {
  const total = finiteUsageNumber(limit.limit);
  const used = finiteUsageNumber(limit.used);
  const explicitRemaining = finiteUsageNumber(limit.remaining);
  const remaining =
    explicitRemaining !== null
      ? explicitRemaining
      : total !== null && used !== null
        ? total - used
        : null;
  if (total === null || total <= 0 || remaining === null) {
    return 0;
  }
  return Math.max(0, Math.min(100, (remaining / total) * 100));
}

function finiteUsageNumber(value?: number | null) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatUsageLimitEnd(value: string | null | undefined, locale: string, t: Translate) {
  const date = parseUsageLimitEnd(value);
  if (!date) {
    return value?.trim() || t("providers.limitEndUnknown");
  }
  return new Intl.DateTimeFormat(resolvedUsageLocale(locale), {
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    month: "short",
  }).format(date);
}

function parseUsageLimitEnd(value: string | null | undefined) {
  const trimmed = value?.trim();
  if (!trimmed) {
    return null;
  }
  const numeric = Number(trimmed);
  if (Number.isFinite(numeric) && /^\d+(?:\.\d+)?$/.test(trimmed)) {
    return new Date(numeric < 10_000_000_000 ? numeric * 1000 : numeric);
  }
  const parsed = Date.parse(trimmed);
  return Number.isNaN(parsed) ? null : new Date(parsed);
}

function defaultOfficialOpenAIUsageWindow() {
  const endTime = Math.floor(Date.now() / 1000);
  return {
    startTime: endTime - (OPENAI_USAGE_QUERY_WINDOW_DAYS - 1) * OPENAI_USAGE_DAY_SECONDS,
    endTime,
  };
}

function buildOfficialOpenAIUsageDays(
  snapshot: OpenAIUsageSnapshot | null,
  visibleColumnCount: number,
): OfficialOpenAIUsageDay[] {
  if (!snapshot || snapshot.start_time >= snapshot.end_time) {
    return [];
  }
  const endDay = localDayStartSeconds(Math.max(snapshot.start_time, snapshot.end_time - 1));
  const displayWindowDays = Math.max(
    OPENAI_USAGE_MIN_WINDOW_DAYS,
    visibleColumnCount * 7,
  );
  const startDay = addLocalDays(endDay, -(displayWindowDays - 1));
  const totals = new Map<string, Omit<OfficialOpenAIUsageDay, "date" | "dateKey" | "startTime">>();
  for (const bucket of snapshot.buckets) {
    const bucketDate = bucket.date ? parseLocalUsageDate(bucket.date) : new Date(bucket.start_time * 1000);
    if (!bucketDate) {
      continue;
    }
    const day = localDayStartSeconds(bucketDate);
    if (day < startDay || day > endDay) {
      continue;
    }
    const dateKey = localDateKey(bucketDate);
    const current = totals.get(dateKey) ?? {
      inputTokens: 0,
      outputTokens: 0,
      requests: 0,
      totalTokens: 0,
    };
    totals.set(dateKey, {
      inputTokens: current.inputTokens + bucket.input_tokens,
      outputTokens: current.outputTokens + bucket.output_tokens,
      requests: current.requests + bucket.num_model_requests,
      totalTokens: current.totalTokens + bucket.total_tokens,
    });
  }

  const days: OfficialOpenAIUsageDay[] = [];
  for (let time = startDay; time <= endDay; time = addLocalDays(time, 1)) {
    const date = new Date(time * 1000);
    const dateKey = localDateKey(date);
    const total = totals.get(dateKey) ?? {
      inputTokens: 0,
      outputTokens: 0,
      requests: 0,
      totalTokens: 0,
    };
    days.push({
      date,
      dateKey,
      startTime: time,
      ...total,
    });
  }
  return days;
}

function buildOfficialOpenAIUsageChart(
  days: OfficialOpenAIUsageDay[],
  mode: OpenAIUsageMode,
  visibleColumnCount: number,
) {
  const allColumns = buildOfficialOpenAIUsageWeekColumns(days);
  const columns = visibleUsageColumns(allColumns, visibleColumnCount);
  if (mode === "week") {
    const maxWeekTotal = Math.max(1, ...allColumns.map((column) => column.totalTokens));
    const cells = columns.flatMap((column) => {
      const intensity = column.totalTokens > 0 ? Math.max(0.18, Math.min(1, column.totalTokens / maxWeekTotal)) : 0;
      const filledRows = column.totalTokens > 0 ? Math.max(1, Math.ceil(intensity * 7)) : 0;
      return column.days.map((day, rowIndex): OfficialOpenAIUsageChartCell => {
        const filled = filledRows > 0 && rowIndex >= 7 - filledRows;
        return {
          column,
          columnKey: column.key,
          day,
          filled,
          intensity: filled ? intensity : 0,
          key: `${column.key}-row-${rowIndex}`,
          mode,
          rowIndex,
          selectionKey: column.key,
          value: column.totalTokens,
        };
      });
    });
    return { cells, columns };
  }

  const maxDayTotal = Math.max(1, ...days.map((day) => day.totalTokens));
  const cells = columns.flatMap((column) =>
    column.days.map((day, rowIndex): OfficialOpenAIUsageChartCell | null => {
      if (!day) {
        return null;
      }
      const intensity = day.totalTokens > 0 ? Math.max(0.18, Math.min(1, day.totalTokens / maxDayTotal)) : 0;
      return {
        column,
        columnKey: column.key,
        day,
        filled: day.totalTokens > 0,
        intensity,
        key: `day-${day.startTime}`,
        mode,
        rowIndex,
        selectionKey: `day-${day.startTime}`,
        value: day.totalTokens,
      };
    }),
  );
  return { cells, columns };
}

function responsiveUsageColumnCount(contentWidth: number) {
  const minimumColumns = Math.ceil(OPENAI_USAGE_MIN_WINDOW_DAYS / 7);
  if (contentWidth <= 0) {
    return minimumColumns;
  }
  return Math.max(
    1,
    Math.floor((contentWidth + OFFICIAL_USAGE_CELL_GAP) / (OFFICIAL_USAGE_CELL_SIZE + OFFICIAL_USAGE_CELL_GAP)),
  );
}

function visibleUsageColumns(columns: OfficialOpenAIUsageChartColumn[], visibleColumnCount: number) {
  const start = Math.max(0, columns.length - visibleColumnCount);
  return columns.slice(start).map((column, index) => ({ ...column, index }));
}

function usageGridWidth(columnCount: number) {
  if (columnCount <= 0) {
    return 0;
  }
  return columnCount * OFFICIAL_USAGE_CELL_SIZE + (columnCount - 1) * OFFICIAL_USAGE_CELL_GAP;
}

function usageGridHeight() {
  return 7 * OFFICIAL_USAGE_CELL_SIZE + 6 * OFFICIAL_USAGE_CELL_GAP;
}

function buildOfficialOpenAIUsageWeekColumns(days: OfficialOpenAIUsageDay[]): OfficialOpenAIUsageChartColumn[] {
  if (!days.length) {
    return [];
  }
  const leadingBlanks = mondayWeekdayIndex(days[0].date);
  const rawSlots: Array<OfficialOpenAIUsageDay | null> = [
    ...Array.from({ length: leadingBlanks }, () => null),
    ...days,
  ];
  const trailingBlanks = (7 - (rawSlots.length % 7)) % 7;
  const slots = [...rawSlots, ...Array.from({ length: trailingBlanks }, () => null)];
  const columns: OfficialOpenAIUsageChartColumn[] = [];
  for (let index = 0; index < slots.length; index += 7) {
    const weekSlots = slots.slice(index, index + 7);
    const actualDays = weekSlots.filter((day): day is OfficialOpenAIUsageDay => Boolean(day));
    const firstDay = actualDays[0] ?? days[0];
    const weekStart = addLocalDays(firstDay.startTime, -mondayWeekdayIndex(firstDay.date));
    const totals = actualDays.reduce(
      (sum, day) => ({
        inputTokens: sum.inputTokens + day.inputTokens,
        outputTokens: sum.outputTokens + day.outputTokens,
        requests: sum.requests + day.requests,
        totalTokens: sum.totalTokens + day.totalTokens,
      }),
      { inputTokens: 0, outputTokens: 0, requests: 0, totalTokens: 0 },
    );
    columns.push({
      date: new Date(weekStart * 1000),
      days: weekSlots,
      endTime: addLocalDays(weekStart, 6),
      index: columns.length,
      key: `week-${weekStart}`,
      startTime: weekStart,
      ...totals,
    });
  }
  return columns;
}

function usageStreaks(days: OfficialOpenAIUsageDay[]) {
  let currentRun = 0;
  let longest = 0;
  let run = 0;
  for (const day of days) {
    if (day.totalTokens > 0) {
      run += 1;
      longest = Math.max(longest, run);
    } else {
      run = 0;
    }
  }
  for (let index = days.length - 1; index >= 0; index -= 1) {
    if (days[index].totalTokens <= 0) {
      break;
    }
    currentRun += 1;
  }
  return { current: currentRun, longest };
}

function usageMonthLabels(columns: OfficialOpenAIUsageChartColumn[], locale: string, gridWidth: number) {
  const timeZone = localUsageTimeZone();
  const formatter = new Intl.DateTimeFormat(resolvedUsageLocale(locale), {
    month: "short",
    ...(timeZone ? { timeZone } : {}),
  });
  const labels: Array<{ align: "start" | "center" | "end"; key: string; label: string; leftPercent: number }> = [];
  let previous = "";
  for (const column of columns) {
    for (const day of column.days) {
      if (!day) {
        continue;
      }
      const key = `${day.date.getFullYear()}-${day.date.getMonth()}`;
      if (key === previous) {
        continue;
      }
      previous = key;
      const leftPercent = columns.length <= 1 ? 0 : (column.index / (columns.length - 1)) * 100;
      labels.push({
        align: leftPercent <= 3 ? "start" : leftPercent >= 97 ? "end" : "center",
        key,
        label: formatter.format(day.date),
        leftPercent,
      });
    }
  }
  return filterCrowdedUsageMonthLabels(labels, gridWidth);
}

function filterCrowdedUsageMonthLabels<
  TLabel extends { leftPercent: number },
>(labels: TLabel[], gridWidth: number) {
  if (labels.length <= 1 || gridWidth <= 0) {
    return labels;
  }

  const filtered: TLabel[] = [];
  for (let index = 0; index < labels.length; index += 1) {
    const label = labels[index];
    const currentLeftPx = (label.leftPercent / 100) * gridWidth;
    const next = labels[index + 1];
    if (next) {
      const nextLeftPx = (next.leftPercent / 100) * gridWidth;
      if (nextLeftPx - currentLeftPx < USAGE_MONTH_LABEL_MIN_GAP_PX) {
        continue;
      }
    }
    const previous = filtered[filtered.length - 1];
    if (previous) {
      const previousLeftPx = (previous.leftPercent / 100) * gridWidth;
      if (currentLeftPx - previousLeftPx < USAGE_MONTH_LABEL_MIN_GAP_PX) {
        continue;
      }
    }
    filtered.push(label);
  }
  return filtered;
}

function usageCellColor(intensity: number, filled = true) {
  if (!filled || intensity <= 0) {
    return OFFICIAL_USAGE_COLOR_STOPS[0];
  }
  const index = Math.min(
    OFFICIAL_USAGE_COLOR_STOPS.length - 1,
    Math.max(1, Math.ceil(Math.min(1, intensity) * (OFFICIAL_USAGE_COLOR_STOPS.length - 1))),
  );
  return OFFICIAL_USAGE_COLOR_STOPS[index];
}

function resolvedUsageLocale(locale: string) {
  const normalized = locale.replace(/_/g, "-").toLowerCase();
  return normalized === "zh" || normalized.startsWith("zh-") ? "zh-CN" : "en-US";
}

function formatUsageCellLabel(cell: OfficialOpenAIUsageChartCell, locale: string, t: Translate) {
  if (cell.mode === "week") {
    return `${formatUsageDateRange(cell.column.startTime, cell.column.endTime, locale)}: ${formatUsageNumber(cell.column.totalTokens, locale)} ${t("gateway.tokens")}`;
  }
  const date = cell.day?.date ?? cell.column.date;
  return `${formatUsageDate(date, locale)}: ${formatUsageNumber(cell.value, locale)} ${t("gateway.tokens")}`;
}

function formatUsageDateRange(startTime: number, endTime: number, locale: string) {
  return `${formatUsageDate(new Date(startTime * 1000), locale)} - ${formatUsageDate(new Date(endTime * 1000), locale)}`;
}

function formatUsageNumber(value: number, locale: string) {
  return new Intl.NumberFormat(locale, {
    maximumFractionDigits: value >= 10_000 ? 1 : 0,
    notation: value >= 10_000 ? "compact" : "standard",
  }).format(value);
}

function formatUsageDuration(seconds: number | null | undefined, locale: string, t: Translate) {
  if (seconds == null) {
    return t("common.unknown");
  }
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const formatter = new Intl.NumberFormat(locale, { maximumFractionDigits: 0 });
  if (days > 0) {
    return `${formatter.format(days)} ${t("providers.daysShort")} ${formatter.format(hours)} ${t("providers.hoursShort")}`;
  }
  if (hours > 0) {
    return `${formatter.format(hours)} ${t("providers.hoursShort")} ${formatter.format(minutes)} ${t("providers.minutesShort")}`;
  }
  return `${formatter.format(Math.max(1, minutes))} ${t("providers.minutesShort")}`;
}

function formatUsageDate(date: Date, locale: string) {
  const timeZone = localUsageTimeZone();
  return new Intl.DateTimeFormat(resolvedUsageLocale(locale), {
    day: "numeric",
    month: "short",
    ...(timeZone ? { timeZone } : {}),
  }).format(date);
}

function localDateKey(date: Date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function localUsageTimeZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
  } catch {
    return undefined;
  }
}

function parseLocalUsageDate(value: string) {
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  return new Date(year, month - 1, day);
}

function localDayStartSeconds(value: number | Date) {
  const date = value instanceof Date ? value : new Date(value * 1000);
  return Math.floor(new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime() / 1000);
}

function addLocalDays(timestamp: number, days: number) {
  const date = new Date(timestamp * 1000);
  date.setDate(date.getDate() + days);
  return Math.floor(date.getTime() / 1000);
}

function mondayWeekdayIndex(date: Date) {
  return (date.getDay() + 6) % 7;
}

function sortOfficialModels(models: Model[], sortOrder: string[]) {
  const order = new Map<string, number>();
  const effectiveOrder = shouldFollowOfficialCatalogOrder(sortOrder)
    ? DEFAULT_OFFICIAL_MODEL_ORDER
    : sortOrder;
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

function shouldFollowOfficialCatalogOrder(currentOrder: string[]) {
  if (!currentOrder.length) {
    return true;
  }
  const normalized = currentOrder
    .map((id) => normalizeOfficialModelId(id))
    .filter((id): id is string => Boolean(id));
  let legacyIndex = 0;
  let sawNewModel = false;
  for (const id of normalized) {
    const index = LEGACY_AUTOMATIC_OFFICIAL_MODEL_ORDER.indexOf(id);
    if (index < 0) {
      sawNewModel = true;
      continue;
    }
    if (sawNewModel || index !== legacyIndex) {
      return false;
    }
    legacyIndex += 1;
  }
  return legacyIndex > 0;
}

function refreshedOfficialModelOrder(currentOrder: string[], refreshedModels: Model[]) {
  const refreshedKeySets = refreshedModels.map((model) => new Set(officialModelSortKeys(model.id)));
  const nextOrder = currentOrder.filter((id) => {
    const keys = officialModelSortKeys(id);
    return refreshedKeySets.some((refreshedKeys) => keys.some((key) => refreshedKeys.has(key)));
  });
  const seen = new Set(nextOrder.flatMap(officialModelSortKeys));
  for (const model of refreshedModels) {
    const keys = officialModelSortKeys(model.id);
    if (keys.some((key) => seen.has(key))) {
      continue;
    }
    nextOrder.push(model.id);
    keys.forEach((key) => seen.add(key));
  }
  return nextOrder;
}

function mergeOfficialModelSources(catalog: Model[], metadata: Model[]) {
  const knownOfficialIds = officialModelIdSet(catalog, metadata);
  const merged = new Map<string, Model>();
  for (const model of catalog.filter(isOfficialModel)) {
    const canonicalId = normalizeOfficialModelId(model.id, knownOfficialIds);
    if (!canonicalId) {
      continue;
    }
    const existing = merged.get(canonicalId);
    merged.set(canonicalId, {
      ...existing,
      ...model,
      id: canonicalId,
      enabled: existing
        ? (existing.enabled ?? true) || (model.enabled ?? true)
        : model.enabled ?? true,
    });
  }
  for (const model of metadata.filter(isOfficialModel)) {
    const canonicalId = normalizeOfficialModelId(model.id, knownOfficialIds);
    if (!canonicalId) {
      continue;
    }
    const existing = merged.get(canonicalId);
    merged.set(canonicalId, {
      ...existing,
      ...model,
      id: canonicalId,
      enabled: existing
        ? (existing.enabled ?? true) || (model.enabled ?? true)
        : model.enabled ?? true,
    });
  }
  return filterCodexVisibleOfficialModels(Array.from(merged.values()));
}

function officialModelIdSet(...groups: Model[][]) {
  const known = new Set<string>();
  for (const model of groups.flatMap((group) => group).filter(isOfficialModel)) {
    const value = model.id.trim();
    const bare = value.startsWith("openai/gpt-") ? value.slice("openai/".length) : value;
    if (bare.startsWith("gpt-")) {
      known.add(bare);
    }
  }
  return known;
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
  const normalized = normalizeOfficialModelId(id);
  return normalized ? [normalized, `openai/${normalized}`] : [id.trim()];
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
              toolProtocol={form.tool_protocol}
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
        onCancelNewModel={(modelId) =>
          onFormChange({
            ...form,
            models: renumberModels(form.models.filter((model) => model.id !== modelId)),
          })
        }
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
  toolProtocol,
  value,
}: {
  availableFormats?: UpstreamFormat[] | null;
  onChange: (value: UpstreamFormat) => void;
  onProbe: () => void;
  probeDisabled: boolean;
  result?: UpstreamFormatProbeResult | null;
  testState: InlineTestState;
  toolProtocol?: ToolProtocol | null;
  value?: UpstreamFormat | null;
}) {
  const { t } = useTranslation();
  const selected = normalizedEndpointFormat(value);
  const mergedAvailableFormats = mergeEndpointFormats(availableFormats, probeAvailableFormats(result));

  return (
    <div className="grid min-w-0 gap-1 text-sm font-medium text-slate-700">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span>{t("common.endpointSelection")}</span>
        <span className="truncate text-xs font-medium text-slate-500">{toolProtocolLabel(toolProtocol)}</span>
      </div>
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

function toolProtocolLabel(value?: ToolProtocol | null) {
  if (value === "responses_structured") {
    return "Structured Responses tools";
  }
  if (value === "chat_tools") {
    return "Chat tool calls";
  }
  if (value === "text_compat") {
    return "Gateway compatibility";
  }
  if (value === "none") {
    return "Tools unavailable";
  }
  return "Auto tools";
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
  const recommendedFormat = normalizedProbeEndpointFormat(result.recommended_format);
  if (recommendedFormat && !formats.includes(recommendedFormat)) {
    formats.push(recommendedFormat);
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

function HeaderRow({
  actions,
  subtitle,
  title,
  titleAccessory,
}: {
  actions?: React.ReactNode;
  subtitle?: string;
  title: string;
  titleAccessory?: React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3">
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          <h2 className="min-w-0 truncate text-base font-semibold">{title}</h2>
          {titleAccessory && <div className="shrink-0">{titleAccessory}</div>}
        </div>
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
