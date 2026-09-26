import assert from "node:assert/strict";
import test from "node:test";
import {
  connectionStateFromInfo,
  listReachedClientBusyTarget,
  switchCheckedFromState,
} from "../src/lib/clientConnectionState.ts";

function info(route_mode, extra = {}) {
  return {
    id: "opencode",
    name: "OpenCode",
    kind: "Terminal client",
    installed: true,
    auto_apply_supported: true,
    route_mode,
    route_owner: extra.route_owner ?? "official",
    managed_by_current_app: extra.managed_by_current_app ?? true,
    status: "",
    ...extra,
  };
}

test("connect toggle stays on while busy even if the listed route is still disconnected", () => {
  const listed = info("official");
  assert.equal(connectionStateFromInfo(listed), "disconnected");
  assert.equal(
    switchCheckedFromState(connectionStateFromInfo(listed, true)),
    true,
  );
  assert.equal(
    listReachedClientBusyTarget("opencode:switch:release", listed),
    false,
  );
});

test("connect busy clears only after the list reports a hub route", () => {
  const listed = info("hub", { route_owner: "release" });
  assert.equal(connectionStateFromInfo(listed), "connected");
  assert.equal(
    listReachedClientBusyTarget("opencode:switch:release", listed),
    true,
  );
  assert.equal(
    switchCheckedFromState(connectionStateFromInfo(listed, false)),
    true,
  );
});

test("failed connect drops busy and paints disconnected from the still-official list", () => {
  const listed = info("official");
  assert.equal(
    switchCheckedFromState(connectionStateFromInfo(listed, false)),
    false,
  );
});

test("Claude settings apply waits for the write even when the old route is connected", () => {
  assert.equal(listReachedClientBusyTarget("claude:apply:release", info("hub", { route_owner: "release" })), false);
});
