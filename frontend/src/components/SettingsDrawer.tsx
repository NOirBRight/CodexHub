import { RefreshCcw, Save, X } from "lucide-react";
import { useEffect, useState } from "react";
import { cx } from "../lib/format";
import type { Settings } from "../lib/types";
import { PendingPanel } from "./PendingPanel";

interface SettingsDrawerProps {
  busy?: string | null;
  open: boolean;
  settings: Settings | null;
  onClose: () => void;
  onSave: (settings: Settings) => Promise<void>;
  onSyncHistory: () => Promise<string>;
}

export function SettingsDrawer({
  busy,
  onClose,
  onSave,
  onSyncHistory,
  open,
  settings,
}: SettingsDrawerProps) {
  const [draft, setDraft] = useState<Settings | null>(settings);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    setDraft(settings);
    setMessage(null);
  }, [settings, open]);

  async function saveDraft() {
    if (!draft) {
      return;
    }
    await onSave(draft);
    setMessage("Settings saved");
  }

  async function syncHistory() {
    setMessage(await onSyncHistory());
  }

  return (
    <aside
      className={cx(
        "fixed inset-y-0 right-0 z-50 grid w-full max-w-[420px] grid-rows-[auto_minmax(0,1fr)_auto] border-l border-line bg-white shadow-2xl transition-transform",
        open ? "translate-x-0" : "translate-x-full",
      )}
      aria-hidden={!open}
    >
      <div className="flex items-center justify-between gap-3 border-b border-line px-5 py-4">
        <div>
          <div className="text-xs font-semibold uppercase tracking-[0.06em] text-slate-500">
            Settings
          </div>
          <h2 className="text-base font-semibold text-ink">CodexHub &amp; Gateway</h2>
        </div>
        <button
          type="button"
          className="focus-ring grid h-8 w-8 place-items-center rounded-md border border-line bg-panel text-slate-600 hover:bg-slate-100"
          onClick={onClose}
          title="Close settings"
        >
          <X size={16} />
        </button>
      </div>

      <div className="min-h-0 overflow-auto p-5">
        {!draft ? (
          <div className="rounded-md border border-line bg-panel p-4 text-sm text-slate-500">
            Loading settings
          </div>
        ) : (
          <div className="grid gap-5">
            <section className="grid gap-3">
              <h3 className="text-sm font-semibold text-ink">CodexHub</h3>
              <div className="grid gap-3 rounded-md border border-line bg-panel p-3">
                <Toggle
                  checked={draft.include_official_models}
                  label="Include official models"
                  onChange={(value) => setDraft({ ...draft, include_official_models: value })}
                />
                <Toggle
                  checked={draft.auto_sync_history}
                  label="Auto-sync history"
                  onChange={(value) => setDraft({ ...draft, auto_sync_history: value })}
                />
                <Toggle
                  checked={draft.auto_sync_clients}
                  label="Auto-sync bound clients"
                  onChange={(value) => setDraft({ ...draft, auto_sync_clients: value })}
                />
              </div>
            </section>

            <section className="grid gap-3">
              <h3 className="text-sm font-semibold text-ink">Gateway</h3>
              <div className="grid gap-3 rounded-md border border-line bg-panel p-3">
                <label className="grid gap-1 text-sm font-medium text-slate-700">
                  Bind address
                  <input
                    className="field h-9"
                    value={draft.gateway_bind_address}
                    disabled
                    onChange={() => undefined}
                  />
                  <span className="text-xs font-normal text-slate-500">
                    Local-only binding is enforced for this release.
                  </span>
                </label>
                <label className="grid gap-1 text-sm font-medium text-slate-700">
                  Port
                  <input
                    className="field h-9"
                    type="number"
                    min={1024}
                    max={65535}
                    value={draft.proxy_port}
                    onChange={(event) => setDraft({ ...draft, proxy_port: Number(event.target.value) })}
                  />
                </label>
                <label className="grid gap-1 text-sm font-medium text-slate-700">
                  Local client key
                  <input
                    className="field h-9"
                    type="text"
                    autoComplete="off"
                    value={draft.gateway_client_key}
                    onChange={(event) => setDraft({ ...draft, gateway_client_key: event.target.value })}
                  />
                  <span className="text-xs font-normal text-slate-500">
                    Local compatibility key only; not an upstream provider or OpenAI key.
                  </span>
                </label>
                <Toggle
                  checked={draft.auto_start_proxy}
                  label="Auto-start runtime"
                  onChange={(value) => setDraft({ ...draft, auto_start_proxy: value })}
                />
              </div>
              <PendingPanel
                compact
                label="partial support"
                title="Client adapters"
                message="OpenCode, ZCode, Pi, and OMP can switch between official routing and CodexHub-managed Gateway config with backups where native files are overwritten."
              />
            </section>
          </div>
        )}
      </div>

      <div className="border-t border-line px-5 py-4">
        {message && <div className="mb-3 text-sm text-slate-600">{message}</div>}
        <div className="flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold text-slate-700 hover:bg-slate-100"
            disabled={Boolean(busy)}
            onClick={() => void syncHistory()}
          >
            <RefreshCcw size={15} />
            Sync history
          </button>
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-ink px-3 text-sm font-semibold text-white disabled:bg-slate-300"
            disabled={Boolean(busy) || !draft}
            onClick={() => void saveDraft()}
          >
            <Save size={15} />
            Save
          </button>
        </div>
      </div>
    </aside>
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
    <label className="flex min-h-9 items-center justify-between gap-4 rounded-md border border-line bg-white px-3 py-2 text-sm font-medium text-slate-700">
      <span className="min-w-0 truncate">{label}</span>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    </label>
  );
}
