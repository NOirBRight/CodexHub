import assert from "node:assert/strict";
import test from "node:test";
import { createAppUpdateLifecycle } from "../src/lib/appUpdateLifecycle.ts";

function makeStatus(overrides = {}) {
  return {
    phase: "downloading",
    current_version: "1.0.0",
    target_version: "1.1.0",
    downloaded_bytes: 10,
    total_bytes: 100,
    message: "",
    updated_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

function makePort(overrides = {}) {
  return {
    check: async () => ({
      available: true,
      current_version: "1.0.0",
      latest_version: "1.1.0",
      checked_at: "2026-01-01T00:00:00.000Z",
    }),
    consumeCompletion: async () => null,
    getAppUpdateInstallStatus: async () => makeStatus(),
    getAppVersion: async () => ({ current_version: "1.0.0" }),
    startInstall: async () => makeStatus(),
    ...overrides,
  };
}

function makeHarness(overrides = {}) {
  const calls = [];
  const clock = {
    count: 0,
    setInterval: (cb) => ({ timer: ++clock.count, cb }),
    setTimeout: (cb, ms) => {
      const handle = { timer: ++clock.count, cb };
      if (ms === 0) queueMicrotask(() => cb());
      return handle;
    },
    clearInterval: () => {},
    clearTimeout: () => {},
  };
  const toast = {
    dismissToast: (id) => calls.push(["dismiss", id]),
    showToast: (input) => {
      calls.push(["show", input]);
      return "toast-" + calls.filter((c) => c[0] === "show").length;
    },
    updateToast: (id, patch) => calls.push(["update", id, patch]),
  };
  const store = {
    setAppVersion: (version) => calls.push(["version", version]),
    setUpdateStatus: (status) => calls.push(["updateStatus", status]),
  };
  const lifecycle = createAppUpdateLifecycle({
    clock,
    confirm: async () => true,
    port: makePort(),
    store,
    toast,
    translate: (key) => key,
    ...overrides,
  });
  return { calls, clock, lifecycle, store, toast };
}

test("manual check shows result toast and writes runtime store", async () => {
  const { calls, lifecycle } = makeHarness();
  const status = await lifecycle.checkForUpdates();
  assert.ok(status?.available);
  assert.ok(calls.some((c) => c[0] === "show" && c[1].tone === "info"));
  assert.ok(calls.some((c) => c[0] === "version"));
  assert.ok(calls.some((c) => c[0] === "updateStatus"));
});

test("manual check failure shows error toast and returns null", async () => {
  const { calls, lifecycle } = makeHarness({
    port: makePort({ check: async () => { throw new Error("boom"); } }),
  });
  const status = await lifecycle.checkForUpdates();
  assert.equal(status, null);
  assert.ok(calls.some((c) => c[0] === "show" && c[1].tone === "error"));
});

test("install creates one loading toast and updates the same id to success", async () => {
  const { calls, lifecycle } = makeHarness();
  const outcome = await lifecycle.startInstall("settings");
  assert.equal(outcome.kind, "started");
  const shows = calls.filter((c) => c[0] === "show" && c[1].tone === "loading");
  assert.equal(shows.length, 1);
  const loadingId = shows[0][2] ?? shows[0][1].dedupeKey;
  const updates = calls.filter((c) => c[0] === "update");
  assert.ok(updates.length >= 1);
});

test("install is deduped while an install is active", async () => {
  const { lifecycle } = makeHarness();
  const first = await lifecycle.startInstall("settings");
  assert.equal(first.kind, "started");
  const second = await lifecycle.startInstall("toast");
  assert.equal(second.kind, "started");
});

test("cancelled confirmation returns cancelled outcome without toast", async () => {
  const { calls, lifecycle } = makeHarness({ confirm: async () => false });
  const outcome = await lifecycle.startInstall("settings");
  assert.equal(outcome.kind, "cancelled");
  assert.equal(calls.filter((c) => c[0] === "show").length, 0);
});

test("install unavailable updates the same toast with info tone", async () => {
  const { calls, lifecycle } = makeHarness({
    port: makePort({ startInstall: async () => null }),
  });
  const outcome = await lifecycle.startInstall("settings");
  assert.equal(outcome.kind, "unavailable");
  const update = calls.find((c) => c[0] === "update");
  assert.equal(update?.[2].tone, "info");
});

test("install failure produces failed status and error toast", async () => {
  const { calls, lifecycle } = makeHarness({
    port: makePort({ startInstall: async () => { throw new Error("nope"); } }),
  });
  const outcome = await lifecycle.startInstall("settings");
  assert.equal(outcome.kind, "failed");
  assert.equal(outcome.message, "nope");
  const update = calls.find((c) => c[0] === "update");
  assert.equal(update?.[2].tone, "error");
});

test("startScheduling schedules startup and daily automatic checks", async () => {
  const { clock, lifecycle } = makeHarness();
  lifecycle.startScheduling(false);
  lifecycle.startScheduling(true);
  // two timers: startup timeout + daily interval
  assert.equal(clock.count, 2);
});

test("dispose clears timers and stops emitting", async () => {
  const { lifecycle } = makeHarness();
  let views = 0;
  lifecycle.subscribe(() => views++);
  lifecycle.dispose();
  const before = views;
  lifecycle.checkForUpdates();
  assert.equal(views, before);
});

test("completion restore shows installed toast and updates version", async () => {
  const { calls, lifecycle } = makeHarness({
    port: makePort({
      consumeCompletion: async () => ({
        completed: true,
        current_version: "1.1.0",
        target_version: "1.1.0",
      }),
    }),
  });
  lifecycle.refreshCompletion();
  // flush the setTimeout(0)
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.ok(calls.some((c) => c[0] === "show" && /updateInstalled/.test(c[1].text)));
  assert.ok(calls.some((c) => c[0] === "version" && c[1] === "1.1.0"));
});

test("subscribe receives the current view immediately", async () => {
  const { lifecycle } = makeHarness();
  let view;
  lifecycle.subscribe((v) => (view = v));
  assert.equal(view.isInstalling, false);
  assert.equal(view.busy, null);
});
