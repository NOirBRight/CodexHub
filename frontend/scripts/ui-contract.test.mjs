import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const json = async (path) => JSON.parse(await readFile(new URL(path, import.meta.url), "utf8"));

function parseLocaleObject(source) {
  const objectSource = source.match(/const\s+\w+\s*=\s*(\{[\s\S]*\});\s*export default/)?.[1];
  assert.ok(objectSource, "locale source should export a plain object");
  return Function(`"use strict"; return (${objectSource});`)();
}

function flattenKeys(value, prefix = "") {
  return Object.entries(value).flatMap(([key, child]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return child && typeof child === "object" && !Array.isArray(child)
      ? flattenKeys(child, path)
      : [path];
  });
}

test("UI data contract has stable unique ids and portable config paths", async () => {
  const contract = await json("../src/lib/ui-contract.json");
  assert.deepEqual(contract.tabs.map(({ id }) => id), ["codexhub", "gateway"]);
  const ids = contract.gatewayClients.map(({ id }) => id);
  assert.equal(new Set(ids).size, ids.length);
  for (const client of contract.gatewayClients) {
    assert.match(client.id, /^[a-z][a-z0-9-]*$/);
    assert.match(client.config_path, /^~\//);
  }
});

test("English and Chinese locales expose exactly the same keys", async () => {
  const [enSource, zhSource] = await Promise.all([
    readFile(new URL("../src/i18n/locales/en-US.ts", import.meta.url), "utf8"),
    readFile(new URL("../src/i18n/locales/zh-CN.ts", import.meta.url), "utf8"),
  ]);
  assert.deepEqual(
    flattenKeys(parseLocaleObject(zhSource)).sort(),
    flattenKeys(parseLocaleObject(enSource)).sort(),
  );
});

test("Tauri packaging preserves updater and Python resource contracts", async () => {
  const config = await json("../../src-tauri/tauri.conf.json");
  assert.equal(config.bundle.active, true);
  assert.equal(config.bundle.createUpdaterArtifacts, true);
  assert.equal(config.bundle.resources["../src-python/*.py"], "src-python");
  assert.ok(config.plugins.updater.endpoints.length > 0);
  assert.ok(config.plugins.updater.pubkey);
});

test("Linux window override remains an undecorated visible taskbar window", async () => {
  const config = await json("../../src-tauri/tauri.linux.conf.json");
  const [window] = config.app.windows;
  assert.equal(window.decorations, false);
  assert.equal(window.skipTaskbar, false);
  assert.equal(window.transparent, false);
  assert.ok(window.minWidth > 0 && window.minHeight > 0);
});

test("main window capability keeps required native window permissions", async () => {
  const capability = await json("../../src-tauri/capabilities/default.json");
  assert.deepEqual(capability.windows, ["main"]);
  for (const permission of [
    "core:window:allow-start-dragging",
    "core:window:allow-start-resize-dragging",
    "core:window:allow-internal-toggle-maximize",
  ]) {
    assert.ok(capability.permissions.includes(permission));
  }
});
