import type { Settings } from "./types";

/** Refresh clean fields while preserving edits made since the last backend snapshot. */
export function rebaseSettingsDraft(
  current: Settings | null,
  baseline: Settings | null,
  incoming: Settings | null,
): Settings | null {
  if (!current || !incoming) return current ?? incoming;
  if (!baseline) return current;
  return Object.fromEntries(
    Object.entries(incoming).map(([key, value]) => [
      key,
      JSON.stringify(current[key as keyof Settings]) ===
      JSON.stringify(baseline[key as keyof Settings])
        ? value
        : current[key as keyof Settings],
    ]),
  ) as unknown as Settings;
}
