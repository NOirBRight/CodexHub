import { useEffect, useMemo, useRef, useState } from "react";
import { useConfirmDialog } from "../components/ConfirmDialog";
import { useToasts } from "../components/PageToast";
import { api } from "../lib/tauri";
import {
  createAppUpdateLifecycle,
  type AppUpdateLifecycle,
  type AppUpdateView,
} from "../lib/appUpdateLifecycle";
import type { AppUpdateInstallStatus, AppUpdateStatus } from "../lib/types";
import type { RuntimeSnapshot } from "../lib/runtimeStore";

/**
 * React seam over the App Update Lifecycle module.
 *
 * The hook owns only composition: it builds the production ports from the
 * existing `api` surface and the shared Toast/confirm context, then exposes
 * the module's small interface. All scheduling, polling, completion restore,
 * and dedupe live inside the module implementation.
 */
export function useAppUpdateLifecycle(options: {
  getRuntime: () => RuntimeSnapshot;
  setRuntime: (update: (current: RuntimeSnapshot) => RuntimeSnapshot) => void;
  translate: (key: string, options?: Record<string, unknown>) => string;
}) {
  const { getRuntime, setRuntime, translate } = options;
  const { confirm: confirmAction } = useConfirmDialog();
  const { dismissToast, showToast, updateToast } = useToasts();
  const [view, setView] = useState<AppUpdateView>(() => ({
    busy: null,
    installStatus: null,
    isInstalling: false,
    updateStatus: null,
  }));
  const lifecycleRef = useRef<AppUpdateLifecycle | null>(null);

  if (!lifecycleRef.current) {
    lifecycleRef.current = createAppUpdateLifecycle({
      clock: {
        clearInterval: (handle) => window.clearInterval(handle as number),
        clearTimeout: (handle) => window.clearTimeout(handle as number),
        setInterval: (callback, ms) => window.setInterval(callback, ms),
        setTimeout: (callback, ms) => window.setTimeout(callback, ms),
      },
      confirm: confirmAction,
      port: {
        check: () => api.checkAppUpdate(),
        consumeCompletion: () => api.consumeAppUpdateCompletion(),
        getAppUpdateInstallStatus: () => api.getAppUpdateInstallStatus(),
        getAppVersion: () => api.getAppVersion(),
        startInstall: () => api.startAppUpdateInstall(),
      },
      store: {
        setAppVersion: (version) => {
          setRuntime((current) => ({
            ...current,
            appVersion: { ...current.appVersion, data: { current_version: version } },
          }));
        },
        setUpdateStatus: (status) => {
          setRuntime((current) => ({
            ...current,
            updateStatus: { ...current.updateStatus, data: status },
          }));
        },
      },
      toast: {
        dismissToast,
        showToast: (input) =>
          showToast({
            action: input.action,
            dedupeKey: input.dedupeKey,
            text: input.text,
            timeoutMs: input.timeoutMs,
            tone: input.tone,
          }),
        updateToast: (id, patch) =>
          updateToast(id, {
            action: patch.action as never,
            text: patch.text,
            tone: patch.tone,
          }),
      },
      translate,
    });
  }

  useEffect(() => {
    const lifecycle = lifecycleRef.current!;
    const unsubscribe = lifecycle.subscribe(setView);
    lifecycle.refreshCompletion();
    return () => {
      unsubscribe();
      lifecycle.dispose();
      lifecycleRef.current = null;
    };
  }, []);

  const actions = useMemo(
    () => ({
      checkForUpdates: () => lifecycleRef.current!.checkForUpdates(),
      startInstall: (source: "settings" | "toast") => lifecycleRef.current!.startInstall(source),
      startScheduling: (settingsLoaded: boolean) => lifecycleRef.current!.startScheduling(settingsLoaded),
    }),
    [],
  );

  return { view, ...actions };
}

export type { AppUpdateInstallStatus, AppUpdateStatus };
