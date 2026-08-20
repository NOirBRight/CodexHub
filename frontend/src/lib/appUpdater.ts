import { api } from "./tauri";
import type { AppUpdateInstallStatus, AppUpdateStatus } from "./types";

export const AppUpdater = {
  check(): Promise<AppUpdateStatus | null> {
    return api.checkAppUpdate();
  },
  install(): Promise<AppUpdateInstallStatus | null> {
    return api.startAppUpdateInstall();
  },
};
