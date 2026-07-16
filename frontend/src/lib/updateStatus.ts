import type { AppUpdateInstallStatus } from "./types";

type Translate = (key: string, options?: Record<string, unknown>) => string;

export function isUpdateInstallActive(status: AppUpdateInstallStatus | null | undefined) {
  return Boolean(
    status &&
      (status.phase === "checking" ||
        status.phase === "downloading" ||
        status.phase === "installing" ||
        status.phase === "restarting"),
  );
}

export function updateInstallProgressPercent(status: AppUpdateInstallStatus) {
  if (status.phase !== "downloading" || !status.total_bytes || status.total_bytes <= 0) {
    return null;
  }
  return Math.max(0, Math.min(100, Math.round((status.downloaded_bytes / status.total_bytes) * 100)));
}

export function updateInstallToastText(status: AppUpdateInstallStatus, t: Translate) {
  if (status.phase === "checking") {
    return t("settings.checkingUpdates");
  }
  if (status.phase === "downloading") {
    const percent = updateInstallProgressPercent(status);
    return percent === null
      ? t("settings.downloadingUpdate")
      : t("settings.downloadingUpdateProgress", { percent });
  }
  if (status.phase === "installing" || status.phase === "restarting") {
    return t("settings.installingUpdateRestarting");
  }
  if (status.phase === "failed") {
    return t("settings.updateInstallFailed", { message: status.message });
  }
  return status.target_version ? t("settings.installingUpdateRestarting") : t("settings.updateInstallUnavailable");
}

export function updateInstallButtonLabel(status: AppUpdateInstallStatus | null, t: Translate) {
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

export function formatUpdateDate(value: string | null | undefined, locale: string) {
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
