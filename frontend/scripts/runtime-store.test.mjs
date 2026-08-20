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
