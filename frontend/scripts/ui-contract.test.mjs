import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";
import { test } from "node:test";

const contractPath = new URL("../src/lib/ui-contract.json", import.meta.url);
const appPath = new URL("../src/App.tsx", import.meta.url);
const appUpdateE2ePath = new URL("../../scripts/e2e-app-update.ps1", import.meta.url);
const buildWindowsReleasePath = new URL("../../scripts/build-windows-release.ps1", import.meta.url);
const endpointRowPath = new URL("../src/components/EndpointRow.tsx", import.meta.url);
const gatewayClientCardPath = new URL("../src/components/GatewayClientCard.tsx", import.meta.url);
const segmentedSwitchPath = new URL("../src/components/SegmentedSwitch.tsx", import.meta.url);
const gatewayPagePath = new URL("../src/pages/GatewayPage.tsx", import.meta.url);
const indexCssPath = new URL("../src/index.css", import.meta.url);
const pageToastPath = new URL("../src/components/PageToast.tsx", import.meta.url);
const perfMouseHookPath = new URL("./perf-mouse-hook.ps1", import.meta.url);
const perfTabSwitchPath = new URL("./perf-tab-switch.mjs", import.meta.url);
const providersPagePath = new URL("../src/pages/ProvidersPage.tsx", import.meta.url);
const runtimeBarPath = new URL("../src/components/RuntimeBar.tsx", import.meta.url);
const settingsLibPath = new URL("../src/lib/settings.ts", import.meta.url);
const settingsDrawerPath = new URL("../src/components/SettingsDrawer.tsx", import.meta.url);
const settingsPagePath = new URL("../src/pages/SettingsPage.tsx", import.meta.url);
const sortableListPath = new URL("../src/components/SortableList.tsx", import.meta.url);
const stackedUsagePath = new URL("../src/components/StackedUsageChartShell.tsx", import.meta.url);
const tauriSourcePath = new URL("../src/lib/tauri.ts", import.meta.url);
const tailwindConfigPath = new URL("../tailwind.config.js", import.meta.url);
const typesPath = new URL("../src/lib/types.ts", import.meta.url);
const viteConfigPath = new URL("../vite.config.ts", import.meta.url);
const designPath = new URL("../../DESIGN.md", import.meta.url);
const preparePythonRuntimePath = new URL("../../scripts/Prepare-PythonRuntime.ps1", import.meta.url);
const tauriConfigPath = new URL("../../src-tauri/tauri.conf.json", import.meta.url);
const tauriDefaultCapabilityPath = new URL("../../src-tauri/capabilities/default.json", import.meta.url);
const tauriAppUpdatesPath = new URL("../../src-tauri/src/app_updates.rs", import.meta.url);
const tauriCargoPath = new URL("../../src-tauri/Cargo.toml", import.meta.url);
const tauriMainPath = new URL("../../src-tauri/src/main.rs", import.meta.url);
const tauriOpenAiUsagePath = new URL("../../src-tauri/src/openai_usage.rs", import.meta.url);
const tauriModelsPath = new URL("../../src-tauri/src/models.rs", import.meta.url);
const tauriWebBridgePath = new URL("../../src-tauri/src/web_bridge.rs", import.meta.url);
const i18nIndexPath = new URL("../src/i18n/index.ts", import.meta.url);
const enLocalePath = new URL("../src/i18n/locales/en-US.ts", import.meta.url);
const zhLocalePath = new URL("../src/i18n/locales/zh-CN.ts", import.meta.url);
const desktopIconAssetPaths = [
  new URL("../src/assets/codex-logo.svg", import.meta.url),
  new URL("../src/assets/omp-icon.png", import.meta.url),
  new URL("../src/assets/opencode-icon.png", import.meta.url),
  new URL("../src/assets/pi-icon.png", import.meta.url),
  new URL("../src/assets/zcode-icon.png", import.meta.url),
];

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

test("main tabs use persistent panes and tab clicks do not reload runtime data", async () => {
  const appSource = await readFile(appPath, "utf8");

  assert.match(appSource, /const \[mountedTabs, setMountedTabs\] = useState<Record<TabId, boolean>>/);
  assert.match(appSource, /const \[visibleTab, setVisibleTab\] = useState<TabId>\("codexhub"\)/);
  assert.match(appSource, /const \[gatewayVisited, setGatewayVisited\] = useState\(false\)/);
  assert.match(appSource, /setActiveTab\(tabId\);[\s\S]*setVisibleTab\(tabId\);[\s\S]*setMountedTabs/);
  const selectTabSource = appSource.match(/const selectTab = useCallback[\s\S]*?\}, \[\]\);/)?.[0] ?? "";
  const gatewayTelemetryEffect = appSource.match(/useEffect\(\(\) => \{[\s\S]*?refreshGatewayTelemetry[\s\S]*?\}, \[gatewayVisited, loadGatewayClients, refreshGatewayTelemetry, visibleTab\]\);/)?.[0] ?? "";
  assert.match(appSource, /setMountedTabs\(\(current\) => \(current\.gateway \? current : \{ \.\.\.current, gateway: true \}\)\)/);
  assert.match(appSource, /function tabPaneClass\(active: boolean\)/);
  assert.match(appSource, /"absolute inset-0 min-h-0 min-w-0 p-4 \[contain:layout_paint_style\]"/);
  assert.match(appSource, /"visible z-10 opacity-100 \[content-visibility:visible\] \[will-change:opacity\]"/);
  assert.match(appSource, /"invisible z-0 opacity-0 pointer-events-none \[content-visibility:hidden\]"/);
  assert.match(appSource, /mountedTabs\.codexhub &&/);
  assert.match(appSource, /mountedTabs\.gateway &&/);
  assert.match(appSource, /aria-hidden=\{visibleTab !== "codexhub"\}/);
  assert.match(appSource, /aria-hidden=\{visibleTab !== "gateway"\}/);
  assert.match(appSource, /data-tab-pane="codexhub"/);
  assert.match(appSource, /data-tab-pane="gateway"/);
  assert.doesNotMatch(selectTabSource, /refreshGatewayTelemetry|loadGatewayClients|setTimeout|requestAnimationFrame/);
  assert.match(gatewayTelemetryEffect, /visibleTab !== "gateway"/);
  assert.match(gatewayTelemetryEffect, /window\.setTimeout\(\(\) => \{[\s\S]*refreshGatewayTelemetry/);
  assert.match(gatewayTelemetryEffect, /loadGatewayClients\(\{ staleMs: 30_000 \}\)/);
  assert.doesNotMatch(appSource, /activeTab === "codexhub"\s*\?\s*\(/);
  assert.doesNotMatch(appSource, /activeTab === "codexhub" \? "block" : "hidden"/);
  assert.doesNotMatch(appSource, /activeTab === "gateway" \? "block" : "hidden"/);
});

test("tab switch performance harness uses CDP metrics without package dependencies", async () => {
  const [perfSource, mouseHookSource] = await Promise.all([
    readFile(perfTabSwitchPath, "utf8"),
    readFile(perfMouseHookPath, "utf8"),
  ]);

  assert.doesNotMatch(perfSource, /from "playwright"|from '@playwright\/test'/);
  assert.match(perfSource, /Performance\.getMetrics/);
  assert.match(perfSource, /PerformanceObserver/);
  assert.match(perfSource, /entryTypes: \["longtask"\]/);
  assert.match(perfSource, /data-tab-pane/);
  assert.match(perfSource, /steadySummary/);
  assert.match(perfSource, /CODEXHUB_PERF_WARMUP_DISCARD/);
  assert.match(perfSource, /CODEXHUB_PERF_OS_HOOK/);
  assert.match(perfSource, /pointerEpochMs/);
  assert.match(perfSource, /osToPointerMs/);
  assert.match(perfSource, /perf-mouse-hook\.ps1/);
  assert.match(mouseHookSource, /SetWindowsHookEx/);
  assert.match(mouseHookSource, /WH_MOUSE_LL/);
  assert.match(mouseHookSource, /WM_LBUTTONDOWN/);
});

test("heavy tab pages are memoized behind stable app callbacks", async () => {
  const [appSource, providersSource, gatewaySource] = await Promise.all([
    readFile(appPath, "utf8"),
    readFile(providersPagePath, "utf8"),
    readFile(gatewayPagePath, "utf8"),
  ]);

  assert.match(providersSource, /import \{ memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState \} from "react";/);
  assert.match(providersSource, /function ProvidersPageImpl\(/);
  assert.match(providersSource, /export const ProvidersPage = memo\(ProvidersPageImpl\);/);
  assert.match(gatewaySource, /import \{ memo, useEffect, useMemo, useRef, useState \} from "react";/);
  assert.match(gatewaySource, /function GatewayPageImpl\(/);
  assert.match(gatewaySource, /export const GatewayPage = memo\(GatewayPageImpl\);/);
  assert.match(appSource, /const updateProvidersCache = useCallback/);
  assert.match(appSource, /const applyGatewaySettings = useCallback/);
  assert.match(appSource, /onProvidersChanged=\{updateProvidersCache\}/);
  assert.match(appSource, /onApplySettings=\{applyGatewaySettings\}/);
  assert.doesNotMatch(appSource, /onApplySettings=\{async \(settings\) =>/);
  assert.doesNotMatch(appSource, /onProvidersChanged=\{\(nextProviders\) =>/);
});

test("runtime data uses app-level cached refreshes instead of page lifecycle reloads", async () => {
  const [appSource, providersSource] = await Promise.all([
    readFile(appPath, "utf8"),
    readFile(providersPagePath, "utf8"),
  ]);
  const runtimeCacheType = appSource.match(/type RuntimeCache<T> = \{[\s\S]*?\};/)?.[0] ?? "";
  const providersMountEffect = providersSource.match(/useEffect\(\(\) => \{[\s\S]*?\}, \[\]\);/)?.[0] ?? "";

  assert.match(runtimeCacheType, /data: T \| null/);
  assert.match(runtimeCacheType, /loading: boolean/);
  assert.match(runtimeCacheType, /error: string \| null/);
  assert.match(runtimeCacheType, /updatedAt: number \| null/);
  assert.match(runtimeCacheType, /inflight\?: Promise<T>/);
  assert.match(appSource, /runtimeInflight/);
  assert.match(appSource, /refreshRuntimeStatus/);
  assert.match(appSource, /refreshGatewayTelemetry/);
  assert.match(providersSource, /appStatus: AppStatus \| null/);
  assert.match(providersSource, /providers: Provider\[\]/);
  assert.match(providersSource, /catalogModels: Model\[\]/);
  assert.match(providersSource, /modelMetadata: Model\[\]/);
  assert.doesNotMatch(providersMountEffect, /api\.getSettings|api\.getProviders|api\.listModels|api\.listModelMetadata|api\.getStatus|api\.gatewayStatus/);
  assert.doesNotMatch(providersSource, /async function load\(\)/);
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
  const [runtimeSource, tauriSource, tauriConfig, tauriDefaultCapability, css] = await Promise.all([
    readFile(runtimeBarPath, "utf8"),
    readFile(tauriMainPath, "utf8"),
    readFile(tauriConfigPath, "utf8"),
    readFile(tauriDefaultCapabilityPath, "utf8"),
    readFile(indexCssPath, "utf8"),
  ]);

  assert.doesNotMatch(runtimeSource, /FlowChip/);
  assert.doesNotMatch(runtimeSource, /Hub ·|Clients ·/);
  assert.match(runtimeSource, /data-tauri-drag-region/);
  assert.match(runtimeSource, /windowMinimize/);
  assert.match(runtimeSource, /windowToggleMaximize/);
  assert.match(runtimeSource, /windowCloseToTray/);
  assert.match(runtimeSource, /getCurrentWindow/);
  assert.match(runtimeSource, /startDragging/);
  assert.match(runtimeSource, /MouseEvent/);
  assert.match(runtimeSource, /onMouseDownCapture=\{startWindowDrag\}/);
  assert.doesNotMatch(runtimeSource, /onPointerDown=\{startWindowDrag\}/);
  assert.ok((runtimeSource.match(/data-tauri-drag-region/g) ?? []).length >= 4);
  assert.match(css, /\[data-tauri-drag-region\][\s\S]*user-select:\s*none/);
  assert.doesNotMatch(css, /-webkit-app-region:\s*drag/);
  assert.doesNotMatch(css, /-webkit-app-region:\s*no-drag/);
  const capability = JSON.parse(tauriDefaultCapability);
  assert.deepEqual(capability.windows, ["main"]);
  assert.ok(capability.permissions.includes("core:default"));
  assert.ok(capability.permissions.includes("core:window:allow-start-dragging"));
  assert.ok(capability.permissions.includes("core:window:allow-internal-toggle-maximize"));
  assert.match(runtimeSource, /t\("runtime\.closeToTray"\)/);
  assert.match(tauriSource, /WindowEvent::CloseRequested/);
  assert.match(tauriSource, /TrayIconBuilder::with_id\("codexhub"\)/);
  assert.match(tauriSource, /Connect Codex to CodexHub/);
  assert.match(tauriSource, /Restart Codex App/);
  assert.match(tauriSource, /Get-StartApps/);
  assert.doesNotMatch(tauriSource, /Restart CodexHub/);
  assert.equal(JSON.parse(tauriConfig).app.windows[0].decorations, false);
});

test("runtime header treats SVG icon clicks inside controls as interactive", async () => {
  const runtimeSource = await readFile(runtimeBarPath, "utf8");
  const interactiveGuard = runtimeSource.match(/function isInteractiveWindowControl[\s\S]*?^}/m)?.[0] ?? "";
  const settingsButton = runtimeSource.match(/<button[\s\S]*onClick=\{onOpenSettings\}[\s\S]*?<\/button>/)?.[0] ?? "";

  assert.match(interactiveGuard, /target instanceof Element/);
  assert.match(interactiveGuard, /\.closest\("button,a,input,select,textarea,\[role='button'\],\[data-window-control\]"\)/);
  assert.match(settingsButton, /aria-label=\{t\("common\.settings"\)\}/);
});

test("main desktop window opens tall enough for the primary dashboard", async () => {
  const tauriConfig = JSON.parse(await readFile(tauriConfigPath, "utf8"));
  const mainWindow = tauriConfig.app.windows[0];

  assert.equal(mainWindow.width, 1280);
  assert.ok(mainWindow.height >= 900);
  assert.ok(mainWindow.minHeight >= 800);
});

test("tauri config enables Windows updater packaging", async () => {
  const tauriConfig = JSON.parse(await readFile(tauriConfigPath, "utf8"));

  assert.equal(tauriConfig.bundle.active, true);
  assert.deepEqual(tauriConfig.bundle.targets, ["nsis"]);
  assert.equal(tauriConfig.bundle.createUpdaterArtifacts, true);
  assert.deepEqual(tauriConfig.plugins.updater.endpoints, [
    "https://github.com/NOirBRight/CodexHub/releases/latest/download/latest.json",
  ]);
  assert.equal(tauriConfig.plugins.updater.windows.installMode, "quiet");
  assert.equal(typeof tauriConfig.plugins.updater.pubkey, "string");
  assert.ok(tauriConfig.plugins.updater.pubkey.length > 80);
  assert.equal(tauriConfig.bundle.resources["resources/python/*"], "python");
});

test("Windows release build vendors a pinned Python runtime", async () => {
  const [buildScript, prepareScript] = await Promise.all([
    readFile(buildWindowsReleasePath, "utf8"),
    readFile(preparePythonRuntimePath, "utf8"),
  ]);

  assert.match(buildScript, /Prepare-PythonRuntime\.ps1/);
  assert.match(prepareScript, /python-3\.13\.14-embed-amd64\.zip/);
  assert.match(prepareScript, /90b4e5b9898b72d744650524bff92377c367f44bd5fbd09e3148656c080ad907/);
  assert.match(prepareScript, /src-python\\codex_proxy\.py/);
});

test("release desktop binary does not allocate a Windows console", async () => {
  const mainSource = await readFile(tauriMainPath, "utf8");

  assert.match(mainSource, /windows_subsystem\s*=\s*"windows"/);
  assert.match(mainSource, /not\(debug_assertions\)/);
});

test("desktop exe starts the web bridge in the background", async () => {
  const [mainSource, bridgeSource] = await Promise.all([
    readFile(tauriMainPath, "utf8"),
    readFile(tauriWebBridgePath, "utf8"),
  ]);
  const setupSource = mainSource.match(/\.setup\(\|app\| \{[\s\S]*?Ok\(\(\)\)/)?.[0] ?? "";

  assert.match(setupSource, /web_bridge::start_background\(app\.handle\(\)\.clone\(\)\)/);
  assert.ok(
    setupSource.indexOf("web_bridge::start_background(app.handle().clone())") < setupSource.indexOf("Ok(())"),
    "web bridge should start during GUI setup before setup succeeds",
  );
  assert.match(bridgeSource, /pub fn start_background\(app: AppHandle\) -> Result<\(\), String>/);
  assert.match(bridgeSource, /std::thread::Builder::new\(\)[\s\S]*\.name\("codexhub-web-bridge"/);
  assert.match(bridgeSource, /ErrorKind::AddrInUse/);
});

test("desktop startup opens the gateway backend and reuses the existing app instance", async () => {
  const [mainSource, cargoSource] = await Promise.all([
    readFile(tauriMainPath, "utf8"),
    readFile(tauriCargoPath, "utf8"),
  ]);
  const setupSource = mainSource.match(/\.setup\(\|app\| \{[\s\S]*?Ok\(\(\)\)/)?.[0] ?? "";

  assert.match(setupSource, /start_gateway_on_launch\(\)/);
  assert.ok(
    setupSource.indexOf("runtime_paths::set_resource_root(resource_dir)") <
      setupSource.indexOf("start_gateway_on_launch()"),
    "gateway startup should run after packaged resources are registered",
  );
  assert.match(mainSource, /fn start_gateway_on_launch\(\)/);
  assert.match(mainSource, /tauri::async_runtime::spawn_blocking\(\|\|/);
  assert.match(mainSource, /proxy::start\(\)/);
  assert.match(cargoSource, /tauri-plugin-single-instance\s*=\s*"2"/);
  assert.match(mainSource, /tauri_plugin_single_instance::init\(\|app,[\s\S]*show_main_window\(app\)/);
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
  assert.match(css, /\.vision-model-listbox::-webkit-scrollbar-button\s*\{[\s\S]*display:\s*none;[\s\S]*height:\s*0;[\s\S]*width:\s*0;/);
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

test("gateway top card avoids duplicated status summary cards", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.doesNotMatch(gatewaySource, /StatusCard/);
  assert.doesNotMatch(gatewaySource, /authPresent|bindAddress/);
  assert.doesNotMatch(gatewaySource, /label=\{t\("gateway\.openaiAuth"\)\}/);
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

test("frontend asset imports are emitted as files for the desktop shell", async () => {
  const viteConfigSource = await readFile(viteConfigPath, "utf8");

  assert.match(viteConfigSource, /build:\s*\{[\s\S]*assetsInlineLimit:\s*0/);
});

test("desktop icon source assets stay compact", async () => {
  const maxIconBytes = 4096;
  const sizes = await Promise.all(desktopIconAssetPaths.map(async (assetPath) => ({
    name: assetPath.pathname.split("/").at(-1),
    size: (await stat(assetPath)).size,
  })));

  for (const { name, size } of sizes) {
    assert.ok(size <= maxIconBytes, `${name} should be <= ${maxIconBytes} bytes, got ${size}`);
  }
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

test("web preview infers the bridge port from alternate local dev ports", async () => {
  const tauriSource = await readFile(tauriSourcePath, "utf8");

  assert.match(tauriSource, /const DEFAULT_BRIDGE_URL = "http:\/\/127\.0\.0\.1:1421\/api\/invoke"/);
  assert.match(tauriSource, /localBridgeUrlFromLocation\(window\.location\)/);
  assert.match(tauriSource, /const LOCAL_DEV_HOSTS = new Set\(\["127\.0\.0\.1", "localhost", "::1", "\[::1\]"\]\)/);
  assert.match(tauriSource, /const bridgePort = frontendPort \+ 1/);
  assert.match(tauriSource, /formatHostnameForUrl\(location\.hostname\)/);
  assert.match(tauriSource, /import\.meta\.env\.VITE_CODEXHUB_BRIDGE_URL \|\|/);
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
  assert.match(pageToastSource, /dedupeKey\?: string/);
  assert.match(pageToastSource, /toast\.action/);
  assert.match(pageToastSource, /\{toast\.action\.label\}/);
  assert.match(pageToastSource, /existingToast = toastInput\.dedupeKey/);
  assert.match(pageToastSource, /toast\.dedupeKey === toastInput\.dedupeKey/);
  assert.match(pageToastSource, /grid-cols-\[auto_minmax\(0,1fr\)_auto_auto\]/);
  assert.match(pageToastSource, /whitespace-nowrap/);
  assert.match(providersSource, /showBackendDisconnectedToast/);
  assert.match(providersSource, /BACKEND_DISCONNECTED_TOAST_KEY/);
  assert.match(providersSource, /dedupeKey: BACKEND_DISCONNECTED_TOAST_KEY/);
  assert.match(providersSource, /label: t\("gateway\.startBackend"\)/);
  assert.match(providersSource, /onStartProxy\?: \(\) => Promise<void>;/);
  assert.match(gatewaySource, /showBackendDisconnectedToast/);
  assert.match(gatewaySource, /BACKEND_DISCONNECTED_TOAST_KEY/);
  assert.match(gatewaySource, /dedupeKey: BACKEND_DISCONNECTED_TOAST_KEY/);
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
  assert.match(usageSource, /rounded-inner bg-panel px-2 py-1\.5 shadow-control/);
  assert.match(usageSource, /truncate text-\[10px\] font-semibold uppercase leading-3 text-slate-500/);
  assert.match(usageSource, /mt-0\.5 truncate font-mono text-\[13px\] font-semibold leading-5 text-ink/);
  assert.doesNotMatch(usageSource, /rounded-inner bg-panel p-2\.5 shadow-control|mt-1\.5 truncate font-mono text-base/);
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

test("stacked usage chart uses neutral separators instead of misleading series outlines", async () => {
  const usageSource = await readFile(stackedUsagePath, "utf8");
  const svgSection = usageSource.match(/<svg[\s\S]*?<\/svg>/)?.[0] ?? "";
  const seriesBuilder = usageSource.match(/const series = seriesKeys\.map[\s\S]*?const buckets = rawBuckets\.map/)?.[0] ?? "";

  assert.match(usageSource, /const STACK_AREA_OPACITY = 0\.24/);
  assert.match(seriesBuilder, /const color = key === OTHER_SERIES_KEY \? OTHER_SERIES_COLOR : STACK_COLORS\[index % STACK_COLORS\.length\]/);
  assert.match(seriesBuilder, /fillColor:\s*stackAreaColor\(color\)/);
  assert.match(svgSection, /fill=\{layer\.fillColor\}/);
  assert.doesNotMatch(svgSection, /fillOpacity=/);
  assert.match(usageSource, /style=\{\{ backgroundColor: segment\.fillColor \}\}/);
  assert.doesNotMatch(usageSource, /backgroundColor: segment\.color/);
  assert.match(usageSource, /style=\{\{ backgroundColor: item\.fillColor \}\}/);
  assert.doesNotMatch(svgSection, /`\$\{metric\}:\$\{breakdown\}:\$\{layer\.key\}:line`/);
  assert.match(usageSource, /const STACK_SEPARATOR_COLOR = "rgba\(255, 255, 255, 0\.78\)"/);
  assert.match(svgSection, /`\$\{metric\}:\$\{breakdown\}:\$\{layer\.key\}:separator`/);
  assert.match(svgSection, /d=\{linePath\(layer\.topPoints\)\}/);
  assert.match(svgSection, /stroke=\{STACK_SEPARATOR_COLOR\}/);
  assert.match(svgSection, /strokeWidth="1\.15"/);
  assert.match(svgSection, /strokeLinecap="round"/);
  assert.doesNotMatch(svgSection, /stroke=\{layer\.color\}/);
  assert.doesNotMatch(usageSource, /activeTopPoints/);
  assert.match(usageSource, /activeSegments[\s\S]*\.sort\(\(left, right\) => right\.value - left\.value\)/);
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
  const telemetryRefresh = appSource.match(/const refreshGatewayTelemetry = useCallback[\s\S]*?\}, \[runCachedRequest, usageWindow\]\);/)?.[0] ?? "";
  assert.match(appSource, /gatewayUsageSnapshot: RuntimeCache<GatewayUsageSnapshot>/);
  assert.match(telemetryRefresh, /api\.gatewayUsageSnapshot\(usageWindow\)/);
  assert.match(telemetryRefresh, /quiet: true/);
  assert.match(appSource, /usageError=\{runtime\.gatewayUsageSnapshot\.error\}/);
  assert.match(usageSource, /telemetryStatus/);
  assert.doesNotMatch(usageSource, /Indexing usage/);
  assert.match(gatewaySource, /lastUsageErrorToast/);
  assert.match(gatewaySource, /const text = isBackendDisconnectedMessage\(usageError\)[\s\S]*t\("gateway\.usageTelemetryDelayed", \{ message: usageError \}\);/);
  assert.match(gatewaySource, /if \(isBackendDisconnectedMessage\(usageError\)\) \{\s*showBackendDisconnectedToast\(\);\s*return;\s*\}/);
  assert.match(gatewaySource, /showToast\(text, "error"\)/);
  assert.doesNotMatch(usageSource, /Usage telemetry delayed/);
  assert.doesNotMatch(usageSource, /usageError/);
});

test("official OpenAI usage chart reads cached Codex account usage only on the official provider page", async () => {
  const [providersSource, tauriSource, typesSource, mainSource, webBridgeSource, openAiUsageSource, enSource, zhSource] =
    await Promise.all([
      readFile(providersPagePath, "utf8"),
      readFile(tauriSourcePath, "utf8"),
      readFile(typesPath, "utf8"),
      readFile(tauriMainPath, "utf8"),
      readFile(tauriWebBridgePath, "utf8"),
      readFile(tauriOpenAiUsagePath, "utf8"),
      readFile(enLocalePath, "utf8"),
      readFile(zhLocalePath, "utf8"),
    ]);

  assert.match(tauriSource, /openaiUsageCompletions: \(window\?: OpenAIUsageQueryWindow \| null\) =>/);
  assert.match(tauriSource, /call<OpenAIUsageSnapshot>\("openai_usage_completions"/);
  assert.match(typesSource, /export interface OpenAIUsageSnapshot/);
  assert.match(typesSource, /export interface OpenAIUsageBucket/);
  assert.match(typesSource, /export interface OpenAIUsageLimit/);
  assert.match(typesSource, /limits: OpenAIUsageLimit\[\];/);
  assert.match(typesSource, /forceRefresh\?: boolean \| null;/);
  assert.match(typesSource, /date: string;/);
  assert.match(tauriSource, /forceRefresh: window\?\.forceRefresh \?\? null/);
  assert.match(mainSource, /fn openai_usage_completions\([\s\S]*force_refresh: Option<bool>/);
  assert.match(mainSource, /openai_usage_completions,/);
  assert.match(webBridgeSource, /"openai_usage_completions"/);
  assert.match(webBridgeSource, /optional_bool_arg\(&request\.args, &\["forceRefresh", "force_refresh"\]\)/);
  assert.match(openAiUsageSource, /account\/usage\/read/);
  assert.match(openAiUsageSource, /codex app-server/);
  assert.match(openAiUsageSource, /const CACHE_REFRESH_INTERVAL_SECONDS: u64 = 12 \* 60 \* 60;/);
  assert.match(openAiUsageSource, /const USAGE_REFRESH_MAX_ATTEMPTS: usize = 3;/);
  assert.match(openAiUsageSource, /struct CodexAccountUsageCache/);
  assert.match(openAiUsageSource, /struct OpenAiUsageLimit/);
  assert.match(openAiUsageSource, /usageLimits/);
  assert.match(openAiUsageSource, /write_usage_cache/);
  assert.match(openAiUsageSource, /read_usage_cache/);
  assert.match(openAiUsageSource, /read_codex_account_usage_with_retries/);
  assert.doesNotMatch(openAiUsageSource, /OPENAI_ADMIN_KEY|organization\/usage\/completions|OPENAI_USAGE_COMPLETIONS_URL/);

  const officialUsagePanel = providersSource.match(/function OfficialOpenAIUsagePanel[\s\S]*function OfficialOpenAIUsageTooltip/)?.[0] ?? "";
  assert.ok(officialUsagePanel, "OfficialOpenAIUsagePanel should be present");
  const officialDetail = providersSource.match(/function OfficialDetail[\s\S]*function ProviderDetail/)?.[0] ?? "";
  assert.ok(officialDetail, "OfficialDetail should be present");

  assert.match(providersSource, /openaiUsageCompletions/);
  assert.match(providersSource, /OfficialOpenAIUsagePanel/);
  assert.match(providersSource, /function OfficialOpenAIUsageSkeleton/);
  assert.match(providersSource, /const OFFICIAL_OPENAI_USAGE_STORAGE_KEY/);
  assert.match(providersSource, /const OPENAI_USAGE_REFRESH_INTERVAL_MS = 3 \* 60 \* 1000;/);
  assert.match(providersSource, /const OPENAI_USAGE_STORAGE_TTL_MS = OPENAI_USAGE_REFRESH_INTERVAL_MS;/);
  assert.match(providersSource, /stored_at: Date\.now\(\),\s*snapshot,/);
  assert.match(providersSource, /Date\.now\(\) - stored\.stored_at > OPENAI_USAGE_STORAGE_TTL_MS/);
  assert.doesNotMatch(providersSource, /return isOpenAIUsageSnapshot\(snapshot\) \? snapshot : null;/);
  assert.match(providersSource, /const \[officialUsageHidden, setOfficialUsageHidden\] = useState\(false\);/);
  assert.match(providersSource, /const officialUsageSnapshotRef = useRef<OpenAIUsageSnapshot \| null>\(null\);/);
  assert.match(providersSource, /readStoredOfficialOpenAIUsageSnapshot/);
  assert.match(providersSource, /storeOfficialOpenAIUsageSnapshot\(snapshot\)/);
  assert.match(providersSource, /async function primeOfficialOpenAIUsage\(\)/);
  assert.match(providersSource, /await loadOfficialOpenAIUsage\(false, false, undefined, \{ showBusy: false \}\)/);
  assert.match(providersSource, /void loadOfficialOpenAIUsage\(true\)/);
  assert.match(providersSource, /async function loadOfficialOpenAIUsage\([\s\S]*forceRefresh = true[\s\S]*notify = false[\s\S]*toastId\?: string[\s\S]*options\?: \{ showBusy\?: boolean \}/);
  assert.match(providersSource, /api\.openaiUsageCompletions\(\{[\s\S]*forceRefresh[\s\S]*\}\)/);
  assert.match(providersSource, /void primeOfficialOpenAIUsage\(\)/);
  assert.match(providersSource, /window\.setInterval\(\(\) => void loadOfficialOpenAIUsage\(true\), OPENAI_USAGE_REFRESH_INTERVAL_MS\)/);
  assert.match(providersSource, /if \(officialUsageSnapshotRef\.current\) \{[\s\S]*setOfficialUsageError\(null\);[\s\S]*setOfficialUsageHidden\(false\);[\s\S]*return;[\s\S]*\}/);
  assert.match(providersSource, /selectedId === OFFICIAL_ID[\s\S]*loadOfficialOpenAIUsage/);
  assert.match(providersSource, /if \(selectedId !== OFFICIAL_ID \|\| codexAuthState !== "authorized"\) \{/);
  assert.match(providersSource, /\}, \[codexAuthState, selectedId\]\);/);
  assert.match(providersSource, /longest_running_turn_sec/);
  assert.match(providersSource, /formatUsageDuration/);
  assert.match(providersSource, /type OpenAIUsageMode = "day" \| "week";/);
  assert.match(providersSource, /function SourceMetric\(\{ label, value \}/);
  assert.match(providersSource, /className="grid min-w-0 place-items-center rounded-inner bg-surface px-2 py-1\.5 text-center shadow-control"/);
  assert.match(providersSource, /className="text-\[9px\] font-semibold uppercase leading-3 text-slate-500"/);
  assert.match(officialUsagePanel, /busy && !snapshot \? \(\s*<OfficialOpenAIUsageSkeleton/);
  assert.match(providersSource, /function OfficialOpenAIUsageLimitBars/);
  assert.match(providersSource, /const OPENAI_USAGE_LIMIT_PLACEHOLDERS/);
  assert.match(officialDetail, /<HeaderRow[\s\S]*titleAccessory=\{[\s\S]*<SourceStatusChip \{\.\.\.codexAuthChip\(authState, t as Translate\)\} \/>[\s\S]*actions=\{[\s\S]*<OfficialOpenAIUsageLimitBars busy=\{usageBusy\} limits=\{usageSnapshot\?\.limits \?\? \[\]\}[\s\S]*aria-label=\{t\("providers\.refreshOpenAIUsage"\)\}[\s\S]*onClick=\{onRefreshUsage\}/);
  assert.doesNotMatch(officialDetail, /actions=\{[\s\S]*<SourceStatusChip/);
  assert.match(providersSource, /titleAccessory\?: React\.ReactNode/);
  assert.match(providersSource, /<h2[\s\S]*\{title\}[\s\S]*\{titleAccessory &&/);
  assert.doesNotMatch(officialUsagePanel, /OfficialOpenAIUsageLimitBars|RefreshCcw|onRefresh/);
  assert.match(officialUsagePanel, /<h3[\s\S]*\{t\("providers\.openaiUsage"\)\}[\s\S]*modeOptions\.map/);
  assert.match(providersSource, /visibleLimits\.length \? visibleLimits : OPENAI_USAGE_LIMIT_PLACEHOLDERS/);
  assert.match(providersSource, /remainingPercent/);
  assert.match(providersSource, /limitRemainingPercent/);
  assert.match(providersSource, /t\("providers\.limitRemainingPercent", \{ percent: Math\.round\(percent\) \}\)/);
  assert.match(enSource, /fiveHourLimit/);
  assert.match(enSource, /weeklyLimit/);
  assert.match(enSource, /limitRefreshing/);
  assert.match(enSource, /limitRemainingPercent/);
  assert.match(zhSource, /fiveHourLimit/);
  assert.match(zhSource, /weeklyLimit/);
  assert.match(zhSource, /limitRefreshing/);
  assert.match(zhSource, /limitRemainingPercent/);
  assert.match(providersSource, /OfficialOpenAIUsageTooltip/);
  assert.match(providersSource, /const \[hoveredUsageCell, setHoveredUsageCell\]/);
  assert.match(providersSource, /const \[selectedUsageCellKey, setSelectedUsageCellKey\]/);
  assert.match(providersSource, /onPointerEnter=\{\(event\) => activateUsageCell\(event, cell\)\}/);
  assert.match(providersSource, /onPointerMove=\{\(event\) => activateUsageCell\(event, cell\)\}/);
  assert.match(providersSource, /onClick=\{\(\) => setSelectedUsageCellKey\(cell\.selectionKey\)\}/);
  assert.match(providersSource, /const highlightedUsageCellKey/);
  assert.match(providersSource, /cursorX: event\.clientX - hostRect\.left/);
  assert.match(providersSource, /cursorY: event\.clientY - hostRect\.top/);
  assert.match(providersSource, /const tooltipWidth = Math\.min\(184, Math\.max\(148, tooltip\.hostWidth - 16\)\)/);
  assert.match(providersSource, /const top = isWeek \? -8 : tooltip\.cursorY - 8/);
  assert.match(providersSource, /transform: "translate\(-50%, -100%\)"/);
  assert.match(providersSource, /text-center/);
  assert.match(providersSource, /t\("providers\.openaiUsageTooltipCompact"/);
  assert.doesNotMatch(officialUsagePanel, /scale-110|duration-100|shadow-\[0_0_0_1px/);
  assert.doesNotMatch(providersSource, /openaiUsageTooltipTokens|truncate">\s*\{\s*t\("providers\.openaiUsageTooltip|tooltipWidth = 220|UsageTooltipMetric|t\("usage\.requests"\)|isWeek \? t\("usage\.week"\) : t\("usage\.day"\)/);
  assert.match(providersSource, /const selectedUsageColumnKey/);
  assert.match(providersSource, /buildOfficialOpenAIUsageWeekColumns/);
  assert.match(providersSource, /filledRows/);
  assert.match(providersSource, /usageMonthLabels\(chart\.columns, locale, usageGridWidth\(chart\.columns\.length\)\)/);
  assert.match(providersSource, /const USAGE_MONTH_LABEL_MIN_GAP_PX = \d+;/);
  assert.match(providersSource, /function filterCrowdedUsageMonthLabels/);
  assert.match(providersSource, /nextLeftPx - currentLeftPx < USAGE_MONTH_LABEL_MIN_GAP_PX/);
  assert.match(providersSource, /data-openai-usage-month-label/);
  assert.match(providersSource, /function localDateKey/);
  assert.match(providersSource, /function localUsageTimeZone/);
  assert.match(providersSource, /Intl\.DateTimeFormat\(\)\.resolvedOptions\(\)\.timeZone/);
  assert.match(providersSource, /function parseLocalUsageDate/);
  assert.match(providersSource, /function localDayStartSeconds/);
  assert.match(providersSource, /bucket\.date \? parseLocalUsageDate\(bucket\.date\)/);
  assert.doesNotMatch(providersSource, /timeZone: "UTC"|getUTCDay\(\)|getUTCFullYear\(\)|getUTCMonth\(\)|utcDayStartSeconds|utcWeekStartSeconds/);
  assert.match(providersSource, /const OFFICIAL_USAGE_CELL_SIZE = 8;/);
  assert.match(providersSource, /const OFFICIAL_USAGE_CELL_GAP = 2;/);
  assert.match(providersSource, /const OPENAI_USAGE_MIN_WINDOW_DAYS = 365;/);
  assert.match(providersSource, /const OPENAI_USAGE_QUERY_WINDOW_DAYS = 730;/);
  assert.match(providersSource, /OPENAI_USAGE_QUERY_WINDOW_DAYS - 1/);
  assert.match(providersSource, /function useElementContentWidth/);
  assert.match(providersSource, /const visibleUsageColumnCount = responsiveUsageColumnCount\(chartContentWidth\);/);
  assert.match(providersSource, /buildOfficialOpenAIUsageDays\(snapshot, visibleUsageColumnCount\)/);
  assert.match(providersSource, /const displayWindowDays = Math\.max\([\s\S]*OPENAI_USAGE_MIN_WINDOW_DAYS,[\s\S]*visibleColumnCount \* 7,[\s\S]*\);/);
  assert.match(providersSource, /const startDay = addLocalDays\(endDay, -\(displayWindowDays - 1\)\);/);
  assert.match(providersSource, /buildOfficialOpenAIUsageChart\(days, mode, visibleUsageColumnCount\)/);
  assert.match(providersSource, /function responsiveUsageColumnCount\(contentWidth: number\)/);
  assert.match(providersSource, /const minimumColumns = Math\.ceil\(OPENAI_USAGE_MIN_WINDOW_DAYS \/ 7\);/);
  assert.match(providersSource, /if \(contentWidth <= 0\) \{[\s\S]*return minimumColumns;[\s\S]*\}/);
  assert.match(providersSource, /function visibleUsageColumns\(columns: OfficialOpenAIUsageChartColumn\[\], visibleColumnCount: number\)/);
  assert.match(providersSource, /function usageGridWidth\(columnCount: number\)/);
  assert.match(providersSource, /function usageGridHeight\(\)/);
  assert.match(providersSource, /gridTemplateColumns: `repeat\(\$\{Math\.max\(1, chart\.columns\.length\)\}, \$\{OFFICIAL_USAGE_CELL_SIZE\}px\)`/);
  assert.match(providersSource, /gridTemplateRows: `repeat\(7, \$\{OFFICIAL_USAGE_CELL_SIZE\}px\)`/);
  assert.match(providersSource, /height: usageGridHeight\(\)/);
  assert.match(providersSource, /width: usageGridWidth\(chart\.columns\.length\)/);
  assert.doesNotMatch(officialUsagePanel, /gridAutoColumns: "10px"|className="grid w-max gap-\[3px\]"|minmax\(0, 1fr\)|className="grid h-\[68px\] w-full gap-\[2px\]"/);
  assert.match(providersSource, /resolvedUsageLocale/);
  assert.match(providersSource, /OFFICIAL_USAGE_COLOR_STOPS/);
  assert.doesNotMatch(providersSource, /providers\.cumulative|mode === "cumulative"|value: "cumulative"/);
  assert.doesNotMatch(providersSource, /isMissingOpenAIAdminKeyError|OPENAI_ADMIN_KEY|num_model_requests, locale\)/);
  assert.doesNotMatch(providersSource, /gatewayUsageSnapshot|gatewayUsageEvents|StackedUsageChartShell/);
  assert.match(enSource, /openaiUsage/);
  assert.match(enSource, /longestTaskDuration/);
  assert.match(zhSource, /openaiUsage/);
  assert.match(zhSource, /longestTaskDuration/);
});

test("slow desktop commands run off the Tauri invoke thread", async () => {
  const mainSource = await readFile(tauriMainPath, "utf8");

  assert.match(mainSource, /tauri::async_runtime::spawn_blocking/);
  for (const command of [
    "get_status",
    "refresh_official_models",
    "openai_usage_completions",
    "gateway_status",
    "gateway_recent_events",
    "gateway_usage_summary",
    "gateway_usage_snapshot",
    "gateway_usage_events",
    "list_gateway_clients",
    "sync_gateway_clients",
    "generate_catalog",
  ]) {
    assert.match(mainSource, new RegExp(`async fn ${command}\\(`));
    assert.match(mainSource, new RegExp(`run_blocking\\("${command}"`));
  }
});

test("Codex app-server probes time out and avoid visible Windows consoles", async () => {
  const [openAiUsageSource, modelsSource] = await Promise.all([
    readFile(tauriOpenAiUsagePath, "utf8"),
    readFile(tauriModelsPath, "utf8"),
  ]);

  for (const source of [openAiUsageSource, modelsSource]) {
    assert.match(source, /CREATE_NO_WINDOW/);
    assert.match(source, /configure_no_window/);
    assert.match(source, /recv_timeout/);
    assert.match(source, /kill_child/);
  }
  assert.match(openAiUsageSource, /CODEX_APP_SERVER_RESPONSE_TIMEOUT/);
  assert.match(modelsSource, /CODEX_APP_SERVER_MODEL_LIST_TIMEOUT/);
});

test("manual OpenAI usage refresh uses a persistent toast", async () => {
  const [providersSource, enSource, zhSource] = await Promise.all([
    readFile(providersPagePath, "utf8"),
    readFile(enLocalePath, "utf8"),
    readFile(zhLocalePath, "utf8"),
  ]);

  const loadUsage = providersSource.match(/async function loadOfficialOpenAIUsage[\s\S]*?async function saveProviders/)?.[0] ?? "";
  assert.match(loadUsage, /toastId\?: string/);
  assert.match(loadUsage, /notify = false/);
  assert.match(
    loadUsage,
    /const activeToastId = toastId \?\? \(notify \? showToast\(t\("providers\.refreshingOpenAIUsage"\), "loading"\) : null\)/,
  );
  assert.match(loadUsage, /if \(activeToastId\) \{[\s\S]*text: t\("providers\.openaiUsageRefreshed"\),[\s\S]*tone: "success"/);
  assert.match(loadUsage, /if \(activeToastId\) \{[\s\S]*updateToastWithError\(activeToastId, err\)/);
  assert.match(providersSource, /onRefreshUsage=\{\(\) => void loadOfficialOpenAIUsage\(true, true\)\}/);
  assert.match(enSource, /refreshingOpenAIUsage: "Refreshing OpenAI usage\.\.\."/);
  assert.match(enSource, /openaiUsageRefreshed: "OpenAI usage refreshed"/);
  assert.match(zhSource, /refreshingOpenAIUsage: "正在刷新 OpenAI 用量\.\.\."/);
  assert.match(zhSource, /openaiUsageRefreshed: "OpenAI 用量已刷新"/);
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

  assert.match(gatewaySource, /min-h-\[704px\] w-full max-w-full min-w-0 grid-cols-\[minmax\(0,1fr\)_minmax\(300px,340px\)\] gap-4 overflow-hidden/);
  assert.match(gatewaySource, /<section className="grid min-h-0 min-w-0/);
  assert.match(gatewaySource, /grid min-w-0 gap-2 overflow-hidden rounded-panel bg-surface p-2\.5/);
  assert.doesNotMatch(gatewaySource, /max-h-8 max-w-xl overflow-hidden text-xs leading-4/);
  assert.doesNotMatch(gatewaySource, /\[-webkit-line-clamp:2\]/);
  assert.doesNotMatch(gatewaySource, /Local API key, port, and timeout for OpenAI-compatible clients\./);
  assert.doesNotMatch(gatewaySource, /Clients discover models from/);
  assert.match(gatewaySource, /grid-cols-\[minmax\(300px,1fr\)_minmax\(270px,0\.95fr\)\] items-stretch gap-2/);
  assert.match(gatewaySource, /<Server size=\{15\} className="shrink-0 text-action" \/>/);
  assert.match(gatewaySource, /<SwitchControl/);
  assert.match(gatewaySource, /grid min-w-0 content-start rounded-panel bg-panel p-2 shadow-card/);
  assert.doesNotMatch(gatewaySource, /<div className="grid grid-cols-3 gap-1\.5">[\s\S]*label=\{t\("gateway\.gateway"\)\}/);
  assert.match(gatewaySource, /grid min-w-0 content-start gap-1\.5 rounded-inner bg-surface p-2 shadow-control/);
  assert.match(gatewaySource, /grid min-w-0 grid-rows-\[auto_minmax\(0,1fr\)\] gap-1\.5 rounded-panel bg-panel p-2 pb-2\.5 shadow-card/);
  assert.match(gatewaySource, /grid min-h-\[118px\] grid-rows-3 gap-1\.5/);
  assert.match(gatewaySource, /grid min-w-0 grid-cols-\[minmax\(0,1fr\)_auto_auto\] items-center gap-2/);
  assert.match(gatewaySource, /grid-cols-\[minmax\(64px,0\.75fr\)_minmax\(64px,0\.75fr\)_minmax\(112px,0\.9fr\)\] items-end gap-1\.5/);
  assert.match(gatewaySource, /className="focus-ring inline-flex h-9 self-end/);
  assert.match(gatewaySource, /whitespace-nowrap rounded-control bg-ink/);
  assert.match(gatewaySource, /className="flex items-center justify-between gap-3 whitespace-nowrap"/);
  assert.match(gatewaySource, /<h3 className="shrink-0 text-xs font-semibold text-ink">\{t\("gateway\.copyConnection"\)\}<\/h3>/);
  assert.match(gatewaySource, /<aside className="grid h-full min-h-\[704px\] min-w-0 grid-rows-\[auto_minmax\(0,1fr\)\]/);
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
  assert.doesNotMatch(gatewaySource, /min-w-\[972px\]/);
  assert.doesNotMatch(gatewaySource, /0\.86fr|1\.14fr/);
});

test("gateway recovery panel stays compact and labels actual observed requests", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.match(gatewaySource, /<RecoveryActivityPanel/);
  assert.match(gatewaySource, /grid min-w-0 gap-1\.5 rounded-panel bg-surface px-2\.5 py-2 shadow-card/);
  assert.match(gatewaySource, /grid-cols-\[repeat\(3,minmax\(0,0\.72fr\)\)_minmax\(210px,1\.7fr\)\]/);
  assert.match(gatewaySource, /const routeText = event \? \(client \? `\$\{client\} → \$\{provider\}` : provider\) : t\("gateway\.recoveryEmpty"\)/);
  assert.match(gatewaySource, /title=\{event \? recoveryEventTitle\(event\) : t\("gateway\.recoveryOverviewTitle"\)\}/);
  assert.match(gatewaySource, /grid-cols-\[auto_minmax\(0,1fr\)_auto\]/);
  assert.match(gatewaySource, /<RecoveryEventRow[\s\S]*event=\{latestEvent\}[\s\S]*onOverview=\{\(\) => void openOverview\(\)\}/);
  assert.match(gatewaySource, /aria-label=\{t\("gateway\.recoveryOverviewTitle"\)\}/);
  assert.match(gatewaySource, /className="focus-ring grid h-7 w-7 shrink-0 place-items-center/);
  assert.match(gatewaySource, /event: GatewayEvent \| null;/);
  assert.match(gatewaySource, /const RECOVERY_OVERVIEW_HOURS = 24;/);
  assert.match(gatewaySource, /const RECOVERY_OVERVIEW_PAGE_SIZE = 50;/);
  assert.match(gatewaySource, /api\.gatewayRecentEvents\(\{[\s\S]*limit: RECOVERY_OVERVIEW_LIMIT,[\s\S]*sinceTs,/);
  assert.match(gatewaySource, /setOverviewEvents\(sortRecoveryRetryEvents\(recent\)\)/);
  assert.match(gatewaySource, /const pageEvents = events\.slice\(pageStart, pageStart \+ RECOVERY_OVERVIEW_PAGE_SIZE\)/);
  assert.match(gatewaySource, /t\("gateway\.recoveryOverviewSubtitle", \{ count: events\.length, hours: RECOVERY_OVERVIEW_HOURS \}\)/);
  assert.match(gatewaySource, /t\("gateway\.recoveryPageSummary"/);
  assert.doesNotMatch(gatewaySource, /disabled=\{summary\.retryCount === 0\}[\s\S]*\{t\("gateway\.recoveryOverview"\)\}/);
  assert.doesNotMatch(gatewaySource, /<ListChecks size=\{13\} \/>\s*\{t\("gateway\.recoveryOverview"\)\}/);
  assert.doesNotMatch(gatewaySource, /summary\.overviewEvents/);
  assert.match(gatewaySource, /recoveryProviderRaw\(event\)/);
  assert.match(gatewaySource, /providerFromGatewayPath\(event\.path\)/);
  assert.match(gatewaySource, /<span>\{t\("gateway\.recoveryColumnClient"\)\}<\/span>/);
  assert.match(gatewaySource, /<span>\{t\("gateway\.recoveryColumnProvider"\)\}<\/span>/);
  assert.match(gatewaySource, /<span>\{t\("gateway\.recoveryColumnRequest"\)\}<\/span>/);
  assert.match(gatewaySource, /className="sticky top-0 z-10 grid grid-cols-\[86px_92px_112px_142px_70px_62px_116px_60px_minmax\(0,1fr\)\] bg-panel/);
  assert.match(gatewaySource, /\{formatRecoveryClient\(event\.client_id\) \?\? t\("common\.unknown"\)\}/);
  assert.doesNotMatch(gatewaySource, /main_generation/);
});

test("gateway copy actions use inline copied state instead of success toasts", async () => {
  const [gatewaySource, endpointSource] = await Promise.all([
    readFile(gatewayPagePath, "utf8"),
    readFile(endpointRowPath, "utf8"),
  ]);

  assert.match(endpointSource, /min-h-9 grid-cols-\[104px_minmax\(0,1fr\)_auto\] px-2 py-1/);
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

test("gateway client stale CodexHub route is shown as reapply state", async () => {
  const cardSource = await readFile(gatewayClientCardPath, "utf8");

  assert.match(cardSource, /type DisplayRouteMode = RouteMode \| "stale" \| "unknown";/);
  assert.match(cardSource, /type ClientStatusKind = "checking" \| "not_installed" \| "installed" \| "ready" \| "pending_sync" \| "unknown";/);
  assert.match(cardSource, /const routeValue = routeMode === "stale" \? "hub" : routeMode === "unknown" \? null : routeMode;/);
  assert.match(cardSource, /routeMode === "stale"[\s\S]*\? "pending_sync"/);
  assert.match(cardSource, /statusKind === "pending_sync"[\s\S]*t\("gateway\.routePendingSync"\)/);
  assert.match(cardSource, /statusKind === "ready"[\s\S]*t\("gateway\.routeReady"\)/);
  assert.match(cardSource, /routeMode === "stale"[\s\S]*t\("gateway\.routePendingSyncTitle"\)/);
  assert.match(cardSource, /onClick=\{\(\) => onSwitchMode\("hub"\)\}/);
  assert.match(cardSource, /statusKind === "not_installed" \? "bg-panel opacity-75 grayscale" : "bg-surface"/);
  assert.match(cardSource, /grid-cols-\[56px_minmax\(0,1fr\)\]/);
  assert.match(cardSource, /<code className="truncate text-left font-mono">/);
  assert.match(cardSource, /info\?\.route_mode === "official" \|\| info\?\.route_mode === "hub" \|\| info\?\.route_mode === "stale"/);
});

test("gateway client route switching refreshes without version probes", async () => {
  const [appSource, gatewaySource] = await Promise.all([
    readFile(appPath, "utf8"),
    readFile(gatewayPagePath, "utf8"),
  ]);

  assert.match(gatewaySource, /await onRefreshClients\(\)/);
  assert.doesNotMatch(gatewaySource, /await onRefreshClients\(\{ includeClientVersions: true \}\)[\s\S]*setMessage\(`\$\{clientName\} switched/);
  assert.match(appSource, /void loadGatewayClients\(\);/);
  assert.match(appSource, /const clientTimer = window\.setInterval\(\(\) => void loadGatewayClients\(\), 12 \* 60 \* 60 \* 1000\)/);
});

test("gateway client versions are cached and refreshed after startup or manually", async () => {
  const appSource = await readFile(appPath, "utf8");
  const startupEffect = appSource.match(/useEffect\(\(\) => \{[\s\S]*?return \(\) => \{[\s\S]*?\};/)?.[0] ?? "";

  assert.match(appSource, /GATEWAY_CLIENT_VERSION_CACHE_KEY = "codexhub\.gatewayClientVersions\.v1"/);
  assert.match(appSource, /BACKGROUND_VERSION_PROBE_DELAY_MS = 1000/);
  assert.match(appSource, /function readGatewayClientVersionCache/);
  assert.match(appSource, /function applyGatewayClientVersionCache/);
  assert.match(appSource, /function writeGatewayClientVersionCache/);
  assert.match(appSource, /window\.localStorage\.getItem\(GATEWAY_CLIENT_VERSION_CACHE_KEY\)/);
  assert.match(appSource, /window\.localStorage\.setItem\(/);
  assert.match(appSource, /const cachedClients = applyGatewayClientVersionCache\(clients\)/);
  assert.match(appSource, /client\.id === "generic"/);
  assert.match(startupEffect, /void loadGatewayClients\(\);/);
  assert.match(startupEffect, /void loadGatewayClients\(\{ includeClientVersions: true \}\)/);
  assert.ok(
    startupEffect.indexOf("void loadGatewayClients();") <
      startupEffect.indexOf("void loadGatewayClients({ includeClientVersions: true })"),
  );
  assert.match(startupEffect, /window\.clearTimeout\(versionProbeTimer\)/);
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

test("gateway stopped proxy state stays out of warning banners and toasts", async () => {
  const [gatewaySource, gatewayBackendSource] = await Promise.all([
    readFile(gatewayPagePath, "utf8"),
    readFile(new URL("../../src-tauri/src/gateway.rs", import.meta.url), "utf8"),
  ]);

  assert.match(gatewayBackendSource, /category:\s*"proxy_state"\.to_string\(\)/);
  assert.match(gatewayBackendSource, /level:\s*"status"\.to_string\(\)/);
  assert.doesNotMatch(gatewayBackendSource, /Gateway endpoints are unavailable/);
  assert.match(gatewaySource, /function isActionableDiagnostic/);
  assert.match(gatewaySource, /item\.level !== "status"/);
  assert.match(gatewaySource, /item\.category !== "proxy_state"/);
  assert.match(gatewaySource, /if \(!running && isBackendDisconnectedMessage\(usageError\)\) \{\s*return;\s*\}/);
  assert.doesNotMatch(gatewaySource, /Proxy is not running; Gateway endpoints are unavailable/);
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

test("settings drawer separates software and gateway autostart controls", async () => {
  const [drawerSource, settingsSource, typesSource, appSource, mainSource, zhSource, enSource] = await Promise.all([
    readFile(settingsDrawerPath, "utf8"),
    readFile(settingsLibPath, "utf8"),
    readFile(typesPath, "utf8"),
    readFile(appPath, "utf8"),
    readFile(tauriMainPath, "utf8"),
    readFile(zhLocalePath, "utf8"),
    readFile(enLocalePath, "utf8"),
  ]);

  const languageIndex = drawerSource.indexOf('t("settings.language")');
  const softwareIndex = drawerSource.indexOf('t("settings.autoStartSoftware")');
  const gatewayIndex = drawerSource.indexOf('t("settings.autoStartGateway")');
  const includeOfficialIndex = drawerSource.indexOf('t("settings.includeOfficialModels")');

  assert.ok(languageIndex >= 0, "language control should be present");
  assert.ok(softwareIndex > languageIndex, "software autostart should be below language");
  assert.ok(gatewayIndex > softwareIndex, "gateway autostart should be below software autostart");
  assert.ok(includeOfficialIndex > gatewayIndex, "autostart controls should be above official models");
  assert.match(drawerSource, /checked=\{draft\.auto_start_software\}/);
  assert.match(drawerSource, /checked=\{draft\.auto_start_gateway\}/);
  assert.match(drawerSource, /auto_start_software: value/);
  assert.match(drawerSource, /auto_start_gateway: value/);
  assert.match(typesSource, /auto_start_software: boolean;/);
  assert.match(typesSource, /auto_start_gateway: boolean;/);
  assert.doesNotMatch(typesSource, /auto_start_proxy: boolean;/);
  assert.match(settingsSource, /auto_start_software:\s*true/);
  assert.match(settingsSource, /auto_start_gateway:\s*true/);
  assert.match(settingsSource, /source\.auto_start_proxy/);
  assert.doesNotMatch(appSource, /next\.auto_start_proxy|settings\.auto_start_proxy/);
  assert.match(appSource, /next\.auto_start_software !== settings\.auto_start_software/);
  assert.doesNotMatch(appSource, /next\.auto_start_gateway[\s\S]*setAutostart/);
  assert.match(mainSource, /settings\.auto_start_gateway/);
  assert.match(zhSource, /autoStartSoftware:\s*"开机自启动软件"/);
  assert.match(zhSource, /autoStartGateway:\s*"打开软件后启动 Gateway"/);
  assert.match(enSource, /autoStartSoftware:\s*"Launch app at startup"/);
  assert.match(enSource, /autoStartGateway:\s*"Start Gateway when app opens"/);
});

test("settings drawer keeps repair action and compact model select visually quiet", async () => {
  const drawerSource = await readFile(settingsDrawerPath, "utf8");
  const repairAction =
    drawerSource.match(
      /<button\s+type="button"\s+className="[^"]*"\s+disabled=\{Boolean\(busy\) \|\| historyBusy\}\s+onClick=\{\(\) => void repairHistory\(\)\}\s*>\s*\{t\("settings\.repairHistoryBucket"\)\}/,
    )?.[0] ?? "";
  const visionModelSelect =
    drawerSource.match(/function VisionModelSelect[\s\S]*?<button[\s\S]*?<\/button>/)?.[0] ?? "";

  assert.ok(repairAction, "repair history action should be present");
  assert.doesNotMatch(repairAction, /font-semibold/);
  assert.match(repairAction, /text-sm font-medium/);

  assert.ok(visionModelSelect, "vision model select trigger should be present");
  assert.match(visionModelSelect, /bg-transparent/);
  assert.doesNotMatch(visionModelSelect, /bg-panel/);
  assert.doesNotMatch(visionModelSelect, /bg-white/);
  assert.doesNotMatch(visionModelSelect, /hover:bg-white/);
  assert.doesNotMatch(visionModelSelect, /border border-transparent/);
  assert.doesNotMatch(visionModelSelect, /border-action\/40/);
  assert.doesNotMatch(visionModelSelect, /shadow-field/);
  assert.doesNotMatch(visionModelSelect, /shadow-raised/);
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
  assert.match(drawerSource, /relative grid min-h-9 min-w-0 grid-cols-\[minmax\(0,1fr\)_minmax\(0,190px\)\] items-center gap-3 rounded-inner bg-surface/);
  assert.match(drawerSource, /function visionModelParts\(model: Model, providerLabels: Map<string, string>\): VisionModelParts/);
  assert.match(drawerSource, /const modelId = slashIndex > 0 \? rawId\.slice\(slashIndex \+ 1\) : rawId/);
  assert.match(drawerSource, /providerLabel\(providerFromDisplayName\(model\.display_name, modelId\), providerLabels\)/);
  assert.match(drawerSource, /provider\.display_prefix\?\.trim\(\)/);
  assert.match(drawerSource, /labels\.set\(displayPrefix\.toLowerCase\(\), name\)/);
  assert.match(drawerSource, /function VisionModelValue/);
  assert.match(drawerSource, /w-\[min\(340px,calc\(100vw-2rem\)\)\] -translate-x-1\/2 overflow-hidden rounded-overlay bg-surface p-1 shadow-overlay/);
  assert.match(drawerSource, /vision-model-listbox max-h-56 overflow-y-auto overscroll-contain pr-1/);
  assert.match(drawerSource, /role="listbox"/);
  assert.doesNotMatch(drawerSource, /label=\{t\("common\.selectModel"\)\}/);
  assert.doesNotMatch(drawerSource, /selected=\{!value\}/);
  assert.doesNotMatch(drawerSource, /onSelect=\{\(\) => selectModel\(""\)\}/);
  assert.match(drawerSource, /absolute bottom-\[calc\(100%\+6px\)\] left-1\/2/);
  assert.match(drawerSource, /-translate-x-1\/2 overflow-hidden rounded-overlay/);
  assert.doesNotMatch(drawerSource, /absolute bottom-\[calc\(100%\+6px\)\] right-0/);
  assert.doesNotMatch(drawerSource, /absolute bottom-\[calc\(100%\+6px\)\] left-0 right-0/);
  assert.doesNotMatch(drawerSource, /top-\[calc\(100%\+6px\)\]/);
  assert.match(drawerSource, /<div ref=\{ref\} className="min-w-0">/);
  assert.doesNotMatch(drawerSource, /<div ref=\{ref\} className="relative min-w-0">/);
  assert.match(drawerSource, /flex h-7 w-full min-w-0/);
  assert.match(drawerSource, /grid min-w-0 flex-1 grid-cols-\[minmax\(0,1fr\)_auto\] items-center gap-2/);
  assert.match(drawerSource, /truncate font-mono text-sm font-semibold leading-5 text-ink/);
  assert.match(drawerSource, /truncate text-sm font-medium leading-5 text-slate-500/);
  assert.doesNotMatch(drawerSource, /font-mono text-\[12px\]/);
  assert.doesNotMatch(drawerSource, /truncate text-\[11px\] font-medium leading-5 text-slate-500/);
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
  assert.match(appSource, /appStatus\?\.proxy_running/);
  assert.match(appSource, /api\.restartProxy\(\)/);
  assert.match(appSource, /t\("gateway\.gatewaySettingsSavedRestarted"\)/);
  assert.match(appSource, /setBanner\(null\)/);
  assert.doesNotMatch(appSource, /setBanner\(saveMessage\)/);
});

test("gateway client card does not render a disabled fake updater", async () => {
  const cardSource = await readFile(gatewayClientCardPath, "utf8");

  assert.match(cardSource, /min-h-\[136px\]/);
  assert.match(cardSource, /t\("gateway\.versionNotChecked"\)/);
  assert.doesNotMatch(cardSource, /manualUpdateAvailable/);
  assert.doesNotMatch(cardSource, /noUpdateAction/);
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
  assert.match(
    providersSource,
    /<main className="relative grid h-full min-h-0 min-w-\[972px\] grid-cols-\[430px_minmax\(0,1fr\)\] gap-4 overflow-hidden"/,
  );
  assert.match(providersSource, /<aside className="min-h-0 min-w-0 overflow-hidden/);
  assert.match(providersSource, /<section className="min-h-0 min-w-0 overflow-hidden/);
  assert.doesNotMatch(providersSource, /grid-cols-\[minmax\(0,4fr\)_minmax\(0,6fr\)\]/);
});

test("app content region owns horizontal overflow for minimum-width pages", async () => {
  const appSource = await readFile(appPath, "utf8");

  assert.match(appSource, /h-screen min-h-\[720px\] min-w-0/);
  assert.doesNotMatch(appSource, /min-w-\[1004px\]/);
  assert.match(appSource, /<div className="relative min-h-0 min-w-0 max-w-full overflow-hidden">/);
  assert.match(appSource, /<div className="h-full min-h-0 min-w-0 overflow-x-auto overflow-y-auto">/);
  assert.match(appSource, /<div className="h-full min-h-0 min-w-0 overflow-x-hidden overflow-y-auto">/);
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
  const pageSource = providersSource.match(/function ProvidersPageImpl[\s\S]*?function UnsavedProviderChangesDialog/)?.[0] ?? "";
  const providerDetail = providersSource.match(/function ProviderDetail[\s\S]*?function ModelSection/)?.[0] ?? "";
  const addProviderPanel = providersSource.match(/function AddProviderPanel[\s\S]*?function EndpointSelectionPanel/)?.[0] ?? "";

  assert.match(typesSource, /available_upstream_formats\?: UpstreamFormat\[\] \| null;/);
  assert.match(typesSource, /tool_protocol\?: ToolProtocol \| null;/);
  assert.match(typesSource, /recommended_tool_protocol: ToolProtocol;/);
  assert.match(pageSource, /async function persistProviderProbeResult\(providerId: string, result: UpstreamFormatProbeResult\)/);
  assert.match(pageSource, /provider\.id === providerId \? applyProviderProbeResult\(provider, result\) : provider/);
  assert.match(pageSource, /const detectedFormat = probeDetectedEndpointFormat\(result\);/);
  assert.match(pageSource, /detectedFormat[\s\S]*t\("providers\.probeCompleted"/);
  assert.match(pageSource, /t\("providers\.probeNoSupportedEndpoint"\)/);
  assert.match(pageSource, /const saved = await api\.saveProviders\(nextProviders\);/);
  assert.match(providerDetail, /const normalizedProvider = useMemo\(\(\) => normalizeProviderEndpointSelection\(provider\), \[provider\]\);/);
  assert.match(providerDetail, /const dirty = JSON\.stringify\(draft\) !== JSON\.stringify\(normalizedProvider\);/);
  assert.doesNotMatch(providerDetail, /const dirty = JSON\.stringify\(draft\) !== JSON\.stringify\(provider\);/);
  assert.match(providerDetail, /setDraft\(\(current\) => applyProviderProbeResult\(current, result\)\);/);
  assert.match(providerDetail, /current\.id === provider\.id[\s\S]*available_upstream_formats: availableFormats/);
  assert.doesNotMatch(providerDetail, /const upstreamFormat = normalizedEndpointFormat\(provider\.upstream_format\);/);
  assert.match(addProviderPanel, /onFormChange\(applyAddProviderProbeResult\(form, result\)\);/);
  assert.match(providersSource, /function probeDetectedEndpointFormat\([\s\S]*?normalizedProbeEndpointFormat\(result\.recommended_format\) \?\? probeAvailableFormats\(result\)\[0\] \?\? null/);
  assert.match(providersSource, /function normalizedProbeEndpointFormat\([\s\S]*?normalized === "responses" \|\| normalized === "response"/);
  assert.match(providersSource, /function applyProviderProbeResult\([\s\S]*?const detectedFormat = probeDetectedEndpointFormat\(result\);[\s\S]*?upstream_format: detectedFormat \?\? provider\.upstream_format,[\s\S]*?available_upstream_formats: probeAvailableFormats\(result\),[\s\S]*?tool_protocol: result\.recommended_tool_protocol,/);
  assert.match(providersSource, /function applyProviderProbeAvailability\([\s\S]*?available_upstream_formats: probeAvailableFormats\(result\),[\s\S]*?tool_protocol: result\.recommended_tool_protocol,[\s\S]*?\};/);
  assert.match(providersSource, /function applyAddProviderProbeResult\([\s\S]*?const detectedFormat = probeDetectedEndpointFormat\(result\);[\s\S]*?upstream_format: detectedFormat \?\? form\.upstream_format,[\s\S]*?available_upstream_formats: probeAvailableFormats\(result\),[\s\S]*?tool_protocol: result\.recommended_tool_protocol,/);
  assert.match(providersSource, /function toolProtocolLabel\(value\?: ToolProtocol \| null\)/);
  assert.match(providersSource, /toolProtocol=\{draft\.tool_protocol\}/);
  assert.match(providersSource, /toolProtocol=\{form\.tool_protocol\}/);
  assert.match(
    providersSource,
    /const recommendedFormat = normalizedProbeEndpointFormat\(result\.recommended_format\);[\s\S]*if \(recommendedFormat && !formats\.includes\(recommendedFormat\)\) \{[\s\S]*formats\.push\(recommendedFormat\);/,
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
  assert.match(officialDetail, /api\.gatewayTestRequest\("responses_stream", model\.id\)/);
  assert.doesNotMatch(officialDetail, /OPENAI_API_KEY/);
  assert.match(officialDetail, /onTestModel=\{testOfficialModel\}/);
  assert.match(officialDetail, /modelTestDisabled=\{authState !== "authorized"\}/);
  assert.match(providerDetail, /onTestModel=\{testModel\}/);
  assert.match(addProviderPanel, /onTestModel=\{testModel\}/);
  assert.doesNotMatch(providersSource, /function officialModelProbeId\(model: Model\)/);
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

test("canceling a newly added model removes the temporary draft before navigation", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const modelSection = providersSource.match(/function ModelSection[\s\S]*?function providerQualifiedModelId/)?.[0] ?? "";
  const providerDetail = providersSource.match(/function ProviderDetail[\s\S]*?function ModelSection/)?.[0] ?? "";
  const addProviderPanel = providersSource.match(/function AddProviderPanel[\s\S]*?function EndpointSelectionPanel/)?.[0] ?? "";

  assert.match(modelSection, /const \[pendingNewModelId, setPendingNewModelId\]/);
  assert.match(modelSection, /setPendingNewModelId\(modelId\);[\s\S]*setEditingModelId\(modelId\);/);
  assert.match(modelSection, /function closeModelEditor\(\)[\s\S]*onCancelNewModel\?\.\(pendingNewModelId\);/);
  assert.match(modelSection, /onClose=\{closeModelEditor\}/);
  assert.match(providerDetail, /onCancelNewModel=\{\(modelId\) =>[\s\S]*models: renumberModels\(current\.models\.filter\(\(model\) => model\.id !== modelId\)\)/);
  assert.match(addProviderPanel, /onCancelNewModel=\{\(modelId\) =>[\s\S]*models: renumberModels\(form\.models\.filter\(\(model\) => model\.id !== modelId\)\)/);
});

test("official model rows remain pointer-interactive while editing is disabled", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const modelSection = providersSource.match(/function ModelSection[\s\S]*?function providerQualifiedModelId/)?.[0] ?? "";

  assert.match(modelSection, /const rowInteractable = !interactionDisabled && \(!disabled \|\| Boolean\(onToggleOfficialModel\)\);/);
  assert.match(modelSection, /if \(interactionDisabled\) \{[\s\S]*return;[\s\S]*\}/);
  assert.match(modelSection, /function activateModelRow\(\)[\s\S]*onToggleOfficialModel\(model\.id, !modelEnabled\)/);
  assert.match(modelSection, /rowInteractable && "cursor-pointer"/);
  assert.match(modelSection, /role=\{rowInteractable \? "button" : undefined\}/);
  assert.match(modelSection, /tabIndex=\{rowInteractable \? 0 : undefined\}/);
  assert.match(modelSection, /onClick=\{rowInteractable \? activateModelRow : undefined\}/);
});

test("official OpenAI controls are locked while Codex auth is missing", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const sidebar = providersSource.match(/<OfficialOpenAICard[\s\S]*?\/>/)?.[0] ?? "";
  const officialCard = providersSource.match(/function OfficialOpenAICard[\s\S]*?function HubConnectionBridge/)?.[0] ?? "";
  const providerNavButton = providersSource.match(/function ProviderNavButton[\s\S]*?function OfficialDetail/)?.[0] ?? "";
  const officialDetail = providersSource.match(/function OfficialDetail[\s\S]*?function ProviderDetail/)?.[0] ?? "";
  const modelSection = providersSource.match(/function ModelSection[\s\S]*?function providerQualifiedModelId/)?.[0] ?? "";
  const modelIdentity = providersSource.match(/function ModelIdentity[\s\S]*?function ModelEditorOverlay/)?.[0] ?? "";
  const switchControl = providersSource.match(/function SwitchControl[\s\S]*?function isOfficialModelDisabled/)?.[0] ?? "";

  assert.match(sidebar, /toggleDisabled=\{codexAuthState !== "authorized"\}/);
  assert.match(officialCard, /toggleDisabled: boolean;/);
  assert.match(officialCard, /toggleDisabled=\{toggleDisabled\}/);
  assert.match(providerNavButton, /toggleDisabled[\s\S]*bg-slate-50 text-slate-400 shadow-control ring-1 ring-slate-200/);
  assert.match(providerNavButton, /disabled=\{toggleDisabled\}/);
  assert.match(officialDetail, /interactionDisabled=\{authState !== "authorized"\}/);
  assert.match(modelSection, /interactionDisabled = false/);
  assert.match(modelSection, /interactionDisabled\?: boolean;/);
  assert.match(modelSection, /disabled=\{interactionDisabled\}/);
  assert.match(modelSection, /testDisabled=\{interactionDisabled \|\| modelTestDisabled \|\| Boolean\(testingModelId\)\}/);
  assert.match(modelSection, /actionsDisabled=\{interactionDisabled\}/);
  assert.match(modelSection, /disabled=\{interactionDisabled \|\| refreshBusy\}/);
  assert.match(modelSection, /interactionDisabled && "opacity-60 grayscale"/);
  assert.match(modelIdentity, /actionsDisabled = false/);
  assert.match(modelIdentity, /disabled=\{actionsDisabled\}/);
  assert.match(switchControl, /disabled = false/);
  assert.match(switchControl, /disabled\?: boolean;/);
  assert.match(switchControl, /disabled=\{disabled\}/);
  assert.match(switchControl, /disabled[\s\S]*border-slate-200 bg-slate-200/);
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
  assert.match(officialCard, /<div className="rounded-inner text-left">/);
  assert.doesNotMatch(officialCard, /<button type="button" className="focus-ring rounded-inner text-left"/);
  assert.doesNotMatch(officialCard, /<ConnectedSurfaceFlow \/>/);
  assert.doesNotMatch(officialCard, /border-emerald-300\/70 bg-emerald-50\/55/);
  assert.doesNotMatch(officialCard, /border-transparent bg-surface/);
  assert.match(officialCard, /active=\{active\}/);
  assert.match(officialCard, /t\("providers\.openaiExportHint"\)/);
  assert.doesNotMatch(officialCard, /<SourceMetric label="Official models"/);
  assert.doesNotMatch(officialCard, /active \? "border-action bg-blue-50\/70"/);
});

test("official OpenAI source explains export-only semantics", async () => {
  const [providersSource, enSource, zhSource] = await Promise.all([
    readFile(providersPagePath, "utf8"),
    readFile(enLocalePath, "utf8"),
    readFile(zhLocalePath, "utf8"),
  ]);
  const officialDetail = providersSource.match(/function OfficialDetail[\s\S]*?function ProviderDetail/)?.[0] ?? "";

  assert.match(providersSource, /officialIncluded=\{settings\?\.include_official_models \?\? false\}/);
  assert.match(officialDetail, /officialIncluded: boolean;/);
  assert.match(officialDetail, /!officialIncluded && \(/);
  assert.match(officialDetail, /t\("providers\.openaiSourceExcludedDetail"\)/);
  assert.match(enSource, /openaiExportHint: "Only affects CodexHub\/Gateway export; Codex official direct access is unchanged\."/);
  assert.match(zhSource, /openaiExportHint: "仅影响 CodexHub\/Gateway 导出，不影响 Codex 官方直连。"/);
  assert.match(enSource, /openaiSourceExcludedDetail: "OpenAI is excluded from CodexHub\/Gateway export; Codex official direct access is unchanged\."/);
  assert.match(zhSource, /openaiSourceExcludedDetail: "OpenAI 来源已从 CodexHub\/Gateway 导出中排除；Codex 官方直连不受影响。"/);
});

test("official OpenAI auth prompt guides login before showing usage", async () => {
  const [providersSource, tauriSource, mainSource, webBridgeSource, enSource, zhSource] = await Promise.all([
    readFile(providersPagePath, "utf8"),
    readFile(tauriSourcePath, "utf8"),
    readFile(tauriMainPath, "utf8"),
    readFile(tauriWebBridgePath, "utf8"),
    readFile(enLocalePath, "utf8"),
    readFile(zhLocalePath, "utf8"),
  ]);
  const officialDetail = providersSource.match(/function OfficialDetail[\s\S]*?function ProviderDetail/)?.[0] ?? "";
  const authPrompt = providersSource.match(/function CodexAuthPrompt[\s\S]*?function ProviderDetail/)?.[0] ?? "";

  assert.match(providersSource, /async function openCodexAppForLogin\(\)/);
  assert.match(providersSource, /await api\.openCodexApp\(\);/);
  assert.match(providersSource, /text: t\("providers\.codexAppOpened"\)/);
  assert.match(providersSource, /isUnknownCodexHubCommand\(message, "open_codex_app"\)/);
  assert.match(providersSource, /text: t\("providers\.openCodexAppUnsupportedCopied"\)/);
  assert.match(providersSource, /text: t\("providers\.openCodexAppUnsupported"\)/);
  assert.match(providersSource, /navigator\.clipboard\.writeText\("codex login"\)/);
  assert.match(providersSource, /async function refreshCodexAuthStatus\(\)/);
  assert.match(providersSource, /setBusy\("auth-refresh"\)/);
  assert.match(providersSource, /const authState = codexAuthStateFromGatewayStatus\(gatewayStatus\);/);
  assert.match(providersSource, /setCodexAuthPreviewState\(null\);/);
  assert.match(providersSource, /clearCodexAuthPreviewParam\(\);/);
  assert.match(providersSource, /setCodexAuthState\(authState\);/);
  assert.match(providersSource, /authIssue=\{gatewayStatus\?\.codex_auth\?\.issue \?\? null\}/);
  assert.match(providersSource, /const \[codexAuthPreviewState, setCodexAuthPreviewState\] = useState<CodexAuthState \| null>\(\(\) => readCodexAuthPreviewState\(\)\);/);
  assert.match(providersSource, /useState<CodexAuthState>\(\(\) => codexAuthPreviewState \?\? "unknown"\)/);
  assert.match(providersSource, /setCodexAuthState\(codexAuthPreviewState \?\? codexAuthStateFromGatewayStatus\(gatewayStatusSnapshot \?\? null\)\)/);
  assert.match(providersSource, /setCodexAuthState\(codexAuthPreviewState \?\? codexAuthStateFromGatewayStatus\(gatewayStatus \?\? null\)\)/);
  assert.match(providersSource, /function readCodexAuthPreviewState\(\): CodexAuthState \| null/);
  assert.match(providersSource, /function clearCodexAuthPreviewParam\(\)/);
  assert.match(providersSource, /url\.searchParams\.delete\("codexAuth"\)/);
  assert.match(providersSource, /window\.history\.replaceState/);
  assert.match(providersSource, /!import\.meta\.env\.DEV && !isLocalHttpPreviewLocation\(window\.location\)/);
  assert.match(providersSource, /new URLSearchParams\(window\.location\.search\)\.get\("codexAuth"\)/);
  assert.match(providersSource, /value === "authorized" \|\| value === "missing" \|\| value === "unknown" \? value : null/);
  assert.match(providersSource, /function isLocalHttpPreviewLocation\(location: Location\)/);
  assert.match(providersSource, /location\.protocol === "http:"/);
  assert.match(providersSource, /location\.hostname === "127\.0\.0\.1"/);
  assert.match(providersSource, /location\.hostname === "localhost"/);
  assert.match(providersSource, /function isUnknownCodexHubCommand\(message: string, command: string\)/);
  assert.match(officialDetail, /const authorized = authState === "authorized";/);
  assert.match(officialDetail, /authorized \? \([\s\S]*<OfficialOpenAIUsagePanel[\s\S]*\) : \([\s\S]*<CodexAuthPrompt/);
  assert.match(officialDetail, /actions=\{[\s\S]*authorized && \([\s\S]*<OfficialOpenAIUsageLimitBars/);
  assert.doesNotMatch(officialDetail, /actions=\{[\s\S]*t\("providers\.refreshCodexAuth"\)[\s\S]*\}\s*\/>/);
  assert.match(officialDetail, /modelTestDisabled=\{authState !== "authorized"\}/);
  assert.match(authPrompt, /authState === "unknown"[\s\S]*t\("providers\.codexAuthUnknownTitle"\)[\s\S]*t\("providers\.codexAuthRequiredTitle"\)/);
  assert.match(authPrompt, /t\("providers\.codexAuthRequiredBody"\)/);
  assert.match(authPrompt, /rounded-inner bg-amber-50\/70 p-3 text-sm shadow-hairline/);
  assert.match(authPrompt, /text-sm font-semibold text-ink/);
  assert.match(authPrompt, /text-xs leading-5 text-slate-700/);
  assert.doesNotMatch(authPrompt, /absolute inset-y-3 left-0/);
  assert.doesNotMatch(authPrompt, /bg-amber-400\/70/);
  assert.doesNotMatch(authPrompt, /text-amber-900|text-amber-800|text-amber-700/);
  assert.match(authPrompt, /<ExternalLink size=\{15\} \/>/);
  assert.match(authPrompt, /<Copy size=\{15\} \/>/);
  assert.match(authPrompt, /<RefreshCcw size=\{15\}/);
  assert.match(tauriSource, /openCodexApp: \(\) => call<string>\("open_codex_app"\)/);
  assert.match(mainSource, /fn open_codex_app\(\) -> Result<String, String> \{[\s\S]*launch_codex_app\(\)/);
  assert.match(mainSource, /open_codex_app,/);
  assert.match(mainSource, /fn launch_codex_app\(\) -> Result<String, String> \{[\s\S]*Get-StartApps[\s\S]*Start-Process \('shell:AppsFolder\\' \+ \$app\.AppID\)/);
  assert.match(webBridgeSource, /"open_codex_app" => to_value\(crate::open_codex_app\(\)\)/);
  assert.match(enSource, /codexAuthRequiredTitle: "Sign in to Codex"/);
  assert.match(zhSource, /codexAuthRequiredTitle: "需要登录 Codex"/);
  assert.match(enSource, /openCodexApp: "Open Codex App to sign in"/);
  assert.match(zhSource, /openCodexApp: "打开 Codex App 登录"/);
  assert.match(enSource, /copyCodexLoginCommand: "Copy CLI login command"/);
  assert.match(zhSource, /copyCodexLoginCommand: "复制 CLI 登录命令"/);
  assert.match(enSource, /openCodexAppUnsupportedCopied/);
  assert.match(zhSource, /openCodexAppUnsupportedCopied/);
  assert.match(enSource, /paste it into PowerShell, Windows Terminal, or another shell, and run codex login/);
  assert.match(zhSource, /再到 PowerShell、Windows Terminal 或其他终端里粘贴运行 codex login/);
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
  assert.match(providersSource, /const realCodexConnected = codexStatus\?\.mode === "custom" && codexStatus\.proxy_running === true;/);
  assert.match(providersSource, /const codexConnected = realCodexConnected;/);
  assert.doesNotMatch(providersSource, /connectionPreview/);
  assert.doesNotMatch(action, /if \(!settingsDraft\) \{\s*return;\s*\}/);
  assert.match(action, /const actionLabel = nextMode === "custom" \? t\("providers\.connectingToHub"\) : t\("providers\.disconnectingFromHub"\);/);
  assert.match(action, /const nextMode: ConnectionMode = realCodexConnected \? "official" : "custom";/);
  assert.match(action, /setConnectionPendingMode\(nextMode\);[\s\S]*setBusy\("route"\);/);
  assert.match(action, /showToast\(`\$\{actionLabel\}\.\.\.`, "loading"\);/);
  assert.ok(action.indexOf("showToast(`${actionLabel}...`, \"loading\");") < action.indexOf("api.switchMode("));
  assert.match(action, /api\.switchMode\(nextMode, false\)/);
  assert.match(action, /if \(nextMode === "custom" && !status\.proxy_running\) \{/);
  assert.match(action, /await startProxyForHubConnection\(\);/);
  assert.match(action, /status = refreshedStatus \?\? status;/);
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
  const [drawerSource, zhSource, enSource] = await Promise.all([
    readFile(settingsDrawerPath, "utf8"),
    readFile(zhLocalePath, "utf8"),
    readFile(enLocalePath, "utf8"),
  ]);

  assert.match(drawerSource, /function settingsSaveComparable\(settings: Settings\)/);
  assert.match(drawerSource, /locale: _locale/);
  assert.match(drawerSource, /unified_codex_history: _unifiedHistory/);
  assert.match(drawerSource, /const hasUnsavedChanges = Boolean/);
  assert.match(drawerSource, /disabled=\{Boolean\(busy\) \|\| historyBusy \|\| !draft \|\| !hasUnsavedChanges\}/);
  assert.match(drawerSource, /setClosePromptOpen\(true\)/);
  assert.match(drawerSource, /t\("settings\.unsavedChangesTitle"\)/);
  assert.match(drawerSource, /void saveDraft\(\{ closeOnSuccess: true \}\)/);
  assert.match(drawerSource, /await changeAppLocale\(locale\)/);
  assert.match(zhSource, /includeOfficialModels:\s*"包含 OpenAI 官方模型"/);
  assert.match(enSource, /includeOfficialModels:\s*"Include OpenAI official models"/);
});

test("app owns version cache and settings drawer only receives update actions", async () => {
  const [appSource, drawerSource] = await Promise.all([
    readFile(appPath, "utf8"),
    readFile(settingsDrawerPath, "utf8"),
  ]);
  const loadAppVersion = appSource.match(/const loadAppVersion = useCallback[\s\S]*?\}, \[runCachedRequest, t\]\);/)?.[0] ?? "";

  assert.match(loadAppVersion, /api\.getAppVersion\(\)/);
  assert.match(loadAppVersion, /catch\s*\{\s*return null;\s*\}/);
  assert.match(drawerSource, /appVersion: AppVersionInfo \| null/);
  assert.match(drawerSource, /onCheckUpdate: \(\) => Promise<AppUpdateStatus \| null>/);
  assert.match(drawerSource, /onInstallUpdate: \(\) => Promise<void>/);
  assert.doesNotMatch(drawerSource, /api\.getAppVersion\(\)/);
  assert.doesNotMatch(drawerSource, /api\.checkAppUpdate\(\)/);
  assert.doesNotMatch(drawerSource, /api\.installAppUpdate\(\)/);
});

test("app update flow separates silent automatic checks from settings checks and install state", async () => {
  const appSource = await readFile(appPath, "utf8");
  const installAction = appSource.match(/const startAppUpdateInstall = useCallback[\s\S]*?const checkForUpdates = useCallback/)?.[0] ?? "";
  const settingsCheck = appSource.match(/const checkForUpdates = useCallback[\s\S]*?const runAutomaticUpdateCheck = useCallback/)?.[0] ?? "";
  const automaticCheck = appSource.match(/const runAutomaticUpdateCheck = useCallback[\s\S]*?const updateUsageWindow = useCallback/)?.[0] ?? "";

  assert.match(appSource, /const \{ dismissToast, showToast, updateToast \} = useToasts\(\)/);
  assert.match(appSource, /const updateAvailableToastId = useRef<string \| null>\(null\)/);
  assert.match(appSource, /APP_UPDATE_CHECK_INTERVAL_MS\s*=\s*24 \* 60 \* 60 \* 1000/);
  assert.match(appSource, /UPDATE_INSTALL_STATUS_POLL_MS\s*=\s*500/);
  assert.match(automaticCheck, /updateAvailableToastId\.current = showToast\(\{/);
  assert.match(automaticCheck, /label: t\("settings\.update"\)/);
  assert.match(automaticCheck, /timeoutMs: null/);
  assert.doesNotMatch(automaticCheck, /settings\.checkForUpdates/);
  assert.doesNotMatch(automaticCheck, /tone: "loading"/);
  assert.doesNotMatch(settingsCheck, /showToast\(t\("settings\.checkForUpdates"\), "loading"\)/);
  assert.match(settingsCheck, /t\("settings\.noUpdatesAvailable"\)/);
  assert.match(settingsCheck, /t\("settings\.updateAvailable", \{ version: status\.latest_version \}\)/);
  assert.doesNotMatch(settingsCheck, /action:\s*\{/);
  assert.match(installAction, /const toastId = updateAvailableToastId\.current/);
  assert.match(installAction, /if \(toastId\) \{[\s\S]*dismissToast\(toastId\);[\s\S]*updateAvailableToastId\.current = null;[\s\S]*\}/);
  assert.match(installAction, /api\.startAppUpdateInstall\(\)/);
  assert.match(installAction, /settings\.downloadingUpdate/);
  assert.match(appSource, /settings\.installingUpdateRestarting/);
  assert.ok(
    installAction.indexOf("dismissToast(toastId)") < installAction.indexOf("api.startAppUpdateInstall()"),
    "the stale update-available toast should be removed before the install loading toast settles",
  );
});

test("app update APIs use the web bridge fallback and bridge dispatches updater commands", async () => {
  const [tauriSource, typesSource, bridgeSource, mainSource] = await Promise.all([
    readFile(tauriSourcePath, "utf8"),
    readFile(typesPath, "utf8"),
    readFile(new URL("../../src-tauri/src/web_bridge.rs", import.meta.url), "utf8"),
    readFile(tauriMainPath, "utf8"),
  ]);

  assert.match(typesSource, /export interface AppVersionInfo/);
  assert.match(typesSource, /current_version: string/);
  assert.match(typesSource, /export interface AppUpdateStatus/);
  assert.match(typesSource, /available: boolean/);
  assert.match(typesSource, /latest_version\?: string \| null/);
  assert.match(typesSource, /export type AppUpdateInstallPhase =[\s\S]*"idle"[\s\S]*"checking"[\s\S]*"downloading"[\s\S]*"installing"[\s\S]*"restarting"[\s\S]*"failed"/);
  assert.match(typesSource, /export interface AppUpdateInstallStatus/);
  assert.match(typesSource, /phase: AppUpdateInstallPhase/);
  assert.match(typesSource, /target_version\?: string \| null/);
  assert.match(typesSource, /downloaded_bytes: number/);
  assert.match(typesSource, /total_bytes\?: number \| null/);
  assert.match(typesSource, /export interface AppUpdateCompletionStatus/);
  assert.match(tauriSource, /getAppVersion: \(\) => call<AppVersionInfo>\("get_app_version"\)/);
  assert.match(tauriSource, /checkAppUpdate: \(\) => call<AppUpdateStatus>\("check_app_update"\)/);
  assert.match(tauriSource, /startAppUpdateInstall: \(\) => call<AppUpdateInstallStatus>\("start_app_update_install"\)/);
  assert.match(tauriSource, /getAppUpdateInstallStatus: \(\) => call<AppUpdateInstallStatus>\("get_app_update_install_status"\)/);
  assert.match(tauriSource, /consumeAppUpdateCompletion: \(\) =>\s*call<AppUpdateCompletionStatus \| null>\("consume_app_update_completion"\)/);
  assert.match(mainSource, /web_bridge::start_background\(app\.handle\(\)\.clone\(\)\)/);
  assert.match(bridgeSource, /"get_app_version"\s*=>\s*to_value\(Ok\(app_updates::get_app_version\(desktop_app\(&app\)\?\)\)\)/);
  assert.match(bridgeSource, /"check_app_update"\s*=>\s*to_value\(tauri::async_runtime::block_on\([\s\S]*app_updates::check_app_update\(desktop_app\(&app\)\?\)/);
  assert.match(bridgeSource, /"start_app_update_install"\s*=>\s*\{?\s*to_value\(app_updates::start_app_update_install\(desktop_app\(&app\)\?\)\)\s*\}?/);
  assert.match(bridgeSource, /"get_app_update_install_status"\s*=>\s*to_value\(Ok\(\s*app_updates::get_app_update_install_status\(desktop_app\(&app\)\?\),\s*\)\)/);
  assert.match(bridgeSource, /"consume_app_update_completion"\s*=>\s*to_value\(app_updates::consume_app_update_completion\(\s*desktop_app\(&app\)\?,\s*\)\)/);
});

test("settings drawer version updates use the design-system grouped settings surface", async () => {
  const drawerSource = await readFile(settingsDrawerPath, "utf8");
  const blockSource = drawerSource.match(/function VersionUpdateBlock[\s\S]*?function clampRetryAttempts/)?.[0] ?? "";
  const checkButton = blockSource.match(/<button[\s\S]*?onClick=\{onCheck\}[\s\S]*?<\/button>/)?.[0] ?? "";

  assert.match(blockSource, /rounded-panel bg-panel p-3 shadow-card/);
  assert.match(blockSource, /grid-cols-\[minmax\(0,1fr\)_auto_auto\]/);
  assert.match(blockSource, /rounded-inner bg-surface px-3 py-2[\s\S]*shadow-control/);
  assert.match(blockSource, /const rawCurrentVersion = status\?\.current_version \?\? versionInfo\?\.current_version \?\? null/);
  assert.match(blockSource, /const currentVersion = rawCurrentVersion \? `v\$\{rawCurrentVersion\}` : t\("common.unknown"\)/);
  assert.doesNotMatch(blockSource, /v\{currentVersion\}/);
  assert.doesNotMatch(blockSource, /className="mini-button"/);
  assert.match(checkButton, /aria-label=\{t\("settings\.checkForUpdates"\)\}/);
  assert.match(checkButton, /title=\{t\("settings\.checkForUpdates"\)\}/);
  assert.match(checkButton, /h-7 w-7/);
  assert.match(checkButton, /<RefreshCcw/);
  assert.doesNotMatch(checkButton, />\s*\{t\("settings\.checkForUpdates"\)\}\s*<\/button>/);
});

test("settings drawer update-available state shows version, release notes, and install action", async () => {
  const [drawerSource, enSource, zhSource] = await Promise.all([
    readFile(settingsDrawerPath, "utf8"),
    readFile(enLocalePath, "utf8"),
    readFile(zhLocalePath, "utf8"),
  ]);
  const blockSource = drawerSource.match(/function VersionUpdateBlock[\s\S]*?function clampRetryAttempts/)?.[0] ?? "";

  assert.match(blockSource, /const updateAvailable = Boolean\(status\?\.available && latestVersion\)/);
  assert.match(blockSource, /const installActive = isUpdateInstallActive\(installStatus\)/);
  assert.match(blockSource, /\{updateAvailable && \(/);
  assert.match(blockSource, /t\("settings\.latestVersion"\)/);
  assert.match(blockSource, /`v\$\{latestVersion\}`/);
  assert.match(blockSource, /t\("settings\.releaseNotes"\)/);
  assert.match(blockSource, /status\?\.notes\?\.trim\(\) \|\| t\("settings\.noReleaseNotes"\)/);
  assert.match(blockSource, /onClick=\{onInstall\}/);
  assert.match(blockSource, /installActive \? "animate-spin" : ""/);
  assert.match(blockSource, /updateInstallButtonLabel\(installStatus, t\)/);
  assert.match(enSource, /latestVersion: "New version"/);
  assert.match(enSource, /releaseNotes: "Release notes"/);
  assert.match(enSource, /noReleaseNotes: "No release notes provided."/);
  assert.match(zhSource, /latestVersion: "新版本"/);
  assert.match(zhSource, /releaseNotes: "更新日志"/);
  assert.match(zhSource, /noReleaseNotes: "暂无更新日志。"/);
});

test("settings drawer does not render a persistent no-update row", async () => {
  const drawerSource = await readFile(settingsDrawerPath, "utf8");
  const blockSource = drawerSource.match(/function VersionUpdateBlock[\s\S]*?function isUpdateInstallActive/)?.[0] ?? "";

  assert.doesNotMatch(blockSource, /status && !status\.available/);
  assert.doesNotMatch(blockSource, /settings\.noUpdatesAvailable/);
});

test("app updater has an opt-in E2E script for virtual release detection and install", async () => {
  const [script, appUpdatesSource] = await Promise.all([
    readFile(appUpdateE2ePath, "utf8"),
    readFile(tauriAppUpdatesPath, "utf8"),
  ]);

  assert.match(script, /latest\.json/);
  assert.match(script, /virtual CodexHub update/);
  assert.match(script, /check_app_update/);
  assert.match(script, /start_app_update_install/);
  assert.match(script, /get_app_update_install_status/);
  assert.match(script, /CODEXHUB_UPDATE_E2E_SKIP_INSTALL/);
  assert.match(script, /\[switch\]\$Install/);
  assert.match(script, /\[switch\]\$DownloadOnly/);
  assert.match(script, /\[switch\]\$KeepAlive/);
  assert.match(script, /\[switch\]\$ValidateOnly/);
  assert.match(script, /KeepAlive enabled/);
  assert.match(script, /windows-x86_64/);
  assert.match(script, /windows-x86_64-nsis/);
  assert.match(appUpdatesSource, /CODEXHUB_UPDATE_E2E_ENDPOINT/);
  assert.match(appUpdatesSource, /CODEXHUB_UPDATE_E2E_SKIP_INSTALL/);
  assert.match(appUpdatesSource, /app\.updater_builder\(\)/);
  assert.match(appUpdatesSource, /builder[\s\S]*\.endpoints\(vec!\[endpoint\]\)/);
  assert.match(appUpdatesSource, /cfg\(debug_assertions\)/);
});

test("settings drawer places version updates at the bottom and keeps backdrop blur", async () => {
  const [settingsDrawerSource, settingsPageSource, enSource, zhSource] = await Promise.all([
    readFile(settingsDrawerPath, "utf8"),
    readFile(settingsPagePath, "utf8"),
    readFile(enLocalePath, "utf8"),
    readFile(zhLocalePath, "utf8"),
  ]);

  const codexSection =
    settingsDrawerSource.match(/<section className="grid gap-3">[\s\S]*?<h3 className="text-sm font-semibold text-ink">CodexHub<\/h3>[\s\S]*?<section className="grid gap-3">/)?.[0] ?? "";
  const imageProxyIndex = settingsDrawerSource.indexOf('t("settings.imageProxy")');
  const updatesIndex = settingsDrawerSource.lastIndexOf('t("settings.updates")');

  assert.doesNotMatch(codexSection, /settings\.updates/);
  assert.ok(imageProxyIndex >= 0, "image proxy section should be present");
  assert.ok(updatesIndex > imageProxyIndex, "version updates should be below image proxy settings");
  assert.match(settingsDrawerSource, /backdrop-blur-\[1px\]/);
  assert.match(settingsDrawerSource, /function VersionUpdateBlock/);
  assert.doesNotMatch(settingsPageSource, /settings\.updates/);
  assert.match(enSource, /updates: "Version & Updates"/);
  assert.match(zhSource, /updates: "版本与更新"/);
  assert.match(enSource, /installUpdate: "Install update"/);
  assert.match(zhSource, /installUpdate: "安装更新"/);
});

test("startup update check is delayed and silent on failure", async () => {
  const appSource = await readFile(appPath, "utf8");

  assert.match(appSource, /STARTUP_UPDATE_CHECK_DELAY_MS\s*=\s*2500/);
  assert.match(appSource, /APP_UPDATE_CHECK_INTERVAL_MS\s*=\s*24 \* 60 \* 60 \* 1000/);
  assert.match(appSource, /startupUpdateCheckStarted/);
  assert.match(appSource, /api\.checkAppUpdate\(\)/);
  assert.match(appSource, /settings\.updateAvailable/);
  assert.match(appSource, /settings\.update/);
  assert.match(appSource, /api\.startAppUpdateInstall\(\)/);
  assert.match(appSource, /window\.setInterval\(\(\) => void runAutomaticUpdateCheck\(\), APP_UPDATE_CHECK_INTERVAL_MS\)/);
  assert.match(appSource, /Automatic update checks are best-effort/);
  assert.doesNotMatch(appSource, /setBanner\(messageFromError\(err\)\)[\s\S]*Startup update/);
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
