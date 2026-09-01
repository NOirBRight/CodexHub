import type {
  AppUpdateCompletionStatus,
  AppUpdateInstallStatus,
  AppUpdateStatus,
  AppVersionInfo,
} from "./types";

/** External view of the update lifecycle, projected for UI and SettingsDrawer. */
export interface AppUpdateView {
  busy: "check" | null;
  installStatus: AppUpdateInstallStatus | null;
  isInstalling: boolean;
  updateStatus: AppUpdateStatus | null;
}

/** Outcome of a user-initiated install attempt. */
export type AppUpdateActionOutcome =
  | { kind: "cancelled" }
  | { kind: "started" }
  | { kind: "unavailable" }
  | { kind: "failed"; message: string };

export type AppUpdateInstallSource = "settings" | "toast";

/** Scriptable boundary between the lifecycle and the desktop backend. */
export interface AppUpdatePort {
  check(): Promise<AppUpdateStatus | null>;
  consumeCompletion(): Promise<AppUpdateCompletionStatus | null>;
  getAppUpdateInstallStatus(): Promise<AppUpdateInstallStatus>;
  getAppVersion(): Promise<AppVersionInfo>;
  startInstall(): Promise<AppUpdateInstallStatus | null>;
}

/** Scriptable time source (production timers vs fake clock). */
export interface AppUpdateClock {
  clearInterval(handle: unknown): void;
  clearTimeout(handle: unknown): void;
  setInterval(callback: () => void, ms: number): unknown;
  setTimeout(callback: () => void, ms: number): unknown;
}

export interface AppUpdateToastApi {
  dismissToast(id: string): void;
  showToast(input: {
    action?: { label: string; onClick: () => void };
    dedupeKey?: string;
    text: string;
    timeoutMs?: number | null;
    tone: "info" | "success" | "error" | "loading";
  }): string;
  updateToast(id: string, patch: { action?: unknown; text: string; tone: "info" | "success" | "error" | "loading" }): void;
}

/** How the lifecycle writes appVersion/updateStatus back into the runtime store. */
export interface AppUpdateStore {
  setAppVersion(version: string): void;
  setUpdateStatus(status: AppUpdateStatus): void;
}

export interface AppUpdateDeps {
  clock: AppUpdateClock;
  confirm: (options: { cancelLabel: string; confirmLabel: string; message: string; title: string }) => Promise<boolean>;
  port: AppUpdatePort;
  store: AppUpdateStore;
  toast: AppUpdateToastApi;
  translate: (key: string, options?: Record<string, unknown>) => string;
}

const STARTUP_DELAY_MS = 2500;
const AUTO_CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000;
const INSTALL_POLL_MS = 500;
const INSTALL_TOAST_KEY = "app-update-install";
const AVAILABLE_TOAST_KEY = "app-update-available";

function isInstallActive(status: AppUpdateInstallStatus | null): boolean {
  return Boolean(
    status &&
      (status.phase === "checking" ||
        status.phase === "downloading" ||
        status.phase === "installing" ||
        status.phase === "restarting"),
  );
}

function failedInstallStatus(
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

export interface AppUpdateLifecycle {
  checkForUpdates(): Promise<AppUpdateStatus | null>;
  dispose(): void;
  refreshCompletion(): void;
  startInstall(source: AppUpdateInstallSource): Promise<AppUpdateActionOutcome>;
  startScheduling(settingsLoaded: boolean): void;
  subscribe(listener: (view: AppUpdateView) => void): () => void;
  getView(): AppUpdateView;
}

/** Deep module owning the whole desktop update lifecycle behind a small interface. */
export function createAppUpdateLifecycle(deps: AppUpdateDeps): AppUpdateLifecycle {
  const { clock, confirm, port, store, toast, translate } = deps;
  let listeners: Array<(view: AppUpdateView) => void> = [];
  let disposed = false;
  let installStatus: AppUpdateInstallStatus | null = null;
  let busy: "check" | null = null;
  let updateStatus: AppUpdateStatus | null = null;
  let availableToastId: string | null = null;
  let installToastId: string | null = null;
  let installSource: AppUpdateInstallSource = "settings";
  let startupStarted = false;
  let autoTimer: unknown | null = null;
  let autoInterval: unknown | null = null;
  let pollTimer: unknown | null = null;
  let completionTimer: unknown | null = null;
  let latestAppVersion = "";

  function emit() {
    const view: AppUpdateView = {
      busy,
      installStatus,
      isInstalling: isInstallActive(installStatus),
      updateStatus,
    };
    for (const listener of listeners) {
      listener(view);
    }
  }

  function setInstall(status: AppUpdateInstallStatus | null) {
    installStatus = status;
    emit();
  }

  function setUpdate(status: AppUpdateStatus | null) {
    updateStatus = status;
    emit();
  }

  function setBusy(next: "check" | null) {
    busy = next;
    emit();
  }

  async function loadStatus(): Promise<AppUpdateStatus | null> {
    const status = await port.check();
    if (status) {
      store.setUpdateStatus(status);
      store.setAppVersion(status.current_version);
      setUpdate(status);
    }
    return status;
  }

  function updateInstallToast(status: AppUpdateInstallStatus, source: AppUpdateInstallSource) {
    if (!installToastId) {
      return;
    }
    const tone =
      status.phase === "failed" ? "error" : isInstallActive(status) ? "loading" : "success";
    toast.updateToast(installToastId, {
      action: null,
      text: installToastText(status, translate, source),
      tone,
    });
    if (!isInstallActive(status)) {
      installToastId = null;
    }
  }

  function installToastText(
    status: AppUpdateInstallStatus,
    t: (key: string, options?: Record<string, unknown>) => string,
    source: AppUpdateInstallSource,
  ) {
    if (status.phase === "failed") {
      return t("settings.updateInstallFailed", { message: status.message });
    }
    if (source === "settings") {
      if (status.phase === "checking") return t("settings.checkingUpdates");
      if (status.phase === "downloading") {
        const percent =
          status.total_bytes && status.total_bytes > 0
            ? Math.round((status.downloaded_bytes / status.total_bytes) * 100)
            : null;
        return percent === null
          ? t("settings.downloadingUpdate")
          : t("settings.downloadingUpdateProgress", { percent });
      }
      return t("settings.installingUpdateRestarting");
    }
    // toast source: the toast-supplied messages keep the same semantics as before.
    return t("settings.installingUpdateRestarting");
  }

  async function pollOnce() {
    try {
      const status = await port.getAppUpdateInstallStatus();
      if (disposed) return;
      setInstall(status);
      updateInstallToast(status, installSource);
      if (installSource === "settings" && status.phase === "failed") {
        toast.showToast({
          text: t("settings.updateInstallFailed", { message: status.message }),
          tone: "error",
        });
      }
    } catch (err) {
      if (disposed || installStatus?.phase === "installing" || installStatus?.phase === "restarting") {
        return;
      }
      const message = err instanceof Error ? err.message : String(err);
      const failed = failedInstallStatus(
        installStatus,
        currentVersion(),
        updateStatus?.latest_version ?? null,
        message,
      );
      setInstall(failed);
      updateInstallToast(failed, installSource);
      if (installSource === "settings") {
        toast.showToast({
          text: t("settings.updateInstallFailed", { message }),
          tone: "error",
        });
      }
    }
  }

  function currentVersion(): string {
    return latestAppVersion;
  }

  async function startPolling() {
    if (pollTimer) clock.clearInterval(pollTimer);
    pollTimer = clock.setInterval(() => void pollOnce(), INSTALL_POLL_MS);
    await pollOnce();
  }

  async function startInstall(source: AppUpdateInstallSource): Promise<AppUpdateActionOutcome> {
    if (isInstallActive(installStatus)) {
      return { kind: "started" };
    }
    const confirmed = await confirm({
      cancelLabel: t("common.cancel"),
      confirmLabel: t("common.confirm"),
      message: t("settings.updateInstallConfirm"),
      title: t("common.confirm"),
    });
    if (!confirmed) {
      return { kind: "cancelled" };
    }
    installSource = source;
    if (availableToastId) {
      toast.dismissToast(availableToastId);
      availableToastId = null;
    }
    installToastId = toast.showToast({
      dedupeKey: INSTALL_TOAST_KEY,
      text: t("settings.downloadingUpdate"),
      timeoutMs: null,
      tone: "loading",
    });
    try {
      const status = await port.startInstall();
      if (disposed) return { kind: "started" };
      if (!status) {
        const unavailable = t("settings.desktopUpdatesUnavailable");
        if (installToastId) {
          toast.updateToast(installToastId, {
            action: null,
            text: unavailable,
            tone: "info",
          });
          installToastId = null;
        } else {
          toast.showToast({ text: unavailable, tone: "info" });
        }
        return { kind: "unavailable" };
      }
      setInstall(status);
      updateInstallToast(status, source);
      void startPolling();
      return { kind: "started" };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const failed = failedInstallStatus(
        installStatus,
        currentVersion(),
        updateStatus?.latest_version ?? null,
        message,
      );
      setInstall(failed);
      updateInstallToast(failed, source);
      return { kind: "failed", message };
    }
  }

  async function checkForUpdates(): Promise<AppUpdateStatus | null> {
    setBusy("check");
    try {
      const status = await loadStatus();
      toast.showToast({
        text:
          status?.available && status.latest_version
            ? t("settings.updateAvailable", { version: status.latest_version })
            : t("settings.noUpdatesAvailable"),
        tone: status?.available ? "info" : "success",
      });
      return status;
    } catch (err) {
      toast.showToast({
        text: t("settings.updateCheckFailed", { message: err instanceof Error ? err.message : String(err) }),
        tone: "error",
      });
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function runAutomaticCheck() {
    try {
      const status = await loadStatus();
      if (!status?.available || !status.latest_version) {
        return;
      }
      availableToastId = toast.showToast({
        dedupeKey: AVAILABLE_TOAST_KEY,
        action: {
          label: t("settings.update"),
          onClick: () => void startInstall("toast"),
        },
        text: t("settings.updateAvailable", { version: status.latest_version }),
        timeoutMs: null,
        tone: "info",
      });
    } catch {
      // Automatic update checks are best-effort and should not create noisy banners.
    }
  }

  function startScheduling(settingsLoaded: boolean) {
    if (startupStarted || !settingsLoaded || disposed) {
      return;
    }
    startupStarted = true;
    autoTimer = clock.setTimeout(() => void runAutomaticCheck(), STARTUP_DELAY_MS);
    autoInterval = clock.setInterval(() => void runAutomaticCheck(), AUTO_CHECK_INTERVAL_MS);
  }

  function refreshCompletion(): void {
    completionTimer = clock.setTimeout(async () => {
      try {
        const completion = await port.consumeCompletion();
        if (!completion?.completed) return;
        toast.showToast({
          text: t("settings.updateInstalled", { version: completion.current_version }),
          tone: "success",
        });
        latestAppVersion = completion.current_version;
        store.setAppVersion(completion.current_version);
      } catch {
        // Completion verification is best-effort; pending failures should not interrupt startup.
      }
    }, 0);
  }

  function dispose() {
    disposed = true;
    if (autoTimer) clock.clearTimeout(autoTimer);
    if (autoInterval) clock.clearInterval(autoInterval);
    if (pollTimer) clock.clearInterval(pollTimer);
    if (completionTimer) clock.clearTimeout(completionTimer);
    listeners = [];
  }

  function subscribe(listener: (view: AppUpdateView) => void): () => void {
    listeners.push(listener);
    listener(getView());
    return () => {
      listeners = listeners.filter((entry) => entry !== listener);
    };
  }

  function getView(): AppUpdateView {
    return { busy, installStatus, isInstalling: isInstallActive(installStatus), updateStatus };
  }

  function t(key: string, options?: Record<string, unknown>): string {
    return translate(key, options);
  }

  return {
    checkForUpdates,
    dispose,
    refreshCompletion,
    startInstall,
    startScheduling,
    subscribe,
    getView,
  };
}
