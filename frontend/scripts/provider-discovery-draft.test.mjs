import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const actionsPath = new URL("../src/hooks/useProviderCatalogActions.ts", import.meta.url);
const editorPath = new URL("../src/components/providers/ProviderEditor.tsx", import.meta.url);
const endpointPath = new URL("../src/lib/providerEndpoint.ts", import.meta.url);
const formatPath = new URL("../src/lib/format.ts", import.meta.url);
const typesPath = new URL("../src/lib/types.ts", import.meta.url);

async function readFormatModule() {
  const [formatSource, typesSource] = await Promise.all([
    readFile(formatPath, "utf8"),
    readFile(typesPath, "utf8"),
  ]);

  const combinedSource = [
    typesSource
      .replace(/export (interface|type) /g, "declare $1 ")
      .replace(/export function/g, "function"),
    formatSource
      .replace(/^\s*import[\s\S]*?;\s*$/gm, "")
      .replace(/export function/g, "function"),
  ].join("\n\n");

  const jsOutput = ts.transpileModule(combinedSource, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      strict: false,
    },
  }).outputText;

  const moduleExports = {};
  const wrappedModule = new Function(
    "exports",
    jsOutput + "\nexports.mergeDiscoveredModels = mergeDiscoveredModels; exports.renumberModels = renumberModels;",
  );
  wrappedModule(moduleExports);
  return moduleExports;
}

function makeModel(overrides = {}) {
  return {
    id: "model-a",
    display_name: "Model A",
    upstream_model: "model-a",
    context_window: null,
    max_output_tokens: null,
    input_modalities: ["text"],
    supported_reasoning_levels: [],
    default_reasoning_level: null,
    source_kind: "manual",
    locked: false,
    codex_enabled: true,
    gateway_exported: true,
    sort_order: 1,
    enabled: true,
    ...overrides,
  };
}

async function readEndpointModule() {
  const source = (await readFile(endpointPath, "utf8"))
    .replace(/^import i18n from "\.\.\/i18n";\r?\n/m, "const i18n = { t: () => \"\" };\n")
    .replace(/^import \{ messageFromError \} from "\.\/tauri";\r?\n/m, "const messageFromError = (error) => String(error);\n")
    .replace(/export function/g, "function");
  const jsOutput = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      strict: false,
    },
  }).outputText;
  const moduleExports = {};
  const wrappedModule = new Function(
    "exports",
    jsOutput +
      "\nexports.applyProviderProbeResult = applyProviderProbeResult;" +
      " exports.applyAddProviderProbeResult = applyAddProviderProbeResult;" +
      " exports.probeSucceeded = probeSucceeded;",
  );
  wrappedModule(moduleExports);
  return moduleExports;
}

test("mergeDiscoveredModels preserves manual models absent from discovery", async () => {
  const { mergeDiscoveredModels } = await readFormatModule();
  const manual = makeModel({ id: "manual", display_name: "Manual Model" });
  const discovered = makeModel({ id: "discovered", display_name: "Discovered Model", source_kind: "discovered" });
  const merged = mergeDiscoveredModels([manual], [discovered]);
  const ids = merged.map((model) => model.id);
  assert.ok(ids.includes("manual"), "manual model should be retained");
  assert.ok(ids.includes("discovered"), "discovered model should be added");
});

test("mergeDiscoveredModels preserves existing per-model settings when a model is rediscovered", async () => {
  const { mergeDiscoveredModels } = await readFormatModule();
  const existing = makeModel({
    id: "shared",
    display_name: "Custom Display",
    upstream_model: "custom-upstream",
    input_modalities: ["text", "image"],
    supported_reasoning_levels: ["low", "medium"],
    default_reasoning_level: "low",
    codex_enabled: false,
    gateway_exported: false,
    enabled: false,
  });
  const discovered = makeModel({
    id: "shared",
    display_name: "Discovered Display",
    upstream_model: "discovered-upstream",
    input_modalities: ["text"],
    supported_reasoning_levels: [],
    default_reasoning_level: null,
    codex_enabled: true,
    gateway_exported: true,
    enabled: true,
  });
  const merged = mergeDiscoveredModels([existing], [discovered]);
  const result = merged.find((model) => model.id === "shared");
  assert.ok(result, "rediscovered model should be present");
  assert.equal(result.display_name, "Custom Display", "display_name should be preserved");
  assert.equal(result.upstream_model, "custom-upstream", "upstream_model should be preserved");
  assert.deepEqual(result.input_modalities, ["text", "image"], "input_modalities should be preserved");
  assert.deepEqual(result.supported_reasoning_levels, ["low", "medium"], "reasoning levels should be preserved");
  assert.equal(result.default_reasoning_level, "low", "default reasoning level should be preserved");
  assert.equal(result.codex_enabled, false, "codex_enabled should be preserved");
  assert.equal(result.gateway_exported, false, "gateway_exported should be preserved");
  assert.equal(result.enabled, false, "enabled should be preserved");
});

test("mergeDiscoveredModels leaves existing models unchanged on empty discovery", async () => {
  const { mergeDiscoveredModels } = await readFormatModule();
  const existing = [makeModel({ id: "manual" })];
  const merged = mergeDiscoveredModels(existing, []);
  assert.equal(merged.length, 1, "existing model should remain");
  assert.equal(merged[0].id, "manual", "existing model id should be unchanged");
});

test("refreshProviderModels merges against persisted provider models, not the stale draft", async () => {
  const source = await readFile(actionsPath, "utf8");
  assert.match(
    source,
    /const\s+persistedProvider\s*=\s*providers\.find\(\(item\)\s*=>\s*item\.id\s*===\s*provider\.id\)\s*\?\?\s*provider;/,
    "should look up the persisted provider by id",
  );
  assert.match(
    source,
    /mergeDiscoveredModels\(persistedProvider\.models,\s*models\)/,
    "should merge discovered models against the persisted provider's models",
  );
  assert.match(
    source,
    /const\s+previousModelIds\s*=\s*new\s+Set\(persistedProvider\.models\.map\(\(model\)\s*=>\s*model\.id\)\);/,
    "should compute previous model ids from the persisted provider's models",
  );
});

test("ProviderDetail keeps draft models in sync with persisted provider models", async () => {
  const source = await readFile(editorPath, "utf8");
  assert.match(
    source,
    /setDraft\(\(current\)\s*=>\s*\{\s*if\s*\(\s*current\.id\s*!==\s*normalizedProvider\.id\s*\|\|\s*current\.models\s*===\s*normalizedProvider\.models\s*\)\s*\{\s*return\s*current;\s*\}\s*return\s*\{\s*\.\.\.current,\s*models:\s*normalizedProvider\.models\s*\};\s*\}\);/,
    "should sync draft models from normalizedProvider.models",
  );
  assert.match(
    source,
    /useEffect\(\(\)\s*=>\s*\{\s*setDraft\(\(current\)\s*=>\s*\{/,
    "should use an effect to update the draft",
  );
});

test("model-required probe preserves existing endpoint capabilities", async () => {
  const { applyAddProviderProbeResult, applyProviderProbeResult, probeSucceeded } = await readEndpointModule();
  const provider = {
    id: "provider-a",
    name: "Provider A",
    base_url: "https://example.test/v1",
    api_key: "secret",
    upstream_format: "chat_completions",
    available_upstream_formats: ["chat_completions"],
    tool_protocol: "chat_tools",
    enabled: true,
    models: [],
  };
  const form = { ...provider, id: "", name: "" };
  const modelRequired = {
    base_url: provider.base_url,
    model: null,
    model_required: true,
    models_ok: false,
    responses_text_ok: false,
    responses_tool_ok: false,
    responses_tool_stream_ok: false,
    chat_text_ok: false,
    chat_tool_ok: false,
    chat_tool_stream_ok: false,
    chat_tool_history_ok: false,
    anthropic_text_ok: false,
    recommended_format: "auto",
    recommended_tool_protocol: "none",
    notes: ["No model is available for POST probes."],
  };

  assert.equal(probeSucceeded(modelRequired), false);
  assert.deepEqual(applyProviderProbeResult(provider, modelRequired), provider);
  assert.deepEqual(applyAddProviderProbeResult(form, modelRequired), form);
});

test("model-required probe cannot be marked successful by a suggested format", async () => {
  const { probeSucceeded } = await readEndpointModule();
  assert.equal(
    probeSucceeded({
      base_url: "https://example.test/v1",
      model: null,
      model_required: true,
      models_ok: false,
      responses_text_ok: false,
      responses_tool_ok: false,
      responses_tool_stream_ok: false,
      chat_text_ok: false,
      chat_tool_ok: false,
      chat_tool_stream_ok: false,
      chat_tool_history_ok: false,
      anthropic_text_ok: false,
      recommended_format: "responses",
      recommended_tool_protocol: "responses_structured",
      notes: [],
    }),
    false,
  );
});
