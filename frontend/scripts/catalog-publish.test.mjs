import assert from "node:assert/strict";
import test from "node:test";
import { publishCatalog } from "../src/lib/catalogPublish.ts";

test("publishCatalog generates then optionally syncs bound clients", async () => {
  const calls = [];
  const result = await publishCatalog(
    { reason: "provider-save", persist: true, syncClients: true },
    {
      generate: async () => { calls.push("generate"); },
      sync: async () => { calls.push("sync"); return { applied: 1 }; },
    },
  );
  assert.deepEqual(calls, ["generate", "sync"]);
  assert.deepEqual(result, { syncResult: { applied: 1 } });
});

test("publishCatalog can skip generate when catalog is already published", async () => {
  const calls = [];
  await publishCatalog(
    { reason: "already-published", persist: false, syncClients: false },
    {
      generate: async () => { calls.push("generate"); },
      sync: async () => { calls.push("sync"); return { applied: 0 }; },
    },
  );
  assert.deepEqual(calls, []);
});
