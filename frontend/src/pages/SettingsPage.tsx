import { RefreshCcw, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { api, messageFromError } from "../lib/tauri";
import type { Settings } from "../lib/types";

export function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    void load();
  }, []);

  async function load() {
    setBusy("load");
    try {
      setSettings(await api.getSettings());
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function save(next: Settings) {
    setBusy("save");
    try {
      const saved = await api.saveSettings(next);
      setSettings(saved);
      setMessage("Settings saved");
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function toggleAutostart(enabled: boolean) {
    if (!settings) {
      return;
    }
    setBusy("autostart");
    try {
      if (enabled) {
        await api.setAutostart(true);
      } else {
        await api.removeAutostart();
      }
      await save({ ...settings, auto_start_proxy: enabled });
    } catch (err) {
      setError(messageFromError(err));
      setBusy(null);
    }
  }

  async function syncNow() {
    setBusy("sync");
    try {
      setMessage(await api.syncHistory());
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  if (!settings) {
    return (
      <main className="rounded-md border border-line bg-white p-6 text-sm text-slate-500">
        Loading settings
      </main>
    );
  }

  return (
    <main className="grid gap-4">
      <section className="rounded-md border border-line bg-white p-4 shadow-subtle">
        <div className="grid gap-4 md:grid-cols-2">
          <Toggle
            label="Auto-sync history"
            checked={settings.auto_sync_history}
            onChange={(value) => void save({ ...settings, auto_sync_history: value })}
          />
          <Toggle
            label="Auto-start proxy"
            checked={settings.auto_start_proxy}
            onChange={(value) => void toggleAutostart(value)}
          />
          <Toggle
            label="Include official models"
            checked={settings.include_official_models}
            onChange={(value) => void save({ ...settings, include_official_models: value })}
          />
          <Toggle
            label="Auto-sync catalog"
            checked={settings.auto_sync_catalog}
            onChange={(value) => void save({ ...settings, auto_sync_catalog: value })}
          />
          <Toggle
            label="Enable /v1/models"
            checked={settings.gateway_enable_models}
            onChange={(value) => void save({ ...settings, gateway_enable_models: value })}
          />
          <Toggle
            label="Enable Responses"
            checked={settings.gateway_enable_responses}
            onChange={(value) => void save({ ...settings, gateway_enable_responses: value })}
          />
          <Toggle
            label="Enable Chat Completions"
            checked={settings.gateway_enable_chat_completions}
            onChange={(value) => void save({ ...settings, gateway_enable_chat_completions: value })}
          />
          <label className="grid gap-1 text-sm font-medium text-slate-700">
            Proxy port
            <input
              className="field max-w-[180px]"
              type="number"
              min={1024}
              max={65535}
              value={settings.proxy_port}
              onChange={(event) =>
                setSettings({ ...settings, proxy_port: Number(event.target.value) })
              }
            />
          </label>
          <label className="grid gap-1 text-sm font-medium text-slate-700">
            Default Codex route
            <select
              className="field max-w-[220px]"
              value={settings.default_codex_route}
              onChange={(event) =>
                setSettings({ ...settings, default_codex_route: event.target.value })
              }
            >
              <option value="hub">Hub</option>
              <option value="official">Official</option>
            </select>
          </label>
          <label className="grid gap-1 text-sm font-medium text-slate-700">
            Bind address
            <input
              className="field max-w-[220px]"
              value={settings.gateway_bind_address}
              onChange={(event) =>
                setSettings({ ...settings, gateway_bind_address: event.target.value })
              }
            />
            <span className="text-xs font-normal text-slate-500">Only 127.0.0.1 is accepted in this release.</span>
          </label>
          <label className="grid gap-1 text-sm font-medium text-slate-700">
            Local client key
            <input
              className="field max-w-[260px]"
              value={settings.gateway_client_key}
              onChange={(event) =>
                setSettings({ ...settings, gateway_client_key: event.target.value })
              }
            />
            <span className="text-xs font-normal text-slate-500">Compatibility key for local clients, not an upstream secret.</span>
          </label>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="focus-ring inline-flex h-10 items-center gap-2 rounded-md bg-action px-3 text-sm font-semibold text-white"
            disabled={Boolean(busy)}
            onClick={() => void save(settings)}
          >
            <Save size={16} />
            Save
          </button>
          <button
            type="button"
            className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold"
            disabled={busy === "sync"}
            onClick={() => void syncNow()}
          >
            <RefreshCcw size={16} />
            Sync now
          </button>
        </div>
      </section>

      {(error || message) && (
        <section className="rounded-md border border-line bg-white p-4 text-sm shadow-subtle">
          {error ? <span className="text-danger">{error}</span> : <span>{message}</span>}
        </section>
      )}
    </main>
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
    <label className="flex items-center justify-between gap-4 rounded-md border border-line bg-panel px-3 py-3 text-sm font-medium">
      <span>{label}</span>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    </label>
  );
}
