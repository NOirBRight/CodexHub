import assert from "node:assert/strict";
import test from "node:test";
import { readQuotaCache, writeQuotaCache, clearQuotaCache } from "../src/lib/quotaCache.ts";

test("last successful quota survives remount and storage failure; logout clears it", async () => {
  const values = new Map();
  globalThis.window = { localStorage: {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  } };
  const snapshot = { limits: [{ key: "weekly", remaining_percent: 95 }] };
  writeQuotaCache("xai", snapshot);
  const reloaded = await import("../src/lib/quotaCache.ts?remounted");
  assert.deepEqual(reloaded.readQuotaCache("xai"), snapshot);
  window.localStorage.setItem = () => { throw new Error("storage unavailable"); };
  writeQuotaCache("xai", { limits: [{ key: "weekly", remaining_percent: 94 }] });
  assert.equal(readQuotaCache("xai").limits[0].remaining_percent, 94);
  clearQuotaCache("xai");
  assert.equal(readQuotaCache("xai"), null);
  delete globalThis.window;
});

test("invalid cached data is ignored", () => {
  globalThis.window = { localStorage: { getItem: () => '{"savedAt":1,"snapshot":{}}' } };
  assert.equal(readQuotaCache("invalid"), null);
  delete globalThis.window;
});
