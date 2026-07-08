import type { TFunction } from "i18next";
import type { ToastContextValue } from "../components/PageToast";
import { messageFromError } from "./tauri";
import type { AppUpdateInstallResult } from "./types";

type InstallAppUpdateDeps = Pick<ToastContextValue, "showToast" | "updateToast"> & {
  installAppUpdate: () => Promise<AppUpdateInstallResult | null>;
  t: TFunction;
  onStart?: () => void;
  onSettled?: () => void;
};

export async function runAppUpdateInstall({
  installAppUpdate,
  onSettled,
  onStart,
  showToast,
  t,
  updateToast,
}: InstallAppUpdateDeps) {
  if (!window.confirm(t("settings.updateInstallConfirm"))) {
    return false;
  }

  const toastId = showToast({
    text: t("settings.installingUpdate"),
    tone: "loading",
    timeoutMs: null,
  });

  onStart?.();

  try {
    const result = await installAppUpdate();
    if (!result) {
      updateToast(toastId, {
        action: null,
        text: t("settings.desktopUpdatesUnavailable"),
        tone: "info",
      });
      return false;
    }

    if (!result.installed) {
      updateToast(toastId, {
        action: null,
        text: result.message,
        tone: "info",
      });
    }

    return result.installed;
  } catch (err) {
    updateToast(toastId, {
      action: null,
      text: messageFromError(err),
      tone: "error",
    });
    return false;
  } finally {
    onSettled?.();
  }
}
