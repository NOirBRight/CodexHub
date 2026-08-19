import assert from "node:assert/strict";
import test from "node:test";
import {
  formatRestartDisclosure,
  runPersistentAction,
} from "../src/lib/persistentAction.ts";

test("restart none discloses no restart required", () => {
  assert.equal(formatRestartDisclosure({ kind: "none" }), "No restart required.");
});

test("restart names the exact client", () => {
  assert.equal(
    formatRestartDisclosure({ kind: "client", name: "OpenCode" }),
    "Restart OpenCode.",
  );
});

test("persistent action updates the same toast from loading to success", async () => {
  const calls = [];
  const showToast = (input) => {
    calls.push(["show", input]);
    return "toast-1";
  };
  const updateToast = (id, patch) => {
    calls.push(["update", id, patch]);
  };
  const result = await runPersistentAction({
    showToast,
    updateToast,
    loading: "Connecting...",
    work: async () => "ok",
    success: () => ({
      text: "Connected OpenCode — injected block written.",
      restart: { kind: "client", name: "OpenCode" },
    }),
  });
  assert.equal(result, "ok");
  assert.equal(calls[0][0], "show");
  assert.equal(calls[0][1].tone, "loading");
  assert.equal(calls[0][1].timeoutMs, null);
  assert.equal(calls[1][0], "update");
  assert.equal(calls[1][1], "toast-1");
  assert.match(calls[1][2].text, /Restart OpenCode/);
  assert.equal(calls[1][2].tone, "success");
  assert.equal(calls.filter((call) => call[0] === "show").length, 1);
});

test("backend disconnect updates the same toast", async () => {
  const calls = [];
  await assert.rejects(
    () =>
      runPersistentAction({
        showToast: (input) => {
          calls.push(["show", input]);
          return "toast-1";
        },
        updateToast: (id, patch) => {
          calls.push(["update", id, patch]);
        },
        loading: "Saving...",
        work: async () => {
          throw new Error("Failed to connect to the CodexHub backend");
        },
        success: () => ({ text: "Saved.", restart: { kind: "none" } }),
        disconnected: "start-gateway",
        onStartGateway: async () => {},
      }),
  );
  assert.equal(calls[1][0], "update");
  assert.equal(calls[1][1], "toast-1");
  assert.equal(calls[1][2].tone, "error");
  assert.equal(typeof calls[1][2].action?.onClick, "function");
});
