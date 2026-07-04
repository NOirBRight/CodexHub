import { History, Save, X } from "lucide-react";
import { useEffect, useState } from "react";
import { cx } from "../lib/format";
import { messageFromError } from "../lib/tauri";
import type { Model, Settings } from "../lib/types";
import { useToasts } from "./PageToast";

interface SettingsDrawerProps {
  busy?: string | null;
  open: boolean;
  settings: Settings | null;
  visionModels: Model[];
  onClose: () => void;
  onSave: (settings: Settings) => Promise<string | void>;
  onSyncHistory: (targetProvider: string) => Promise<string>;
}

export function SettingsDrawer({
  busy,
  onClose,
  onSave,
  onSyncHistory,
  open,
  settings,
  visionModels,
}: SettingsDrawerProps) {
  const { showToast, updateToast } = useToasts();
  const [draft, setDraft] = useState<Settings | null>(settings);
  const [historyBusy, setHistoryBusy] = useState(false);

  useEffect(() => {
    setDraft(settings);
    setHistoryBusy(false);
  }, [settings, open]);

  async function saveDraft() {
    if (!draft) {
      return;
    }
    const toastId = showToast("Saving settings...", "loading");
    try {
      const savedMessage = await onSave(draft);
      updateToast(toastId, {
        action: null,
        text: savedMessage ?? "Settings saved",
        tone: "success",
      });
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
    }
  }

  async function repairHistory() {
    if (!draft) {
      return;
    }
    const toastId = showToast("Repairing history bucket...", "loading");
    const targetProvider = draft.unified_codex_history ? "custom" : "openai";
    try {
      const message = await onSyncHistory(targetProvider);
      updateToast(toastId, {
        action: null,
        text: message,
        tone: "success",
      });
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
    }
  }

  async function toggleUnifiedHistory(enabled: boolean) {
    if (!draft || historyBusy) {
      return;
    }
    const previous = draft;
    const next = { ...draft, unified_codex_history: enabled };
    const toastId = showToast(
      enabled ? "Enabling unified Codex history..." : "Restoring official Codex history...",
      "loading",
    );
    setDraft(next);
    setHistoryBusy(true);
    try {
      const savedMessage = await onSave(next);
      updateToast(toastId, {
        action: null,
        text: savedMessage ?? (enabled ? "Unified Codex history enabled" : "Official Codex history restored"),
        tone: "success",
      });
    } catch (err) {
      setDraft(previous);
      updateToast(toastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
    } finally {
      setHistoryBusy(false);
    }
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
                  disabled={historyBusy || Boolean(busy)}
                  label="Unified Codex history"
                  onChange={(value) => void toggleUnifiedHistory(value)}
                />
                <button
                  type="button"
                  className="focus-ring inline-flex h-9 items-center justify-start gap-2 rounded-control bg-surface px-3 text-sm font-semibold text-slate-700 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
                  disabled={Boolean(busy) || historyBusy}
                  onClick={() => void repairHistory()}
                >
                  <History size={15} />
                  Repair history bucket
                </button>
                <Toggle
                  checked={draft.auto_sync_clients}
                  label="Auto-sync bound clients"
                  onChange={(value) => setDraft({ ...draft, auto_sync_clients: value })}
                />
              </div>
            </section>

            <section className="grid gap-3">
              <SectionHeaderToggle
                checked={draft.gateway_auto_retry_enabled}
                disabled={Boolean(busy)}
                title="Auto retry"
                onChange={(value) => setDraft({ ...draft, gateway_auto_retry_enabled: value })}
              />
              <div className="grid gap-3 rounded-panel bg-panel p-3 shadow-card">
                <label className="grid gap-1.5 text-xs font-semibold text-slate-600">
                  <span>Max attempts</span>
                  <input
                    className="field h-9"
                    type="number"
                    min={1}
                    max={30}
                    value={draft.gateway_auto_retry_max_attempts}
                    disabled={!draft.gateway_auto_retry_enabled}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        gateway_auto_retry_max_attempts: clampRetryAttempts(event.target.value),
                      })
                    }
                  />
                </label>
              </div>
            </section>

            <section className="grid gap-3">
              <SectionHeaderToggle
                checked={draft.gateway_image_proxy_enabled}
                disabled={Boolean(busy)}
                title="Image proxy"
                onChange={(value) => setDraft({ ...draft, gateway_image_proxy_enabled: value })}
              />
              <div className="grid gap-3 rounded-panel bg-panel p-3 shadow-card">
                <label className="grid gap-1.5 text-xs font-semibold text-slate-600">
                  <span>Vision model</span>
                  <select
                    className="field h-9"
                    value={draft.gateway_image_proxy_model}
                    disabled={!draft.gateway_image_proxy_enabled || visionModels.length === 0}
                    onChange={(event) => setDraft({ ...draft, gateway_image_proxy_model: event.target.value })}
                  >
                    <option value="">
                      {visionModels.length === 0 ? "No vision-capable models" : "Select a vision model"}
                    </option>
                    {visionModels.map((model) => (
                      <option key={model.id} value={model.id}>
                        {visionModelLabel(model)}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            </section>
          </div>
        )}
      </div>

      <div className="px-5 py-4 shadow-[0_-1px_0_rgba(31,41,51,0.06)]">
        <div className="flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-control bg-ink px-3 text-sm font-semibold text-white shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-slate-800 hover:shadow-raised active:scale-[0.96] disabled:bg-slate-300"
            disabled={Boolean(busy) || historyBusy || !draft}
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

function clampRetryAttempts(value: string) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return 30;
  }
  return Math.max(1, Math.min(30, Math.round(parsed)));
}

function visionModelLabel(model: Model) {
  const name = model.display_name?.trim();
  return name && name !== model.id ? `${name} (${model.id})` : model.id;
}

function Toggle({
  checked,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex min-h-9 items-center justify-between gap-4 rounded-inner bg-surface px-3 py-2 text-sm font-medium text-slate-700 shadow-control">
      <span className="min-w-0 truncate">{label}</span>
      <SwitchControl checked={checked} disabled={disabled} onChange={onChange} />
    </label>
  );
}

function SectionHeaderToggle({
  checked,
  disabled,
  onChange,
  title,
}: {
  checked: boolean;
  disabled?: boolean;
  onChange: (value: boolean) => void;
  title: string;
}) {
  return (
    <label className="flex min-h-9 items-center justify-between gap-3">
      <h3 className="min-w-0 truncate text-sm font-semibold text-ink">{title}</h3>
      <SwitchControl checked={checked} disabled={disabled} onChange={onChange} ariaLabel={title} />
    </label>
  );
}

function SwitchControl({
  ariaLabel,
  checked,
  disabled,
  onChange,
}: {
  ariaLabel?: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <span className="relative inline-flex h-5 w-9 shrink-0 items-center">
      <input
        type="checkbox"
        className="peer sr-only"
        aria-label={ariaLabel}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="absolute inset-0 rounded-full bg-slate-200 shadow-control transition-colors peer-checked:bg-action peer-disabled:opacity-60" />
      <span className="absolute left-0.5 h-4 w-4 rounded-full bg-white shadow-sm transition-transform peer-checked:translate-x-4 peer-disabled:opacity-80" />
    </span>
  );
}
