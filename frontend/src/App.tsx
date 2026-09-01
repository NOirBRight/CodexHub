import { useCallback, useEffect, useMemo, useRef, useState, useTransition } from "react";
import { listen } from "@tauri-apps/api/event";
import { useTranslation } from "react-i18next";
import { useConfirmDialog } from "./components/ConfirmDialog";
import { FitStage } from "./components/FitStage";
import { WindowResizeHandles } from "./components/WindowResizeHandles";
import { RuntimeBar } from "./components/RuntimeBar";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { useToasts } from "./components/PageToast";
import { changeAppLocale } from "./i18n";
import { cx } from "./lib/format";
import { addDays, endOfDay, startOfDay } from "./lib/dateRange";
import { api, messageFromError } from "./lib/tauri";
import { useAppUpdateLifecycle } from "./hooks/useAppUpdateLifecycle";
import contract from "./lib/ui-contract.json";
import {
  createEmptyRuntimeSnapshot,
  setCacheData,
  setCacheError,
  setCacheLoading,
  type RuntimeCache,
  type RuntimeCacheKey,
  type RuntimeData,
  type RuntimeSnapshot,
} from "./lib/runtimeStore";
import type {
  AppStatus,
  GatewayClientContract,
  GatewayClientInfo,
  GatewayStatus,
  Model,
  Provider,
  Settings,
  TabId,
  UsageQueryWindow,
} from "./lib/types";
import { GatewayPage } from "./pages/GatewayPage";
import { ProvidersPage } from "./pages/ProvidersPage";
import type { CodexSwitchRequest } from "./pages/ProvidersPage";

type TrayToastPayload = {
  id: string;
  text: string;
  tone: "loading" | "success" | "error";
};

type LoadRuntimeOptions = {
  force?: boolean;
  includeClientVersions?: boolean;
  staleMs?: number;
};

type RuntimeCacheOptions<T> = {
  apply?: (current: RuntimeSnapshot, data: T) => RuntimeSnapshot;
  force?: boolean;
  quiet?: boolean;
  staleMs?: number;
};

type GatewayClientVersionCacheEntry = {
  checked_at?: string | null;
  current_version?: string | null;
  id: string;
  latest_version?: string | null;
  versions_checked?: boolean | null;
};

const GATEWAY_CLIENT_VERSION_CACHE_KEY = "codexhub.gatewayClientVersions.v1";

function defaultUsageWindow(): UsageQueryWindow {
  const end = startOfDay(new Date());
  return {
    startTs: addDays(end, -6).toISOString(),
    endTs: endOfDay(end).toISOString(),
  };
}


function readGatewayClientVersionCache(): Map<string, GatewayClientVersionCacheEntry> {
  if (typeof window === "undefined") {
    return new Map();
  }
  try {
    const raw = window.localStorage.getItem(GATEWAY_CLIENT_VERSION_CACHE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) {
      return new Map();
    }
    return new Map(
      parsed
        .filter((entry): entry is GatewayClientVersionCacheEntry => (
          Boolean(entry) && typeof entry === "object" && typeof entry.id === "string"
        ))
        .map((entry) => [
          entry.id,
          {
            checked_at: typeof entry.checked_at === "string" ? entry.checked_at : null,
            current_version: typeof entry.current_version === "string" ? entry.current_version : null,
            id: entry.id,
            latest_version: typeof entry.latest_version === "string" ? entry.latest_version : null,
            versions_checked: Boolean(entry.versions_checked),
          },
        ]),
    );
  } catch {
    return new Map();
  }
}

function applyGatewayClientVersionCache(clients: GatewayClientInfo[]): GatewayClientInfo[] {
  const cache = readGatewayClientVersionCache();
  if (!cache.size) {
    return clients;
  }
  return clients.map((client) => {
    const cached = cache.get(client.id);
    if (!client.installed || !cached) {
      return client;
    }
    return {
      ...client,
      versions_checked: Boolean(client.versions_checked ?? cached.versions_checked),
      current_version: client.current_version ?? cached.current_version ?? null,
      latest_version: client.latest_version ?? cached.latest_version ?? null,
    };
  });
}

function writeGatewayClientVersionCache(clients: GatewayClientInfo[]) {
  if (typeof window === "undefined") {
    return;
  }
  const cache = readGatewayClientVersionCache();
  const checkedAt = new Date().toISOString();
  clients.forEach((client) => {
    if (client.id === "generic") {
      cache.delete(client.id);
      return;
    }
    if (!client.installed) {
      cache.delete(client.id);
      return;
    }
    if (!client.versions_checked && !client.current_version && !client.latest_version) {
      return;
    }
    const previous = cache.get(client.id);
    cache.set(client.id, {
      checked_at: client.versions_checked ? checkedAt : previous?.checked_at ?? null,
      current_version: client.current_version ?? null,
      id: client.id,
      latest_version: client.latest_version ?? null,
      versions_checked: Boolean(client.versions_checked ?? previous?.versions_checked),
    });
  });
  try {
    window.localStorage.setItem(
      GATEWAY_CLIENT_VERSION_CACHE_KEY,
      JSON.stringify(Array.from(cache.values())),
    );
  } catch {
    // Version cache is best-effort; UI should still work when storage is blocked.
  }
}

function mergeGatewayClients(
  previous: GatewayClientInfo[],
  next: GatewayClientInfo[],
): GatewayClientInfo[] {
  const previousById = new Map(previous.map((client) => [client.id, client]));
  return next.map((client) => {
    const previousClient = previousById.get(client.id);
    if (!client.installed) {
      return { ...client, versions_checked: false, current_version: null, latest_version: null };
    }
    const versionsChecked = Boolean(client.versions_checked ?? previousClient?.versions_checked);
    return {
      ...client,
      versions_checked: versionsChecked,
      current_version: client.current_version ?? previousClient?.current_version ?? null,
      latest_version: client.latest_version ?? previousClient?.latest_version ?? null,
    };
  });
}

function gatewayRuntimeSettingsChanged(previous: Settings | null, next: Settings) {
  if (!previous) {
    return false;
  }
  return (
    previous.gateway_client_key !== next.gateway_client_key ||
    previous.gateway_auto_retry_enabled !== next.gateway_auto_retry_enabled ||
    previous.gateway_auto_retry_max_attempts !== next.gateway_auto_retry_max_attempts ||
    previous.gateway_image_proxy_enabled !== next.gateway_image_proxy_enabled ||
    previous.gateway_image_proxy_model !== next.gateway_image_proxy_model ||
    previous.openai_context_guard_enabled !== next.openai_context_guard_enabled ||
    previous.proxy_port !== next.proxy_port ||
    previous.gateway_request_timeout_seconds !== next.gateway_request_timeout_seconds
  );
}

function shouldRestartGateway(
  previous: Settings | null,
  next: Settings,
  status: Pick<GatewayStatus, "proxy_running"> | null,
) {
  return Boolean(status?.proxy_running && gatewayRuntimeSettingsChanged(previous, next));
}

function visionModelOptions(models: Model[]) {
  return models
    .filter((model) => model.enabled !== false && model.input_modalities?.includes("image"))
    .sort((left, right) => {
      const leftName = left.display_name?.trim() || left.id;
      const rightName = right.display_name?.trim() || right.id;
      return leftName.localeCompare(rightName);
    });
}

function tabPaneClass(active: boolean) {
  return cx(
    "absolute inset-0 min-h-0 min-w-0 p-4 [contain:layout_paint_style]",
    active
      ? "visible z-10 opacity-100 [content-visibility:visible]"
      : "invisible z-0 opacity-0 pointer-events-none [content-visibility:hidden]",
  );
}

export default function App() {
  const { t } = useTranslation();
  const { confirm: confirmAction, dialog: confirmDialog } = useConfirmDialog();
  const { showToast, updateToast } = useToasts();
  const [activeTab, setActiveTab] = useState<TabId>("codexhub");
  const [visibleTab, setVisibleTab] = useState<TabId>("codexhub");
  const [mountedTabs, setMountedTabs] = useState<Record<TabId, boolean>>({
    codexhub: true,
    gateway: false,
  });
  const [gatewayVisited, setGatewayVisited] = useState(false);
  const [, startUiTransition] = useTransition();
  const [runtime, setRuntime] = useState<RuntimeSnapshot>(createEmptyRuntimeSnapshot);
  const [busy, setBusy] = useState<string | null>("load");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [codexSwitchRequest, setCodexSwitchRequest] = useState<CodexSwitchRequest | null>(null);
  const [usageWindow, setUsageWindow] = useState<UsageQueryWindow>(() => defaultUsageWindow());
  const runtimeInflight = useRef<Partial<Record<RuntimeCacheKey, Promise<unknown>>>>({});
  const runtimeRef = useRef<RuntimeSnapshot | null>(null);
  const trayToastIds = useRef<Map<string, string>>(new Map());
  const nextCodexSwitchRequestId = useRef(1);
  runtimeRef.current = runtime;
  const settingsLoaded = Boolean(runtime.settings.data);

  const appUpdate = useAppUpdateLifecycle({
    confirm: confirmAction,
    getRuntime: () => runtimeRef.current ?? runtime,
    setRuntime: (update) => setRuntime(update),
    translate: t,
  });
  const { view: updateView } = appUpdate;
  const updateBusy = updateView.busy;
  const updateInstallStatus = updateView.installStatus;

  const runCachedRequest = useCallback(async <K extends RuntimeCacheKey>(
    key: K,
    loader: () => Promise<RuntimeData<K>>,
    options?: RuntimeCacheOptions<RuntimeData<K>>,
  ): Promise<RuntimeData<K>> => {
    const existing = runtimeInflight.current[key] as Promise<RuntimeData<K>> | undefined;
    if (existing && !options?.force) {
      return existing;
    }
    const cached = runtimeRef.current?.[key] as RuntimeCache<RuntimeData<K>> | undefined;
    const staleMs = options?.staleMs ?? 0;
    if (
      !options?.force &&
      staleMs > 0 &&
      cached?.data !== null &&
      cached?.data !== undefined &&
      cached.updatedAt !== null &&
      Date.now() - cached.updatedAt < staleMs
    ) {
      return cached.data;
    }

    if (!options?.quiet) {
      startUiTransition(() => {
        setRuntime((current) => setCacheLoading(current, key));
      });
    }

    let request: Promise<RuntimeData<K>>;
    request = loader()
      .then((data) => {
        startUiTransition(() => {
          setRuntime((current) =>
            options?.apply ? options.apply(current, data) : setCacheData(current, key, data),
          );
        });
        return data;
      })
      .catch((err) => {
        const message = messageFromError(err);
        startUiTransition(() => {
          setRuntime((current) => setCacheError(current, key, message));
        });
        if (!options?.quiet) {
          setBanner(message);
        }
        throw err;
      })
      .finally(() => {
        if (runtimeInflight.current[key] === request) {
          delete runtimeInflight.current[key];
        }
      });

    runtimeInflight.current[key] = request;
    return request;
  }, [startUiTransition]);

  const setRuntimeCacheData = useCallback(<K extends RuntimeCacheKey>(key: K, data: RuntimeData<K>) => {
    startUiTransition(() => {
      setRuntime((current) => setCacheData(current, key, data));
    });
  }, [startUiTransition]);

  const refreshStatus = useCallback(
    (options?: { force?: boolean; quiet?: boolean }) =>
      runCachedRequest("status", () => api.getStatus(), options),
    [runCachedRequest],
  );

  const refreshGatewayStatus = useCallback(
    (options?: { force?: boolean; quiet?: boolean }) =>
      runCachedRequest("gatewayStatus", () => api.gatewayStatus(), options),
    [runCachedRequest],
  );

  const refreshSettings = useCallback(
    (options?: { force?: boolean; quiet?: boolean }) =>
      runCachedRequest("settings", () => api.getSettings(), options),
    [runCachedRequest],
  );

  const refreshProviders = useCallback(
    (options?: { force?: boolean; quiet?: boolean }) =>
      runCachedRequest("providers", () => api.getProviders(), options),
    [runCachedRequest],
  );

  const refreshCatalogModels = useCallback(
    (options?: { force?: boolean; quiet?: boolean }) =>
      runCachedRequest("catalogModels", () => api.listModels(), options),
    [runCachedRequest],
  );

  const refreshModelMetadata = useCallback(
    (options?: { force?: boolean; quiet?: boolean }) =>
      runCachedRequest("modelMetadata", () => api.listModelMetadata(), {
        quiet: true,
        ...options,
      }),
    [runCachedRequest],
  );

  const loadAppFlavor = useCallback(async (options?: LoadRuntimeOptions) => {
    await runCachedRequest(
      "appFlavor",
      () => api.getAppFlavor(),
      {
        force: options?.force,
        staleMs: options?.staleMs,
      },
    );
  }, [runCachedRequest]);

  const loadGatewayClients = useCallback(async (options?: LoadRuntimeOptions) => {
    const includeClientVersions = Boolean(options?.includeClientVersions);
    await runCachedRequest(
      "gatewayClients",
      async () => {
        const clients = await api.listGatewayClients(includeClientVersions);
        const cachedClients = applyGatewayClientVersionCache(clients);
        return includeClientVersions
          ? cachedClients.map((client) => ({
              ...client,
              versions_checked: Boolean(client.versions_checked ?? (client.installed && client.id !== "generic")),
            }))
          : cachedClients;
      },
      {
        force: options?.force,
        staleMs: options?.staleMs,
        quiet: true,
        apply: (current, clients) =>
          setCacheData(
            current,
            "gatewayClients",
            mergeGatewayClients(current.gatewayClients.data ?? [], clients),
          ),
      },
    );
  }, [runCachedRequest]);

  const refreshGatewayTelemetry = useCallback(async (options?: { force?: boolean }) => {
    if (!gatewayVisited || visibleTab !== "gateway") {
      return;
    }
    await Promise.allSettled([
      runCachedRequest(
        "gatewayUsageSnapshot",
        () => api.gatewayUsageSnapshot(usageWindow),
        { force: options?.force, quiet: true, staleMs: 4000 },
      ),
      runCachedRequest(
        "gatewayEvents",
        () => api.gatewayRecentEvents(80),
        { force: options?.force, quiet: true, staleMs: 4000 },
      ),
    ]);
  }, [gatewayVisited, runCachedRequest, usageWindow, visibleTab]);

  const refreshCoreRuntime = useCallback(async (options?: { force?: boolean }) => {
    try {
      await Promise.allSettled([
        refreshStatus({ force: options?.force }),
        refreshSettings({ force: options?.force }),
        refreshProviders({ force: options?.force }),
        refreshGatewayStatus({ force: options?.force }),
        refreshCatalogModels({ force: options?.force }),
        refreshModelMetadata({ force: options?.force, quiet: true }),
        loadAppFlavor({ force: options?.force }),
      ]);
    } finally {
      setBusy((current) => (current === "load" ? null : current));
    }
  }, [
    refreshCatalogModels,
    refreshGatewayStatus,
    refreshModelMetadata,
    refreshProviders,
    refreshSettings,
    refreshStatus,
    loadAppFlavor,
  ]);

  const refreshRuntimeStatus = useCallback(async (options?: { force?: boolean }) => {
    await Promise.allSettled([
      refreshStatus({ force: options?.force, quiet: true }),
      refreshGatewayStatus({ force: options?.force, quiet: true }),
    ]);
  }, [refreshGatewayStatus, refreshStatus]);

  const refreshProviderRuntime = useCallback(async () => {
    const [gatewayResult] = await Promise.allSettled([
      refreshGatewayStatus({ force: true, quiet: true }),
      refreshCatalogModels({ force: true, quiet: true }),
      refreshModelMetadata({ force: true, quiet: true }),
      loadGatewayClients({ force: true }),
    ]);
    return gatewayResult.status === "fulfilled" ? gatewayResult.value : null;
  }, [loadGatewayClients, refreshCatalogModels, refreshGatewayStatus, refreshModelMetadata]);

  const loadAppVersion = useCallback(async () => {
    try {
      return await runCachedRequest(
        "appVersion",
        async () => {
          const info = await api.getAppVersion();
          if (!info) {
            throw new Error(t("settings.desktopUpdatesUnavailable"));
          }
          return info;
        },
        { quiet: true },
      );
    } catch {
      return null;
    }
  }, [runCachedRequest, t]);









  const updateUsageWindow = useCallback((nextWindow: UsageQueryWindow) => {
    setUsageWindow((current) => {
      if (current.startTs === nextWindow.startTs && current.endTs === nextWindow.endTs) {
        return current;
      }
      return nextWindow;
    });
  }, []);

  const selectTab = useCallback((tabId: TabId) => {
    setActiveTab(tabId);
    setVisibleTab(tabId);
    setMountedTabs((current) => (current[tabId] ? current : { ...current, [tabId]: true }));
    if (tabId === "gateway") {
      setGatewayVisited(true);
    }
  }, []);

  useEffect(() => {
    void refreshCoreRuntime();
    // Version probes execute installed CLI shims and hit package registries;
    // keep them behind the explicit Gateway refresh action instead of startup.
    void loadGatewayClients();
    const timer = window.setInterval(() => void refreshRuntimeStatus(), 5000);
    const clientTimer = window.setInterval(() => void loadGatewayClients(), 12 * 60 * 60 * 1000);
    return () => {
      window.clearInterval(timer);
      window.clearInterval(clientTimer);
    };
  }, [loadGatewayClients, refreshCoreRuntime, refreshRuntimeStatus]);

  useEffect(() => {
    if (!window.__TAURI_INTERNALS__) {
      return;
    }
    let disposed = false;
    let unlisten: (() => void) | null = null;
    void listen<TrayToastPayload>("codexhub:toast", (event) => {
      const existingToastId = trayToastIds.current.get(event.payload.id);
      if (existingToastId) {
        updateToast(existingToastId, {
          action: null,
          text: event.payload.text,
          tone: event.payload.tone,
        });
        if (event.payload.tone !== "loading") {
          trayToastIds.current.delete(event.payload.id);
        }
        return;
      }
      const toastId = showToast({ text: event.payload.text, tone: event.payload.tone });
      if (event.payload.tone === "loading") {
        trayToastIds.current.set(event.payload.id, toastId);
      }
    })
      .then((nextUnlisten) => {
        if (disposed) {
          nextUnlisten();
        } else {
          unlisten = nextUnlisten;
        }
      })
      .catch(() => {
        // The bridge-only frontend has no native tray event surface.
      });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [showToast, updateToast]);

  useEffect(() => {
    if (!window.__TAURI_INTERNALS__) {
      return;
    }
    let disposed = false;
    let unlisten: (() => void) | null = null;
    void listen<"official" | "custom">("codexhub:request-codex-switch", (event) => {
      if (event.payload !== "official" && event.payload !== "custom") {
        return;
      }
      selectTab("codexhub");
      setCodexSwitchRequest({
        id: nextCodexSwitchRequestId.current++,
        mode: event.payload,
      });
    })
      .then((nextUnlisten) => {
        if (disposed) {
          nextUnlisten();
        } else {
          unlisten = nextUnlisten;
        }
      })
      .catch(() => {
        // The bridge-only frontend has no native tray event surface.
      });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [selectTab]);

  useEffect(() => {
    appUpdate.startScheduling(settingsLoaded);
  }, [appUpdate, settingsLoaded]);





  useEffect(() => {
    if (!settingsOpen || runtime.appVersion.data || runtime.appVersion.loading) {
      return;
    }
    const timer = window.setTimeout(() => void loadAppVersion(), 0);
    return () => window.clearTimeout(timer);
  }, [loadAppVersion, runtime.appVersion.data, runtime.appVersion.loading, settingsOpen]);

  useEffect(() => {
    if (!gatewayVisited || visibleTab !== "gateway") {
      return;
    }
    const refreshTimer = window.setTimeout(() => {
      void refreshGatewayTelemetry();
      void loadGatewayClients({ staleMs: 30_000 });
    }, 150);
    const timer = window.setInterval(() => void refreshGatewayTelemetry(), 5000);
    return () => {
      window.clearTimeout(refreshTimer);
      window.clearInterval(timer);
    };
  }, [gatewayVisited, loadGatewayClients, refreshGatewayTelemetry, visibleTab]);

  useEffect(() => {
    writeGatewayClientVersionCache(runtime.gatewayClients.data ?? []);
  }, [runtime.gatewayClients.data]);

  const appStatus = runtime.status.data;
  const settings = runtime.settings.data;
  const providers = runtime.providers.data ?? [];
  const gatewayStatus = runtime.gatewayStatus.data;
  const gatewayUsageSnapshot = runtime.gatewayUsageSnapshot.data;
  const gatewayEvents = runtime.gatewayEvents.data ?? [];
  const gatewayClients = runtime.gatewayClients.data ?? [];
  const catalogModels = runtime.catalogModels.data ?? [];
  const modelMetadata = runtime.modelMetadata.data ?? [];
  const appFlavor = runtime.appFlavor.data;
  const visionModels = useMemo(() => visionModelOptions(catalogModels), [catalogModels]);

  useEffect(() => {
    if (settings?.locale) {
      void changeAppLocale(settings.locale);
    }
  }, [settings?.locale]);

  const runRuntimeAction = useCallback(async (
    label: string,
    action: () => Promise<AppStatus>,
    options?: { toast?: boolean; warnBeforeGatewayRetirement?: boolean },
  ): Promise<AppStatus | null> => {
    if (options?.warnBeforeGatewayRetirement && !(await confirmAction({
      cancelLabel: t("common.cancel"),
      confirmLabel: t("common.confirm"),
      message: t("runtime.gatewayRetirementWarning"),
      title: t("common.confirm"),
    }))) {
      return null;
    }
    setBusy(label);
    const toastId =
      options?.toast === false
        ? null
        : showToast(runtimeActionLoadingMessage(label, t), "loading");
    try {
      const status = await action();
      setRuntimeCacheData("status", status);
      setBanner(status.message);
      if (toastId) {
        updateToast(toastId, {
          action: null,
          text: runtimeActionSuccessMessage(label, t),
          tone: "success",
        });
      }
      await refreshRuntimeStatus({ force: true });
      return status;
    } catch (err) {
      const message = messageFromError(err);
      setBanner(message);
      await refreshRuntimeStatus({ force: true });
      if (toastId) {
        updateToast(toastId, {
          action: null,
          text: message,
          tone: "error",
        });
      }
      if (options?.toast === false) {
        throw err;
      }
      return null;
    } finally {
      setBusy(null);
    }
  }, [confirmAction, refreshRuntimeStatus, setRuntimeCacheData, showToast, t, updateToast]);

  const saveSettings = useCallback(async (next: Settings) => {
    setBusy("settings");
    try {
      const restartGateway = shouldRestartGateway(settings, next, gatewayStatus);
      if (restartGateway && !(await confirmAction({
        cancelLabel: t("common.cancel"),
        confirmLabel: t("common.confirm"),
        message: t("runtime.gatewayRetirementWarning"),
        title: t("common.confirm"),
      }))) {
        return t("runtime.gatewayRetirementCancelled");
      }
      if (settings && next.auto_start_software !== settings.auto_start_software) {
        if (next.auto_start_software) {
          await api.setAutostart(true);
        } else {
          await api.removeAutostart();
        }
      }
      const savedSettings = await api.saveSettings(next);
      setRuntimeCacheData("settings", savedSettings);
      let saveMessage = t("settings.settingsSaved");
      if (restartGateway) {
        const status = await api.restartProxy();
        setRuntimeCacheData("status", status);
        saveMessage = t("gateway.gatewaySettingsSavedRestarted");
      }
      setBanner(null);
      await refreshRuntimeStatus({ force: true });
      return saveMessage;
    } catch (err) {
      const message = messageFromError(err);
      setBanner(message);
      await refreshRuntimeStatus({ force: true });
      throw err;
    } finally {
      setBusy(null);
    }
  }, [confirmAction, gatewayStatus, refreshRuntimeStatus, setRuntimeCacheData, settings, t]);

  const syncHistory = useCallback(async (targetProvider: string) => {
    setBusy("history");
    try {
      const result = await api.syncConversationHistory(targetProvider);
      if (result.status === "deferred" || result.status === "restart_required") {
        const message = t("settings.historySyncDeferred");
        setBanner(null);
        return message;
      }
      if (result.status === "conflict") {
        throw new Error(result.error ?? t("settings.historyProviderConflict"));
      }
      const message = result.status === "repaired"
        ? t("settings.historyStartupRepaired", {
            rows: result.changed_rows,
            files: result.changed_files,
          })
        : t("settings.conversationHistoryAlreadySynced");
      setBanner(message);
      return message;
    } catch (err) {
      const message = messageFromError(err);
      setBanner(message);
      throw err;
    } finally {
      setBusy(null);
    }
  }, [t]);

  const openSettings = useCallback(() => setSettingsOpen(true), []);
  const closeSettings = useCallback(() => setSettingsOpen(false), []);
  const startProxy = useCallback(() => runRuntimeAction("start", api.startProxy), [runRuntimeAction]);
  const stopProxy = useCallback(
    () => runRuntimeAction("stop", api.stopProxy, { warnBeforeGatewayRetirement: true }),
    [runRuntimeAction],
  );
  const startProxyQuiet = useCallback(
    () => runRuntimeAction("start", api.startProxy, { toast: false }),
    [runRuntimeAction],
  );
  const stopProxyQuiet = useCallback(
    () => runRuntimeAction("stop", api.stopProxy, { toast: false, warnBeforeGatewayRetirement: true }),
    [runRuntimeAction],
  );
  const updateProvidersCache = useCallback(
    (nextProviders: Provider[]) => setRuntimeCacheData("providers", nextProviders),
    [setRuntimeCacheData],
  );
  const updateSettingsCache = useCallback(
    (nextSettings: Settings) => setRuntimeCacheData("settings", nextSettings),
    [setRuntimeCacheData],
  );
  const updateStatusCache = useCallback(
    (status: AppStatus) => setRuntimeCacheData("status", status),
    [setRuntimeCacheData],
  );
  const applyGatewaySettings = useCallback(async (nextSettings: Settings) => {
    return await saveSettings(nextSettings);
  }, [saveSettings]);

  return (
    <FitStage>
    <div className="relative grid h-full min-h-0 min-w-0 grid-rows-[auto_auto_minmax(0,1fr)] bg-canvas text-ink">
      <WindowResizeHandles />
      <RuntimeBar
        appFlavor={appFlavor}
        busy={busy}
        message={banner}
        settings={settings}
        status={appStatus}
        onOpenSettings={openSettings}
        onStart={startProxy}
        onStop={stopProxy}
      />

      <nav className="flex min-h-[45px] items-center gap-1 bg-surface px-4 shadow-hairline">
        {contract.tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={cx(
              "focus-ring relative h-11 px-3 text-sm font-semibold",
              activeTab === tab.id ? "text-ink" : "text-slate-500 hover:text-ink",
            )}
            onClick={() => selectTab(tab.id as TabId)}
          >
            {t(`common.${tab.id === "codexhub" ? "codexHub" : "gateway"}`)}
            {activeTab === tab.id && (
              <span className="absolute inset-x-3 bottom-0 h-0.5 rounded-full bg-ink" />
            )}
          </button>
        ))}
        <span className="ml-auto hidden truncate text-xs text-slate-400 xl:block">
          {t("runtime.gatewayHint")}
        </span>
      </nav>

      <div className="relative min-h-0 min-w-0 max-w-full overflow-hidden">
        {mountedTabs.codexhub && (
          <section
            aria-hidden={visibleTab !== "codexhub"}
            className={tabPaneClass(visibleTab === "codexhub")}
            data-tab-pane="codexhub"
          >
            <div className="h-full min-h-0 min-w-0 overflow-x-auto overflow-y-auto">
              <ProvidersPage
                appFlavor={appFlavor}
                appStatus={appStatus}
                catalogModels={catalogModels}
                gatewayStatus={gatewayStatus}
                modelMetadata={modelMetadata}
                providers={providers}
                settings={settings}
                onGatewayChanged={refreshProviderRuntime}
                onRefreshClients={loadGatewayClients}
                onProvidersChanged={updateProvidersCache}
                onSettingsChanged={updateSettingsCache}
                onStatusChanged={updateStatusCache}
                onStartProxy={startProxyQuiet}
                codexSwitchRequest={codexSwitchRequest}
                onCodexSwitchRequestHandled={(id) => {
                  setCodexSwitchRequest((current) => current?.id === id ? null : current);
                }}
              />
            </div>
          </section>
        )}
        {mountedTabs.gateway && (
          <section
            aria-hidden={visibleTab !== "gateway"}
            className={tabPaneClass(visibleTab === "gateway")}
            data-tab-pane="gateway"
          >
            <div className="h-full min-h-0 min-w-0 overflow-hidden">
              <GatewayPage
                appFlavor={appFlavor}
                settings={settings}
                providers={providers}
                status={gatewayStatus}
                usageSummary={gatewayUsageSnapshot?.summary ?? null}
                usageEvents={gatewayUsageSnapshot?.events ?? []}
                usageStatus={gatewayUsageSnapshot?.telemetry_status ?? null}
                usageError={runtime.gatewayUsageSnapshot.error}
                recentEvents={gatewayEvents}
                clientInfos={gatewayClients}
                busy={busy}
                clients={contract.gatewayClients as GatewayClientContract[]}
                onApplySettings={applyGatewaySettings}
                onRefreshClients={loadGatewayClients}
                onStartProxy={startProxyQuiet}
                onStopProxy={stopProxyQuiet}
                onUsageWindowChange={updateUsageWindow}
              />
            </div>
          </section>
        )}
      </div>

      {confirmDialog}
      <SettingsDrawer
        busy={busy}
        appVersion={runtime.appVersion.data}
        open={settingsOpen}
        providers={providers}
        settings={settings}
        updateInstallStatus={updateInstallStatus}
        updateBusy={updateBusy}
        updateStatus={updateView.updateStatus ?? runtime.updateStatus.data}
        visionModels={visionModels}
        onCheckUpdate={appUpdate.checkForUpdates}
        onClose={closeSettings}
        onInstallUpdate={async () => {
          await appUpdate.startInstall("settings");
        }}
        onSave={saveSettings}
        onSyncHistory={syncHistory}
      />
    </div>
    </FitStage>
  );
}

function runtimeActionLoadingMessage(label: string, t: (key: string) => string) {
  if (label === "start") {
    return t("runtime.startingRuntime");
  }
  if (label === "stop") {
    return t("runtime.stoppingRuntime");
  }
  if (label === "restart") {
    return t("runtime.restartingRuntime");
  }
  return t("runtime.updatingRuntime");
}

function runtimeActionSuccessMessage(label: string, t: (key: string) => string) {
  if (label === "start") {
    return t("runtime.runtimeStarted");
  }
  if (label === "stop") {
    return t("runtime.runtimeStopped");
  }
  if (label === "restart") {
    return t("runtime.runtimeRestarted");
  }
  return t("runtime.runtimeUpdated");
}
