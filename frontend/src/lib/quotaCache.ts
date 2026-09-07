import type { OpenAIUsageLimit } from "./types";

type QuotaSnapshot = { limits: OpenAIUsageLimit[]; balance?: number | null; currency?: string };
const prefix = "codexhub.quota.v1.";
const maxAge = 24 * 60 * 60 * 1000;
const memory = new Map<string, { savedAt: number; snapshot: QuotaSnapshot }>();

export function readQuotaCache(key: string): QuotaSnapshot | null {
  try {
    const stored = memory.get(key) ?? JSON.parse(window.localStorage.getItem(prefix + key) || "null");
    if (!stored || !Array.isArray(stored.snapshot?.limits) || !Number.isFinite(stored.savedAt) || Date.now() - stored.savedAt > maxAge) return null;
    return stored.snapshot;
  } catch {
    return null;
  }
}

export function writeQuotaCache(key: string, snapshot: QuotaSnapshot): void {
  const stored = { savedAt: Date.now(), snapshot };
  memory.set(key, stored);
  try { window.localStorage.setItem(prefix + key, JSON.stringify(stored)); } catch { /* Memory cache remains available. */ }
}

export function clearQuotaCache(key: string): void {
  memory.delete(key);
  try { window.localStorage.removeItem(prefix + key); } catch { /* Storage can be unavailable. */ }
}
