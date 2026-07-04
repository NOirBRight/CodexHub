import { Eye, EyeOff, History, Save, X } from "lucide-react";
import { useEffect, useState } from "react";
import { cx } from "../lib/format";
import type { Settings } from "../lib/types";

interface SettingsDrawerProps {
  busy?: string | null;
  open: boolean;
  settings: Settings | null;
  onClose: () => void;
  onSave: (settings: Settings) => Promise<void>;
  onSyncHistory: (targetProvider: string) => Promise<string>;
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
  const [showClientKey, setShowClientKey] = useState(false);

  useEffect(() => {
    setDraft(settings);
    setMessage(null);
    setShowClientKey(false);
  }, [settings, open]);

  async function saveDraft() {
    if (!draft) {
      return;
    }
    await onSave(draft);
    setMessage("Settings saved");
  }

  async function repairHistory() {
    if (!draft) {
      return;
    }
    const targetProvider = draft.unified_codex_history ? "custom" : "openai";
    setMessage(await onSyncHistory(targetProvider));
  }

  return (
    <aside
      className={cx(
        "fixed inset-y-0 right-0 z-50 grid w-full max-w-[420px] grid-rows-[auto_minmax(0,1fr)_auto] rounded-l-overlay bg-surface shadow-overlay transition-transform",
        open ? "translate-x-0" : "translate-x-full",
      )}
      aria-hidden={!open}
    >
      <div className="flex items-center justify-between gap-3 px-5 py-4 shadow-hairline">
        <div>
          <div className="text-xs font-semibold uppercase tracking-[0.06em] text-slate-500">
            Settings
          </div>
          <h2 className="text-base font-semibold text-ink">CodexHub &amp; Gateway</h2>
        </div>
        <button
          type="button"
          className="focus-ring grid h-8 w-8 place-items-center rounded-control bg-panel text-slate-600 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
          onClick={onClose}
          title="Close settings"
        >
          <X size={16} />
        </button>
      </div>

      <div className="min-h-0 overflow-auto p-5">
        {!draft ? (
          <div className="rounded-panel bg-panel p-4 text-sm text-slate-500 shadow-card">
            Loading settings
          </div>
        ) : (
          <div className="grid gap-5">
            <section className="grid gap-3">
              <h3 className="text-sm font-semibold text-ink">CodexHub</h3>
              <div className="grid gap-3 rounded-panel bg-panel p-3 shadow-card">
                <Toggle
                  checked={draft.include_official_models}
                  label="Include official models"
                  onChange={(value) => setDraft({ ...draft, include_official_models: value })}
                />
                <Toggle
                  checked={draft.unified_codex_history}
                  label="Unified Codex history"
                  onChange={(value) => setDraft({ ...draft, unified_codex_history: value })}
                />
                <Toggle
                  checked={draft.auto_sync_clients}
                  label="Auto-sync bound clients"
                  onChange={(value) => setDraft({ ...draft, auto_sync_clients: value })}
                />
              </div>
            </section>

            <section className="grid gap-3">
              <h3 className="text-sm font-semibold text-ink">Maintenance</h3>
              <div className="grid gap-2 rounded-panel bg-panel p-3 shadow-card">
                <button
                  type="button"
                  className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-control bg-surface px-3 text-sm font-semibold text-slate-700 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
                  disabled={Boolean(busy)}
                  onClick={() => void repairHistory()}
                >
                  <History size={15} />
                  Repair history bucket
                </button>
              </div>
            </section>

            <section className="grid gap-3">
              <h3 className="text-sm font-semibold text-ink">Gateway</h3>
              <div className="grid gap-3 rounded-panel bg-panel p-3 shadow-card">
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
                <div className="grid gap-1 text-sm font-medium text-slate-700">
                  <label htmlFor="settings-local-client-key">Local client key</label>
                  <div className="relative min-w-0">
                    <input
                      id="settings-local-client-key"
                      className="field h-9 w-full pr-9"
                      type={showClientKey ? "text" : "password"}
                      autoComplete="off"
                      value={draft.gateway_client_key}
                      onChange={(event) => setDraft({ ...draft, gateway_client_key: event.target.value })}
                    />
                    <button
                      type="button"
                      className="focus-ring absolute right-1.5 top-1/2 grid h-6 w-6 -translate-y-1/2 place-items-center rounded-control text-slate-500 transition-colors hover:bg-panel hover:text-ink"
                      aria-label={showClientKey ? "Hide local client key" : "Show local client key"}
                      title={showClientKey ? "Hide local client key" : "Show local client key"}
                      onClick={() => setShowClientKey((value) => !value)}
                    >
                      {showClientKey ? <EyeOff size={14} /> : <Eye size={14} />}
                    </button>
                  </div>
                  <span className="text-xs font-normal text-slate-500">
                    Local compatibility key only; not an upstream provider or OpenAI key.
                  </span>
                </div>
                <Toggle
                  checked={draft.auto_start_proxy}
                  label="Auto-start runtime"
                  onChange={(value) => setDraft({ ...draft, auto_start_proxy: value })}
                />
              </div>
            </section>
          </div>
        )}
      </div>

      <div className="px-5 py-4 shadow-[0_-1px_0_rgba(31,41,51,0.06)]">
        {message && <div className="mb-3 text-sm text-slate-600">{message}</div>}
        <div className="flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-control bg-ink px-3 text-sm font-semibold text-white shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-slate-800 hover:shadow-raised active:scale-[0.96] disabled:bg-slate-300"
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
    <label className="flex min-h-9 cursor-pointer items-center justify-between gap-4 rounded-inner bg-surface px-3 py-2 text-sm font-medium text-slate-700 shadow-control">
      <span className="min-w-0 truncate">{label}</span>
      <span className="relative inline-flex h-5 w-9 shrink-0 items-center">
        <input
          type="checkbox"
          className="peer sr-only"
          checked={checked}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span className="absolute inset-0 rounded-full bg-slate-200 shadow-control transition-colors peer-checked:bg-action" />
        <span className="absolute left-0.5 h-4 w-4 rounded-full bg-white shadow-sm transition-transform peer-checked:translate-x-4" />
      </span>
    </label>
  );
}
