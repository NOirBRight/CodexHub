import { useCallback, useEffect, useMemo, useState } from "react";
import { RuntimeBar } from "./components/RuntimeBar";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { cx } from "./lib/format";
import { api, messageFromError } from "./lib/tauri";
import contract from "./lib/ui-contract.json";
import type {
  AppStatus,
  GatewayClientContract,
  GatewayStatus,
  Provider,
  Settings,
  TabId,
} from "./lib/types";
import { GatewayPage } from "./pages/GatewayPage";
import { ProvidersPage } from "./pages/ProvidersPage";

type RuntimeSnapshot = {
  status: AppStatus | null;
  settings: Settings | null;
  providers: Provider[];
  gatewayStatus: GatewayStatus | null;
};

export default function App() {
  const [activeTab, setActiveTab] = useState<TabId>("codexhub");
  const [runtime, setRuntime] = useState<RuntimeSnapshot>({
    status: null,
    settings: null,
    providers: [],
    gatewayStatus: null,
  });
  const [busy, setBusy] = useState<string | null>("load");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);

  const loadRuntime = useCallback(async () => {
    try {
      const [statusResult, settingsResult, providersResult, gatewayResult] =
        await Promise.allSettled([
          api.getStatus(),
          api.getSettings(),
          api.getProviders(),
          api.gatewayStatus(),
        ]);

      setRuntime((current) => ({
        status: statusResult.status === "fulfilled" ? statusResult.value : current.status,
        settings: settingsResult.status === "fulfilled" ? settingsResult.value : current.settings,
        providers: providersResult.status === "fulfilled" ? providersResult.value : current.providers,
        gatewayStatus: gatewayResult.status === "fulfilled" ? gatewayResult.value : current.gatewayStatus,
      }));

      const rejected = [statusResult, settingsResult, providersResult, gatewayResult].find(
        (result) => result.status === "rejected",
      );
      if (rejected?.status === "rejected") {
        setBanner(messageFromError(rejected.reason));
      }
    } catch (err) {
      setBanner(messageFromError(err));
    } finally {
      setBusy((current) => (current === "load" ? null : current));
    }
  }, []);

  useEffect(() => {
    void loadRuntime();
    const timer = window.setInterval(() => void loadRuntime(), 5000);
    return () => window.clearInterval(timer);
  }, [loadRuntime]);

  const providerSourceCount = useMemo(() => {
    const official = runtime.settings?.include_official_models ? 1 : 0;
    return official + runtime.providers.filter((provider) => provider.enabled && !provider.hidden).length;
  }, [runtime.providers, runtime.settings?.include_official_models]);

  const exportedCount = runtime.gatewayStatus?.official_models.length ?? 0;

  async function switchMode(next: "official" | "custom") {
    const current = runtime.status?.mode === "custom" ? "custom" : "official";
    if (current === next) {
      return;
    }

    setBusy("switch");
    try {
      const settings = runtime.settings ?? (await api.getSettings());
      const label = next === "custom" ? "Hub" : "Official";
      const historyLine = settings.auto_sync_history
        ? "History sync is enabled and will run before the config switch."
        : "History sync is disabled for this switch.";
      if (!window.confirm(`Switch Codex to ${label} mode?\n\n${historyLine}`)) {
        return;
      }
      const status = await api.switchMode(next, settings.auto_sync_history);
      setRuntime((currentRuntime) => ({ ...currentRuntime, status }));
      setBanner(status.message);
      await loadRuntime();
    } catch (err) {
      setBanner(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function runRuntimeAction(label: string, action: () => Promise<AppStatus>) {
    setBusy(label);
    try {
      const status = await action();
      setRuntime((currentRuntime) => ({ ...currentRuntime, status }));
      setBanner(status.message);
      await loadRuntime();
    } catch (err) {
      setBanner(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function saveSettings(next: Settings) {
    setBusy("settings");
    try {
      if (runtime.settings && next.auto_start_proxy !== runtime.settings.auto_start_proxy) {
        if (next.auto_start_proxy) {
          await api.setAutostart(true);
        } else {
          await api.removeAutostart();
        }
      }
      const settings = await api.saveSettings(next);
      setRuntime((currentRuntime) => ({ ...currentRuntime, settings }));
      setBanner("Settings saved");
      await loadRuntime();
    } catch (err) {
      setBanner(messageFromError(err));
      throw err;
    } finally {
      setBusy(null);
    }
  }

  async function syncHistory() {
    setBusy("sync");
    try {
      setBanner(await api.syncHistory());
    } catch (err) {
      setBanner(messageFromError(err));
      throw err;
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="grid h-screen min-h-[620px] grid-rows-[auto_auto_minmax(0,1fr)] bg-panel text-ink">
      <RuntimeBar
        busy={busy}
        exportedCount={exportedCount}
        providerSourceCount={providerSourceCount}
        settings={runtime.settings}
        status={runtime.status}
        onOpenSettings={() => setSettingsOpen(true)}
        onStart={() => void runRuntimeAction("start", api.startProxy)}
        onStop={() => void runRuntimeAction("stop", api.stopProxy)}
        onSwitchMode={(mode) => void switchMode(mode)}
      />

      <nav className="flex min-h-[45px] items-center gap-1 border-b border-line bg-white px-4">
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
            {tab.id === "gateway" && exportedCount > 0 && (
              <span className="ml-2 rounded-full border border-line bg-panel px-1.5 py-0.5 text-[11px] text-slate-500">
                {exportedCount}
              </span>
            )}
            {activeTab === tab.id && (
              <span className="absolute inset-x-3 bottom-0 h-0.5 rounded-full bg-ink" />
            )}
          </button>
        ))}
        <span className="ml-auto hidden truncate text-xs text-slate-400 lg:block">
          Gateway is the local OpenAI-compatible server in front of Hub
        </span>
      </nav>

      <div className="min-h-0 overflow-auto p-4">
        {banner && (
          <div className="mb-3 rounded-md border border-line bg-white px-3 py-2 text-sm text-slate-700 shadow-subtle">
            {banner}
          </div>
        )}
        {activeTab === "codexhub" ? (
          <ProvidersPage />
        ) : (
          <GatewayPage
            settings={runtime.settings}
            status={runtime.gatewayStatus}
            busy={busy}
            pending={contract.pendingBackend}
            clients={contract.gatewayClients as GatewayClientContract[]}
            onApplySettings={saveSettings}
            onRefresh={loadRuntime}
            onRestartProxy={() => runRuntimeAction("restart", api.restartProxy)}
          />
        )}
      </div>

      <SettingsDrawer
        busy={busy}
        open={settingsOpen}
        settings={runtime.settings}
        onClose={() => setSettingsOpen(false)}
        onSave={saveSettings}
        onSyncHistory={syncHistory}
      />
      {settingsOpen && (
        <button
          type="button"
          className="fixed inset-0 z-40 cursor-default bg-black/10"
          aria-label="Close settings"
          onClick={() => setSettingsOpen(false)}
        />
      )}
    </div>
  );
}
