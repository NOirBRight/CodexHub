import assert from "node:assert/strict";
import test from "node:test";
import { createEmptyRuntimeSnapshot, setCacheData, setCacheError, setCacheLoading } from "../src/lib/runtimeStore.ts";

test("runtime store writes one key without copying sibling snapshots", () => {
  const start = createEmptyRuntimeSnapshot();
  const loading = setCacheLoading(start, "settings");
  assert.equal(loading.settings.loading, true);
  assert.equal(loading.status.loading, false);
  const saved = setCacheData(loading, "settings", { locale: "en-US" });
  assert.equal(saved.settings.data?.locale, "en-US");
  assert.equal(saved.settings.loading, false);
  const failed = setCacheError(saved, "status", "offline");
  assert.equal(failed.status.data, null);
  assert.equal(failed.status.error, "offline");
  assert.equal(failed.settings.data?.locale, "en-US");
});

test("runtime store keeps the snapshot when the payload is unchanged", () => {
  const start = createEmptyRuntimeSnapshot();
  const first = setCacheData(start, "settings", { locale: "en-US" });
  const second = setCacheData(first, "settings", { locale: "en-US" });
  assert.equal(second, first);
  assert.equal(second.settings.updatedAt, first.settings.updatedAt);
});

test("runtime store writes a new snapshot when the payload changes", () => {
  const start = createEmptyRuntimeSnapshot();
  const first = setCacheData(start, "settings", { locale: "en-US" });
  const next = setCacheData(first, "settings", { locale: "zh-CN" });
  assert.notEqual(next, first);
  assert.equal(next.settings.data?.locale, "zh-CN");
});
