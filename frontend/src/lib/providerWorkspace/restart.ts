import type { CodexDesktopStatus } from "../types";

export type PendingCodexRestart = { mode: "official" | "custom"; instanceId: number | null };
export const CODEX_RESTART_CHANGED = "codexhub:pending-restart-changed";
const KEY = "codexhub.pendingConnectionRestart.v1";
export function readPendingCodexRestart(): PendingCodexRestart | null {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) || "null");
    return value && ["official", "custom"].includes(value.mode)
      && (value.instanceId === null || typeof value.instanceId === "number") ? value : null;
  } catch { return null; }
}
export function storePendingCodexRestart(value: PendingCodexRestart | null) {
  try {
    if (value) localStorage.setItem(KEY, JSON.stringify(value));
    else localStorage.removeItem(KEY);
  } catch { /* The current view still retains the pending state. */ }
  if (typeof window !== "undefined") window.dispatchEvent(new Event(CODEX_RESTART_CHANGED));
}
export function codexRestartObserved(pending: PendingCodexRestart, status: CodexDesktopStatus): boolean {
  return status.running && pending.instanceId !== null && status.instance_id != null
    && pending.instanceId !== status.instance_id;
}

/** Restart disclosure is a readback, never part of the durable save outcome. */
export async function readCodexRestartNotice(
  source: {
    getStatus: () => Promise<{ mode: string }>;
    getCodexDesktopStatus: () => Promise<{ running: boolean; instance_id?: number | null }>;
  },
): Promise<"required" | "none" | "unknown"> {
  try {
    const status = await source.getStatus();
    if (status.mode !== "custom") return "none";
    const desktop = await source.getCodexDesktopStatus();
    if (!desktop.running) return "none";
    storePendingCodexRestart({ mode: "custom", instanceId: desktop.instance_id ?? null });
    return "required";
  } catch {
    return "unknown";
  }
}
