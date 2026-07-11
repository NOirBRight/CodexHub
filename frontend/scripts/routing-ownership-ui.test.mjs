import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = async (path) => readFile(new URL(path, import.meta.url), "utf8");

test("Gateway keeps the original two-option route control and makes foreign ownership the takeover target", async () => {
  const [card, switchSource, page] = await Promise.all([
    source("../src/components/GatewayClientCard.tsx"),
    source("../src/components/SegmentedSwitch.tsx"),
    source("../src/pages/GatewayPage.tsx"),
  ]);

  assert.doesNotMatch(card, /\{t\("gateway\.takeover"\)\}/);
  assert.match(card, /const takeoverRequired = routeOwner !== "official" && info\?\.managed_by_current_app === false/);
  assert.match(card, /<SegmentedSwitch/);
  assert.match(card, /activeTone=\{takeoverRequired \? "foreign" : "default"\}/);
  assert.match(card, /takeoverRequired && mode === "current_owner" \? "takeover" : mode/);
  assert.match(card, /routeOwnerLabel/);
  assert.match(switchSource, /activeTone\?: "default" \| "foreign"/);
  assert.match(switchSource, /activeTone === "foreign"/);
  assert.match(switchSource, /bg-\[#e7e7e4\] text-slate-500/);
  assert.match(switchSource, /bg-ink text-white shadow-raised/);
  assert.doesNotMatch(page, /TakeoverSummaryDialog/);
  assert.match(page, /action === "takeover"[\s\S]*switchClientMode\(clientId, runtimeOwner, true\)/);
});

test("Codex keeps connected surfaces visible for a foreign owner and takes over through the existing button", async () => {
  const providers = await source("../src/pages/ProvidersPage.tsx");

  assert.doesNotMatch(providers, /TakeoverSummaryDialog/);
  assert.match(providers, /const \[codexTargetOwnerOverride, setCodexTargetOwnerOverride\]/);
  assert.match(providers, /!realCodexConnected &&[\s\S]*effectiveCodexTargetOwner !== appFlavor\?\.routing_owner/);
  assert.match(providers, /const codexOwnedByOtherApp =/);
  assert.match(providers, /const codexConnected = realCodexConnected \|\| codexOwnedByOtherApp/);
  assert.match(providers, /await applyCodexHubConnection\(nextMode, Boolean\(appFlavor\?\.codex_takeover_required\)\)/);
  assert.match(providers, /setCodexTargetOwnerOverride\(nextMode === "custom" \? appFlavor\?\.routing_owner \?\? null : "official"\)/);
  assert.match(providers, /codexForeignOwner=\{codexOwnedByOtherApp\}/);
  assert.match(providers, /codexOwnerLabel=\{codexRouteOwnerLabel\}/);
  assert.match(providers, /foreignOwner[\s\S]*bg-emerald-100 text-emerald-700/);
  assert.match(providers, /!pendingMode && connected[\s\S]*bg-emerald-600 text-white/);
  assert.match(providers, /connectedToHubChannel/);
});
