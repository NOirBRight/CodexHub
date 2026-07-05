import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const contractPath = new URL("../src/lib/ui-contract.json", import.meta.url);
const appPath = new URL("../src/App.tsx", import.meta.url);
const endpointRowPath = new URL("../src/components/EndpointRow.tsx", import.meta.url);
const gatewayClientCardPath = new URL("../src/components/GatewayClientCard.tsx", import.meta.url);
const segmentedSwitchPath = new URL("../src/components/SegmentedSwitch.tsx", import.meta.url);
const gatewayPagePath = new URL("../src/pages/GatewayPage.tsx", import.meta.url);
const indexCssPath = new URL("../src/index.css", import.meta.url);
const pageToastPath = new URL("../src/components/PageToast.tsx", import.meta.url);
const providersPagePath = new URL("../src/pages/ProvidersPage.tsx", import.meta.url);
const runtimeBarPath = new URL("../src/components/RuntimeBar.tsx", import.meta.url);
const settingsLibPath = new URL("../src/lib/settings.ts", import.meta.url);
const settingsDrawerPath = new URL("../src/components/SettingsDrawer.tsx", import.meta.url);
const sortableListPath = new URL("../src/components/SortableList.tsx", import.meta.url);
const stackedUsagePath = new URL("../src/components/StackedUsageChartShell.tsx", import.meta.url);
const tauriSourcePath = new URL("../src/lib/tauri.ts", import.meta.url);
const tailwindConfigPath = new URL("../tailwind.config.js", import.meta.url);
const typesPath = new URL("../src/lib/types.ts", import.meta.url);
const designPath = new URL("../../DESIGN.md", import.meta.url);
const tauriConfigPath = new URL("../../src-tauri/tauri.conf.json", import.meta.url);
const tauriMainPath = new URL("../../src-tauri/src/main.rs", import.meta.url);
const i18nIndexPath = new URL("../src/i18n/index.ts", import.meta.url);
const enLocalePath = new URL("../src/i18n/locales/en-US.ts", import.meta.url);
const zhLocalePath = new URL("../src/i18n/locales/zh-CN.ts", import.meta.url);

async function readContract() {
  return JSON.parse(await readFile(contractPath, "utf8"));
}

function parseLocaleObject(source) {
  const objectSource = source.match(/const\s+\w+\s*=\s*(\{[\s\S]*\});\s*export default/)?.[1];
  assert.ok(objectSource, "locale source should export a plain object");
  return Function(`"use strict"; return (${objectSource});`)();
}

function flattenKeys(value, prefix = "") {
  return Object.entries(value).flatMap(([key, child]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    if (child && typeof child === "object" && !Array.isArray(child)) {
      return flattenKeys(child, path);
    }
    return [path];
  });
}

test("i18n locales are registered and keep matching translation keys", async () => {
  const [indexSource, enSource, zhSource] = await Promise.all([
    readFile(i18nIndexPath, "utf8"),
    readFile(enLocalePath, "utf8"),
    readFile(zhLocalePath, "utf8"),
  ]);

  assert.match(indexSource, /SUPPORTED_LOCALES\s*=\s*\["zh-CN",\s*"en-US"\]\s*as const/);
  assert.deepEqual(flattenKeys(parseLocaleObject(zhSource)).sort(), flattenKeys(parseLocaleObject(enSource)).sort());
});

test("default locale resolution treats Chinese system variants as Chinese and otherwise falls back to English", async () => {
  const indexSource = await readFile(i18nIndexPath, "utf8");

  assert.match(indexSource, /function isChineseLocale/);
  assert.match(indexSource, /normalizedLocale === "zh"/);
  assert.match(indexSource, /normalizedLocale\?\.startsWith\("zh-"\) === true/);
  assert.match(indexSource, /replace\(\/_\/g, "-"\)/);
  assert.match(indexSource, /return DEFAULT_LOCALE/);
  assert.match(indexSource, /const primaryLanguage = globalThis\.navigator\?\.language\?\.trim\(\)/);
  assert.match(indexSource, /const primaryLanguageFromList = globalThis\.navigator\?\.languages\?\.find/);
  assert.match(indexSource, /return resolveLocale\(primaryLanguage \|\| primaryLanguageFromList\)/);
});

test("main navigation exposes only CodexHub and Gateway", async () => {
  const contract = await readContract();
  const appSource = await readFile(appPath, "utf8");

  assert.deepEqual(
    contract.tabs.map((tab) => tab.id),
    ["codexhub", "gateway"],
  );
  assert.ok(contract.tabs.every((tab) => !("label" in tab)));
  assert.doesNotMatch(appSource, /exportedCount/);
  assert.doesNotMatch(appSource, /tab\.id === "gateway" && exportedCount/);
});

test("ui contract keeps ids and paths but no localizable display copy", async () => {
  const contract = await readContract();

  assert.ok(!("pendingBackend" in contract));
  assert.ok(contract.gatewayClients.every((client) => !("kind" in client) && !("description" in client)));
  assert.deepEqual(
    contract.gatewayClients.map((client) => ({ id: client.id, name: client.name, config_path: client.config_path })),
    [
      { id: "opencode", name: "OpenCode", config_path: "~/.config/opencode/opencode.json" },
      { id: "zcode", name: "ZCode", config_path: "~/.zcode/v2/config.json" },
      { id: "pi", name: "Pi", config_path: "~/.pi/agent/settings.json" },
      { id: "omp", name: "OMP", config_path: "~/.omp/agent/config.yml" },
    ],
  );
});

test("gateway request timeout defaults to 300 seconds across UI and runtime", async () => {
  const [settingsSource, gatewaySource, tauriSource] = await Promise.all([
    readFile(settingsLibPath, "utf8"),
    readFile(gatewayPagePath, "utf8"),
    readFile(tauriMainPath, "utf8"),
  ]);

  assert.match(settingsSource, /gateway_request_timeout_seconds:\s*300/);
  assert.doesNotMatch(settingsSource, /gateway_request_timeout_seconds:\s*120/);
  assert.doesNotMatch(gatewaySource, /gateway_request_timeout_seconds\s*\?\?\s*120/);
  assert.doesNotMatch(gatewaySource, /setDraftTimeout\(settings\?\.gateway_request_timeout_seconds\s*\?\?\s*120\)/);
  assert.match(tauriSource, /gateway_request_timeout_seconds:\s*300/);
  assert.doesNotMatch(tauriSource, /gateway_request_timeout_seconds:\s*120/);
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
  assert.match(runtimeSource, /t\("runtime\.closeToTray"\)/);
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

test("visual system defines warm surfaces, concentric radii, and layered shadows", async () => {
  const [tailwindConfig, designDoc] = await Promise.all([
    readFile(tailwindConfigPath, "utf8"),
    readFile(designPath, "utf8"),
  ]);

  assert.match(tailwindConfig, /canvas:\s*"#f8f8f7"/);
  assert.match(tailwindConfig, /surface:\s*"#ffffff"/);
  assert.match(tailwindConfig, /panel:\s*"#f4f3f0"/);
  assert.match(tailwindConfig, /line:\s*"#dedbd6"/);
  assert.match(tailwindConfig, /control:\s*"10px"/);
  assert.match(tailwindConfig, /inner:\s*"12px"/);
  assert.match(tailwindConfig, /panel:\s*"16px"/);
  assert.match(tailwindConfig, /overlay:\s*"20px"/);
  assert.match(tailwindConfig, /card:\s*"0 0 0 1px rgba\(31, 41, 51, 0\.07\), 0 10px 28px -22px rgba\(31, 41, 51, 0\.35\)"/);
  assert.match(tailwindConfig, /floating:\s*"0 0 0 1px rgba\(31, 41, 51, 0\.08\), 0 18px 46px -28px rgba\(31, 41, 51, 0\.45\)"/);

  assert.match(designDoc, /## Visual System/);
  assert.match(designDoc, /Outer radius = inner radius \+ padding/);
  assert.match(designDoc, /Use layered shadows for element depth/);
  assert.match(designDoc, /Scrollable regions/);
  assert.match(designDoc, /overflow-auto -mr-3 pr-1/);
  assert.match(designDoc, /normal panel padding when the content does not\s+overflow/);
  assert.match(designDoc, /content\s+should gain\s+width when the scrollbar moves outward/);
  assert.match(designDoc, /sidebars,\s+drawers,\s+model\s+lists,\s+client\s+lists,\s+and\s+popovers/);
});

test("global controls use polished radius, shadow, and exact transitions", async () => {
  const css = await readFile(indexCssPath, "utf8");

  assert.match(css, /\.focus-ring\s*\{[\s\S]*ring-action\/20/);
  assert.match(css, /\.field\s*\{[\s\S]*rounded-control[\s\S]*shadow-field[\s\S]*transition-\[box-shadow,border-color,background-color\]/);
  assert.match(css, /\.select-trigger\s*\{[\s\S]*rounded-control[\s\S]*shadow-field[\s\S]*transition-\[box-shadow,border-color,background-color\]/);
  assert.match(css, /\.select-popover\s*\{[\s\S]*rounded-inner[\s\S]*shadow-floating/);
  assert.match(css, /\.select-option\s*\{[\s\S]*rounded-control[\s\S]*aria-selected:bg-action\/10/);
  assert.match(css, /\.mini-button\s*\{[\s\S]*rounded-control[\s\S]*shadow-control[\s\S]*active:scale-\[0\.96\]/);
  assert.doesNotMatch(css, /\.field\s*\{[\s\S]*rounded-md[\s\S]*shadow-subtle/);
});

test("copy buttons keep stable dimensions when copied feedback appears", async () => {
  const [endpointSource, gatewaySource, providersSource] = await Promise.all([
    readFile(endpointRowPath, "utf8"),
    readFile(gatewayPagePath, "utf8"),
    readFile(providersPagePath, "utf8"),
  ]);

  assert.match(endpointSource, /inline-flex shrink-0/);
  assert.match(endpointSource, /compact \? "h-6 w-6" : "h-8 w-8"/);
  assert.doesNotMatch(endpointSource, /min-w-\[70px\]/);
  assert.match(gatewaySource, /inline-flex h-8 w-8 shrink-0/);
  assert.match(providersSource, /inline-flex h-6 w-6 shrink-0/);
  assert.doesNotMatch(providersSource, /min-w-\[66px\]/);
});

test("gateway OpenAI auth status uses unambiguous signed-in copy", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.match(gatewaySource, /label=\{t\("gateway\.openaiAuth"\)\}[\s\S]*value=\{authPresent \? t\("gateway\.signedIn"\) : t\("gateway\.notSignedIn"\)\}/);
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

test("gateway empty states are localized outside the static contract", async () => {
  const contract = await readContract();
  const [gatewaySource, usageSource] = await Promise.all([
    readFile(gatewayPagePath, "utf8"),
    readFile(stackedUsagePath, "utf8"),
  ]);

  assert.ok(!("pendingBackend" in contract));
  assert.match(gatewaySource, /t\("gateway\.pendingUsage"\)/);
  assert.match(usageSource, /t\("usage\.pendingData"\)/);
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

test("backend unavailable errors stay short and can render toast actions", async () => {
  const [tauriSource, pageToastSource, providersSource, gatewaySource] = await Promise.all([
    readFile(tauriSourcePath, "utf8"),
    readFile(pageToastPath, "utf8"),
    readFile(providersPagePath, "utf8"),
    readFile(gatewayPagePath, "utf8"),
  ]);

  assert.match(tauriSource, /throw new Error\("Backend is not connected"\)/);
  assert.match(tauriSource, /function isBackendDisconnectedMessage\(message: string\)/);
  assert.doesNotMatch(tauriSource, /Start it with: cargo run -- web-bridge --port 1421/);
  assert.match(pageToastSource, /action\?:/);
  assert.match(pageToastSource, /toast\.action/);
  assert.match(pageToastSource, /\{toast\.action\.label\}/);
  assert.match(providersSource, /showBackendDisconnectedToast/);
  assert.match(providersSource, /label: t\("gateway\.startBackend"\)/);
  assert.match(providersSource, /onStartProxy\?: \(\) => Promise<void>;/);
  assert.match(gatewaySource, /showBackendDisconnectedToast/);
  assert.match(gatewaySource, /label: t\("gateway\.startBackend"\)/);
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
  assert.match(usageSource, /const \[hiddenSeriesKeys, setHiddenSeriesKeys\] = useState<Set<string>>/);
  assert.match(usageSource, /visibleUsageSummary\(/);
  assert.match(usageSource, /hiddenSeriesKeys\.has\(segment\.key\)/);
  assert.match(usageSource, /onHiddenSeriesKeysChange=\{setHiddenSeriesKeys\}/);
  assert.match(usageSource, /<Metric label=\{t\("gateway\.tokens"\)\} value=\{visibleSummary/);
  assert.match(usageSource, /className="grid grid-cols-4 gap-2"/);
  assert.doesNotMatch(usageSource, /sm:grid-cols-4/);
  assert.doesNotMatch(usageSource, /function filterEventsByRange/);
  assert.match(tauriSource, /startTs/);
  assert.match(tauriSource, /endTs/);
});

test("usage telemetry uses a single snapshot call and keeps usage errors out of runtime banner", async () => {
  const [appSource, gatewaySource, tauriSource, usageSource] = await Promise.all([
    readFile(appPath, "utf8"),
    readFile(gatewayPagePath, "utf8"),
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
  assert.doesNotMatch(usageSource, /Indexing usage/);
  assert.match(gatewaySource, /lastUsageErrorToast/);
  assert.match(gatewaySource, /const text = isBackendDisconnectedMessage\(usageError\)[\s\S]*t\("gateway\.usageTelemetryDelayed", \{ message: usageError \}\);/);
  assert.match(gatewaySource, /if \(isBackendDisconnectedMessage\(usageError\)\) \{\s*showBackendDisconnectedToast\(\);\s*return;\s*\}/);
  assert.match(gatewaySource, /showToast\(text, "error"\)/);
  assert.doesNotMatch(usageSource, /Usage telemetry delayed/);
  assert.doesNotMatch(usageSource, /usageError/);
});

test("usage custom date popover closes on outside click", async () => {
  const usageSource = await readFile(stackedUsagePath, "utf8");

  assert.match(usageSource, /customRangeRef/);
  assert.match(usageSource, /document\.addEventListener\("pointerdown", handlePointerDown\)/);
  assert.match(usageSource, /customRangeRef\.current\?\.contains\(target\)/);
  assert.match(usageSource, /document\.removeEventListener\("pointerdown", handlePointerDown\)/);
  assert.match(usageSource, /event\.key === "Escape"/);
  assert.match(usageSource, /bg-action\/15 text-ink/);
  assert.doesNotMatch(usageSource, /bg-slate-100 text-ink/);
});

test("gateway layout reserves space for the client rail", async () => {
  const [gatewaySource, usageSource] = await Promise.all([
    readFile(gatewayPagePath, "utf8"),
    readFile(stackedUsagePath, "utf8"),
  ]);

  assert.match(gatewaySource, /min-h-\[704px\] min-w-\[972px\] grid-cols-\[minmax\(636px,1fr\)_minmax\(320px,340px\)\] gap-4/);
  assert.match(gatewaySource, /<section className="grid min-h-0 min-w-0/);
  assert.match(gatewaySource, /grid min-w-0 gap-3 overflow-hidden rounded-panel bg-surface p-3/);
  assert.doesNotMatch(gatewaySource, /max-h-8 max-w-xl overflow-hidden text-xs leading-4/);
  assert.doesNotMatch(gatewaySource, /\[-webkit-line-clamp:2\]/);
  assert.doesNotMatch(gatewaySource, /Local API key, port, and timeout for OpenAI-compatible clients\./);
  assert.doesNotMatch(gatewaySource, /Clients discover models from/);
  assert.match(gatewaySource, /grid-cols-\[minmax\(300px,1fr\)_minmax\(270px,0\.95fr\)\] items-stretch gap-3/);
  assert.match(gatewaySource, /grid h-full min-w-0 grid-rows-\[auto_1fr\] gap-3 rounded-panel bg-panel p-3 shadow-card/);
  assert.match(gatewaySource, /grid min-w-0 self-end content-start gap-3 rounded-inner bg-surface p-3 shadow-control/);
  assert.match(gatewaySource, /grid h-full min-w-0 content-start gap-3 rounded-panel bg-panel p-3 shadow-card/);
  assert.match(gatewaySource, /grid min-w-0 grid-cols-\[minmax\(0,1fr\)_auto_auto\] items-center gap-2/);
  assert.match(gatewaySource, /grid-cols-\[minmax\(64px,0\.75fr\)_minmax\(64px,0\.75fr\)_minmax\(112px,0\.9fr\)\] items-end gap-2/);
  assert.match(gatewaySource, /className="focus-ring inline-flex h-9 self-end/);
  assert.match(gatewaySource, /whitespace-nowrap rounded-control bg-ink/);
  assert.match(gatewaySource, /className="flex items-center justify-between gap-3 whitespace-nowrap"/);
  assert.match(gatewaySource, /<h3 className="shrink-0 text-sm font-semibold text-ink">\{t\("gateway\.copyConnection"\)\}<\/h3>/);
  assert.match(gatewaySource, /<aside className="grid h-full min-h-\[704px\] grid-rows-\[auto_minmax\(0,1fr\)\]/);
  assert.match(gatewaySource, /clients\.length > 4 \? "min-h-0 overflow-auto" : "overflow-visible"/);
  assert.match(gatewaySource, /clients\.length > 4 \? "auto-rows-\[minmax\(144px,auto\)\]" : "min-h-full auto-rows-fr"/);
  assert.match(usageSource, /min-h-\[320px\] min-w-0 grid-rows-\[auto_auto_minmax\(0,1fr\)\].*overflow-hidden rounded-panel bg-surface/);
  assert.match(usageSource, /<div className="flex min-w-0 items-center justify-between gap-3">/);
  assert.match(usageSource, /<div className="flex shrink-0 items-center justify-end gap-1\.5">/);
  assert.match(usageSource, /left-14 right-4 top-6/);
  assert.doesNotMatch(usageSource, /inset-x-14/);
  assert.doesNotMatch(gatewaySource, /OpenAI-compatible routes/);
  assert.doesNotMatch(gatewaySource, /sm:grid-cols-\[minmax\(0,1fr\)_auto_auto\]/);
  assert.doesNotMatch(gatewaySource, /min-w-\[1200px\]/);
  assert.doesNotMatch(gatewaySource, /0\.86fr|1\.14fr/);
});

test("gateway copy actions use inline copied state instead of success toasts", async () => {
  const [gatewaySource, endpointSource] = await Promise.all([
    readFile(gatewayPagePath, "utf8"),
    readFile(endpointRowPath, "utf8"),
  ]);

  assert.match(gatewaySource, /const \[copiedTarget, setCopiedTarget\]/);
  assert.match(gatewaySource, /markCopied\(target\)/);
  assert.match(gatewaySource, /aria-label=\{apiKeyCopied \? t\("gateway\.apiKeyCopied"\) : t\("gateway\.copyApiKey"\)\}/);
  assert.match(gatewaySource, /title=\{apiKeyCopied \? t\("common\.copied"\) : t\("gateway\.copyApiKey"\)\}/);
  assert.match(gatewaySource, /aria-label=\{t\("gateway\.regenerateApiKey"\)\}/);
  assert.match(gatewaySource, /title=\{t\("gateway\.regenerateApiKey"\)\}/);
  assert.match(endpointSource, /aria-label=\{copied \? t\("gateway\.copyEndpointCopied", \{ label \}\) : t\("gateway\.copyEndpoint", \{ label \}\)\}/);
  assert.match(endpointSource, /title=\{copied \? t\("common\.copied"\) : t\("gateway\.copyEndpoint", \{ label \}\)\}/);
  assert.match(endpointSource, /compact \? "h-6 w-6" : "h-8 w-8"/);
  assert.doesNotMatch(endpointSource, /copied \? "Copied" : "Copy"/);
  assert.doesNotMatch(gatewaySource, /\{apiKeyCopied \? "Copied" : "Copy"\}/);
  assert.doesNotMatch(gatewaySource, />\s*Regenerate\s*<\/button>/);
  assert.doesNotMatch(gatewaySource, /setMessage\(`\$\{label\} copied`\)/);
});

test("gateway client route switching reports completion", async () => {
  const [gatewaySource, cardSource, segmentedSource] = await Promise.all([
    readFile(gatewayPagePath, "utf8"),
    readFile(gatewayClientCardPath, "utf8"),
    readFile(segmentedSwitchPath, "utf8"),
  ]);

  assert.match(gatewaySource, /t\("gateway\.switchClient", \{ clientName, routeName \}\)/);
  assert.match(gatewaySource, /showToast\(t\("gateway\.switchClient", \{ clientName, routeName \}\), "loading"\)/);
  assert.match(gatewaySource, /api\.switchGatewayClientRoute\(clientId, mode, defaultModel\)/);
  assert.match(gatewaySource, /updateToast\(toastId,[\s\S]*text: t\("gateway\.switchClientDone", \{ clientName, routeName \}\),[\s\S]*tone: "success"/);
  assert.match(cardSource, /const routeMode = routeModeFromInfo\(info\);/);
  assert.match(cardSource, /const pendingRouteValue = busy \? busyMode \?\? null : null;/);
  assert.match(cardSource, /pendingValue=\{pendingRouteValue\}/);
  assert.doesNotMatch(cardSource, /busyMode \?\? routeModeFromInfo\(info\)/);
  assert.match(segmentedSource, /pendingValue\?: T \| null;/);
  assert.match(segmentedSource, /const pending = !active && option\.value === pendingValue;/);
  assert.match(segmentedSource, /pending[\s\S]*\? "bg-slate-200\/80 text-slate-500 shadow-control"/);
  assert.match(segmentedSource, /aria-busy=\{pending \|\| undefined\}/);
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
  const [gatewaySource, pageToastSource, mainSource] = await Promise.all([
    readFile(gatewayPagePath, "utf8"),
    readFile(pageToastPath, "utf8"),
    readFile(new URL("../src/main.tsx", import.meta.url), "utf8"),
  ]);

  assert.match(gatewaySource, /const \{ showToast, updateToast \} = useToasts\(\)/);
  assert.match(gatewaySource, /showToast\([\s\S]*"loading"/);
  assert.doesNotMatch(gatewaySource, /const \[toast, setToastState\]/);
  assert.doesNotMatch(gatewaySource, /window\.setTimeout\(\(\) => dismissToast\(\), 3000\)/);
  assert.match(gatewaySource, /<main className="relative grid/);
  assert.doesNotMatch(gatewaySource, /"fixed bottom-4 left-4/);
  assert.match(mainSource, /<ToastProvider>/);
  assert.match(pageToastSource, /function ToastViewport/);
  assert.match(pageToastSource, /"fixed bottom-4 left-4 z-\[70\]/);
  assert.match(pageToastSource, /flex-col gap-2/);
  assert.match(pageToastSource, /aria-label=\{t\("common\.dismissNotification"\)\}/);
  assert.match(pageToastSource, /toast\.tone === "success"[\s\S]*<CheckCircle2/);
  assert.match(pageToastSource, /toast\.tone === "error"[\s\S]*<AlertCircle/);
  assert.match(pageToastSource, /toast\.tone === "loading"[\s\S]*<RefreshCcw/);
  assert.match(pageToastSource, /return 3000;/);
  assert.match(pageToastSource, /toast\.action \|\| toast\.tone === "loading" \|\| toast\.tone === "error"/);
  assert.match(pageToastSource, /function ToastItem\(\{ dismissToast, toast \}: ToastItemProps\)/);
  assert.match(pageToastSource, /const dismissCurrentToast = useCallback\(\(\) => dismissToast\(toast\.id\), \[dismissToast, toast\.id\]\)/);
  assert.match(pageToastSource, /\}, \[dismissToast, hasAction, toast\.id, toast\.timeoutMs, toast\.tone\]\);/);
  assert.doesNotMatch(pageToastSource, /onDismiss=\{\(\) => dismissToast\(toast\.id\)\}/);
  assert.doesNotMatch(pageToastSource, /\[onDismiss, toast\]/);
});

test("gateway client version refresh uses a persistent loading toast", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.match(gatewaySource, /showToast\(t\("gateway\.refreshingClients"\), "loading"\)/);
  assert.match(gatewaySource, /await onRefreshClients\(\{ includeClientVersions: true \}\)/);
  assert.match(gatewaySource, /updateToast\(toastId,[\s\S]*text: t\("gateway\.clientsRefreshed"\),[\s\S]*tone: "success"/);
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

  assert.match(drawerSource, /t\("settings\.autoSyncBoundClients"\)/);
  assert.match(drawerSource, /auto_sync_clients/);
  assert.doesNotMatch(drawerSource, /Auto-sync catalog/);
  assert.match(settingsSource, /t\("settings\.autoSyncBoundClients"\)/);
});

test("settings normalization restores default-on fields when persisted settings omit them", async () => {
  const [settingsSource, tauriSource] = await Promise.all([
    readFile(settingsLibPath, "utf8"),
    readFile(tauriSourcePath, "utf8"),
  ]);

  assert.match(settingsSource, /unified_codex_history:\s*true/);
  assert.match(settingsSource, /unified_codex_history:\s*source\.unified_codex_history \?\? DEFAULT_SETTINGS\.unified_codex_history/);
  assert.match(settingsSource, /source\.auto_sync_clients\s*\?\?\s*source\.auto_sync_catalog\s*\?\?\s*DEFAULT_SETTINGS\.auto_sync_clients/s);
  assert.match(tauriSource, /getSettings: async \(\) => normalizeSettings\(await call<Partial<Settings>>\("get_settings"\)\)/);
  assert.match(tauriSource, /settings: normalizeSettings\(settings\)/);
});

test("settings drawer omits duplicated local endpoint controls", async () => {
  const drawerSource = await readFile(settingsDrawerPath, "utf8");

  assert.doesNotMatch(drawerSource, /showClientKey/);
  assert.doesNotMatch(drawerSource, /Bind address/);
  assert.doesNotMatch(drawerSource, />Port</);
  assert.doesNotMatch(drawerSource, /Local client key/);
  assert.doesNotMatch(drawerSource, /Auto-start runtime/);
  assert.doesNotMatch(drawerSource, /Client adapters/);
  assert.doesNotMatch(drawerSource, /partial support/);
});

test("settings drawer uses switch toggles and exposes history repair as a settings action", async () => {
  const drawerSource = await readFile(settingsDrawerPath, "utf8");

  assert.match(drawerSource, /className="peer sr-only"/);
  assert.match(drawerSource, /peer-checked:bg-action/);
  assert.match(drawerSource, /t\("settings\.unifiedCodexHistory"\)/);
  assert.match(drawerSource, /t\("settings\.repairHistoryBucket"\)/);
  assert.match(drawerSource, /draft\.unified_codex_history \? "custom" : "openai"/);
  assert.match(drawerSource, /onClick=\{\(\) => void repairHistory\(\)\}/);
  assert.doesNotMatch(drawerSource, /<History/);
  assert.ok(drawerSource.indexOf("settings.autoSyncBoundClients") < drawerSource.indexOf("settings.repairHistoryBucket"));
  assert.match(drawerSource, /setDraft\(\(current\) => current \?\? settings\)/);
  assert.doesNotMatch(drawerSource, /Migrate official history/);
  assert.doesNotMatch(drawerSource, /Restore migrated official history/);
  assert.doesNotMatch(drawerSource, /Auto-sync history/);
  assert.doesNotMatch(drawerSource, />\s*Sync history\s*</);
});

test("settings drawer exposes gateway retry and image proxy controls", async () => {
  const [drawerSource, appSource, typesSource] = await Promise.all([
    readFile(settingsDrawerPath, "utf8"),
    readFile(appPath, "utf8"),
    readFile(typesPath, "utf8"),
  ]);

  assert.match(typesSource, /gateway_auto_retry_enabled: boolean;/);
  assert.match(typesSource, /gateway_auto_retry_max_attempts: number;/);
  assert.match(typesSource, /gateway_image_proxy_enabled: boolean;/);
  assert.match(typesSource, /gateway_image_proxy_model: string;/);
  assert.match(drawerSource, /<h3 className="text-sm font-semibold text-ink">\{t\("settings\.autoRetry"\)\}<\/h3>/);
  assert.match(drawerSource, /gateway_auto_retry_enabled/);
  assert.match(drawerSource, /label=\{t\("common\.enabled"\)\}/);
  assert.match(drawerSource, /t\("settings\.maxAttempts"\)/);
  assert.match(drawerSource, /grid min-h-9 min-w-0 grid-cols-\[minmax\(0,1fr\)_36px\] items-center gap-3 rounded-inner bg-surface/);
  assert.match(drawerSource, /px-3 py-1\.5 text-sm font-medium text-slate-700 shadow-control/);
  assert.match(drawerSource, /className="h-6 w-9 min-w-0/);
  assert.doesNotMatch(drawerSource, /grid min-h-10 min-w-0 grid-cols-\[minmax\(0,1fr\)_36px\]/);
  assert.match(drawerSource, /border border-transparent bg-transparent/);
  assert.match(drawerSource, /focus:border-action\/40 focus:bg-surface focus:shadow-field/);
  assert.doesNotMatch(drawerSource, /className="field field-compact min-w-0"/);
  assert.match(drawerSource, /min=\{1\}/);
  assert.match(drawerSource, /max=\{30\}/);
  assert.match(drawerSource, /<h3 className="text-sm font-semibold text-ink">\{t\("settings\.imageProxy"\)\}<\/h3>/);
  assert.match(drawerSource, /t\("settings\.visionModel"\)/);
  assert.match(drawerSource, /function VisionModelSelect/);
  assert.match(drawerSource, /grid min-h-9 min-w-0 grid-cols-\[minmax\(0,1fr\)_minmax\(0,190px\)\] items-center gap-3 rounded-inner bg-surface/);
  assert.match(drawerSource, /function visionModelParts\(model: Model, providerLabels: Map<string, string>\): VisionModelParts/);
  assert.match(drawerSource, /const modelId = slashIndex > 0 \? rawId\.slice\(slashIndex \+ 1\) : rawId/);
  assert.match(drawerSource, /providerLabel\(providerFromDisplayName\(model\.display_name, modelId\), providerLabels\)/);
  assert.match(drawerSource, /provider\.display_prefix\?\.trim\(\)/);
  assert.match(drawerSource, /labels\.set\(displayPrefix\.toLowerCase\(\), name\)/);
  assert.match(drawerSource, /function VisionModelValue/);
  assert.match(drawerSource, /rounded-overlay bg-surface p-1 shadow-overlay/);
  assert.match(drawerSource, /role="listbox"/);
  assert.match(drawerSource, /absolute bottom-\[calc\(100%\+6px\)\] left-0 right-0/);
  assert.doesNotMatch(drawerSource, /top-\[calc\(100%\+6px\)\]/);
  assert.match(drawerSource, /relative min-w-0/);
  assert.match(drawerSource, /flex h-7 w-full min-w-0/);
  assert.match(drawerSource, /grid min-w-0 flex-1 grid-cols-\[minmax\(0,1fr\)_auto\] items-center gap-2/);
  assert.doesNotMatch(drawerSource, /visionModelLabel/);
  assert.doesNotMatch(drawerSource, /\$\{name\} \(\$\{model\.id\}\)/);
  assert.doesNotMatch(drawerSource, /<select/);
  assert.match(drawerSource, /visionModels/);
  assert.match(drawerSource, /gateway_image_proxy_enabled/);
  assert.match(drawerSource, /gateway_image_proxy_model/);
  assert.match(appSource, /input_modalities\?\.\includes\("image"\)/);
  assert.match(appSource, /visionModels=\{visionModels\}/);
});

test("settings save restarts running gateway when retry or image proxy runtime settings change", async () => {
  const appSource = await readFile(appPath, "utf8");

  assert.match(appSource, /function gatewayRuntimeSettingsChanged/);
  assert.match(appSource, /gateway_auto_retry_enabled/);
  assert.match(appSource, /gateway_auto_retry_max_attempts/);
  assert.match(appSource, /gateway_image_proxy_enabled/);
  assert.match(appSource, /gateway_image_proxy_model/);
  assert.match(appSource, /runtime\.status\?\.proxy_running/);
  assert.match(appSource, /api\.restartProxy\(\)/);
  assert.match(appSource, /t\("gateway\.gatewaySettingsSavedRestarted"\)/);
  assert.match(appSource, /setBanner\(null\)/);
  assert.doesNotMatch(appSource, /setBanner\(saveMessage\)/);
});

test("gateway client card does not render a disabled fake updater", async () => {
  const cardSource = await readFile(gatewayClientCardPath, "utf8");

  assert.match(cardSource, /min-h-\[136px\]/);
  assert.match(cardSource, /t\("gateway\.manualUpdateAvailable"\)/);
  assert.doesNotMatch(cardSource, /<button[\s\S]*?\{hasUpdate \? "Manual" : "Update"\}/);
  assert.doesNotMatch(cardSource, /safe updater is not exposed by the backend/);
});

test("provider model removal persists through provider save path", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /function removeModel\(modelId: string\)/);
  assert.match(providersSource, /onChange\(next, t\("providers\.modelRemoved"\)\)/);
  assert.match(providersSource, /onRemove=\{removeModel\}/);
});

test("providers page uses stable zero-min split columns", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /min-w-\[972px\] grid-cols-\[430px_minmax\(0,1fr\)\]/);
  assert.match(providersSource, /<aside className="min-h-0 min-w-0 overflow-hidden/);
  assert.match(providersSource, /<section className="min-h-0 min-w-0 overflow-hidden/);
  assert.doesNotMatch(providersSource, /grid-cols-\[minmax\(0,4fr\)_minmax\(0,6fr\)\]/);
});

test("app content region owns horizontal overflow for minimum-width pages", async () => {
  const appSource = await readFile(appPath, "utf8");

  assert.match(appSource, /h-screen min-h-\[720px\] min-w-0/);
  assert.doesNotMatch(appSource, /min-w-\[1004px\]/);
  assert.match(appSource, /className="min-h-0 overflow-x-auto overflow-y-auto p-4"/);
  assert.doesNotMatch(appSource, /className="min-h-0 overflow-hidden p-4"/);
});

test("provider detail keeps model area tall and moves the scrollbar outside cards", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const codexHubProviderCard =
    providersSource.match(/function CodexHubProviderCard[\s\S]*?function gatewayStatusChip/)?.[0] ?? "";
  const providerDetail = providersSource.match(/function ProviderDetail[\s\S]*?function ModelSection/)?.[0] ?? "";
  const endpointSelectionPanel =
    providersSource.match(/function EndpointSelectionPanel[\s\S]*?function EndpointFormatSelect/)?.[0] ?? "";
  const endpointFormatSelect =
    providersSource.match(/function EndpointFormatSelect[\s\S]*?function normalizedEndpointFormat/)?.[0] ?? "";
  const modelIdentity = providersSource.match(/function ModelIdentity[\s\S]*?function ModelEditorOverlay/)?.[0] ?? "";
  const modelTestStateIcon = providersSource.match(/function ModelTestStateIcon[\s\S]*?function normalizedEndpointFormat/)?.[0] ?? "";
  const modelSection = providersSource.match(/function ModelSection[\s\S]*?function ModelIdentity/)?.[0] ?? "";
  const headerRow = providersSource.match(/function HeaderRow[\s\S]*?function Toggle/)?.[0] ?? "";

  assert.match(providerDetail, /className="grid gap-2 border-b border-line p-4"/);
  assert.match(providerDetail, /className="grid grid-cols-2 gap-2"/);
  assert.match(providerDetail, /className="field field-compact"/);
  assert.match(providerDetail, /className="col-span-2"/);
  assert.match(providerDetail, /<div className="col-span-2">\s*<EndpointSelectionPanel/);
  assert.doesNotMatch(providerDetail, /className="grid gap-4 border-b border-line p-5"/);
  assert.doesNotMatch(providerDetail, /lg:grid-cols-2/);
  assert.doesNotMatch(providerDetail, /lg:col-span-2/);
  assert.match(endpointSelectionPanel, /className="grid min-w-0 gap-1 text-sm font-medium text-slate-700"/);
  assert.match(endpointSelectionPanel, /grid min-w-0 grid-cols-\[minmax\(0,1fr\)_auto\] items-center gap-2/);
  assert.match(endpointSelectionPanel, /availableFormats\?: UpstreamFormat\[\] \| null;/);
  assert.match(endpointSelectionPanel, /const mergedAvailableFormats = mergeEndpointFormats\(availableFormats, probeAvailableFormats\(result\)\);/);
  assert.match(endpointSelectionPanel, /<EndpointFormatSelect availableFormats=\{mergedAvailableFormats\} value=\{selected\} onChange=\{onChange\} \/>/);
  assert.match(endpointSelectionPanel, /<TestStateIcon state=\{testState\} size=\{16\} \/>/);
  assert.match(endpointSelectionPanel, /status-pop border-emerald-200 bg-emerald-50 text-emerald-700/);
  assert.match(endpointSelectionPanel, /status-pop border-red-200 bg-red-50 text-danger/);
  assert.match(endpointSelectionPanel, /t\("common\.endpointSelection"\)/);
  assert.match(endpointFormatSelect, /className="select-trigger h-9 w-full"/);
  assert.match(endpointFormatSelect, /className="select-popover absolute left-0 top-\[calc\(100%\+6px\)\] z-30 w-full min-w-\[240px\]"/);
  assert.match(endpointFormatSelect, /className="select-option"/);
  assert.match(endpointFormatSelect, /const selectedAvailable = available\.has\(selected\.value\);/);
  assert.match(endpointFormatSelect, /selectedAvailable && <EndpointAvailableChip \/>/);
  assert.match(endpointFormatSelect, /optionAvailable && <EndpointAvailableChip \/>/);
  assert.doesNotMatch(endpointFormatSelect, /selectedOption && <Check/);
  assert.doesNotMatch(endpointSelectionPanel, /Adapter \{/);
  assert.doesNotMatch(endpointSelectionPanel, /rounded-inner border border-line bg-panel/);
  assert.doesNotMatch(endpointSelectionPanel, /upstreamFormatShortLabel/);
  assert.doesNotMatch(endpointSelectionPanel, /flex-wrap/);
  assert.doesNotMatch(providersSource, /Provider test result/);
  assert.doesNotMatch(providersSource, /Apply recommendation/);
  assert.doesNotMatch(providersSource, /Saved:/);
  assert.doesNotMatch(providersSource, /Responses available/);
  assert.doesNotMatch(endpointFormatSelect, /<select/);
  assert.match(headerRow, /className="grid grid-cols-\[minmax\(0,1fr\)_auto\] items-center gap-3"/);
  assert.match(headerRow, /flex shrink-0 flex-nowrap items-center gap-2 whitespace-nowrap/);
  assert.doesNotMatch(headerRow, /lg:grid-cols-\[minmax\(0,1fr\)_auto\]/);
  assert.doesNotMatch(headerRow, /flex-wrap/);

  assert.match(providersSource, /function useVerticalOverflow/);
  assert.match(providersSource, /scrollHeight > element\.clientHeight \+ 1/);
  assert.match(codexHubProviderCard, /ref=\{providerListRef\}/);
  assert.match(codexHubProviderCard, /providerListHasOverflow && "-mr-3 pr-1"/);
  assert.match(modelSection, /ref=\{modelListRef\}/);
  assert.match(modelSection, /modelListHasOverflow && "-mr-5 pr-1"/);
  assert.match(modelSection, /flex shrink-0 flex-nowrap items-center justify-end gap-2 whitespace-nowrap/);
  assert.match(modelSection, /grid min-h-\[52px\] grid-cols-\[minmax\(0,1fr\)_auto\] items-center gap-3/);
  assert.match(modelSection, /onTestModel\?: \(model: Model\) => Promise<boolean>;/);
  assert.match(modelSection, /<ModelIdentity[\s\S]*onTest=\{onTestModel \? \(\) => void runModelTest\(model\) : undefined\}/);
  assert.match(modelIdentity, /title=\{t\("providers\.testModelTitle", \{ id: copyValue \}\)\}/);
  assert.match(modelIdentity, /aria-label=\{t\("providers\.testModelTitle", \{ id: copyValue \}\)\}/);
  assert.match(modelIdentity, /<ModelTestStateIcon state=\{testState\} size=\{13\} \/>/);
  assert.doesNotMatch(modelIdentity, />\s*Test\s*</);
  assert.match(modelTestStateIcon, /return <Cable size=\{size\} className="shrink-0" \/>;/);
  assert.doesNotMatch(modelSection, /flex flex-wrap items-center gap-2 text-xs text-slate-500 lg:justify-end/);
  assert.doesNotMatch(modelSection, /lg:grid-cols-\[minmax\(0,1fr\)_auto\] lg:items-center/);
  assert.doesNotMatch(modelSection, /className="min-h-0 overflow-auto pr-1"/);
  assert.doesNotMatch(providersSource, /overflow-auto -mr-[35] pr-[35]/);
});

test("provider endpoint probe persists detected formats and selects the recommendation", async () => {
  const [providersSource, typesSource] = await Promise.all([
    readFile(providersPagePath, "utf8"),
    readFile(typesPath, "utf8"),
  ]);
  const pageSource = providersSource.match(/export function ProvidersPage[\s\S]*?function UnsavedProviderChangesDialog/)?.[0] ?? "";
  const providerDetail = providersSource.match(/function ProviderDetail[\s\S]*?function ModelSection/)?.[0] ?? "";
  const addProviderPanel = providersSource.match(/function AddProviderPanel[\s\S]*?function EndpointSelectionPanel/)?.[0] ?? "";

  assert.match(typesSource, /available_upstream_formats\?: UpstreamFormat\[\] \| null;/);
  assert.match(pageSource, /async function persistProviderProbeResult\(providerId: string, result: UpstreamFormatProbeResult\)/);
  assert.match(pageSource, /provider\.id === providerId \? applyProviderProbeAvailability\(provider, result\) : provider/);
  assert.match(pageSource, /probeRecommendedEndpointFormat\(result, fallbackFormat\)/);
  assert.match(pageSource, /const saved = await api\.saveProviders\(nextProviders\);/);
  assert.match(providerDetail, /const normalizedProvider = useMemo\(\(\) => normalizeProviderEndpointSelection\(provider\), \[provider\]\);/);
  assert.match(providerDetail, /const dirty = JSON\.stringify\(draft\) !== JSON\.stringify\(normalizedProvider\);/);
  assert.doesNotMatch(providerDetail, /const dirty = JSON\.stringify\(draft\) !== JSON\.stringify\(provider\);/);
  assert.match(providerDetail, /setDraft\(\(current\) => applyProviderProbeResult\(current, result\)\);/);
  assert.match(providerDetail, /current\.id === provider\.id[\s\S]*available_upstream_formats: availableFormats/);
  assert.doesNotMatch(providerDetail, /const upstreamFormat = normalizedEndpointFormat\(provider\.upstream_format\);/);
  assert.match(addProviderPanel, /onFormChange\(applyAddProviderProbeResult\(form, result\)\);/);
  assert.match(providersSource, /function probeRecommendedEndpointFormat\([\s\S]*?return probeAvailableFormats\(result\)\[0\] \?\? normalizedEndpointFormat\(fallbackFormat\);/);
  assert.match(providersSource, /function applyProviderProbeResult\([\s\S]*?upstream_format: probeRecommendedEndpointFormat\(result, provider\.upstream_format\),[\s\S]*?available_upstream_formats: probeAvailableFormats\(result\),/);
  assert.match(providersSource, /function applyProviderProbeAvailability\([\s\S]*?available_upstream_formats: probeAvailableFormats\(result\),[\s\S]*?\};/);
  assert.match(providersSource, /function applyAddProviderProbeResult\([\s\S]*?upstream_format: probeRecommendedEndpointFormat\(result, form\.upstream_format\),[\s\S]*?available_upstream_formats: probeAvailableFormats\(result\),/);
  assert.match(
    providersSource,
    /if \(result\.recommended_format !== "auto" && !formats\.includes\(result\.recommended_format\)\) \{[\s\S]*formats\.push\(result\.recommended_format\);/,
  );
});

test("model test buttons use the selected endpoint connectivity check", async () => {
  const [providersSource, tauriSource, typesSource] = await Promise.all([
    readFile(providersPagePath, "utf8"),
    readFile(tauriSourcePath, "utf8"),
    readFile(typesPath, "utf8"),
  ]);
  const providerDetail = providersSource.match(/function ProviderDetail[\s\S]*?function ModelSection/)?.[0] ?? "";
  const addProviderPanel = providersSource.match(/function AddProviderPanel[\s\S]*?function EndpointSelectionPanel/)?.[0] ?? "";
  const officialDetail = providersSource.match(/function OfficialDetail[\s\S]*?function ProviderDetail/)?.[0] ?? "";
  const modelTestBlocks = [...providersSource.matchAll(/async function testModel\(model: Model\)[\s\S]*?\r?\n  }\r?\n/g)].map(
    (match) => match[0],
  );

  assert.match(typesSource, /export interface ModelEndpointTestResult/);
  assert.match(tauriSource, /testModelEndpoint: \(baseUrl: string, apiKey: string, model: string, upstreamFormat: UpstreamFormat\)/);
  assert.match(tauriSource, /call<ModelEndpointTestResult>\("test_model_endpoint"/);
  assert.equal(modelTestBlocks.length, 2);
  for (const block of modelTestBlocks) {
    assert.match(block, /const upstreamFormat = normalizedEndpointFormat/);
    assert.match(block, /api\.testModelEndpoint/);
    assert.match(block, /t\("providers\.testingModel", \{ label, endpoint: endpointLabel \}\)/);
    assert.match(block, /t\("gateway\.connectedHttp", \{ label, endpoint: endpointLabel, status: result\.status \}\)/);
    assert.match(block, /t\("gateway\.connectionFailed", \{ label, endpoint: endpointLabel, message: messageFromError\(err\) \}\)/);
    assert.doesNotMatch(block, /api\.probeUpstreamFormat/);
    assert.doesNotMatch(block, /probeResultSummary/);
  }
  assert.match(officialDetail, /async function testOfficialModel\(model: Model\)/);
  assert.match(officialDetail, /api\.testModelEndpoint\([\s\S]*"https:\/\/api\.openai\.com\/v1"[\s\S]*"\{env:OPENAI_API_KEY\}"[\s\S]*officialModelProbeId\(model\)[\s\S]*"responses"/);
  assert.match(officialDetail, /onTestModel=\{testOfficialModel\}/);
  assert.match(officialDetail, /modelTestDisabled=\{authState !== "authorized"\}/);
  assert.match(providerDetail, /onTestModel=\{testModel\}/);
  assert.match(addProviderPanel, /onTestModel=\{testModel\}/);
  assert.match(providersSource, /function officialModelProbeId\(model: Model\)/);
});

test("add provider only prompts and saves when a name is present", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const selectProvider = providersSource.match(/function selectProvider\(id: string\)[\s\S]*?async function savePendingProviderNavigation/)?.[0] ?? "";
  const savePending =
    providersSource.match(/async function savePendingProviderNavigation\(\)[\s\S]*?function discardPendingProviderNavigation/)?.[0] ?? "";
  const discardPending =
    providersSource.match(/function discardPendingProviderNavigation\(\)[\s\S]*?function setMessage/)?.[0] ?? "";
  const addSave = providersSource.match(/async function saveAddProviderForm[\s\S]*?async function addProvider/)?.[0] ?? "";
  const dirtyHelper = providersSource.match(/function isAddProviderFormDirty[\s\S]*?function pendingProviderName/)?.[0] ?? "";

  assert.doesNotMatch(providersSource, /Discover models before saving the provider\./);
  assert.match(providersSource, /const canAdd = Boolean\(form\.name\.trim\(\)\);/);
  assert.doesNotMatch(providersSource, /const canAdd = form\.name\.trim\(\) && form\.base_url\.trim\(\);/);
  assert.match(selectProvider, /selectedId === ADD_ID/);
  assert.match(selectProvider, /isAddProviderFormDirty\(form\)[\s\S]*kind: "add"/);
  assert.match(selectProvider, /setForm\(emptyProvider\);[\s\S]*setSelectedId\(id\);/);
  assert.match(dirtyHelper, /return Boolean\(form\.name\.trim\(\)\);/);
  assert.doesNotMatch(dirtyHelper, /base_url|api_key|models\.length/);
  assert.match(savePending, /pending\.kind === "add"/);
  assert.match(savePending, /const addedId = await saveAddProviderForm\(pending\.form, pending\.targetId\);/);
  assert.match(discardPending, /pending\.kind === "add"[\s\S]*setForm\(emptyProvider\)/);
  assert.match(addSave, /base_url: nextForm\.base_url\.trim\(\)/);
  assert.match(addSave, /return null;/);
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
  assert.doesNotMatch(sidebar, /connected=\{codexConnected\}/);
  assert.match(officialCard, /<ProviderNavButton/);
  assert.match(officialCard, /label="OpenAI"/);
  assert.match(officialCard, /meta=\{t\("providers\.modelCount", \{ enabled: enabledModelCount, total: modelCount \}\)\}/);
  assert.match(officialCard, /enabled=\{included\}/);
  assert.match(officialCard, /onToggle=\{onToggleInclude\}/);
  assert.match(officialCard, /activeTone="neutral"/);
  assert.match(officialCard, /border border-line bg-surface p-3 shadow-card/);
  assert.doesNotMatch(officialCard, /<ConnectedSurfaceFlow \/>/);
  assert.doesNotMatch(officialCard, /border-emerald-300\/70 bg-emerald-50\/55/);
  assert.doesNotMatch(officialCard, /border-transparent bg-surface/);
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
  assert.match(modelSection, /t\("common\.refresh"\)/);
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
  const [providersSource, css] = await Promise.all([
    readFile(providersPagePath, "utf8"),
    readFile(indexCssPath, "utf8"),
  ]);
  const sidebar = providersSource.match(/<HubConnectionBridge[\s\S]*?\/>/)?.[0] ?? "";
  const bridge = providersSource.match(/function HubConnectionBridge[\s\S]*?function CodexHubProviderCard/)?.[0] ?? "";
  const link = providersSource.match(/function ConnectionLink[\s\S]*?function OfficialOpenAICard/)?.[0] ?? "";
  const hubCard = providersSource.match(/function CodexHubProviderCard[\s\S]*?function gatewayStatusChip/)?.[0] ?? "";

  assert.match(sidebar, /pendingMode=\{connectionPendingMode\}/);
  assert.match(sidebar, /disabled=\{busy === "route" \|\| Boolean\(connectionPendingMode\)\}/);
  assert.doesNotMatch(providersSource, /function SourceRailStep/);
  assert.match(providersSource, /grid-rows-\[auto_auto_minmax\(0,1fr\)\] gap-2 p-3/);

  // The old circuit-board metaphor (floating nodes, protruding wires, tilted
  // guillotine switch) is replaced by a single integrated link spine.
  assert.doesNotMatch(providersSource, /function CircuitNode/);
  assert.doesNotMatch(providersSource, /function CircuitWire/);
  assert.doesNotMatch(providersSource, /<CircuitNode/);
  assert.doesNotMatch(providersSource, /<CircuitWire/);
  assert.doesNotMatch(providersSource, /rotate-\[26deg\]/);
  assert.doesNotMatch(providersSource, /origin-bottom/);

  // Connection link: continuous emerald spine + upward flow when connected,
  // two capped stubs with a clear break + hollow coupler when disconnected.
  assert.match(providersSource, /function ConnectionLink/);
  assert.match(link, /connected \? \(/);
  assert.match(link, /absolute left-1\/2 top-\[-14px\] bottom-\[-14px\] w-\[3px\] -translate-x-1\/2 overflow-hidden rounded-full bg-gradient-to-t/);
  assert.match(link, /from-emerald-400\/60 via-emerald-500\/75 to-emerald-400\/60/);
  assert.match(link, /codexhub-flow-beam absolute left-1\/2 top-0 h-12 w-\[7px\] \[--flow-distance:92px\]/);
  assert.match(link, /codexhub-flow-beam codexhub-flow-beam-delay absolute left-1\/2 top-0 h-12 w-\[7px\] \[--flow-distance:92px\]/);
  assert.match(link, /absolute left-1\/2 top-\[-14px\] h-\[calc\(50%-8px\)\] w-\[3px\] -translate-x-1\/2 rounded-full bg-slate-300\/80/);
  assert.match(link, /absolute left-1\/2 bottom-\[-14px\] h-\[calc\(50%-8px\)\] w-\[3px\] -translate-x-1\/2 rounded-full bg-slate-300\/80/);
  assert.match(link, /relative z-10 grid h-4 w-4 place-items-center rounded-full border/);
  assert.match(link, /connected[\s\S]*\? "border-emerald-500 bg-emerald-500 shadow-\[0_0_0_4px_rgba\(16,185,129,0\.16\)\]"[\s\S]*: "border-slate-300 bg-surface"/);
  assert.doesNotMatch(link, /border-dashed/);

  // Cards no longer reserve space for protruding wires or toast layout.
  assert.match(providersSource, /rounded-panel border border-line bg-surface p-3 shadow-card/);
  assert.doesNotMatch(providersSource, /rounded-panel p-3 pb-8 shadow-card/);
  assert.doesNotMatch(providersSource, /toastVisible=\{Boolean\(toast\)\}/);
  assert.doesNotMatch(providersSource, /toastVisible: boolean;/);
  assert.match(providersSource, /grid h-full min-h-0 grid-rows-\[auto_auto_minmax\(0,1fr\)_auto\] gap-3 overflow-hidden rounded-panel border px-3 pt-3 shadow-card/);
  assert.doesNotMatch(providersSource, /px-3 pt-8 shadow-card/);
  assert.doesNotMatch(providersSource, /toastVisible \? "pb-16" : "pb-3"/);
  assert.doesNotMatch(providersSource, /pb-16/);

  // Softened upward flow animation.
  assert.match(providersSource, /codexhub-flow-beam/);
  assert.match(css, /\.codexhub-flow-beam/);
  assert.match(css, /\.codexhub-flow-beam-delay/);
  assert.match(css, /animation-delay:\s*-1\.4s/);
  assert.match(css, /@keyframes codexhub-flow-up/);
  assert.match(css, /transform:\s*translate\(-50%, var\(--flow-distance, 92px\)\)/);
  assert.match(css, /transform:\s*translate\(-50%, -44px\)/);
  assert.match(css, /filter:\s*blur\(1\.5px\)/);
  assert.match(providersSource, /function ConnectedSurfaceFlow/);
  assert.match(providersSource, /codexhub-card-flow absolute left-0 top-0 h-px w-1\/2/);
  assert.match(providersSource, /codexhub-card-flow codexhub-card-flow-delay absolute bottom-0 left-0 h-px w-1\/2/);
  assert.match(css, /\.codexhub-card-flow/);
  assert.match(css, /@keyframes codexhub-card-flow/);

  // Connection band is flat (no third card), link rail beside the CTA.
  assert.match(bridge, /pendingMode: ConnectionMode \| null;/);
  assert.match(bridge, /t\("providers\.connecting"\)/);
  assert.match(bridge, /t\("providers\.disconnecting"\)/);
  assert.match(bridge, /t\("providers\.connectToHub"\)/);
  assert.match(bridge, /t\("providers\.connectedToHub"\)/);
  assert.match(bridge, /<ConnectionLink connected=\{connected\} \/>/);
  assert.match(bridge, /grid grid-cols-\[44px_minmax\(0,1fr\)\] items-center gap-2\.5 px-1 py-1\.5/);
  assert.doesNotMatch(bridge, /shadow-card/);
  assert.doesNotMatch(bridge, /rounded-panel/);
  assert.doesNotMatch(bridge, /bg-emerald-50\/55/);
  assert.match(bridge, /pendingMode && "animate-pulse bg-slate-200\/85 text-slate-600"/);
  assert.match(bridge, /\{icon\}/);
  assert.match(bridge, /h-11/);
  assert.match(bridge, /!pendingMode && connected[\s\S]*\? "bg-emerald-600 text-white hover:bg-emerald-700 hover:shadow-raised"[\s\S]*: !pendingMode && "bg-ink text-white hover:bg-slate-800 hover:shadow-raised"/);
  assert.match(hubCard, /border-emerald-300\/70 bg-emerald-50\/55/);
  assert.match(hubCard, /border-transparent bg-surface/);
  assert.match(providersSource, /return \{ label: t\("common\.unknown"\), tone: "pending" \};/);
  assert.doesNotMatch(providersSource, /Gateway unknown/);
  assert.match(providersSource, /rounded-inner bg-panel-soft p-4 text-sm text-slate-500 shadow-hairline/);
});

test("Codex Hub connection action reports progress immediately", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const action = providersSource.match(/async function toggleCodexHubConnection\(\)[\s\S]*?async function reorderOfficialModels/)?.[0] ?? "";

  assert.match(providersSource, /const \[connectionPendingMode, setConnectionPendingMode\] = useState<ConnectionMode \| null>\(null\);/);
  assert.match(providersSource, /const realCodexConnected = codexStatus\?\.mode === "custom";/);
  assert.match(providersSource, /const codexConnected = realCodexConnected;/);
  assert.doesNotMatch(providersSource, /connectionPreview/);
  assert.doesNotMatch(action, /if \(!settingsDraft\) \{\s*return;\s*\}/);
  assert.match(action, /const actionLabel = nextMode === "custom" \? t\("providers\.connectingToHub"\) : t\("providers\.disconnectingFromHub"\);/);
  assert.match(action, /const nextMode: ConnectionMode = realCodexConnected \? "official" : "custom";/);
  assert.match(action, /setConnectionPendingMode\(nextMode\);[\s\S]*setBusy\("route"\);/);
  assert.match(action, /showToast\(`\$\{actionLabel\}\.\.\.`, "loading"\);/);
  assert.ok(action.indexOf("showToast(`${actionLabel}...`, \"loading\");") < action.indexOf("api.switchMode("));
  assert.match(action, /api\.switchMode\(nextMode, false\)/);
  assert.match(action, /setConnectionPendingMode\(null\);/);
  assert.match(action, /if \(isBackendDisconnectedMessage\(message\)\) \{[\s\S]*setConnectionPendingMode\(null\);[\s\S]*updateToastWithError\(toastId, err\);[\s\S]*return;[\s\S]*\}/);
  assert.match(providersSource, /function updateToastWithError\(toastId: string, err: unknown\)[\s\S]*label: t\("gateway\.startBackend"\)[\s\S]*startBackendFromToast\(toastId\)/);
  assert.doesNotMatch(action, /historyHint/);
  assert.match(action, /repairUnifiedHistoryInBackground\(targetProvider, toastId, codexHubConnectionSuccessMessage\(nextMode, tr\)\)/);
  assert.doesNotMatch(action, /updateToast\(toastId,[\s\S]*text: codexHubConnectionSuccessMessage\(nextMode\),[\s\S]*tone: "success"/);
  assert.doesNotMatch(action, /setMessage\(codexHubConnectionSuccessMessage\(nextMode\)\)/);
});

test("background history repair can reuse the connection toast", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const repair = providersSource.match(/async function repairUnifiedHistoryInBackground[\s\S]*?async function reorderOfficialModels/)?.[0] ?? "";

  assert.match(repair, /toastId\?: string/);
  assert.match(repair, /prefix\?: string/);
  assert.match(repair, /const activeToastId = toastId \?\? showToast\(t\("settings\.repairingHistoryBucket"\), "loading"\)/);
  assert.match(repair, /await api\.syncHistory\(targetProvider\)/);
  assert.match(repair, /updateToast\(activeToastId,[\s\S]*text: prefix \? `\$\{prefix\}; \$\{message\}` : message,[\s\S]*tone: "success"/);
  assert.match(repair, /t\("providers\.historyRepairFailed", \{ message: messageFromError\(err\) \}\)/);
  assert.match(repair, /updateToast\(activeToastId,[\s\S]*t\("providers\.historyRepairFailed", \{ message: messageFromError\(err\) \}\)[\s\S]*tone: "error"/);
  assert.doesNotMatch(repair, /historyRepairSuccessMessage/);
});

test("Codex Hub connection failures no longer mention history sync", async () => {
  const [providersSource, pageToastSource] = await Promise.all([
    readFile(providersPagePath, "utf8"),
    readFile(pageToastPath, "utf8"),
  ]);
  const action = providersSource.match(/async function toggleCodexHubConnection\(\)[\s\S]*?async function reorderOfficialModels/)?.[0] ?? "";

  assert.match(action, /const errorMessage = codexHubConnectionErrorMessage\(err, tr\);/);
  assert.match(action, /setError\(errorMessage\)/);
  assert.match(action, /updateToast\(toastId,[\s\S]*text: errorMessage,[\s\S]*tone: "error"/);
  assert.match(providersSource, /function codexHubConnectionErrorMessage\(err: unknown, t: Translate\)/);
  assert.doesNotMatch(providersSource, /Connection failed while syncing history/);
  assert.doesNotMatch(providersSource, /Turn off Auto-sync history/);
  assert.match(providersSource, /t\("providers\.codexHubConnectionFailed", \{ message \}\)/);
  assert.match(pageToastSource, /toast\.action\s*\?\s*"truncate"\s*:\s*toast\.tone === "error"\s*\?\s*"max-h-32 overflow-auto whitespace-pre-wrap break-words"\s*:\s*"truncate"/);
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

  assert.match(providersSource, /function formatContextWindow\(value\?: number \| null\)[\s\S]*return i18n\.t\("common\.unknown"\);/);
  assert.match(providersSource, /context_window: model\.context_window \?\? null/);
  assert.doesNotMatch(providersSource, /context_window: model\.context_window \?\? 200_000/);
});

test("provider discovery updates the selected provider and reports progress", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /showToast\(t\("providers\.discoveringProviderModels", \{ name: provider\.name \}\), "loading"\)/);
  assert.match(providersSource, /const nextProvider = \{\s*\.\.\.provider,\s*models: mergeDiscoveredModels\(provider\.models, models\),\s*\}/s);
  assert.match(providersSource, /setProviders\(nextProviders\)/);
  assert.match(providersSource, /t\("providers\.discoveredProviderModels", \{/);
});

test("provider discovery preserves missing API key environment variable names", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /const missingEnv = message\.match/);
  assert.match(providersSource, /t\("providers\.discoveryFailedNotSet", \{ env: missingEnv\[1\] \}\)/);
});

test("providers toast uses the shared dismissible page toast", async () => {
  const [providersSource, pageToastSource] = await Promise.all([
    readFile(providersPagePath, "utf8"),
    readFile(pageToastPath, "utf8"),
  ]);

  assert.match(providersSource, /const \{ showToast, updateToast \} = useToasts\(\)/);
  assert.doesNotMatch(providersSource, /const \[toast, setToastState\]/);
  assert.doesNotMatch(providersSource, /window\.setTimeout\(\(\) => dismissToast\(\), 3000\)/);
  assert.doesNotMatch(providersSource, /<PageToast toast=\{toast\} onDismiss=\{dismissToast\} \/>/);
  assert.match(pageToastSource, /function ToastViewport/);
  assert.match(pageToastSource, /"fixed bottom-4 left-4 z-\[70\]/);
  assert.match(pageToastSource, /aria-label=\{t\("common\.dismissNotification"\)\}/);
  assert.match(pageToastSource, /animate-spin/);
  assert.doesNotMatch(providersSource, /"fixed bottom-4 left-4/);
});

test("model copy uses provider-qualified identifiers", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /function providerQualifiedModelId\(providerId: string, modelId: string\)/);
  assert.match(providersSource, /navigator\.clipboard\.writeText\(copyValue\)/);
  assert.match(providersSource, /title=\{copied \? t\("common\.copied"\) : t\("providers\.copyModelIdTitle", \{ id: copyValue \}\)\}/);
  assert.match(providersSource, /aria-label=\{copied \? t\("providers\.copiedModelId", \{ id: copyValue \}\) : t\("providers\.copyModelId", \{ id: copyValue \}\)\}/);
  assert.match(providersSource, /inline-flex h-6 w-6 shrink-0/);
  assert.doesNotMatch(providersSource, /\{copied \? "Copied" : "Copy"\}/);
  assert.match(providersSource, /<ModelIdentity[\s\S]*model=\{model\}[\s\S]*providerId=\{providerId\}[\s\S]*onTest=\{onTestModel \? \(\) => void runModelTest\(model\) : undefined\}/);
  assert.match(providersSource, /title=\{t\("providers\.testModelTitle", \{ id: copyValue \}\)\}/);
  assert.match(providersSource, /aria-label=\{t\("providers\.testModelTitle", \{ id: copyValue \}\)\}/);
});

test("provider write actions keep explicit success feedback", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /successMessage\?: string/);
  assert.match(providersSource, /t\("providers\.providerAdded", \{ name: providerName \}\)/);
  assert.match(providersSource, /t\("providers\.providerDeleted", \{ name: target\.name \}\)/);
  assert.match(providersSource, /onChange\(draft, t\("providers\.providerSaved", \{ name: draft\.name \}\)\)/);
  assert.match(providersSource, /onChange\(next, t\("providers\.modelRemoved"\)\)/);
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
  assert.match(drawerSource, /showToast\(t\("settings\.repairingHistoryBucket"\), "loading"\)/);
  assert.match(drawerSource, /const message = await onSyncHistory\(targetProvider\)/);
  assert.match(drawerSource, /updateToast\(toastId,[\s\S]*text: message,[\s\S]*tone: "success"/);
  assert.doesNotMatch(drawerSource, /onMigrateOfficialHistory/);
  assert.doesNotMatch(drawerSource, /onRestoreOfficialHistory/);
  assert.match(tauriSource, /migrateOfficialHistoryToUnified: \(\) => call<string>\("migrate_official_history_to_unified"\)/);
  assert.match(tauriSource, /restoreOfficialHistoryFromUnified: \(\) => call<string>\("restore_official_history_from_unified"\)/);
  assert.doesNotMatch(drawerSource, /History sync requested/);
});

test("settings drawer keeps language immediate and protects unsaved drafts", async () => {
  const drawerSource = await readFile(settingsDrawerPath, "utf8");

  assert.match(drawerSource, /function settingsSaveComparable\(settings: Settings\)/);
  assert.match(drawerSource, /locale: _locale/);
  assert.match(drawerSource, /unified_codex_history: _unifiedHistory/);
  assert.match(drawerSource, /const hasUnsavedChanges = Boolean/);
  assert.match(drawerSource, /disabled=\{Boolean\(busy\) \|\| historyBusy \|\| !draft \|\| !hasUnsavedChanges\}/);
  assert.match(drawerSource, /setClosePromptOpen\(true\)/);
  assert.match(drawerSource, /t\("settings\.unsavedChangesTitle"\)/);
  assert.match(drawerSource, /void saveDraft\(\{ closeOnSuccess: true \}\)/);
  assert.match(drawerSource, /await changeAppLocale\(locale\)/);
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
