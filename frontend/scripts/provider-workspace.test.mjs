import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const corePath = new URL("../src/lib/providerWorkspace/core.ts", import.meta.url);
const feedbackPath = new URL("../src/lib/providerWorkspace/feedback.ts", import.meta.url);
const typesPath = new URL("../src/lib/types.ts", import.meta.url);

// Repo-standard pattern: strip imports/exports, combine sources, transpile.
// (providerEndpoint/providerCatalog/format import i18n at runtime; none of the
// functions under test touch i18n, so the combined module is evaluable.)
async function loadCombinedModule() {
  const [coreSource, feedbackSource, typesSource, officialModelsSource] = await Promise.all([
    readFile(corePath, "utf8"),
    readFile(feedbackPath, "utf8"),
    readFile(typesPath, "utf8"),
    readFile(new URL("../src/lib/officialModels.ts", import.meta.url), "utf8"),
  ]);

  const stripImports = (src) =>
    src
      .replace(/^\s*import[\s\S]*?;\s*$/gm, "")
      .replace(/export (interface|type) /g, "declare $1 ")
      .replace(/export (function|const) /g, "$1 ");

  const combined = [
    stripImports(typesSource),
    stripImports(feedbackSource),
    "function normalizeOfficialModelId(value) { return value.trim().replace(/^openai\\//, ''); }",
    stripImports(officialModelsSource),
    // Stub the i18n-adjacent helpers core.ts imports (pure under test).
    "function instantiateCatalogProvider(preset, sortOrder) { return { ...preset, sort_order: sortOrder }; }",
    "function mergeDiscoveredModels(base, discovered) { const seen = new Set(base.map((m) => m.id)); return [...base, ...discovered.filter((m) => !seen.has(m.id))]; }",
    "function slugify(name) { return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); }",
    "function normalizeModel(m) { return m; }",
    "function renumberModels(models) { return models.map((m, i) => ({ ...m, sort_order: i + 1 })); }",
    "function applyPresetReasoningDefaults(models, _preset) { return models; }",
    "function normalizeEndpointFormats(fmts) { return fmts; }",
    "function applyProviderProbeResult(p, _r) { return p; }",
    "function probeSucceeded(r) { return Boolean(r && !r.model_required && !r.inconclusive_reason); }",
    "function bundledPresetFor(_id, _bundled) { return null; }",
    "const emptyProvider = { id: '', name: '', base_url: '', api_key: '', upstream_format: 'auto', available_upstream_formats: [], tool_protocol: 'auto', display_prefix: '', models: [] };",
    stripImports(coreSource),
  ].join("\n\n");

  const js = ts.transpileModule(combined, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, strict: false },
  }).outputText;

  const moduleExports = {};
  const mockRequire = () => ({ t: () => "" });
  const wrapped = new Function(
    "exports",
    "require",
    js + "\nexports.buildOfficialRefreshIntent = buildOfficialRefreshIntent; exports.providerWorkspaceReducer = providerWorkspaceReducer; exports.selectOfficialEnabledCount = selectOfficialEnabledCount; exports.selectOfficialModelDraftDirty = selectOfficialModelDraftDirty; exports.selectSelectedProvider = selectSelectedProvider; exports.catalogOverrideToastMessage = catalogOverrideToastMessage;",
  );
  wrapped(moduleExports, mockRequire);
  return moduleExports;
}

test("official refresh intent ignores DOM event fields", async () => {
  const m = await loadCombinedModule();
  const intent = m.buildOfficialRefreshIntent({ type: "click", target: "button" });
  assert.equal(intent.type, "refreshOfficialModels");
  assert.equal(intent.quiet, undefined);
  assert.equal(intent.throwOnError, undefined);
});

test("provider workspace reducer select intent updates selection", async () => {
  const m = await loadCombinedModule();
  const state = {
    busy: null, catalogModels: [], modelMetadata: [],
    form: { id: "", name: "", base_url: "", api_key: "", upstream_format: "auto", available_upstream_formats: [], tool_protocol: "auto", display_prefix: "", models: [] },
    modelDiscoveryError: null,
    officialDisabledModelsDraft: [], officialModelOrderDraft: [], officialModels: [],
    pendingNavigation: null, pendingNewProvider: null, probeResult: null,
    providers: [{ id: "p1", name: "P1", sort_order: 1 }],
    selectedId: "__official__",
    settings: { official_disabled_models: [], official_model_sort_order: [] },
    settingsDraft: null,
  };
  const next = m.providerWorkspaceReducer(state, { type: "select", targetId: "p1" });
  assert.equal(next.selectedId, "p1");
});

test("toggle official model adds and removes disabled ids", async () => {
  const m = await loadCombinedModule();
  const base = { officialDisabledModelsDraft: ["m1"] };
  const enabled = m.providerWorkspaceReducer(base, { type: "toggleOfficialModel", modelId: "m1", enabled: true });
  assert.deepEqual(enabled.officialDisabledModelsDraft, []);
  const disabled = m.providerWorkspaceReducer(enabled, { type: "toggleOfficialModel", modelId: "m2", enabled: false });
  assert.deepEqual(disabled.officialDisabledModelsDraft, ["m2"]);
});

test("selectOfficialEnabledCount counts non-disabled models", async () => {
  const m = await loadCombinedModule();
  const state = { officialModels: [{ id: "a" }, { id: "b" }, { id: "c" }], officialDisabledModelsDraft: ["b"] };
  assert.equal(m.selectOfficialEnabledCount(state), 2);
});

test("selectOfficialModelDraftDirty detects changes", async () => {
  const m = await loadCombinedModule();
  const clean = { settings: { official_disabled_models: [], official_model_sort_order: [] }, officialDisabledModelsDraft: [], officialModelOrderDraft: [] };
  assert.equal(m.selectOfficialModelDraftDirty(clean), false);
  const dirty = { ...clean, officialDisabledModelsDraft: ["x"] };
  assert.equal(m.selectOfficialModelDraftDirty(dirty), true);
});

test("selectSelectedProvider prefers pending provider when selected", async () => {
  const m = await loadCombinedModule();
  const state = {
    pendingNewProvider: { id: "new1", name: "New", sort_order: 2 },
    selectedId: "new1",
    providers: [{ id: "p1", name: "P1", sort_order: 1 }],
  };
  assert.equal(m.selectSelectedProvider(state).id, "new1");
});

test("stageCatalogPreset stages and selects a new provider", async () => {
  const m = await loadCombinedModule();
  const state = {
    providers: [{ id: "p1", name: "P1", sort_order: 1 }],
    pendingNewProvider: null,
    selectedId: "__official__",
  };
  const next = m.providerWorkspaceReducer(state, { type: "stageCatalogPreset", preset: { id: "new1", name: "New", sort_order: 2 } });
  assert.equal(next.selectedId, "new1");
  assert.ok(next.pendingNewProvider);
});

test("syncExternal repairs external deletion of selected provider", async () => {
  const m = await loadCombinedModule();
  const state = {
    selectedId: "p1",
    pendingNewProvider: null,
    providers: [{ id: "p1", name: "P1", sort_order: 1 }],
    catalogModels: [], modelMetadata: [], settings: null, settingsDraft: null,
  };
  const next = m.providerWorkspaceReducer(state, {
    type: "syncExternal",
    providers: [{ id: "p2", name: "P2", sort_order: 1 }],
    settings: null, catalogModels: [], modelMetadata: [],
  });
  assert.equal(next.selectedId, "p2");
});

test("save readback preserves live models, selection and custom order over an older catalog", async () => {
  const { providerWorkspaceReducer: reduce } = await loadCombinedModule();
  const model = (id) => ({ id, enabled: true, visibility: "list" });
  const oldCatalog = [model("gpt-5.6-sol"), model("gpt-5.4")];
  const source = {
    providers: [], catalogModels: oldCatalog, modelMetadata: oldCatalog,
    settings: { official_model_sort_order: [], official_disabled_models: [] },
  };
  let state = {
    ...source, selectedId: "__official__", officialModels: oldCatalog,
    officialModelSnapshot: null, officialModelOrderDraft: [], officialDisabledModelsDraft: [],
  };
  state = reduce(state, { type: "setOfficialModels", models: [model("gpt-6-astra"), model("gpt-5.6-sol")] });
  state = reduce(state, { type: "toggleOfficialModel", modelId: "gpt-5.6-sol", enabled: false });
  state = reduce(state, { type: "reorderOfficialModels", models: [model("gpt-5.6-sol"), model("gpt-6-astra")] });
  // A background read before Save must also preserve the unsaved order.
  state = reduce(state, { type: "syncExternal", ...source });
  assert.deepEqual(state.officialModels.map((m) => m.id), ["gpt-5.6-sol", "gpt-6-astra"]);
  const saved = { official_model_sort_order: state.officialModelOrderDraft, official_disabled_models: state.officialDisabledModelsDraft };
  state = reduce(state, { type: "setSettings", settings: saved });
  state = reduce(state, { type: "syncExternal", ...source, settings: saved });
  assert.deepEqual(state.officialModels.map((m) => m.id), saved.official_model_sort_order);
  assert.deepEqual(state.officialDisabledModelsDraft, ["gpt-5.6-sol"]);
  // An explicit refresh still replaces the membership, including removals.
  state = reduce(state, { type: "setOfficialModels", models: [model("gpt-6-astra")] });
  state = reduce(state, { type: "syncExternal", ...source, settings: saved });
  assert.deepEqual(state.officialModels.map((m) => m.id), ["gpt-6-astra"]);
});

test("workspace without a live snapshot still receives catalog updates", async () => {
  const { providerWorkspaceReducer: reduce } = await loadCombinedModule();
  const next = reduce({ selectedId: "__official__", officialModelSnapshot: null }, {
    type: "syncExternal", providers: [], settings: null, modelMetadata: [],
    catalogModels: [{ id: "gpt-6-astra", visibility: "list", enabled: true }],
  });
  assert.deepEqual(next.officialModels.map((m) => m.id), ["gpt-6-astra"]);
});

test("snapshot receives published metadata without reverting membership, selection or order", async () => {
  const { providerWorkspaceReducer: reduce } = await loadCombinedModule();
  const sol = { id: "gpt-5.6-sol", visibility: "list", enabled: false, sort_order: 2, context_window: 400000, max_context_window: 400000 };
  const astra = { id: "gpt-6-astra", visibility: "list", enabled: true, sort_order: 1 };
  let state = {
    selectedId: "__official__", officialModelOrderDraft: [astra.id, sol.id],
    officialDisabledModelsDraft: [sol.id], officialModelSnapshot: [astra, sol],
  };
  const source = {
    type: "syncExternal", providers: [], settings: {},
    catalogModels: [
      { ...sol, id: "openai/gpt-5.6-sol", enabled: true, sort_order: 1, context_window: 272000, max_context_window: 300000, multi_agent_version: "v2" },
      { id: "gpt-5.4", visibility: "list", enabled: true },
    ],
    modelMetadata: [{ ...sol, context_window: 999000, max_context_window: 999000 }],
  };
  state = reduce(state, source);
  assert.deepEqual(state.officialModels.map((m) => m.id), [astra.id, sol.id]);
  assert.equal(state.officialModels[1].context_window, 272000);
  assert.equal(state.officialModels[1].max_context_window, 300000);
  assert.equal(state.officialModels[1].multi_agent_version, "v2");
  assert.equal(state.officialModels[1].enabled, false);
  assert.equal(state.officialModels[1].sort_order, 2);
  assert.deepEqual(state.officialDisabledModelsDraft, [sol.id]);
  state = reduce(state, { ...source, catalogModels: [], modelMetadata: [] });
  assert.equal(state.officialModels[1].context_window, 272000);
});

test("catalogOverrideToastMessage null when all zero", async () => {
  const m = await loadCombinedModule();
  assert.equal(m.catalogOverrideToastMessage({ accepted: 0, rejected: 0, migrated: 0 }, () => "x"), null);
});

test("catalogOverrideToastMessage builds message when non-zero", async () => {
  const m = await loadCombinedModule();
  const t = (key, options) => key + ":" + JSON.stringify(options);
  const message = m.catalogOverrideToastMessage({ accepted: 1, rejected: 2, migrated: 3 }, t);
  assert.match(message, /catalogOverrideDiagnostics/);
  assert.match(message, /"accepted":1/);
});
