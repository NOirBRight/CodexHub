/** Restart disclosure is a readback, never part of the durable save outcome. */
export async function readCodexRestartNotice(
  source: {
    getStatus: () => Promise<{ mode: string }>;
    getCodexDesktopStatus: () => Promise<{ running: boolean }>;
  },
): Promise<"required" | "none" | "unknown"> {
  try {
    const status = await source.getStatus();
    if (status.mode !== "custom") return "none";
    return (await source.getCodexDesktopStatus()).running ? "required" : "none";
  } catch {
    return "unknown";
  }
}
