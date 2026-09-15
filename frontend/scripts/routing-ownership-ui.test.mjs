import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = async (path) => readFile(new URL(path, import.meta.url), "utf8");

test("Gateway connect switch paints from the checked prop, not native :checked", async () => {
  const [drawer, models, css] = await Promise.all([
    source("../src/components/SettingsDrawer.tsx"),
    source("../src/components/providers/ProviderModelSection.tsx"),
    source("../src/components/workspace/workspace.css"),
  ]);

  assert.match(drawer, /checked && "translate-x-4"/);
  assert.doesNotMatch(drawer, /peer-checked:/);
  assert.match(models, /checked && "translate-x-4"/);
  assert.doesNotMatch(models, /peer-checked:/);
  assert.match(
    css,
    /:not\(\.ws-switch-control, \.ws-model-switch\) > input\[type="checkbox"\]:checked \+ span/,
  );
  assert.doesNotMatch(
    css,
    /\.ws-model-switch > input:checked \+ span/,
  );
  assert.match(css, /\.ws-model-switch\[data-on\] > input \+ span/);
});

test("Gateway connect toggle maps foreign ownership to takeover without a segmented control", async () => {
  const [card, page] = await Promise.all([
    source("../src/components/GatewayClientCard.tsx"),
    source("../src/pages/GatewayPage.tsx"),
  ]);

  assert.doesNotMatch(card, /SegmentedSwitch/);
  assert.match(card, /<SwitchControl/);
  assert.match(card, /onToggle: \(connect: boolean\) => void/);
  assert.doesNotMatch(page, /TakeoverSummaryDialog/);
  assert.match(page, /takeoverRequired/);
  assert.match(page, /switchClientMode\(clientId, runtimeOwner, takeoverRequired\)/);
  assert.match(page, /if \(!result\.applied\)/);
  assert.match(page, /onRefreshClients\(\{ force: true \}\)/);
  assert.match(page, /listReachedClientBusyTarget/);
  assert.match(page, /setClientBusy\(null\);/);
  const switchFn = page.slice(
    page.indexOf("async function switchClientMode"),
    page.indexOf("async function refreshGatewayClients"),
  );
  assert.match(switchFn, /catch \{\s*setClientBusy\(null\);/);
  assert.doesNotMatch(switchFn, /finally \{\s*setClientBusy\(null\);/);
});

test("Codex keeps connected surfaces visible for a foreign owner and takes over through the existing button", async () => {
  const providers = await source("../src/pages/ProvidersPage.tsx");

  assert.doesNotMatch(providers, /TakeoverSummaryDialog/);
  assert.match(providers, /const \[codexTargetOwnerOverride, setCodexTargetOwnerOverride\]/);
  assert.match(providers, /!realCodexConnected &&[\s\S]*effectiveCodexTargetOwner !== appFlavor\?\.routing_owner/);
  assert.match(providers, /const codexOwnedByOtherApp =/);
  assert.match(providers, /const codexConnected = realCodexConnected \|\| codexOwnedByOtherApp/);
  assert.match(providers, /await applyCodexHubConnection\(\s*nextMode,\s*Boolean\(appFlavor\?\.codex_takeover_required\),\s*\)/);
  assert.match(providers, /await api\.switchMode\(nextMode, false, true\)/);
  assert.match(providers, /await api\.switchMode\(nextMode, false, false\)/);
  assert.doesNotMatch(providers, /authorizeCodexRestart/);
  assert.doesNotMatch(providers, /restartCodex/);
  assert.match(providers, /providers\.codexRouteChangedRestart/);
  const localesEn = await source("../src/i18n/locales/en-US.ts");
  const localesZh = await source("../src/i18n/locales/zh-CN.ts");
  assert.match(localesEn, /codexRouteChangedRestart: "\{\{status\}\}; restart Codex to apply"/);
  assert.match(localesZh, /codexRouteChangedRestart: "\{\{status\}\}；请重启 Codex 使其生效"/);
  assert.doesNotMatch(localesEn, /restart Codex Desktop to apply/);
  const handlers = await source("../../src-tauri/src/desktop_commands/handlers.rs");
  assert.match(
    handlers,
    /Connect\/disconnect writes the overlay while Codex stays open/,
  );
  assert.match(handlers, /serialize_config_writer\(\|\| \{/);
  assert.match(
    providers,
    /setCodexTargetOwnerOverride\(\s*nextMode === "custom" \? \(appFlavor\?\.routing_owner \?\? null\) : "official",\s*\)/,
  );
  assert.match(providers, /codexForeignOwner=\{codexOwnedByOtherApp\}/);
  assert.match(providers, /codexOwnerLabel=\{codexRouteOwnerLabel\}/);
  assert.match(providers, /foreignOwner[\s\S]*bg-emerald-100 text-emerald-700/);
  assert.match(providers, /!pendingMode && connected[\s\S]*bg-emerald-600 text-white/);
  assert.match(providers, /connectedToHubChannel/);
});
