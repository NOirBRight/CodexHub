import { Check, ChevronDown, Download, RefreshCcw, Save, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { changeAppLocale, type AppLocale } from "../i18n";
import { cx } from "../lib/format";
import { messageFromError } from "../lib/tauri";
import type {
  AppUpdateInstallStatus,
  AppUpdateStatus,
  AppVersionInfo,
  Model,
  Provider,
  Settings,
} from "../lib/types";
import { useToasts } from "./PageToast";
import { SegmentedSwitch, type SegmentedOption } from "./SegmentedSwitch";

interface SettingsDrawerProps {
  appVersion: AppVersionInfo | null;
  busy?: string | null;
  open: boolean;
  providers: Provider[];
  settings: Settings | null;
  updateBusy: "check" | null;
  updateInstallStatus: AppUpdateInstallStatus | null;
  updateStatus: AppUpdateStatus | null;
  visionModels: Model[];
  onCheckUpdate: () => Promise<AppUpdateStatus | null>;
  onClose: () => void;
  onInstallUpdate: () => Promise<void>;
  onSave: (settings: Settings) => Promise<string | void>;
  onSyncHistory: (targetProvider: string) => Promise<string>;
}

export function SettingsDrawer({
  appVersion,
  busy,
  onCheckUpdate,
  onClose,
  onInstallUpdate,
  onSave,
  onSyncHistory,
  open,
  providers,
  settings,
  updateBusy,
  updateInstallStatus,
  updateStatus,
  visionModels,
}: SettingsDrawerProps) {
  const { t } = useTranslation();
  const { showToast, updateToast } = useToasts();
  const [draft, setDraft] = useState<Settings | null>(settings);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [closePromptOpen, setClosePromptOpen] = useState(false);
  const hasUnsavedChanges = Boolean(settings && draft && settingsSaveComparable(settings) !== settingsSaveComparable(draft));

  useEffect(() => {
    if (!open) {
      setDraft(settings);
      setHistoryBusy(false);
      setClosePromptOpen(false);
      return;
    }
    setDraft((current) => current ?? settings);
  }, [settings, open]);

  useEffect(() => {
    if (open) {
      setHistoryBusy(false);
    }
  }, [open]);

  async function saveDraft(options?: { closeOnSuccess?: boolean }) {
    if (!draft) {
      return;
    }
    const toastId = showToast(t("settings.savingSettings", { defaultValue: "Saving settings..." }), "loading");
    try {
      const savedMessage = await onSave(draft);
      updateToast(toastId, {
        action: null,
        text: savedMessage ?? t("settings.settingsSaved"),
        tone: "success",
      });
      setClosePromptOpen(false);
      if (options?.closeOnSuccess) {
        onClose();
      }
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
    const toastId = showToast(t("settings.repairingHistoryBucket"), "loading");
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
      enabled ? t("settings.enablingUnifiedHistory") : t("settings.restoringOfficialHistory"),
      "loading",
    );
    setDraft(next);
    setHistoryBusy(true);
    try {
      const savedMessage = await onSave(next);
      updateToast(toastId, {
        action: null,
        text: savedMessage ?? (enabled ? t("settings.unifiedHistoryEnabled") : t("settings.officialHistoryRestored")),
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

  async function changeLanguage(locale: AppLocale) {
    if (!draft || draft.locale === locale) {
      return;
    }
    const previous = draft;
    const next = { ...draft, locale };
    const toastId = showToast(t("settings.languageSaving"), "loading");
    setDraft(next);
    await changeAppLocale(locale);
    try {
      const savedMessage = await onSave(next);
      updateToast(toastId, {
        action: null,
        text: savedMessage ?? t("settings.languageSaved"),
        tone: "success",
      });
    } catch (err) {
      setDraft(previous);
      await changeAppLocale(previous.locale);
      updateToast(toastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
    }
  }

  function requestClose() {
    if (!hasUnsavedChanges) {
      onClose();
      return;
    }
    setClosePromptOpen(true);
  }

  function discardAndClose() {
    setDraft(settings);
    setClosePromptOpen(false);
    onClose();
  }

  const languageOptions: Array<SegmentedOption<AppLocale>> = [
    { value: "zh-CN", label: t("settings.languageChinese") },
    { value: "en-US", label: t("settings.languageEnglish") },
  ];

  return (
    <>
      {open && (
        <button
          type="button"
          className="fixed inset-0 z-40 cursor-default bg-black/10 backdrop-blur-[1px]"
          aria-label={t("common.closeSettings")}
          onClick={requestClose}
        />
      )}
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
            {t("common.settings")}
          </div>
          <h2 className="text-base font-semibold text-ink">{t("settings.codexHubGateway")}</h2>
        </div>
        <button
          type="button"
          className="focus-ring grid h-8 w-8 place-items-center rounded-control bg-panel text-slate-600 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
          onClick={requestClose}
          title={t("common.closeSettings")}
        >
          <X size={16} />
        </button>
      </div>

      <div className="min-h-0 overflow-auto p-5">
        {!draft ? (
          <div className="rounded-panel bg-panel p-4 text-sm text-slate-500 shadow-card">
            {t("common.loadingSettings")}
          </div>
        ) : (
          <div className="grid gap-5">
            <section className="grid gap-3">
              <h3 className="text-sm font-semibold text-ink">CodexHub</h3>
              <div className="grid gap-3 rounded-panel bg-panel p-3 shadow-card">
                <div className="grid gap-1 rounded-inner bg-surface px-3 py-2 text-sm font-medium text-slate-700 shadow-control">
                  <span className="text-xs font-semibold text-slate-500">{t("settings.language")}</span>
                  <SegmentedSwitch
                    ariaLabel={t("settings.language")}
                    className="grid-cols-2"
                    disabled={Boolean(busy)}
                    value={draft.locale}
                    options={languageOptions}
                    onChange={(value) => void changeLanguage(value)}
                  />
                </div>
                <Toggle
                  checked={draft.auto_start_software}
                  label={t("settings.autoStartSoftware")}
                  onChange={(value) => setDraft({ ...draft, auto_start_software: value })}
                />
                <Toggle
                  checked={draft.auto_start_gateway}
                  label={t("settings.autoStartGateway")}
                  onChange={(value) => setDraft({ ...draft, auto_start_gateway: value })}
                />
                <Toggle
                  checked={draft.include_official_models}
                  label={t("settings.includeOfficialModels")}
                  onChange={(value) => setDraft({ ...draft, include_official_models: value })}
                />
                <Toggle
                  checked={draft.unified_codex_history}
                  disabled={historyBusy || Boolean(busy)}
                  label={t("settings.unifiedCodexHistory")}
                  onChange={(value) => void toggleUnifiedHistory(value)}
                />
                <Toggle
                  checked={draft.auto_sync_clients}
                  label={t("settings.autoSyncBoundClients")}
                  onChange={(value) => setDraft({ ...draft, auto_sync_clients: value })}
                />
                <button
                  type="button"
                  className="focus-ring inline-flex h-9 items-center justify-start rounded-control bg-surface px-3 text-sm font-medium text-slate-700 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
                  disabled={Boolean(busy) || historyBusy}
                  onClick={() => void repairHistory()}
                >
                  {t("settings.repairHistoryBucket")}
                </button>
              </div>
            </section>

            <section className="grid gap-3">
              <h3 className="text-sm font-semibold text-ink">{t("settings.autoRetry")}</h3>
              <div className="grid gap-3 rounded-panel bg-panel p-3 shadow-card">
                <Toggle
                  checked={draft.gateway_auto_retry_enabled}
                  disabled={Boolean(busy)}
                  label={t("common.enabled")}
                  onChange={(value) => setDraft({ ...draft, gateway_auto_retry_enabled: value })}
                />
                <label className="grid min-h-9 min-w-0 grid-cols-[minmax(0,1fr)_36px] items-center gap-3 rounded-inner bg-surface px-3 py-1.5 text-sm font-medium text-slate-700 shadow-control">
                  <span className="min-w-0 truncate">{t("settings.maxAttempts")}</span>
                  <input
                    className="h-6 w-9 min-w-0 rounded-control border border-transparent bg-transparent px-0 text-center text-sm font-semibold tabular-nums text-ink shadow-none outline-none transition-[box-shadow,border-color,background-color] duration-150 ease-out [appearance:textfield] focus:border-action/40 focus:bg-surface focus:shadow-field disabled:text-slate-400 [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
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
              <h3 className="text-sm font-semibold text-ink">{t("settings.imageProxy")}</h3>
              <div className="grid gap-3 rounded-panel bg-panel p-3 shadow-card">
                <Toggle
                  checked={draft.gateway_image_proxy_enabled}
                  disabled={Boolean(busy)}
                  label={t("common.enabled")}
                  onChange={(value) => setDraft({ ...draft, gateway_image_proxy_enabled: value })}
                />
                <div className="relative grid min-h-9 min-w-0 grid-cols-[minmax(0,1fr)_minmax(0,190px)] items-center gap-3 rounded-inner bg-surface px-3 py-1 text-sm font-medium text-slate-700 shadow-control">
                  <span className="min-w-0 truncate">{t("settings.visionModel")}</span>
                  <VisionModelSelect
                    models={visionModels}
                    providers={providers}
                    value={draft.gateway_image_proxy_model}
                    disabled={!draft.gateway_image_proxy_enabled || visionModels.length === 0}
                    onChange={(value) => setDraft({ ...draft, gateway_image_proxy_model: value })}
                  />
                </div>
              </div>
            </section>

            <section className="grid gap-3">
              <h3 className="text-sm font-semibold text-ink">{t("settings.updates")}</h3>
              <VersionUpdateBlock
                busy={updateBusy}
                installStatus={updateInstallStatus}
                status={updateStatus}
                versionInfo={appVersion}
                onCheck={() => void onCheckUpdate()}
                onInstall={() => void onInstallUpdate()}
              />
            </section>
          </div>
        )}
      </div>

      <div className="px-5 py-4 shadow-[0_-1px_0_rgba(31,41,51,0.06)]">
        <div className="flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-control bg-ink px-3 text-sm font-semibold text-white shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-slate-800 hover:shadow-raised active:scale-[0.96] disabled:bg-slate-300"
            disabled={Boolean(busy) || historyBusy || !draft || !hasUnsavedChanges}
            onClick={() => void saveDraft()}
          >
            <Save size={15} />
            {t("common.save")}
          </button>
        </div>
      </div>
    </aside>
      {closePromptOpen && (
        <div className="fixed inset-0 z-[90] grid place-items-center bg-black/20 px-4">
          <div className="grid w-full max-w-[360px] gap-4 rounded-overlay bg-surface p-4 shadow-overlay">
            <div>
              <h3 className="text-base font-semibold text-ink">{t("settings.unsavedChangesTitle")}</h3>
              <p className="mt-1 text-sm leading-5 text-slate-500">{t("settings.unsavedChangesBody")}</p>
            </div>
            <div className="flex flex-wrap justify-end gap-2">
              <button
                type="button"
                className="mini-button"
                onClick={() => setClosePromptOpen(false)}
              >
                {t("common.cancel")}
              </button>
              <button
                type="button"
                className="mini-button"
                onClick={discardAndClose}
              >
                {t("settings.discardUnsavedChanges")}
              </button>
              <button
                type="button"
                className="focus-ring inline-flex h-8 items-center justify-center gap-2 rounded-control bg-ink px-3 text-xs font-semibold text-white shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-slate-800 hover:shadow-raised active:scale-[0.96] disabled:bg-slate-300"
                disabled={Boolean(busy) || historyBusy || !draft}
                onClick={() => void saveDraft({ closeOnSuccess: true })}
              >
                <Save size={14} />
                {t("common.save")}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function settingsSaveComparable(settings: Settings) {
  const { locale: _locale, unified_codex_history: _unifiedHistory, ...saveManagedSettings } = settings;
  return JSON.stringify(saveManagedSettings);
}

function VersionUpdateBlock({
  busy,
  installStatus,
  onCheck,
  onInstall,
  status,
  versionInfo,
}: {
  busy: "check" | null;
  installStatus: AppUpdateInstallStatus | null;
  onCheck: () => void;
  onInstall: () => void;
  status: AppUpdateStatus | null;
  versionInfo: AppVersionInfo | null;
}) {
  const { i18n, t } = useTranslation();
  const rawCurrentVersion = status?.current_version ?? versionInfo?.current_version ?? null;
  const currentVersion = rawCurrentVersion ? `v${rawCurrentVersion}` : t("common.unknown");
  const latestVersion = status?.latest_version ?? null;
  const updateAvailable = Boolean(status?.available && latestVersion);
  const releaseNotes = status?.notes?.trim() || t("settings.noReleaseNotes");
  const releaseDate = formatUpdateDate(status?.date, i18n.language);
  const installActive = isUpdateInstallActive(installStatus);

  return (
    <div className="grid gap-3 rounded-panel bg-panel p-3 shadow-card">
      <div className="grid min-h-9 min-w-0 grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-2 rounded-inner bg-surface px-3 py-2 text-sm font-medium text-slate-700 shadow-control">
        <span className="min-w-0 truncate text-xs font-semibold text-slate-500">
          {t("settings.currentVersion")}
        </span>
        <span
          className={cx(
            "shrink-0 rounded-full bg-panel px-2 py-0.5 text-[11px] font-semibold text-slate-600",
            rawCurrentVersion ? "font-mono tabular-nums" : "",
          )}
        >
          {currentVersion}
        </span>
        <button
          type="button"
          className="focus-ring inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-control bg-panel text-slate-600 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:text-ink hover:shadow-raised active:scale-[0.96] disabled:text-slate-400"
          aria-label={t("settings.checkForUpdates")}
          title={t("settings.checkForUpdates")}
          disabled={Boolean(busy) || installActive}
          onClick={onCheck}
        >
          <RefreshCcw size={14} className={busy === "check" ? "animate-spin" : ""} />
        </button>
      </div>
      {updateAvailable && (
        <div className="grid min-w-0 gap-3 rounded-inner bg-surface px-3 py-2 shadow-control">
          <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
            <span className="min-w-0 truncate text-xs font-semibold text-slate-500">
              {t("settings.latestVersion")}
            </span>
            <span className="shrink-0 rounded-full bg-action/10 px-2 py-0.5 font-mono text-[11px] font-semibold tabular-nums text-action">
              {`v${latestVersion}`}
            </span>
          </div>
          {releaseDate && (
            <p className="min-w-0 truncate text-[11px] leading-4 text-slate-400">{releaseDate}</p>
          )}
          <div className="grid min-w-0 gap-1">
            <span className="min-w-0 truncate text-xs font-semibold text-slate-500">
              {t("settings.releaseNotes")}
            </span>
            <p className="max-h-24 min-w-0 overflow-auto whitespace-pre-wrap break-words text-xs leading-5 text-slate-600">
              {releaseNotes}
            </p>
          </div>
          <button
            type="button"
            className="focus-ring inline-flex h-9 min-w-0 items-center justify-center gap-2 rounded-control bg-ink px-3 text-sm font-semibold text-white shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-slate-800 hover:shadow-raised active:scale-[0.96] disabled:bg-slate-300"
            disabled={Boolean(busy) || installActive}
            onClick={onInstall}
          >
            {installActive ? (
              <RefreshCcw size={14} className={installActive ? "animate-spin" : ""} />
            ) : (
              <Download size={14} />
            )}
            {updateInstallButtonLabel(installStatus, t)}
          </button>
        </div>
      )}
    </div>
  );
}

function isUpdateInstallActive(status: AppUpdateInstallStatus | null | undefined) {
  return Boolean(
    status &&
      (status.phase === "checking" ||
        status.phase === "downloading" ||
        status.phase === "installing" ||
        status.phase === "restarting"),
  );
}

function updateInstallButtonLabel(
  status: AppUpdateInstallStatus | null,
  t: (key: string, options?: Record<string, unknown>) => string,
) {
  if (!status) {
    return t("settings.installUpdate");
  }
  if (status.phase === "checking") {
    return t("settings.checkingUpdates");
  }
  if (status.phase === "downloading") {
    const percent = updateInstallProgressPercent(status);
    return percent === null
      ? t("settings.downloadingUpdate")
      : t("settings.downloadingUpdateProgress", { percent });
  }
  if (status.phase === "installing") {
    return t("settings.installingUpdate");
  }
  if (status.phase === "restarting") {
    return t("settings.restartingUpdate");
  }
  return t("settings.installUpdate");
}

function updateInstallProgressPercent(status: AppUpdateInstallStatus) {
  if (!status.total_bytes || status.total_bytes <= 0) {
    return null;
  }
  return Math.max(0, Math.min(100, Math.round((status.downloaded_bytes / status.total_bytes) * 100)));
}

function clampRetryAttempts(value: string) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return 30;
  }
  return Math.max(1, Math.min(30, Math.round(parsed)));
}

function formatUpdateDate(value: string | null | undefined, locale: string) {
  const raw = value?.trim();
  if (!raw) {
    return null;
  }
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) {
    return raw;
  }
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

interface VisionModelParts {
  modelId: string;
  provider: string;
  title: string;
}

function visionModelParts(model: Model, providerLabels: Map<string, string>): VisionModelParts {
  const rawId = model.id.trim();
  const slashIndex = rawId.indexOf("/");
  const modelId = slashIndex > 0 ? rawId.slice(slashIndex + 1) : rawId;
  const idProvider = slashIndex > 0 ? providerLabel(rawId.slice(0, slashIndex), providerLabels) : "";
  const displayProvider = providerLabel(providerFromDisplayName(model.display_name, modelId), providerLabels);
  const sourceProvider =
    model.source_kind === "official"
      ? "OpenAI"
      : providerLabel(model.source_kind ?? "", providerLabels);
  const provider = idProvider || displayProvider || sourceProvider || "Provider";

  return {
    modelId,
    provider,
    title: `${modelId} ${provider}`,
  };
}

function providerFromDisplayName(displayName: string | null | undefined, modelId: string) {
  const name = displayName?.trim();
  if (!name) {
    return "";
  }
  const firstToken = name.split(/\s+/)[0]?.trim();
  if (!firstToken || normalizeProviderToken(modelId).startsWith(normalizeProviderToken(firstToken))) {
    return "";
  }
  return firstToken;
}

function providerLabel(value: string, providerLabels: Map<string, string>) {
  const normalized = value.trim().toLowerCase();
  if (!normalized) {
    return "";
  }
  const known = providerLabels.get(normalized);
  if (known) {
    return known;
  }
  if (normalized === "official" || normalized === "official-openai" || normalized === "official_openai") {
    return "OpenAI";
  }
  return value
    .trim()
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map(formatProviderToken)
    .join(" ");
}

function providerLabelMap(providers: Provider[]) {
  const labels = new Map<string, string>();
  for (const provider of providers) {
    const name = provider.name.trim() || provider.id;
    labels.set(provider.id.trim().toLowerCase(), name);
    const displayPrefix = provider.display_prefix?.trim();
    if (displayPrefix) {
      labels.set(displayPrefix.toLowerCase(), name);
    }
  }
  return labels;
}

function formatProviderToken(part: string) {
  const lower = part.toLowerCase();
  if (lower === "openai") {
    return "OpenAI";
  }
  if (lower === "cn") {
    return "CN";
  }
  return `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`;
}

function normalizeProviderToken(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "");
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

export function SwitchControl({
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

function VisionModelSelect({
  disabled,
  models,
  onChange,
  providers,
  value,
}: {
  disabled?: boolean;
  models: Model[];
  onChange: (value: string) => void;
  providers: Provider[];
  value: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const providerLabels = providerLabelMap(providers);
  const selectedModel = models.find((model) => model.id === value);
  const selectedParts = selectedModel ? visionModelParts(selectedModel, providerLabels) : null;
  const { t } = useTranslation();
  const label = models.length === 0 ? t("common.noVisionModels") : selectedParts?.title ?? t("common.selectModel");

  useEffect(() => {
    if (disabled) {
      setOpen(false);
    }
  }, [disabled]);

  useEffect(() => {
    if (!open) {
      return;
    }

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (target instanceof Node && ref.current?.contains(target)) {
        return;
      }
      setOpen(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
      }
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  function selectModel(nextValue: string) {
    onChange(nextValue);
    setOpen(false);
  }

  return (
    <div ref={ref} className="min-w-0">
      <button
        type="button"
        className="focus-ring flex h-7 w-full min-w-0 items-center justify-between gap-2 overflow-hidden rounded-control bg-transparent px-2 text-left text-sm font-medium text-ink transition-colors duration-150 ease-out disabled:cursor-not-allowed disabled:text-slate-400"
        disabled={disabled}
        title={label}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        {selectedParts ? (
          <VisionModelValue parts={selectedParts} />
        ) : (
          <span className="min-w-0 flex-1 truncate text-slate-500">{label}</span>
        )}
        <ChevronDown
          size={16}
          className={cx(
            "shrink-0 text-slate-500 transition-transform duration-150 ease-out",
            open && "rotate-180 text-ink",
          )}
        />
      </button>

      {open && (
        <div
          className="absolute bottom-[calc(100%+6px)] left-1/2 z-[80] w-[min(340px,calc(100vw-2rem))] -translate-x-1/2 overflow-hidden rounded-overlay bg-surface p-1 shadow-overlay"
        >
          <div className="vision-model-listbox max-h-56 overflow-y-auto overscroll-contain pr-1" role="listbox">
            {models.map((model) => (
              <VisionModelOption
                key={model.id}
                parts={visionModelParts(model, providerLabels)}
                selected={model.id === value}
                onSelect={() => selectModel(model.id)}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function VisionModelValue({ parts }: { parts: VisionModelParts }) {
  return (
    <span className="grid min-w-0 flex-1 grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
      <span className="min-w-0 truncate font-mono text-sm font-semibold leading-5 text-ink">
        {parts.modelId}
      </span>
      <span className="shrink-0 truncate text-sm font-medium leading-5 text-slate-500">{parts.provider}</span>
    </span>
  );
}

function VisionModelOption({
  onSelect,
  parts,
  selected,
}: {
  onSelect: () => void;
  parts: VisionModelParts;
  selected: boolean;
}) {
  return (
    <button
      type="button"
      className={cx(
        "focus-ring flex min-h-8 w-full min-w-0 items-center justify-between gap-2 rounded-control px-2.5 py-1 text-left text-sm font-medium transition-[background-color,color] duration-150 ease-out",
        selected ? "bg-panel text-ink" : "text-slate-600 hover:bg-panel hover:text-ink",
      )}
      role="option"
      aria-selected={selected}
      title={parts.title}
      onClick={onSelect}
    >
      <VisionModelValue parts={parts} />
      {selected && <Check size={15} className="shrink-0 text-action" />}
    </button>
  );
}
