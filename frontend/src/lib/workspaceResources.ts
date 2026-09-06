import type { OpenAIUsageLimit } from "./types";

/** Missing usage is unknown, never an empty or full quota. */
export function quotaPercent(limit: OpenAIUsageLimit): number | null {
  const total = limit.limit;
  const remaining =
    limit.remaining ??
    (typeof total === "number" && typeof limit.used === "number"
      ? total - limit.used
      : null);
  if (
    typeof total !== "number" ||
    !Number.isFinite(total) ||
    total <= 0 ||
    typeof remaining !== "number" ||
    !Number.isFinite(remaining)
  )
    return null;
  return Math.max(0, Math.min(100, (remaining / total) * 100));
}

export function isWeeklyLimit(limit: OpenAIUsageLimit) {
  return /week|7d|168h/i.test(`${limit.period} ${limit.key} ${limit.name}`);
}

/** Quota APIs return ISO timestamps as well as epoch seconds/milliseconds. */
export function quotaResetDate(value?: string | null): Date | null {
  const raw = value?.trim();
  if (!raw) return null;
  const number = Number(raw);
  const date = /^\d+(?:\.\d+)?$/.test(raw)
    ? new Date(number < 10_000_000_000 ? number * 1000 : number)
    : new Date(raw);
  return Number.isFinite(date.getTime()) ? date : null;
}

/** Reserve the right-hand slot for a single weekly quota. */
export function resourceQuotaLayout(limits: OpenAIUsageLimit[]) {
  const rank = (limit: OpenAIUsageLimit) =>
    /month/i.test(`${limit.period} ${limit.key}`)
      ? 2
      : Number(isWeeklyLimit(limit));
  const ordered = [...limits].sort((a, b) => rank(a) - rank(b));
  return {
    ordered,
    single: ordered.length === 1,
    weekOnly: ordered.length === 1 && isWeeklyLimit(ordered[0]),
  };
}
