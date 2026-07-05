import { useCallback, useEffect, useRef, useState } from "react";
import { RuntimeBar } from "./components/RuntimeBar";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { useToasts } from "./components/PageToast";
import { cx } from "./lib/format";
import { api, messageFromError } from "./lib/tauri";
import contract from "./lib/ui-contract.json";
import type {
  AppStatus,
  GatewayClientContract,
  GatewayClientInfo,
  GatewayStatus,
  GatewayUsageEvent,
  GatewayUsageSummary,
  Model,
  Provider,
  Settings,
  TabId,
  TelemetryStatus,
  UsageQueryWindow,
} from "./lib/types";
import { GatewayPage } from "./pages/GatewayPage";
import { ProvidersPage } from "./pages/ProvidersPage";

type RuntimeSnapshot = {
  status: AppStatus | null;
  settings: Settings | null;
  providers: Provider[];
  gatewayStatus: GatewayStatus | null;
  gatewayUsageSummary: GatewayUsageSummary | null;
  gatewayUsageEvents: GatewayUsageEvent[];
  gatewayUsageStatus: TelemetryStatus | null;
  usageError: string | null;
  gatewayClients: GatewayClientInfo[];
  catalogModels: Model[];
};

type LoadRuntimeOptions = {
  includeClientVersions?: boolean;
};

function defaultUsageWindow(): UsageQueryWindow {
  const end = startOfDay(new Date());
  return {
    startTs: addDays(end, -6).toISOString(),
    endTs: endOfDay(end).toISOString(),
  };
}

function startOfDay(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function endOfDay(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate(), 23, 59, 59, 999);
}

function addDays(date: Date, days: number) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
}

function mergeGatewayClients(
  previous: GatewayClientInfo[],
  next: GatewayClientInfo[],
): GatewayClientInfo[] {
  const previousById = new Map(previous.map((client) => [client.id, client]));
  return next.map((client) => {
    const previousClient = previousById.get(client.id);
    if (!client.installed) {
      return { ...client, current_version: null, latest_version: null };
    }
    return {
      ...client,
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
    previous.gateway_image_proxy_model !== next.gateway_image_proxy_model
  );
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

export default function App() {
  const { showToast, updateToast } = useToasts();
  const [activeTab, setActiveTab] = useState<TabId>("codexhub");
  const [runtime, setRuntime] = useState<RuntimeSnapshot>({
    status: null,
    settings: null,
    providers: [],
    gatewayStatus: null,
    gatewayUsageSummary: null,
    gatewayUsageEvents: [],
    gatewayUsageStatus: null,
    usageError: null,
    gatewayClients: [],
    catalogModels: [],
  });
  const [busy, setBusy] = useState<string | null>("load");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [usageWindow, setUsageWindow] = useState<UsageQueryWindow>(() => defaultUsageWindow());
  const gatewayClientLoadSeq = useRef(0);

  const loadGatewayClients = useCallback(async (options?: LoadRuntimeOptions) => {
    const requestSeq = ++gatewayClientLoadSeq.current;
    try {
      const clients = await api.listGatewayClients(Boolean(options?.includeClientVersions));
      setRuntime((current) => {
        if (requestSeq !== gatewayClientLoadSeq.current) {
          return current;
        }
        return {
          ...current,
          gatewayClients: mergeGatewayClients(current.gatewayClients, clients),
        };
      });
    } catch (err) {
      if (requestSeq !== gatewayClientLoadSeq.current) {
        return;
      }
      setBanner(messageFromError(err));
      throw err;
    }
  }, []);

  const loadRuntime = useCallback(async () => {
    try {
      const [
        statusResult,
        settingsResult,
        providersResult,
        gatewayResult,
        catalogResult,
        usageSnapshotResult,
      ] =
        await Promise.allSettled([
          api.getStatus(),
          api.getSettings(),
          api.getProviders(),
          api.gatewayStatus(),
          api.listModels(),
          api.gatewayUsageSnapshot(usageWindow),
        ]);

      setRuntime((current) => ({
        ...current,
        status: statusResult.status === "fulfilled" ? statusResult.value : current.status,
        settings: settingsResult.status === "fulfilled" ? settingsResult.value : current.settings,
        providers: providersResult.status === "fulfilled" ? providersResult.value : current.providers,
        gatewayStatus: gatewayResult.status === "fulfilled" ? gatewayResult.value : current.gatewayStatus,
        catalogModels: catalogResult.status === "fulfilled" ? catalogResult.value : current.catalogModels,
        gatewayUsageSummary:
          usageSnapshotResult.status === "fulfilled"
            ? usageSnapshotResult.value.summary
            : current.gatewayUsageSummary,
        gatewayUsageEvents:
          usageSnapshotResult.status === "fulfilled"
            ? usageSnapshotResult.value.events
            : current.gatewayUsageEvents,
        gatewayUsageStatus:
          usageSnapshotResult.status === "fulfilled"
            ? usageSnapshotResult.value.telemetry_status
            : current.gatewayUsageStatus,
        usageError:
          usageSnapshotResult.status === "fulfilled"
            ? null
            : messageFromError(usageSnapshotResult.reason),
      }));

      const rejected = [
        statusResult,
        settingsResult,
        providersResult,
        gatewayResult,
        catalogResult,
      ].find((result) => result.status === "rejected");
      if (rejected?.status === "rejected") {
        setBanner(messageFromError(rejected.reason));
      }
    } catch (err) {
      setBanner(messageFromError(err));
    } finally {
      setBusy((current) => (current === "load" ? null : current));
    }
  }, [usageWindow]);

  const updateUsageWindow = useCallback((nextWindow: UsageQueryWindow) => {
    setUsageWindow((current) => {
      if (current.startTs === nextWindow.startTs && current.endTs === nextWindow.endTs) {
        return current;
      }
      return nextWindow;
    });
  }, []);

  useEffect(() => {
    void loadRuntime();
    void loadGatewayClients();
    const timer = window.setInterval(() => void loadRuntime(), 5000);
    const clientTimer = window.setInterval(() => void loadGatewayClients(), 12 * 60 * 60 * 1000);
    return () => {
      window.clearInterval(timer);
      window.clearInterval(clientTimer);
    };
  }, [loadGatewayClients, loadRuntime]);

  const visionModels = visionModelOptions(runtime.catalogModels);

  async function runRuntimeAction(
    label: string,
    action: () => Promise<AppStatus>,
    options?: { toast?: boolean },
  ) {
    setBusy(label);
    const toastId =
      options?.toast === false
        ? null
        : showToast(runtimeActionLoadingMessage(label), "loading");
    try {
      const status = await action();
      setRuntime((currentRuntime) => ({ ...currentRuntime, status }));
      setBanner(status.message);
      if (toastId) {
        updateToast(toastId, {
          action: null,
          text: runtimeActionSuccessMessage(label),
          tone: "success",
        });
      }
      await loadRuntime();
    } catch (err) {
      const message = messageFromError(err);
      setBanner(message);
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
  }

  async function saveSettings(next: Settings) {
    setBusy("settings");
    try {
      const previousUnified = runtime.settings?.unified_codex_history ?? true;
      const nextUnified = next.unified_codex_history ?? true;
      const currentMode = runtime.status?.mode;
      const shouldRestartGateway = Boolean(
        runtime.status?.proxy_running && gatewayRuntimeSettingsChanged(runtime.settings, next),
      );
      if (runtime.settings && next.auto_start_proxy !== runtime.settings.auto_start_proxy) {
        if (next.auto_start_proxy) {
          await api.setAutostart(true);
        } else {
          await api.removeAutostart();
        }
      }
      const settings = await api.saveSettings(next);
      setRuntime((currentRuntime) => ({ ...currentRuntime, settings }));
      let historyMessage: string | null = null;
      if (previousUnified !== nextUnified) {
        if (currentMode === "official") {
          const status = await api.switchMode("official", false);
          setRuntime((currentRuntime) => ({ ...currentRuntime, status }));
        }
        if (nextUnified) {
          historyMessage = await api.migrateOfficialHistoryToUnified();
        } else if (currentMode === "official") {
          historyMessage = await api.restoreOfficialHistoryFromUnified();
        }
      }
      let saveMessage = historyMessage ?? "Settings saved";
      if (shouldRestartGateway) {
        const status = await api.restartProxy();
        setRuntime((currentRuntime) => ({ ...currentRuntime, status }));
        saveMessage = "Gateway settings saved and runtime restarted";
      }
      setBanner(null);
      await loadRuntime();
      return saveMessage;
    } catch (err) {
      setBanner(messageFromError(err));
      throw err;
    } finally {
      setBusy(null);
    }
  }

  async function syncHistory(targetProvider: string) {
    setBusy("history");
    try {
      const message = await api.syncHistory(targetProvider);
      setBanner(message);
      return message;
    } catch (err) {
      const message = messageFromError(err);
      setBanner(message);
      throw err;
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="grid h-screen min-h-[720px] min-w-0 grid-rows-[auto_auto_minmax(0,1fr)] bg-canvas text-ink">
      <RuntimeBar
        busy={busy}
        message={banner}
        settings={runtime.settings}
        status={runtime.status}
        onOpenSettings={() => setSettingsOpen(true)}
        onStart={() => void runRuntimeAction("start", api.startProxy)}
        onStop={() => void runRuntimeAction("stop", api.stopProxy)}
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
            onClick={() => setActiveTab(tab.id as TabId)}
          >
            {tab.label}
            {activeTab === tab.id && (
              <span className="absolute inset-x-3 bottom-0 h-0.5 rounded-full bg-ink" />
            )}
          </button>
        ))}
        <span className="ml-auto hidden truncate text-xs text-slate-400 lg:block">
          Gateway is the local OpenAI-compatible server in front of Hub
        </span>
      </nav>

      <div className="min-h-0 overflow-x-auto overflow-y-auto p-4">
        {activeTab === "codexhub" ? (
          <ProvidersPage
            gatewayStatus={runtime.gatewayStatus}
            onGatewayChanged={async () => {
              await loadRuntime();
              await loadGatewayClients();
            }}
            onStartProxy={() => runRuntimeAction("start", api.startProxy, { toast: false })}
          />
        ) : (
          <GatewayPage
            settings={runtime.settings}
            providers={runtime.providers}
            status={runtime.gatewayStatus}
            usageSummary={runtime.gatewayUsageSummary}
            usageEvents={runtime.gatewayUsageEvents}
            usageStatus={runtime.gatewayUsageStatus}
            usageError={runtime.usageError}
            clientInfos={runtime.gatewayClients}
            busy={busy}
            pending={contract.pendingBackend}
            clients={contract.gatewayClients as GatewayClientContract[]}
            onApplySettings={async (settings) => {
              await saveSettings(settings);
            }}
            onRefreshClients={loadGatewayClients}
            onRestartProxy={() => runRuntimeAction("restart", api.restartProxy, { toast: false })}
            onStartProxy={() => runRuntimeAction("start", api.startProxy, { toast: false })}
            onStopProxy={() => runRuntimeAction("stop", api.stopProxy, { toast: false })}
            onUsageWindowChange={updateUsageWindow}
          />
        )}
      </div>

      <SettingsDrawer
        busy={busy}
        open={settingsOpen}
        providers={runtime.providers}
        settings={runtime.settings}
        visionModels={visionModels}
        onClose={() => setSettingsOpen(false)}
        onSave={saveSettings}
        onSyncHistory={syncHistory}
      />
      {settingsOpen && (
        <button
          type="button"
          className="fixed inset-0 z-40 cursor-default bg-black/10 backdrop-blur-[1px]"
          aria-label="Close settings"
          onClick={() => setSettingsOpen(false)}
        />
      )}
    </div>
  );
}

function runtimeActionLoadingMessage(label: string) {
  if (label === "start") {
    return "Starting Gateway runtime...";
  }
  if (label === "stop") {
    return "Stopping Gateway runtime...";
  }
  if (label === "restart") {
    return "Restarting Gateway runtime...";
  }
  return "Updating Gateway runtime...";
}

function runtimeActionSuccessMessage(label: string) {
  if (label === "start") {
    return "Gateway runtime started";
  }
  if (label === "stop") {
    return "Gateway runtime stopped";
  }
  if (label === "restart") {
    return "Gateway runtime restarted";
  }
  return "Gateway runtime updated";
}
