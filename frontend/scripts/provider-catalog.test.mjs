import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const catalogPath = new URL("../src/lib/providerCatalog.ts", import.meta.url);
const source = await readFile(catalogPath, "utf8");
const jsOutput = ts.transpileModule(
  source.replace(/^\s*import[\s\S]*?;\s*$/m, ""),
  {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      strict: false,
    },
  },
).outputText;

const moduleExports = {};
new Function("exports", jsOutput + "\nexports.applyCatalogPresetDefaults = applyCatalogPresetDefaults;")(
  moduleExports,
);
const { applyCatalogPresetDefaults } = moduleExports;

function makeProvider(overrides = {}) {
  return {
    id: "xai",
    name: "xAI",
    base_url: "",
    api_key: null,
    upstream_format: "responses",
    available_upstream_formats: [],
    tool_protocol: "auto",
    display_prefix: null,
    sort_order: 2,
    enabled: true,
    locked: false,
    models: [],
    ...overrides,
  };
}

const catalogXai = makeProvider({
  base_url: "https://api.x.ai/v1",
  api_key: "{env:XAI_API_KEY}",
  display_prefix: "xAI",
  available_upstream_formats: ["responses"],
  reports_cached_input_tokens: false,
  models: [
    {
      id: "grok-4",
      display_name: "Grok 4",
      enabled: true,
      context_window: 256000,
      max_output_tokens: 65536,
      sort_order: 1,
    },
  ],
});

test("empty xAI stub inherits catalog endpoint and Grok 4 without copying the env api key", () => {
  const stub = makeProvider();
  const filled = applyCatalogPresetDefaults(stub, catalogXai);
  assert.equal(filled.base_url, "https://api.x.ai/v1");
  assert.equal(filled.api_key, null);
  assert.equal(filled.display_prefix, "xAI");
  assert.deepEqual(filled.available_upstream_formats, ["responses"]);
  assert.equal(filled.reports_cached_input_tokens, false);
  assert.equal(filled.models.length, 1);
  assert.equal(filled.models[0].id, "grok-4");
  assert.equal(filled.enabled, true);
  assert.equal(filled.sort_order, 2);
});

test("complete provider is left unchanged", () => {
  const existing = applyCatalogPresetDefaults(makeProvider(), catalogXai);
  assert.equal(applyCatalogPresetDefaults(existing, catalogXai), existing);
});

test("missing preset is a no-op", () => {
  const stub = makeProvider();
  assert.equal(applyCatalogPresetDefaults(stub, null), stub);
});

test("includeModels false fills the endpoint without seeding grok-4", () => {
  const filled = applyCatalogPresetDefaults(makeProvider(), catalogXai, { includeModels: false });
  assert.equal(filled.base_url, "https://api.x.ai/v1");
  assert.deepEqual(filled.models, []);
});
