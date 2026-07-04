import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const gatewayPagePath = new URL("../src/pages/GatewayPage.tsx", import.meta.url);
const gatewayClientCardPath = new URL("../src/components/GatewayClientCard.tsx", import.meta.url);
const appPath = new URL("../src/App.tsx", import.meta.url);
const webBridgePath = new URL("../../src-tauri/src/web_bridge.rs", import.meta.url);

test("manual gateway client refresh can run version probes", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.match(gatewaySource, /async function refreshGatewayClients\(\)/);
  assert.match(gatewaySource, /await onRefreshClients\(\{ includeClientVersions: true \}\)/);
});

test("web bridge handles requests concurrently so slow probes do not block switches", async () => {
  const bridgeSource = await readFile(webBridgePath, "utf8");

  assert.match(bridgeSource, /for stream in listener\.incoming\(\)/);
  assert.match(bridgeSource, /std::thread::spawn\(move \|\| handle_stream\(stream\)\)/);
});

test("gateway client refresh clears stale switch busy state", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.match(
    gatewaySource,
    /async function refreshGatewayClients\(\) \{[\s\S]*await onRefreshClients\(\{ includeClientVersions: true \}\);[\s\S]*setClientBusy\(null\);/,
  );
});

test("gateway client switches refresh without version probes", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.match(gatewaySource, /async function switchClientMode/);
  assert.match(gatewaySource, /api\.switchGatewayClientRoute\(clientId, mode, defaultModel\)/);
  assert.match(gatewaySource, /await onRefreshClients\(\);/);
  assert.doesNotMatch(gatewaySource, /await onRefreshClients\(\{ includeClientVersions: true \}\)[\s\S]*setMessage\(`\$\{clientName\} switched/);
});

test("gateway client refreshes discard stale route snapshots", async () => {
  const appSource = await readFile(appPath, "utf8");

  assert.match(appSource, /const gatewayClientLoadSeq = useRef\(0\)/);
  assert.match(appSource, /const requestSeq = \+\+gatewayClientLoadSeq\.current/);
  assert.match(appSource, /requestSeq !== gatewayClientLoadSeq\.current/);
});

test("gateway client card does not coerce unknown route state to official", async () => {
  const cardSource = await readFile(gatewayClientCardPath, "utf8");

  assert.doesNotMatch(cardSource, /info\?\.route_mode === "hub" \? "hub" : "official"/);
  assert.match(cardSource, /type RouteMode = "official" \| "hub"/);
  assert.match(cardSource, /type DisplayRouteMode = RouteMode \| "unknown"/);
  assert.match(cardSource, /routeMode === "unknown" \? null : routeMode/);
});
