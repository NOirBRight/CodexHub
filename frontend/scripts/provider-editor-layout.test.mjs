import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const editorPath = new URL("../src/components/providers/ProviderEditor.tsx", import.meta.url);
const usagePath = new URL(
  "../src/components/providers/OfficialOpenAIUsagePanel.tsx",
  import.meta.url,
);
const providersPagePath = new URL("../src/pages/ProvidersPage.tsx", import.meta.url);

const editorSource = await readFile(editorPath, "utf8");
const usageSource = await readFile(usagePath, "utf8");
const providersPageSource = await readFile(providersPagePath, "utf8");

function namedFunction(source, name) {
  const match = source.match(
    new RegExp(`(?:export\\s+)?function\\s+${name}\\b[\\s\\S]*?(?=\\n(?:export\\s+)?function\\s+|\\nexport function\\s|$)`),
  );
  assert.ok(match, `missing function ${name}`);
  return match[0];
}

function firstDiv(source, fromIndex = 0) {
  const start = source.indexOf("<div", fromIndex);
  assert.ok(start >= 0, "expected a div");
  let depth = 0;
  for (let index = start; index < source.length; index += 1) {
    if (source.startsWith("</div>", index)) {
      depth -= 1;
      if (depth === 0) {
        return source.slice(start, index + "</div>".length);
      }
      index += "</div>".length - 1;
      continue;
    }
    if (source.startsWith("<div", index) && /<div[\s>]/.test(source.slice(index, index + 5))) {
      depth += 1;
    }
  }
  assert.fail("unbalanced div");
}

function connectionGrid(componentSource) {
  const gridStart = componentSource.indexOf('<div className="grid grid-cols-2 gap-2">');
  assert.ok(gridStart >= 0, "connection fields should use a two-column grid");
  return firstDiv(componentSource, gridStart);
}

test("ChatGPT Web runtime controls sit outside the connection grid", () => {
  const detail = namedFunction(editorSource, "ProviderDetail");
  const card = detail.indexOf("<ChatGptWebRuntimeCard");
  const grid = detail.indexOf('className="grid grid-cols-2 gap-2"');
  assert.ok(card >= 0 && grid > card);
  const runtimeBranch = detail.slice(detail.indexOf("if (chatgptWebRuntime)"), grid);
  assert.match(runtimeBranch, /return \(/);
  assert.match(runtimeBranch, /unsaved=\{unsaved\}/);
  assert.doesNotMatch(runtimeBranch, /common\.baseUrl|common\.apiKey|<ModelSection|<EndpointSelectionPanel/);
  assert.doesNotMatch(connectionGrid(detail), /ChatGptWebRuntimeCard/);
});

test("ProviderDetail and AddProviderPanel share a two-column connection grid instead of stacked full-width Base URL and endpoint rows", () => {
  for (const name of ["ProviderDetail", "AddProviderPanel"]) {
    const grid = connectionGrid(namedFunction(editorSource, name));
    assert.match(grid, /common\.name/);
    assert.match(grid, /common\.apiKey/);
    assert.match(grid, /common\.baseUrl/);
    assert.match(grid, /EndpointSelectionPanel/);
    // Subscription login replaces the key field, so only its Name spans both columns.
    assert.doesNotMatch(grid.replace('xaiSubscriptionAuth ? "col-span-2" : undefined', 'undefined'), /col-span-2/);
    if (name === "ProviderDetail") {
      assert.match(grid, /!xaiSubscriptionAuth && \(\s*<Field label=\{t\("common\.apiKey"\)\}/);
    }
    assert.doesNotMatch(
      grid,
      /baseUrl["'`][^>]{0,80}className="col-span-2"/,
    );
  }
  assert.doesNotMatch(editorSource, /账户与用量|模型 \/ /);
});

test("model rows stay one identity line with a compact SortableList gap", async () => {
  const [section, sortable] = await Promise.all([
    readFile(new URL("../src/components/providers/ProviderModelSection.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/components/SortableList.tsx", import.meta.url), "utf8"),
  ]);
  assert.match(section, /className="space-y-1"/);
  assert.match(section, /grid-cols-\[minmax\(0,1fr\)_minmax\(0,1fr\)\]/);
  assert.match(sortable, /cx\("px-px py-px"/);
  assert.doesNotMatch(sortable, /cx\("space-y-3 px-px py-px"/);
});

test("Official usage header composes title, metric windows, and day/week on one row", () => {
  const panel = namedFunction(usageSource, "OfficialOpenAIUsagePanel");
  const sectionStart = panel.indexOf("<section");
  assert.ok(sectionStart >= 0, "usage panel should render a section");
  const header = firstDiv(panel, panel.indexOf("<div", sectionStart));
  assert.match(header, /providers\.openaiUsage/);
  assert.match(header, /UsageMetric/);
  assert.match(header, /grid-cols-\[repeat\(5,minmax\(0,1fr\)\)\]/);
  assert.match(header, /modeOptions\.map/);
  assert.match(header, /setMode\(option\.value\)/);
  assert.match(panel, /t\("usage\.day"\)/);
  assert.match(panel, /t\("usage\.week"\)/);

  const rest = panel.slice(panel.indexOf(header) + header.length);
  assert.doesNotMatch(rest, /UsageMetric/);
  assert.doesNotMatch(rest, /grid-cols-\[repeat\(5,minmax\(0,1fr\)\)\]/);
  assert.match(rest, /data-openai-usage-chart/);

  const skeleton = namedFunction(usageSource, "OfficialOpenAIUsageSkeleton");
  assert.doesNotMatch(skeleton, /grid-cols-\[repeat\(5,minmax\(0,1fr\)\)\]/);

  const officialDetail = namedFunction(providersPageSource, "OfficialDetail");
  assert.match(officialDetail, /OfficialOpenAIUsageLimitBars/);
  assert.match(officialDetail, /OfficialOpenAIUsagePanel/);
  assert.match(officialDetail, /openaiSourceExcludedDetail/);
  assert.doesNotMatch(officialDetail, /账户与用量/);
});

test("model settings overlay is a nested dialog that consumes Escape", async () => {
  const [section, focus] = await Promise.all([
    readFile(new URL("../src/components/providers/ProviderModelSection.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/hooks/useDialogFocus.ts", import.meta.url), "utf8"),
  ]);
  assert.match(section, /useDialogFocus\(true, panel, onClose\)/);
  assert.match(section, /data-nested-dialog=""/);
  assert.match(section, /role="dialog"/);
  assert.match(focus, /querySelector\("\[data-nested-dialog\]"\)/);
  assert.match(focus, /nested\.contains\(document\.activeElement\)/);
});

test("ChatGPT Provider Connection keeps service credentials masked and exposes model choices", async () => {
  const source = await readFile(new URL("../src/components/providers/ChatGptWebRuntimeCard.tsx", import.meta.url), "utf8");
  const types = await readFile(new URL("../src/lib/types.ts", import.meta.url), "utf8");
  const [english, chinese] = await Promise.all([
    readFile(new URL("../src/i18n/locales/en-US.ts", import.meta.url), "utf8"),
    readFile(new URL("../src/i18n/locales/zh-CN.ts", import.meta.url), "utf8"),
  ]);
  assert.match(source, /type="password"/);
  assert.match(source, /api\.chatgptWebConnectionCheck\(provider\.base_url\.trim\(\), provider\.api_key \?\? ""\)/);
  assert.match(source, /function toggleModel/);
  assert.match(source, /gateway_exported: true/);
  assert.match(source, /onProviderChange\(\{ \.\.\.provider, models: nextModels \}\)/);
  assert.doesNotMatch(source, /api\.chatgptWebOpenLogin|api\.chatgptWebDeleteAccount/);
  assert.match(types, /settings_pending_restart: boolean/);
  assert.match(source, /status\?\.settings_pending_restart/);
  assert.match(source, /timeoutMs: null/);
  assert.match(source, /dismissToast\(settingsRestartToast\.current\)/);
  assert.match(english, /Restart the ChatGPT Web component in CodexHub/);
  assert.match(english, /chatgptWebSettingsPendingRestart:/);
  assert.match(chinese, /chatgptWebSettingsPendingRestart:/);
  const readiness = source.slice(source.indexOf("const textReady ="), source.indexOf("const stateKey ="));
  assert.doesNotMatch(readiness, /settings_pending_restart/);
});

test("ChatGPT connection badges require confirmed runtime capabilities, including while settings are pending", async () => {
  const source = await readFile(new URL("../src/components/providers/ChatGptWebRuntimeCard.tsx", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const status = {
    installed: true, disabled: false, restart_required: false, settings_pending_restart: true,
    admitting: true, ready: false, component: { compatible: true },
    login: { state: "signed_in" }, browser_smoke: { state: "passed" },
    tunnel: { state: "not_started" }, connector: { selectable: false },
    process: { running: true, listen_host: "127.0.0.1", port: 8765 },
    models: [],
  };
  function render(readinessChecks) {
    let stateIndex = 0;
    const jsx = (type, props) => ({ type, props });
    const exports = {};
    const require = (id) => {
      if (id === "react") return {
        useState: (initial) => [stateIndex++ === 0 ? { ...status, readiness_checks: readinessChecks } : initial, () => {}],
        useRef: (current) => ({ current }), useEffect: () => {},
      };
      if (id === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (id === "react-i18next") return { useTranslation: () => ({ t: (key) => key }) };
      if (id === "../PageToast") return { useToasts: () => ({}) };
      return {};
    };
    new Function("exports", "require", compiled)(exports, require);
    return exports.ChatGptWebRuntimeCard({
      provider: { enabled: true, base_url: "", api_key: "", models: [] },
      onProviderChange: () => {},
    });
  }
  function visibleText(node) {
    if (typeof node === "string") return node;
    if (Array.isArray(node)) return node.map(visibleText).join(" ");
    return node && typeof node === "object" ? visibleText(node.props?.children) : "";
  }
  const available = visibleText(render({ capabilities_match: true }));
  assert.match(available, /providers\.chatgptWebTextReady/);
  assert.match(available, /providers\.chatgptWebToolsPending/);
  for (const checks of [{ capabilities_match: false }, { capabilities_match: null }, {}, undefined]) {
    const unavailable = visibleText(render(checks));
    assert.doesNotMatch(unavailable, /providers\.chatgptWebTextReady/);
    assert.match(unavailable, /providers\.chatgptWebNotReady/);
  }
});
