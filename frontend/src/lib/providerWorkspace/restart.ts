export const CODEX_RESTART_CHANGED = "codexhub:pending-restart-changed";
const KEY = "codexhub.restartReminder.v1";
const LEGACY_KEY = "codexhub.pendingConnectionRestart.v1";

function notifyReminderChanged() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(CODEX_RESTART_CHANGED));
}

export function readRestartReminder(): boolean {
  try {
    const value = localStorage.getItem(KEY);
    if (value === "true") return true;
    if (value === "false" || value === "null") return false;
    const legacy = localStorage.getItem(LEGACY_KEY);
    if (legacy && legacy !== "null") return true;
  } catch {
    /* Keep the in-memory reminder from the current view. */
  }
  return false;
}

export function storeRestartReminder(value: boolean) {
  try {
    if (value) localStorage.setItem(KEY, "true");
    else {
      localStorage.removeItem(KEY);
      localStorage.removeItem(LEGACY_KEY);
    }
  } catch {
    /* The current view still retains the reminder. */
  }
  notifyReminderChanged();
}

export function markRestartReminder() {
  storeRestartReminder(true);
}

/** Overlay or catalog already written; the user must restart Codex themselves. */
export async function readCodexRestartNotice(source: {
  getStatus: () => Promise<{ mode: string }>;
}): Promise<"required" | "none" | "unknown"> {
  try {
    const status = await source.getStatus();
    if (status.mode !== "custom") return "none";
    markRestartReminder();
    return "required";
  } catch {
    return "unknown";
  }
}
