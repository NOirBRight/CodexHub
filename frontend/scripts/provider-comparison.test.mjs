import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const comparisonPath = new URL("../src/lib/providerComparison.ts", import.meta.url);
const endpointPath = new URL("../src/lib/providerEndpoint.ts", import.meta.url);
const modelPath = new URL("../src/lib/providerModel.ts", import.meta.url);
const typesPath = new URL("../src/lib/types.ts", import.meta.url);

const [comparisonSource, endpointSource, modelSource, typesSource] = await Promise.all([
  readFile(comparisonPath, "utf8"),
  readFile(endpointPath, "utf8"),
  readFile(modelPath, "utf8"),
  readFile(typesPath, "utf8"),
]);

// Strip imports and exports, then combine all sources for evaluation.
// providerEndpoint imports i18n and tauri at runtime but none of those
// functions are called during comparison - only normalizeProviderEndpointSelection
// and normalizeModel are invoked, which are pure and dependency-free.
// Strip ALL import lines (including type imports and multi-line) from every source.
function stripImports(source) {
  return source.replace(/^\s*import[\s\S]*?;\s*$/gm, "");
}

const combinedSource = [
  typesSource
    .replace(/export (interface|type) /g, "declare $1 ")
    .replace(/export function/g, "function"),
  stripImports(endpointSource)
    .replace(/export function/g, "function")
    .replace(/export \{[^}]+\}/g, ""),
  stripImports(modelSource)
    .replace(/export function/g, "function"),
  stripImports(comparisonSource)
    .replace(/export function/g, "function")
    .replace(/export \{[^}]+\}/g, ""),
].join("\n\n");

const jsOutput = ts.transpileModule(combinedSource, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
    strict: false,
  },
}).outputText;

const moduleExports = {};
// Provide a mock require for any residual dependencies (none are called
// during comparison — normalizeProviderEndpointSelection and normalizeModel
// are pure and dependency-free).
const mockRequire = () => ({ t: () => "" });
const wrappedModule = new Function(
  "exports",
  "require",
  jsOutput + "\nexports.normalizeProviderForComparison = normalizeProviderForComparison; exports.isProviderDirty = isProviderDirty;",
);
wrappedModule(moduleExports, mockRequire);
const { isProviderDirty, normalizeProviderForComparison } = moduleExports;

function makeModel(overrides = {}) {
  return {
    id: "model-a",
    display_name: "Model A",
    upstream_model: "model-a",
    enabled: true,
    context_window: null,
    input_modalities: ["text"],
    supported_reasoning_levels: [],
    default_reasoning_level: null,
    ...overrides,
  };
}

function makeProvider(overrides = {}) {
  return {
    id: "prov-1",
    name: "Test Provider",
    base_url: "https://example.com",
    api_key: null,
    upstream_format: "responses",
    available_upstream_formats: [],
    tool_protocol: "auto",
    enabled: true,
    models: [makeModel()],
    ...overrides,
  };
}

test("toggle revert: disabling then re-enabling a model does not produce dirty state", () => {
  const baseline = makeProvider();
  const draftDisabled = makeProvider({
    models: [makeModel({ enabled: false })],
  });
  const draftReverted = makeProvider({
    models: [makeModel({ enabled: true })],
  });
  assert.ok(isProviderDirty(baseline, draftDisabled), "disabling a model should be dirty");
  assert.ok(!isProviderDirty(baseline, draftReverted), "re-enabling the model should not be dirty");
});

test("text-field revert: changing and restoring display_name does not produce dirty state", () => {
  const baseline = makeProvider();
  const draftChanged = makeProvider({
    models: [makeModel({ display_name: "Changed" })],
  });
  const draftReverted = makeProvider({
    models: [makeModel({ display_name: "Model A" })],
  });
  assert.ok(isProviderDirty(baseline, draftChanged), "changing display_name should be dirty");
  assert.ok(!isProviderDirty(baseline, draftReverted), "restoring display_name should not be dirty");
});

test("optional/default fields: omitted fields equal their persisted default equivalents", () => {
  const baselineWithOmitted = {
    id: "prov-1",
    name: "Test Provider",
    base_url: "https://example.com",
    enabled: true,
    models: [{
      id: "model-a",
      display_name: "Model A",
      upstream_model: "model-a",
      enabled: true,
    }],
  };
  const draftWithDefaults = {
    id: "prov-1",
    name: "Test Provider",
    base_url: "https://example.com",
    enabled: true,
    models: [{
      id: "model-a",
      display_name: "Model A",
      upstream_model: "model-a",
      enabled: true,
      context_window: null,
      input_modalities: ["text"],
      supported_reasoning_levels: [],
      default_reasoning_level: null,
    }],
  };
  assert.ok(
    !isProviderDirty(baselineWithOmitted, draftWithDefaults),
    "omitted optional fields should equal their normalized defaults",
  );
});

test("semantic comparison ignores key insertion order at every object depth", () => {
  const baseline = {
    id: "prov-1",
    name: "Test Provider",
    base_url: "https://example.com",
    enabled: true,
    models: [{
      id: "model-a",
      display_name: "Model A",
      upstream_model: "model-a",
      enabled: true,
      pricing: {
        input_per_million: 1,
        cached_input_per_million: 0.5,
        output_per_million: 2,
        currency: "USD",
        source: "catalog",
        estimate: false,
      },
      metadata_provenance: {
        source: "catalog",
        source_url: "https://example.com/catalog",
        fetched_at: "2026-07-14T00:00:00Z",
        confidence: "high",
      },
    }],
  };
  const draft = {
    models: [{
      metadata_provenance: {
        confidence: "high",
        fetched_at: "2026-07-14T00:00:00Z",
        source_url: "https://example.com/catalog",
        source: "catalog",
      },
      pricing: {
        estimate: false,
        source: "catalog",
        currency: "USD",
        output_per_million: 2,
        cached_input_per_million: 0.5,
        input_per_million: 1,
      },
      enabled: true,
      upstream_model: "model-a",
      display_name: "Model A",
      id: "model-a",
    }],
    enabled: true,
    base_url: "https://example.com",
    name: "Test Provider",
    id: "prov-1",
  };
  assert.ok(!isProviderDirty(baseline, draft), "key insertion order should not produce dirty state");
});

test("omitted model codex and Gateway flags equal their persisted true defaults", () => {
  const baseline = makeProvider({
    models: [makeModel({ codex_enabled: undefined, gateway_exported: undefined })],
  });
  const draft = makeProvider({
    models: [makeModel({ codex_enabled: true, gateway_exported: true })],
  });
  assert.ok(!isProviderDirty(baseline, draft), "omitted persisted-true flags should compare equal to true");
});

test("restoring visible model order ignores redundant sort_order metadata", () => {
  const baseline = makeProvider({
    models: [
      makeModel({ id: "a", display_name: "A", sort_order: null }),
      makeModel({ id: "b", display_name: "B" }),
    ],
  });
  const draftRestored = makeProvider({
    models: [
      makeModel({ id: "a", display_name: "A", sort_order: 1 }),
      makeModel({ id: "b", display_name: "B", sort_order: 2 }),
    ],
  });
  assert.ok(!isProviderDirty(baseline, draftRestored), "redundant sequential sort_order should not be dirty");
});

test("actual model reorder remains dirty after sort_order canonicalization", () => {
  const baseline = makeProvider({
    models: [
      makeModel({ id: "a", display_name: "A", sort_order: null }),
      makeModel({ id: "b", display_name: "B" }),
    ],
  });
  const draftReordered = makeProvider({
    models: [
      makeModel({ id: "b", display_name: "B", sort_order: 1 }),
      makeModel({ id: "a", display_name: "A", sort_order: 2 }),
    ],
  });
  assert.ok(isProviderDirty(baseline, draftReordered), "reordering models should be dirty");
});

test("normalizeProviderForComparison normalizes nested models", () => {
  const provider = {
    id: "prov-1",
    name: "Test Provider",
    base_url: "https://example.com",
    enabled: true,
    models: [{
      id: "model-a",
      enabled: true,
    }],
  };
  const normalized = normalizeProviderForComparison(provider);
  assert.equal(normalized.models[0].context_window, null);
  assert.deepEqual(normalized.models[0].input_modalities, ["text"]);
  assert.deepEqual(normalized.models[0].supported_reasoning_levels, []);
  assert.equal(normalized.models[0].default_reasoning_level, null);
  assert.equal(normalized.upstream_format, "responses");
  assert.equal(normalized.tool_protocol, "auto");
});

test("provider-level field changes are dirty", () => {
  const baseline = makeProvider();
  const draft = makeProvider({ name: "Changed Name" });
  assert.ok(isProviderDirty(baseline, draft), "changing provider name should be dirty");
});

test("null vs undefined api_key are equal", () => {
  const baseline = makeProvider({ api_key: undefined });
  const draft = makeProvider({ api_key: null });
  assert.ok(!isProviderDirty(baseline, draft), "undefined and null api_key should be equal");
});
