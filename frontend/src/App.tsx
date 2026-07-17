import { useCallback, useEffect, useMemo, useRef, useState, useTransition } from "react";
import { listen } from "@tauri-apps/api/event";
import { useTranslation } from "react-i18next";
import { RuntimeBar } from "./components/RuntimeBar";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { useToasts } from "./components/PageToast";
import { changeAppLocale } from "./i18n";
import { cx } from "./lib/format";
import { historyIssueKey } from "./lib/history";
import { addDays, endOfDay, startOfDay } from "./lib/dateRange";
import { api, messageFromError } from "./lib/tauri";
import contract from "./lib/ui-contract.json";
import { isUpdateInstallActive, updateInstallToastText } from "./lib/updateStatus";
import type {
  AppFlavorInfo,
  AppStatus,
  AppUpdateInstallStatus,
  AppUpdateStatus,
  AppVersionInfo,
  GatewayClientContract,
  GatewayClientInfo,
  GatewayEvent,
  GatewayStatus,
  GatewayUsageSnapshot,
  Model,
  Provider,
  Settings,
  TabId,
  UsageQueryWindow,
} from "./lib/types";
import { GatewayPage } from "./pages/GatewayPage";
import { ProvidersPage } from "./pages/ProvidersPage";

type RuntimeSnapshot = {
  status: RuntimeCache<AppStatus>;
  settings: RuntimeCache<Settings>;
  providers: RuntimeCache<Provider[]>;
  gatewayStatus: RuntimeCache<GatewayStatus>;
  gatewayUsageSnapshot: RuntimeCache<GatewayUsageSnapshot>;
  gatewayEvents: RuntimeCache<GatewayEvent[]>;
  gatewayClients: RuntimeCache<GatewayClientInfo[]>;
  catalogModels: RuntimeCache<Model[]>;
  modelMetadata: RuntimeCache<Model[]>;
  appFlavor: RuntimeCache<AppFlavorInfo>;
  appVersion: RuntimeCache<AppVersionInfo>;
  updateStatus: RuntimeCache<AppUpdateStatus>;
};

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

type RuntimeCache<T> = {
  data: T | null;
  loading: boolean;
  error: string | null;
  updatedAt: number | null;
  inflight?: Promise<T>;
};

type RuntimeCacheKey = keyof RuntimeSnapshot;

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
const BACKGROUND_VERSION_PROBE_DELAY_MS = 1000;
const STARTUP_UPDATE_CHECK_DELAY_MS = 2500;
const APP_UPDATE_CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000;
const UPDATE_INSTALL_STATUS_POLL_MS = 500;

function defaultUsageWindow(): UsageQueryWindow {
  const end = startOfDay(new Date());
  return {
    startTs: addDays(end, -6).toISOString(),
    endTs: endOfDay(end).toISOString(),
  };
}

function runtimeCache<T>(data: T | null = null): RuntimeCache<T> {
  return {
    data,
    loading: false,
    error: null,
    updatedAt: data === null ? null : Date.now(),
  };
}

function setCacheLoading(current: RuntimeSnapshot, key: RuntimeCacheKey): RuntimeSnapshot {
  const cache = current[key] as RuntimeCache<unknown>;
  return {
    ...current,
    [key]: {
      ...cache,
      loading: true,
      error: null,
    },
  } as RuntimeSnapshot;
}

function setCacheData<T>(
  current: RuntimeSnapshot,
  key: RuntimeCacheKey,
  data: T,
): RuntimeSnapshot {
  const cache = current[key] as RuntimeCache<T>;
  return {
    ...current,
    [key]: {
      ...cache,
      data,
      loading: false,
      error: null,
      updatedAt: Date.now(),
    },
  } as RuntimeSnapshot;
}

function setCacheError(current: RuntimeSnapshot, key: RuntimeCacheKey, error: string): RuntimeSnapshot {
  const cache = current[key] as RuntimeCache<unknown>;
  return {
    ...current,
    [key]: {
      ...cache,
      data: key === "status" ? null : cache.data,
      loading: false,
      error,
    },
  } as RuntimeSnapshot;
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
      ? "visible z-10 opacity-100 [content-visibility:visible] [will-change:opacity]"
      : "invisible z-0 opacity-0 pointer-events-none [content-visibility:hidden]",
  );
}

export default function App() {
  const { t } = useTranslation();
  const { dismissToast, showToast, updateToast } = useToasts();
  const [activeTab, setActiveTab] = useState<TabId>("codexhub");
  const [visibleTab, setVisibleTab] = useState<TabId>("codexhub");
  const [mountedTabs, setMountedTabs] = useState<Record<TabId, boolean>>({
    codexhub: true,
    gateway: false,
  });
  const [gatewayVisited, setGatewayVisited] = useState(false);
  const [, startUiTransition] = useTransition();
  const [runtime, setRuntime] = useState<RuntimeSnapshot>({
    status: runtimeCache<AppStatus>(),
    settings: runtimeCache<Settings>(),
    providers: runtimeCache<Provider[]>([]),
    gatewayStatus: runtimeCache<GatewayStatus>(),
    gatewayUsageSnapshot: runtimeCache<GatewayUsageSnapshot>(),
    gatewayEvents: runtimeCache<GatewayEvent[]>([]),
    gatewayClients: runtimeCache<GatewayClientInfo[]>([]),
    catalogModels: runtimeCache<Model[]>([]),
    modelMetadata: runtimeCache<Model[]>([]),
    appFlavor: runtimeCache<AppFlavorInfo>(),
    appVersion: runtimeCache<AppVersionInfo>(),
    updateStatus: runtimeCache<AppUpdateStatus>(),
  });
  const [busy, setBusy] = useState<string | null>("load");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [updateBusy, setUpdateBusy] = useState<"check" | null>(null);
  const [updateInstallStatus, setUpdateInstallStatus] = useState<AppUpdateInstallStatus | null>(null);
  const [updateInstallSource, setUpdateInstallSource] = useState<"settings" | "toast" | null>(null);
  const [usageWindow, setUsageWindow] = useState<UsageQueryWindow>(() => defaultUsageWindow());
  const runtimeInflight = useRef<Partial<Record<RuntimeCacheKey, Promise<unknown>>>>({});
  const runtimeRef = useRef<RuntimeSnapshot | null>(null);
  const startupUpdateCheckStarted = useRef(false);
  const updateAvailableToastId = useRef<string | null>(null);
  const updateInstallToastId = useRef<string | null>(null);
  const trayToastIds = useRef<Map<string, string>>(new Map());
  runtimeRef.current = runtime;
  const settingsLoaded = Boolean(runtime.settings.data);

  const runCachedRequest = useCallback(async <T,>(
    key: RuntimeCacheKey,
    loader: () => Promise<T>,
  options?: RuntimeCacheOptions<T>,
  ): Promise<T> => {
    const existing = runtimeInflight.current[key] as Promise<T> | undefined;
    if (existing && !options?.force) {
      return existing;
    }
    const cached = runtimeRef.current?.[key] as RuntimeCache<T> | undefined;
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

    let request: Promise<T>;
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

  const setRuntimeCacheData = useCallback(<T,>(key: RuntimeCacheKey, data: T) => {
    startUiTransition(() => {
      setRuntime((current) => setCacheData(current, key, data));
    });
  }, [startUiTransition]);

  const refreshStatus = useCallback(
    (options?: { force?: boolean; quiet?: boolean }) =>
      runCachedRequest<AppStatus>("status", () => api.getStatus(), options),
    [runCachedRequest],
  );

  const refreshGatewayStatus = useCallback(
    (options?: { force?: boolean; quiet?: boolean }) =>
      runCachedRequest<GatewayStatus>("gatewayStatus", () => api.gatewayStatus(), options),
    [runCachedRequest],
  );

  const refreshSettings = useCallback(
    (options?: { force?: boolean; quiet?: boolean }) =>
      runCachedRequest<Settings>("settings", () => api.getSettings(), options),
    [runCachedRequest],
  );

  const refreshProviders = useCallback(
    (options?: { force?: boolean; quiet?: boolean }) =>
      runCachedRequest<Provider[]>("providers", () => api.getProviders(), options),
    [runCachedRequest],
  );

  const refreshCatalogModels = useCallback(
    (options?: { force?: boolean; quiet?: boolean }) =>
      runCachedRequest<Model[]>("catalogModels", () => api.listModels(), options),
    [runCachedRequest],
  );

  const refreshModelMetadata = useCallback(
    (options?: { force?: boolean; quiet?: boolean }) =>
      runCachedRequest<Model[]>("modelMetadata", () => api.listModelMetadata(), {
        quiet: true,
        ...options,
      }),
    [runCachedRequest],
  );

  const loadAppFlavor = useCallback(async (options?: LoadRuntimeOptions) => {
    await runCachedRequest<AppFlavorInfo>(
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
    await runCachedRequest<GatewayClientInfo[]>(
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
    await Promise.allSettled([
      runCachedRequest<GatewayUsageSnapshot>(
        "gatewayUsageSnapshot",
        () => api.gatewayUsageSnapshot(usageWindow),
        { force: options?.force, quiet: true, staleMs: 4000 },
      ),
      runCachedRequest<GatewayEvent[]>(
        "gatewayEvents",
        () => api.gatewayRecentEvents(80),
        { force: options?.force, quiet: true, staleMs: 4000 },
      ),
    ]);
  }, [runCachedRequest, usageWindow]);

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
      return await runCachedRequest<AppVersionInfo>(
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

  const loadAppUpdateStatus = useCallback(async () => {
    return runCachedRequest<AppUpdateStatus>(
      "updateStatus",
      async () => {
        const status = await api.checkAppUpdate();
        if (!status) {
          throw new Error(t("settings.desktopUpdatesUnavailable"));
        }
        return status;
      },
      {
        force: true,
        quiet: true,
        apply: (current, nextStatus) =>
          setCacheData(
            setCacheData(current, "appVersion", { current_version: nextStatus.current_version }),
            "updateStatus",
            nextStatus,
          ),
      },
    );
  }, [runCachedRequest, t]);

  const updateInstallToast = useCallback(
    (status: AppUpdateInstallStatus, source: "settings" | "toast" | null = updateInstallSource) => {
      if (source !== "toast" || !updateInstallToastId.current) {
        return;
      }
      updateToast(updateInstallToastId.current, {
        action: null,
        text: updateInstallToastText(status, t),
        tone: status.phase === "failed" ? "error" : isUpdateInstallActive(status) ? "loading" : "success",
      });
      if (!isUpdateInstallActive(status)) {
        updateInstallToastId.current = null;
      }
    },
    [t, updateInstallSource, updateToast],
  );

  const startAppUpdateInstall = useCallback(
    async (source: "settings" | "toast" = "settings") => {
      const toastId = updateAvailableToastId.current;
      if (toastId) {
        dismissToast(toastId);
        updateAvailableToastId.current = null;
      }

      setUpdateInstallSource(source);
      if (source === "toast") {
        updateInstallToastId.current = showToast({
          dedupeKey: "app-update-install",
          text: t("settings.downloadingUpdate"),
          timeoutMs: null,
          tone: "loading",
        });
      }

      try {
        const status = await api.startAppUpdateInstall();
        setUpdateInstallStatus(status);
        updateInstallToast(status, source);
        if (source === "settings" && status.phase === "failed") {
          showToast(t("settings.updateInstallFailed", { message: status.message }), "error");
        }
      } catch (err) {
        const message = messageFromError(err);
        const failedStatus = failedUpdateInstallStatus(
          updateInstallStatus,
          runtimeRef.current?.appVersion.data?.current_version ?? "",
          runtimeRef.current?.updateStatus.data?.latest_version ?? null,
          message,
        );
        setUpdateInstallStatus(failedStatus);
        updateInstallToast(failedStatus, source);
        if (source === "settings") {
          showToast(t("settings.updateInstallFailed", { message }), "error");
        }
      }
    },
    [dismissToast, showToast, t, updateInstallStatus, updateInstallToast],
  );

  const checkForUpdates = useCallback(async () => {
    setUpdateBusy("check");
    try {
      const status = await loadAppUpdateStatus();
      showToast({
        text: status.available && status.latest_version
          ? t("settings.updateAvailable", { version: status.latest_version })
          : t("settings.noUpdatesAvailable"),
        tone: status.available ? "info" : "success",
      });
      return status;
    } catch (err) {
      showToast({
        text: t("settings.updateCheckFailed", { message: messageFromError(err) }),
        tone: "error",
      });
      return null;
    } finally {
      setUpdateBusy(null);
    }
  }, [loadAppUpdateStatus, showToast, t]);

  const repairConversationHistory = useCallback(async () => {
    setBusy("history");
    const toastId = showToast({
      dedupeKey: "unified-history-preflight",
      text: t("settings.syncingConversationHistory"),
      timeoutMs: null,
      tone: "loading",
    });
    try {
      const result = await api.syncConversationHistory();
      if (result.status === "repaired") {
        updateToast(toastId, {
          action: null,
          text: t("settings.historyStartupRepaired", {
            rows: result.changed_rows,
            files: result.changed_files,
          }),
          tone: "success",
        });
      } else if (result.status === "deferred") {
        updateToast(toastId, {
          action: null,
          text: t("settings.historySyncDeferred"),
          tone: "info",
        });
      } else if (result.status === "restart_required" || result.status === "conflict") {
        updateToast(toastId, {
          action: null,
          text: t(historyIssueKey(result)),
          timeoutMs: null,
          tone: "error",
        });
      } else {
        dismissToast(toastId);
      }
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: t("settings.historyUnexpectedFailure"),
        timeoutMs: null,
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  }, [dismissToast, showToast, t, updateToast]);

  const runAutomaticUpdateCheck = useCallback(async () => {
    try {
      const status = await loadAppUpdateStatus();
      if (!status?.available || !status.latest_version) {
        return;
      }
      updateAvailableToastId.current = showToast({
        dedupeKey: "app-update-available",
        action: {
          label: t("settings.update"),
          onClick: () => void startAppUpdateInstall("toast"),
        },
        text: t("settings.updateAvailable", { version: status.latest_version }),
        timeoutMs: null,
        tone: "info",
      });
    } catch {
      // Automatic update checks are best-effort and should not create noisy banners.
    }
  }, [loadAppUpdateStatus, showToast, startAppUpdateInstall, t]);

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
    void loadGatewayClients();
    const versionProbeTimer = window.setTimeout(
      () => void loadGatewayClients({ includeClientVersions: true }),
      BACKGROUND_VERSION_PROBE_DELAY_MS,
    );
    const timer = window.setInterval(() => void refreshRuntimeStatus(), 5000);
    const clientTimer = window.setInterval(() => void loadGatewayClients(), 12 * 60 * 60 * 1000);
    return () => {
      window.clearTimeout(versionProbeTimer);
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
    const timer = window.setTimeout(() => {
      setMountedTabs((current) => (current.gateway ? current : { ...current, gateway: true }));
    }, 250);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (startupUpdateCheckStarted.current || !settingsLoaded) {
      return;
    }
    startupUpdateCheckStarted.current = true;
    const timer = window.setTimeout(
      () => void runAutomaticUpdateCheck(),
      STARTUP_UPDATE_CHECK_DELAY_MS,
    );
    const interval = window.setInterval(() => void runAutomaticUpdateCheck(), APP_UPDATE_CHECK_INTERVAL_MS);
    return () => {
      window.clearTimeout(timer);
      window.clearInterval(interval);
    };
  }, [runAutomaticUpdateCheck, settingsLoaded]);

  useEffect(() => {
    const timer = window.setTimeout(async () => {
      try {
        const completion = await api.consumeAppUpdateCompletion();
        if (!completion?.completed) {
          return;
        }
        showToast(t("settings.updateInstalled", { version: completion.current_version }), "success");
        setRuntime((current) =>
          setCacheData(current, "appVersion", { current_version: completion.current_version }),
        );
      } catch {
        // Completion verification is best-effort; pending failures should not interrupt startup.
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [showToast, t]);

  useEffect(() => {
    if (!isUpdateInstallActive(updateInstallStatus)) {
      return;
    }

    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const status = await api.getAppUpdateInstallStatus();
        if (cancelled) {
          return;
        }
        setUpdateInstallStatus(status);
        updateInstallToast(status);
        if (updateInstallSource === "settings" && status.phase === "failed") {
          showToast(t("settings.updateInstallFailed", { message: status.message }), "error");
        }
      } catch (err) {
        if (cancelled || updateInstallStatus?.phase === "installing" || updateInstallStatus?.phase === "restarting") {
          return;
        }
        const message = messageFromError(err);
        const failedStatus = failedUpdateInstallStatus(
          updateInstallStatus,
          runtimeRef.current?.appVersion.data?.current_version ?? "",
          runtimeRef.current?.updateStatus.data?.latest_version ?? null,
          message,
        );
        setUpdateInstallStatus(failedStatus);
        updateInstallToast(failedStatus);
        if (updateInstallSource === "settings") {
          showToast(t("settings.updateInstallFailed", { message }), "error");
        }
      }
    }, UPDATE_INSTALL_STATUS_POLL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [showToast, t, updateInstallSource, updateInstallStatus, updateInstallToast]);

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
    options?: { toast?: boolean },
  ) => {
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
    } finally {
      setBusy(null);
    }
  }, [refreshRuntimeStatus, setRuntimeCacheData, showToast, t, updateToast]);

  const saveSettings = useCallback(async (next: Settings) => {
    setBusy("settings");
    try {
      const restartGateway = shouldRestartGateway(settings, next, gatewayStatus);
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
  }, [gatewayStatus, refreshRuntimeStatus, setRuntimeCacheData, settings, t]);

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
  const stopProxy = useCallback(() => runRuntimeAction("stop", api.stopProxy), [runRuntimeAction]);
  const startProxyQuiet = useCallback(
    () => runRuntimeAction("start", api.startProxy, { toast: false }),
    [runRuntimeAction],
  );
  const stopProxyQuiet = useCallback(
    () => runRuntimeAction("stop", api.stopProxy, { toast: false }),
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
    <div className="grid h-screen min-h-[720px] min-w-0 grid-rows-[auto_auto_minmax(0,1fr)] bg-canvas text-ink">
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
        <span className="ml-auto hidden truncate text-xs text-slate-400 lg:block">
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
            <div className="h-full min-h-0 min-w-0 overflow-x-hidden overflow-y-auto">
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

      <SettingsDrawer
        busy={busy}
        appVersion={runtime.appVersion.data}
        open={settingsOpen}
        providers={providers}
        settings={settings}
        updateInstallStatus={updateInstallStatus}
        updateBusy={updateBusy}
        updateStatus={runtime.updateStatus.data}
        visionModels={visionModels}
        onCheckUpdate={checkForUpdates}
        onClose={closeSettings}
        onInstallUpdate={() => startAppUpdateInstall("settings")}
        onSave={saveSettings}
        onSyncHistory={syncHistory}
      />
    </div>
  );
}

function failedUpdateInstallStatus(
  previous: AppUpdateInstallStatus | null,
  currentVersion: string,
  targetVersion: string | null,
  message: string,
): AppUpdateInstallStatus {
  return {
    phase: "failed",
    current_version: previous?.current_version || currentVersion,
    target_version: previous?.target_version ?? targetVersion,
    downloaded_bytes: previous?.downloaded_bytes ?? 0,
    total_bytes: previous?.total_bytes ?? null,
    message,
    updated_at: new Date().toISOString(),
  };
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
