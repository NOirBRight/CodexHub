import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const contractPath = new URL("../src/lib/ui-contract.json", import.meta.url);
const appPath = new URL("../src/App.tsx", import.meta.url);
const endpointRowPath = new URL("../src/components/EndpointRow.tsx", import.meta.url);
const gatewayClientCardPath = new URL("../src/components/GatewayClientCard.tsx", import.meta.url);
const gatewayPagePath = new URL("../src/pages/GatewayPage.tsx", import.meta.url);
const indexCssPath = new URL("../src/index.css", import.meta.url);
const pageToastPath = new URL("../src/components/PageToast.tsx", import.meta.url);
const providersPagePath = new URL("../src/pages/ProvidersPage.tsx", import.meta.url);
const runtimeBarPath = new URL("../src/components/RuntimeBar.tsx", import.meta.url);
const settingsDrawerPath = new URL("../src/components/SettingsDrawer.tsx", import.meta.url);
const sortableListPath = new URL("../src/components/SortableList.tsx", import.meta.url);
const stackedUsagePath = new URL("../src/components/StackedUsageChartShell.tsx", import.meta.url);
const tauriSourcePath = new URL("../src/lib/tauri.ts", import.meta.url);
const typesPath = new URL("../src/lib/types.ts", import.meta.url);
const tauriConfigPath = new URL("../../src-tauri/tauri.conf.json", import.meta.url);
const tauriMainPath = new URL("../../src-tauri/src/main.rs", import.meta.url);

async function readContract() {
  return JSON.parse(await readFile(contractPath, "utf8"));
}

test("main navigation exposes only CodexHub and Gateway", async () => {
  const contract = await readContract();

  assert.deepEqual(
    contract.tabs.map((tab) => tab.label),
    ["CodexHub", "Gateway"],
  );
});

test("runtime header removes flow chips and exposes desktop window controls", async () => {
  const [runtimeSource, tauriSource, tauriConfig] = await Promise.all([
    readFile(runtimeBarPath, "utf8"),
    readFile(tauriMainPath, "utf8"),
    readFile(tauriConfigPath, "utf8"),
  ]);

  assert.doesNotMatch(runtimeSource, /FlowChip/);
  assert.doesNotMatch(runtimeSource, /Hub ·|Clients ·/);
  assert.match(runtimeSource, /data-tauri-drag-region/);
  assert.match(runtimeSource, /windowMinimize/);
  assert.match(runtimeSource, /windowToggleMaximize/);
  assert.match(runtimeSource, /windowCloseToTray/);
  assert.match(runtimeSource, /Close to tray/);
  assert.match(tauriSource, /WindowEvent::CloseRequested/);
  assert.match(tauriSource, /TrayIconBuilder::with_id\("codexhub"\)/);
  assert.match(tauriSource, /Connect Codex to CodexHub/);
  assert.match(tauriSource, /Restart Codex App/);
  assert.match(tauriSource, /Get-StartApps/);
  assert.doesNotMatch(tauriSource, /Restart CodexHub/);
  assert.equal(JSON.parse(tauriConfig).app.windows[0].decorations, false);
});

test("global cursor contract marks interactive controls as pointer and disabled controls as unavailable", async () => {
  const css = await readFile(indexCssPath, "utf8");

  assert.match(css, /button:not\(:disabled\),\s*select:not\(:disabled\),\s*input\[type="checkbox"\]:not\(:disabled\),\s*input\[type="radio"\]:not\(:disabled\),\s*\[role="button"\]:not\(\[aria-disabled="true"\]\)\s*\{\s*cursor:\s*pointer;\s*\}/s);
  assert.match(css, /button:not\(:disabled\) \*,\s*\[role="button"\]:not\(\[aria-disabled="true"\]\) \*\s*\{\s*cursor:\s*inherit;\s*\}/s);
  assert.match(css, /label:has\(input\[type="checkbox"\]:not\(:disabled\)\),\s*label:has\(input\[type="radio"\]:not\(:disabled\)\)\s*\{\s*cursor:\s*pointer;\s*\}/s);
  assert.match(css, /label:has\(input\[type="checkbox"\]:not\(:disabled\)\) \*,\s*label:has\(input\[type="radio"\]:not\(:disabled\)\) \*\s*\{\s*cursor:\s*inherit;\s*\}/s);
  assert.match(css, /button:disabled,\s*select:disabled,\s*input:disabled,\s*\[aria-disabled="true"\]\s*\{\s*cursor:\s*not-allowed;\s*opacity:\s*0\.55;\s*\}/s);
  assert.match(css, /label:has\(input\[type="checkbox"\]:disabled\),\s*label:has\(input\[type="radio"\]:disabled\)\s*\{\s*cursor:\s*not-allowed;\s*\}/s);
});

test("copy buttons keep a fixed width when copied feedback appears", async () => {
  const [endpointSource, gatewaySource, providersSource] = await Promise.all([
    readFile(endpointRowPath, "utf8"),
    readFile(gatewayPagePath, "utf8"),
    readFile(providersPagePath, "utf8"),
  ]);

  assert.match(endpointSource, /inline-flex w-\[76px\]/);
  assert.doesNotMatch(endpointSource, /min-w-\[70px\]/);
  assert.match(gatewaySource, /inline-flex h-8 w-\[76px\]/);
  assert.match(providersSource, /inline-flex h-6 w-\[72px\]/);
  assert.doesNotMatch(providersSource, /min-w-\[66px\]/);
});

test("gateway OpenAI auth status uses unambiguous signed-in copy", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.match(gatewaySource, /label="OpenAI Auth"[\s\S]*value=\{authPresent \? "Signed in" : "Not signed in"\}/);
  assert.doesNotMatch(gatewaySource, /"Present"/);
});

test("sortable drag cursors override the global cursor contract", async () => {
  const [css, sortableSource] = await Promise.all([
    readFile(indexCssPath, "utf8"),
    readFile(sortableListPath, "utf8"),
  ]);

  assert.match(css, /\[data-sortable-handle="true"\][\s\S]*cursor:\s*grab !important;/);
  assert.match(css, /html\.sortable-list-dragging,[\s\S]*cursor:\s*grabbing !important;/);
  assert.match(sortableSource, /style=\{\{ cursor: draggedId \? "grabbing" : "grab" \}\}/);
});

test("gateway client rail is limited to the four planned clients", async () => {
  const contract = await readContract();

  assert.deepEqual(
    contract.gatewayClients.map((client) => client.name),
    ["OpenCode", "ZCode", "Pi", "OMP"],
  );
});

test("gateway client rail shows active managed config paths", async () => {
  const contract = await readContract();

  assert.deepEqual(
    Object.fromEntries(contract.gatewayClients.map((client) => [client.id, client.config_path])),
    {
      opencode: "~/.config/opencode/opencode.json",
      zcode: "~/.zcode/v2/config.json",
      pi: "~/.pi/agent/settings.json",
      omp: "~/.omp/agent/config.yml",
    },
  );
});

test("gateway empty states do not claim missing backends", async () => {
  const contract = await readContract();

  assert.equal(contract.pendingBackend.label, "no data");
  assert.match(contract.pendingBackend.usage, /Usage/i);
  assert.doesNotMatch(contract.pendingBackend.usage, /waiting|pending backend/i);
  assert.match(contract.pendingBackend.clients, /client/i);
});

test("gateway page is wired to real usage and client backend APIs", async () => {
  const [appSource, gatewaySource] = await Promise.all([
    readFile(appPath, "utf8"),
    readFile(gatewayPagePath, "utf8"),
  ]);

  assert.match(appSource, /api\.gatewayUsageSnapshot\(/);
  assert.match(appSource, /api\.listGatewayClients\(/);
  assert.match(gatewaySource, /usageSummary/);
  assert.match(gatewaySource, /usageStatus/);
  assert.match(gatewaySource, /usageError/);
  assert.match(gatewaySource, /clientInfos/);
});

test("web preview falls back to the bridge when host Tauri IPC is unavailable", async () => {
  const tauriSource = await readFile(tauriSourcePath, "utf8");

  assert.match(tauriSource, /function shouldFallbackToBridge\(error: unknown\)/);
  assert.match(tauriSource, /catch \(error\)/);
  assert.match(tauriSource, /if \(!shouldFallbackToBridge\(error\)\) \{\s*throw error;\s*\}/s);
  assert.match(tauriSource, /return bridgeInvoke<T>\(command, args\);/);
  assert.match(tauriSource, /unknown command|ipc|__TAURI_INTERNALS__/);
});

test("web bridge calls use simple POST requests that avoid CORS preflight", async () => {
  const tauriSource = await readFile(tauriSourcePath, "utf8");
  const bridgeInvoke =
    tauriSource.match(/async function bridgeInvoke[\s\S]*?function shouldFallbackToBridge/)?.[0] ?? "";

  assert.match(bridgeInvoke, /method:\s*"POST"/);
  assert.match(bridgeInvoke, /body:\s*JSON\.stringify\(\{ command, args: args \?\? \{\} \}\)/);
  assert.doesNotMatch(bridgeInvoke, /headers:\s*\{/);
  assert.doesNotMatch(bridgeInvoke, /application\/json/);
});

test("usage summary and chart use the same global time window", async () => {
  const [appSource, gatewaySource, usageSource, tauriSource] = await Promise.all([
    readFile(appPath, "utf8"),
    readFile(gatewayPagePath, "utf8"),
    readFile(stackedUsagePath, "utf8"),
    readFile(tauriSourcePath, "utf8"),
  ]);

  assert.match(appSource, /const \[usageWindow, setUsageWindow\]/);
  assert.match(appSource, /api\.gatewayUsageSnapshot\(usageWindow\)/);
  assert.doesNotMatch(appSource, /api\.gatewayUsageSummary\(usageWindow\)/);
  assert.doesNotMatch(appSource, /api\.gatewayUsageEvents\(usageWindow\)/);
  assert.doesNotMatch(appSource, /api\.gatewayUsageEvents\(100\)/);
  assert.match(gatewaySource, /onUsageWindowChange/);
  assert.match(usageSource, /onWindowChange\?\.\(queryWindow\)/);
  assert.match(usageSource, /function usageQueryWindow/);
  assert.doesNotMatch(usageSource, /function filterEventsByRange/);
  assert.match(tauriSource, /startTs/);
  assert.match(tauriSource, /endTs/);
});

test("usage telemetry uses a single snapshot call and keeps usage errors out of runtime banner", async () => {
  const [appSource, tauriSource, usageSource] = await Promise.all([
    readFile(appPath, "utf8"),
    readFile(tauriSourcePath, "utf8"),
    readFile(stackedUsagePath, "utf8"),
  ]);

  assert.match(tauriSource, /gatewayUsageSnapshot: \(window\?: UsageQueryWindow \| null\) =>/);
  assert.match(tauriSource, /call<GatewayUsageSnapshot>\("gateway_usage_snapshot"/);
  assert.match(appSource, /usageSnapshotResult/);
  assert.match(appSource, /gatewayUsageStatus/);
  assert.match(appSource, /usageError/);
  assert.doesNotMatch(
    appSource.match(/const rejected = \[[\s\S]*?\]\.find/)?.[0] ?? "",
    /usageSnapshotResult/,
  );
  assert.match(usageSource, /telemetryStatus/);
  assert.match(usageSource, /Indexing usage/);
});

test("usage custom date popover closes on outside click", async () => {
  const usageSource = await readFile(stackedUsagePath, "utf8");

  assert.match(usageSource, /customRangeRef/);
  assert.match(usageSource, /document\.addEventListener\("pointerdown", handlePointerDown\)/);
  assert.match(usageSource, /customRangeRef\.current\?\.contains\(target\)/);
  assert.match(usageSource, /document\.removeEventListener\("pointerdown", handlePointerDown\)/);
  assert.match(usageSource, /event\.key === "Escape"/);
});

test("gateway endpoint and copy panels use balanced 5:5 columns", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.match(gatewaySource, /grid-cols-\[minmax\(0,1fr\)_minmax\(0,1fr\)\]/);
  assert.doesNotMatch(gatewaySource, /0\.86fr|1\.14fr/);
});

test("gateway copy actions use inline copied state instead of success toasts", async () => {
  const [gatewaySource, endpointSource] = await Promise.all([
    readFile(gatewayPagePath, "utf8"),
    readFile(endpointRowPath, "utf8"),
  ]);

  assert.match(gatewaySource, /const \[copiedTarget, setCopiedTarget\]/);
  assert.match(gatewaySource, /markCopied\(target\)/);
  assert.match(endpointSource, /copied \? "Copied" : "Copy"/);
  assert.doesNotMatch(gatewaySource, /setMessage\(`\$\{label\} copied`\)/);
});

test("gateway client route switching reports completion", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.match(gatewaySource, /Switching \$\{clientName\} to \$\{routeName\}/);
  assert.match(gatewaySource, /showToast\(`Switching \$\{clientName\} to \$\{routeName\}\.\.\.`, "loading"\)/);
  assert.match(gatewaySource, /api\.switchGatewayClientRoute\(clientId, mode, defaultModel\)/);
  assert.ok(gatewaySource.includes("setMessage(`${clientName} switched to ${routeName}`)"));
});

test("gateway client route switching refreshes without version probes", async () => {
  const [appSource, gatewaySource] = await Promise.all([
    readFile(appPath, "utf8"),
    readFile(gatewayPagePath, "utf8"),
  ]);

  assert.match(gatewaySource, /await onRefreshClients\(\)/);
  assert.doesNotMatch(gatewaySource, /await onRefreshClients\(\{ includeClientVersions: true \}\)[\s\S]*setMessage\(`\$\{clientName\} switched/);
  assert.match(appSource, /void loadGatewayClients\(\)/);
  assert.doesNotMatch(appSource, /void loadGatewayClients\(\{ includeClientVersions: true \}\)/);
});

test("gateway toast uses the shared dismissible page toast", async () => {
  const [gatewaySource, pageToastSource] = await Promise.all([
    readFile(gatewayPagePath, "utf8"),
    readFile(pageToastPath, "utf8"),
  ]);

  assert.match(gatewaySource, /<PageToast toast=\{toast\} onDismiss=\{dismissToast\} \/>/);
  assert.match(gatewaySource, /toast\.tone === "loading"/);
  assert.match(gatewaySource, /window\.setTimeout\(\(\) => dismissToast\(\), 3000\)/);
  assert.match(gatewaySource, /<main className="relative grid/);
  assert.doesNotMatch(gatewaySource, /"fixed bottom-4 left-4/);
  assert.match(pageToastSource, /"absolute bottom-3 left-3 z-50/);
  assert.match(pageToastSource, /aria-label="Dismiss notification"/);
});

test("gateway client version refresh uses a persistent loading toast", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.match(gatewaySource, /showToast\("Refreshing gateway clients and checking versions\.\.\.", "loading"\)/);
  assert.match(gatewaySource, /await onRefreshClients\(\{ includeClientVersions: true \}\)/);
  assert.match(gatewaySource, /setMessage\("Gateway clients refreshed"\)/);
});

test("settings drawer hides non-functional route and endpoint toggles", async () => {
  const drawerSource = await readFile(settingsDrawerPath, "utf8");

  assert.doesNotMatch(drawerSource, /Default Codex route/);
  assert.doesNotMatch(drawerSource, /Enable \/v1\/models/);
  assert.doesNotMatch(drawerSource, /Enable \/v1\/responses/);
  assert.doesNotMatch(drawerSource, /Enable \/v1\/chat\/completions/);
});

test("settings exposes bound client auto-sync instead of catalog auto-sync", async () => {
  const [drawerSource, settingsSource] = await Promise.all([
    readFile(settingsDrawerPath, "utf8"),
    readFile(new URL("../src/pages/SettingsPage.tsx", import.meta.url), "utf8"),
  ]);

  assert.match(drawerSource, /Auto-sync bound clients/);
  assert.match(drawerSource, /auto_sync_clients/);
  assert.doesNotMatch(drawerSource, /Auto-sync catalog/);
  assert.match(settingsSource, /Auto-sync bound clients/);
});

test("settings drawer hides the local client key without adapter explainer copy", async () => {
  const drawerSource = await readFile(settingsDrawerPath, "utf8");

  assert.match(drawerSource, /type=\{showClientKey \? "text" : "password"\}/);
  assert.match(drawerSource, /Show local client key/);
  assert.match(drawerSource, /Hide local client key/);
  assert.doesNotMatch(drawerSource, /Client adapters/);
  assert.doesNotMatch(drawerSource, /partial support/);
});

test("settings drawer uses switch toggles and exposes history repair as a settings action", async () => {
  const drawerSource = await readFile(settingsDrawerPath, "utf8");

  assert.match(drawerSource, /className="peer sr-only"/);
  assert.match(drawerSource, /peer-checked:bg-action/);
  assert.match(drawerSource, /Unified Codex history/);
  assert.match(drawerSource, /Repair history bucket/);
  assert.match(drawerSource, /draft\.unified_codex_history \? "custom" : "openai"/);
  assert.match(drawerSource, /onClick=\{\(\) => void repairHistory\(\)\}/);
  assert.doesNotMatch(drawerSource, /Migrate official history/);
  assert.doesNotMatch(drawerSource, /Restore migrated official history/);
  assert.doesNotMatch(drawerSource, /Auto-sync history/);
  assert.doesNotMatch(drawerSource, />\s*Sync history\s*</);
});

test("gateway client card does not render a disabled fake updater", async () => {
  const cardSource = await readFile(gatewayClientCardPath, "utf8");

  assert.match(cardSource, /Manual update available/);
  assert.doesNotMatch(cardSource, /<button[\s\S]*?\{hasUpdate \? "Manual" : "Update"\}/);
  assert.doesNotMatch(cardSource, /safe updater is not exposed by the backend/);
});

test("provider model removal persists through provider save path", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /function removeModel\(modelId: string\)/);
  assert.match(providersSource, /onChange\(next, "Model removed"\)/);
  assert.match(providersSource, /onRemove=\{removeModel\}/);
});

test("providers page uses stable zero-min split columns", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /grid-cols-\[minmax\(0,4fr\)_minmax\(0,6fr\)\]/);
  assert.match(providersSource, /<aside className="min-h-0 min-w-0 overflow-hidden/);
  assert.match(providersSource, /<section className="min-h-0 min-w-0 overflow-hidden/);
  assert.doesNotMatch(providersSource, /grid-cols-\[minmax\(360px,4fr\)_minmax\(0,6fr\)\]/);
});

test("app content region does not create provider-level scrollbars", async () => {
  const appSource = await readFile(appPath, "utf8");

  assert.match(appSource, /className="min-h-0 overflow-hidden p-4"/);
  assert.doesNotMatch(appSource, /className="min-h-0 overflow-auto p-4"/);
});

test("provider detail keeps model area tall and moves the scrollbar outside cards", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const providerDetail = providersSource.match(/function ProviderDetail[\s\S]*?function ModelSection/)?.[0] ?? "";
  const providerCapabilitiesPanel =
    providersSource.match(/function ProviderCapabilitiesPanel[\s\S]*?function boolCapabilityState/)?.[0] ?? "";
  const modelSection = providersSource.match(/function ModelSection[\s\S]*?function ModelIdentity/)?.[0] ?? "";

  assert.match(providerDetail, /className="grid gap-2 border-b border-line p-4"/);
  assert.match(providerDetail, /className="grid gap-2 lg:grid-cols-2"/);
  assert.match(providerDetail, /className="field field-compact"/);
  assert.match(providerDetail, /className="lg:col-span-2"/);
  assert.match(providerDetail, /<div className="lg:col-span-2">\s*<ProviderCapabilitiesPanel/);
  assert.doesNotMatch(providerDetail, /className="grid gap-4 border-b border-line p-5"/);
  assert.match(providerCapabilitiesPanel, /className="flex min-w-0 items-center justify-between gap-2/);
  assert.match(providerCapabilitiesPanel, /className="flex min-w-0 items-center gap-2/);
  assert.match(providerCapabilitiesPanel, /className="flex shrink-0 gap-2/);
  assert.doesNotMatch(providerCapabilitiesPanel, /flex-wrap/);

  assert.match(modelSection, /className="min-h-0 overflow-auto -mr-3 pr-3"/);
  assert.doesNotMatch(modelSection, /className="min-h-0 overflow-auto pr-1"/);
});

test("official model rows remain pointer-interactive while editing is disabled", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const modelSection = providersSource.match(/function ModelSection[\s\S]*?function providerQualifiedModelId/)?.[0] ?? "";

  assert.match(modelSection, /const rowInteractable = !disabled \|\| Boolean\(onToggleOfficialModel\);/);
  assert.match(modelSection, /function activateModelRow\(\)[\s\S]*onToggleOfficialModel\(model\.id, !modelEnabled\)/);
  assert.match(modelSection, /rowInteractable && "cursor-pointer"/);
  assert.match(modelSection, /role=\{rowInteractable \? "button" : undefined\}/);
  assert.match(modelSection, /tabIndex=\{rowInteractable \? 0 : undefined\}/);
  assert.match(modelSection, /onClick=\{rowInteractable \? activateModelRow : undefined\}/);
});

test("official OpenAI source uses the same row card and toggle pattern as providers", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const sidebar = providersSource.match(/<OfficialOpenAICard[\s\S]*?\/>/)?.[0] ?? "";
  const officialCard = providersSource.match(/function OfficialOpenAICard[\s\S]*?function HubConnectionBridge/)?.[0] ?? "";

  assert.match(sidebar, /enabledModelCount=\{officialEnabledCount\}/);
  assert.match(sidebar, /included=\{officialIncluded\}/);
  assert.match(sidebar, /onToggleInclude=\{onToggleOfficialInclude\}/);
  assert.match(officialCard, /<ProviderNavButton/);
  assert.match(officialCard, /label="OpenAI"/);
  assert.match(officialCard, /meta=\{`\$\{enabledModelCount\}\/\$\{modelCount\} models`\}/);
  assert.match(officialCard, /enabled=\{included\}/);
  assert.match(officialCard, /onToggle=\{onToggleInclude\}/);
  assert.match(officialCard, /"grid gap-3 rounded-md border border-line bg-panel p-3"/);
  assert.match(officialCard, /active=\{active\}/);
  assert.doesNotMatch(officialCard, /<SourceMetric label="Official models"/);
  assert.doesNotMatch(officialCard, /active \? "border-action bg-blue-50\/70"/);
});

test("official include toggle is removed from the detail header", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const officialDetail = providersSource.match(/function OfficialDetail[\s\S]*?function ProviderDetail/)?.[0] ?? "";

  assert.doesNotMatch(officialDetail, /Include in Codex Hub/);
  assert.doesNotMatch(officialDetail, /onToggleInclude/);
  assert.doesNotMatch(officialDetail, /included:/);
});

test("official refresh action is placed in the Models toolbar", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const officialDetail = providersSource.match(/function OfficialDetail[\s\S]*?function ProviderDetail/)?.[0] ?? "";
  const modelSection = providersSource.match(/function ModelSection[\s\S]*?function providerQualifiedModelId/)?.[0] ?? "";

  assert.doesNotMatch(officialDetail, /IconButton title="Refresh official models"/);
  assert.match(officialDetail, /onRefresh=\{onRefresh\}/);
  assert.match(officialDetail, /refreshBusy=\{busy === "official-refresh"\}/);
  assert.match(modelSection, /onRefresh\?: \(\) => void;/);
  assert.match(modelSection, /refreshBusy\?: boolean;/);
  assert.match(modelSection, /\{onRefresh && \(/);
  assert.match(modelSection, />\s*Refresh\s*<\/button>/);
});

test("official model list does not expose unsupported drag sorting", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const officialDetail = providersSource.match(/function OfficialDetail[\s\S]*?function ProviderDetail/)?.[0] ?? "";
  const modelSection = providersSource.match(/function ModelSection[\s\S]*?function providerQualifiedModelId/)?.[0] ?? "";

  assert.match(officialDetail, /reorderable=\{false\}/);
  assert.match(modelSection, /reorderable = true/);
  assert.match(modelSection, /reorderable \? \(/);
  assert.match(modelSection, /<SortableList/);
  assert.match(modelSection, /models\.map\(\(model\)[\s\S]*renderModelRow\(model\)/);
});

test("Codex Hub connection CTA is prominent and has a connecting state", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const sidebar = providersSource.match(/<HubConnectionBridge[\s\S]*?\/>/)?.[0] ?? "";
  const bridge = providersSource.match(/function HubConnectionBridge[\s\S]*?function CodexHubProviderCard/)?.[0] ?? "";

  assert.match(sidebar, /connecting=\{busy === "route"\}/);
  assert.match(bridge, /connecting:/);
  assert.match(bridge, /Connecting/);
  assert.match(bridge, /Connect to Codex Hub/);
  assert.match(bridge, /Connected Codex Hub/);
  assert.match(bridge, /animate-pulse/);
  assert.match(bridge, /h-11/);
  assert.match(bridge, /bg-action/);
  assert.match(bridge, /bg-emerald-600/);
});

test("Codex Hub connection action reports progress immediately", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const action = providersSource.match(/async function toggleCodexHubConnection\(\)[\s\S]*?async function reorderOfficialModels/)?.[0] ?? "";

  assert.match(action, /const actionLabel = nextMode === "custom" \? "Connecting Codex App to Codex Hub" : "Disconnecting Codex App from Codex Hub";/);
  assert.match(action, /showToast\(`\$\{actionLabel\}\.\.\.`, "loading"\);/);
  assert.ok(action.indexOf("showToast(`${actionLabel}...`, \"loading\");") < action.indexOf("api.switchMode("));
  assert.match(action, /api\.switchMode\(nextMode, false\)/);
  assert.doesNotMatch(action, /historyHint/);
  assert.match(action, /setMessage\(codexHubConnectionSuccessMessage\(nextMode\)\)/);
});

test("Codex Hub connection failures no longer mention history sync", async () => {
  const [providersSource, pageToastSource] = await Promise.all([
    readFile(providersPagePath, "utf8"),
    readFile(pageToastPath, "utf8"),
  ]);
  const action = providersSource.match(/async function toggleCodexHubConnection\(\)[\s\S]*?async function reorderOfficialModels/)?.[0] ?? "";

  assert.match(action, /setError\(codexHubConnectionErrorMessage\(err\)\)/);
  assert.match(providersSource, /function codexHubConnectionErrorMessage\(err: unknown\)/);
  assert.doesNotMatch(providersSource, /Connection failed while syncing history/);
  assert.doesNotMatch(providersSource, /Turn off Auto-sync history/);
  assert.match(providersSource, /Codex Hub connection failed/);
  assert.match(pageToastSource, /toast\.tone === "error" \? "max-h-32 overflow-auto whitespace-pre-wrap break-words" : "truncate"/);
});

test("Codex Hub connection ignores structured history sync fields from switch status", async () => {
  const [providersSource, typesSource] = await Promise.all([
    readFile(providersPagePath, "utf8"),
    readFile(typesPath, "utf8"),
  ]);
  const action = providersSource.match(/async function toggleCodexHubConnection\(\)[\s\S]*?async function reorderOfficialModels/)?.[0] ?? "";

  assert.match(typesSource, /history_sync_status\?: string \| null;/);
  assert.match(typesSource, /history_sync_message\?: string \| null;/);
  assert.doesNotMatch(action, /status\.history_sync_status/);
  assert.doesNotMatch(action, /history sync failed/);
  assert.doesNotMatch(action, /history sync skipped/);
});

test("Codex Hub connection does not retry after history sync failures", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const action = providersSource.match(/async function toggleCodexHubConnection\(\)[\s\S]*?async function reorderOfficialModels/)?.[0] ?? "";

  assert.doesNotMatch(action, /historyError/);
  assert.doesNotMatch(action, /without history sync/);
  assert.match(action, /api\.switchMode\(nextMode, false\)/);
  assert.doesNotMatch(providersSource, /function codexHubHistorySyncErrorMessage/);
});

test("unknown provider model metadata is not displayed as a 200K default", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /function formatContextWindow\(value\?: number \| null\)[\s\S]*return "Unknown";/);
  assert.match(providersSource, /context_window: model\.context_window \?\? null/);
  assert.doesNotMatch(providersSource, /context_window: model\.context_window \?\? 200_000/);
});

test("provider discovery updates the selected provider and reports progress", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /showToast\(`Discovering \$\{provider\.name\} models/);
  assert.match(providersSource, /const nextProvider = \{\s*\.\.\.provider,\s*models: mergeDiscoveredModels\(provider\.models, models\),\s*\}/s);
  assert.match(providersSource, /setProviders\(nextProviders\)/);
  assert.match(providersSource, /\$\{provider\.name\}: discovered \$\{models\.length\} model/);
});

test("provider discovery preserves missing API key environment variable names", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /const missingEnv = message\.match/);
  assert.match(providersSource, /Discovery failed: \$\{missingEnv\[1\]\} is not set/);
});

test("providers toast is locally anchored, dismissible, and auto-dismisses", async () => {
  const [providersSource, pageToastSource] = await Promise.all([
    readFile(providersPagePath, "utf8"),
    readFile(pageToastPath, "utf8"),
  ]);

  assert.match(providersSource, /toast\.tone !== "info"/);
  assert.match(providersSource, /window\.setTimeout\(\(\) => dismissToast\(\), 3000\)/);
  assert.match(providersSource, /<PageToast toast=\{toast\} onDismiss=\{dismissToast\} \/>/);
  assert.match(pageToastSource, /"absolute bottom-3 left-3 z-50/);
  assert.match(pageToastSource, /aria-label="Dismiss notification"/);
  assert.match(pageToastSource, /animate-spin/);
  assert.doesNotMatch(providersSource, /"fixed bottom-4 left-4/);
});

test("model copy uses provider-qualified identifiers", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /function providerQualifiedModelId\(providerId: string, modelId: string\)/);
  assert.match(providersSource, /navigator\.clipboard\.writeText\(copyValue\)/);
  assert.match(providersSource, /\{copied \? "Copied" : "Copy"\}/);
  assert.match(providersSource, /<ModelIdentity model=\{model\} providerId=\{providerId\} \/>/);
});

test("provider write actions keep explicit success feedback", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /successMessage\?: string/);
  assert.match(providersSource, /`\$\{providerName\} added`/);
  assert.match(providersSource, /`\$\{target\.name\} deleted`/);
  assert.match(providersSource, /onChange\(draft, `\$\{draft\.name\} saved`\)/);
  assert.match(providersSource, /onChange\(next, "Model removed"\)/);
});

test("provider catalog writes trigger best-effort bound client sync", async () => {
  const [providersSource, tauriSource] = await Promise.all([
    readFile(providersPagePath, "utf8"),
    readFile(tauriSourcePath, "utf8"),
  ]);

  assert.match(tauriSource, /syncGatewayClients/);
  assert.match(tauriSource, /"sync_gateway_clients"/);
  assert.match(providersSource, /updateGatewayAfterCatalog/);
  assert.match(providersSource, /api\.syncGatewayClients\(\)/);
  assert.match(providersSource, /auto_sync_clients/);
});

test("settings drawer reports the backend sync result", async () => {
  const [appSource, drawerSource, tauriSource] = await Promise.all([
    readFile(appPath, "utf8"),
    readFile(settingsDrawerPath, "utf8"),
    readFile(tauriSourcePath, "utf8"),
  ]);

  assert.match(appSource, /const message = await api\.syncHistory\(targetProvider\)/);
  assert.match(appSource, /api\.migrateOfficialHistoryToUnified\(\)/);
  assert.match(appSource, /api\.restoreOfficialHistoryFromUnified\(\)/);
  assert.match(appSource, /return message/);
  assert.match(drawerSource, /onSyncHistory: \(targetProvider: string\) => Promise<string>/);
  assert.match(drawerSource, /const result = await onSyncHistory\(targetProvider\)/);
  assert.doesNotMatch(drawerSource, /onMigrateOfficialHistory/);
  assert.doesNotMatch(drawerSource, /onRestoreOfficialHistory/);
  assert.match(tauriSource, /migrateOfficialHistoryToUnified: \(\) =>\s*call<string>\("migrate_official_history_to_unified"\)/);
  assert.match(tauriSource, /restoreOfficialHistoryFromUnified: \(\) =>\s*call<string>\("restore_official_history_from_unified"\)/);
  assert.doesNotMatch(drawerSource, /History sync requested/);
});

test("legacy provider hidden capability is removed from model/provider UI state", async () => {
  const [typesSource, formatSource, appSource, providersSource, modelsSource] = await Promise.all([
    readFile(new URL("../src/lib/types.ts", import.meta.url), "utf8"),
    readFile(new URL("../src/lib/format.ts", import.meta.url), "utf8"),
    readFile(appPath, "utf8"),
    readFile(providersPagePath, "utf8"),
    readFile(new URL("../src/pages/ModelsPage.tsx", import.meta.url), "utf8"),
  ]);

  assert.doesNotMatch(typesSource, /hidden\?: boolean/);
  assert.doesNotMatch(formatSource, /hidden:/);
  assert.doesNotMatch(appSource, /provider\.hidden/);
  assert.doesNotMatch(providersSource, /provider\.hidden|model\.hidden|hidden: false/);
  assert.doesNotMatch(modelsSource, /label="Hidden"|hidden: checked/);
});
