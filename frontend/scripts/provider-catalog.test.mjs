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
new Function(
  "exports",
  jsOutput +
    "\nexports.applyCatalogPresetDefaults = applyCatalogPresetDefaults; exports.subscriptionAuthAdapter = subscriptionAuthAdapter; exports.usesSubscriptionAuth = usesSubscriptionAuth; exports.applyPresetReasoningDefaults = applyPresetReasoningDefaults; exports.instantiateCatalogProvider = instantiateCatalogProvider; exports.mergeOfficialPresetModels = mergeOfficialPresetModels;",
)(moduleExports);
const {
  applyCatalogPresetDefaults,
  subscriptionAuthAdapter,
  usesSubscriptionAuth,
  applyPresetReasoningDefaults,
  instantiateCatalogProvider,
  mergeOfficialPresetModels,
} = moduleExports;

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
  onboarding_hint: "providers.catalogProviderSubscriptionHint",
  models: [
    {
      id: "grok-4",
      display_name: "Grok 4",
      enabled: true,
      context_window: 256000,
      max_output_tokens: 65536,
      input_modalities: ["text", "image"],
      supported_reasoning_levels: ["low", "medium", "high", "xhigh", "max"],
      default_reasoning_level: "high",
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
  assert.equal(filled.onboarding_hint, "providers.catalogProviderSubscriptionHint");
  assert.equal(filled.models.length, 1);
  assert.equal(filled.models[0].id, "grok-4");
  assert.equal(filled.enabled, true);
  assert.equal(filled.sort_order, 2);
});

test("complete provider is left unchanged", () => {
  const existing = applyCatalogPresetDefaults(makeProvider(), catalogXai);
  assert.equal(applyCatalogPresetDefaults(existing, catalogXai), existing);
});

test("subscription auth is declared on the preset, not by provider id", () => {
  assert.equal(usesSubscriptionAuth(makeProvider()), false);
  assert.equal(subscriptionAuthAdapter(makeProvider()), null);
  assert.equal(
    usesSubscriptionAuth(makeProvider({ auth_capabilities: ["subscription:xai_oauth"] })),
    true,
  );
  assert.equal(
    subscriptionAuthAdapter(makeProvider({ auth_capabilities: ["subscription:xai_oauth"] })),
    "xai_oauth",
  );
  assert.equal(
    subscriptionAuthAdapter(makeProvider({ auth_capabilities: ["subscription:future_oauth"] })),
    "future_oauth",
  );
});

test("discovered models inherit thinking metadata only from the matching official id", () => {
  const filled = applyPresetReasoningDefaults(
    [
      { id: "grok-4", enabled: true },
      { id: "grok-4.6", enabled: true },
    ],
    catalogXai,
  );
  assert.deepEqual(filled[0].supported_reasoning_levels, ["low", "medium", "high", "xhigh", "max"]);
  assert.equal(filled[0].default_reasoning_level, "high");
  assert.equal(filled[1].supported_reasoning_levels, undefined);
});

test("additive merge inserts missing official models without re-enabling user-disabled rows", () => {
  const merged = mergeOfficialPresetModels(
    [{ id: "grok-4", enabled: false, display_name: "My Grok" }],
    catalogXai.models.concat([{ id: "grok-4.5", enabled: true, display_name: "Grok 4.5" }]),
  );
  assert.equal(merged[0].id, "grok-4");
  assert.equal(merged[0].enabled, false);
  assert.equal(merged[0].display_name, "My Grok");
  assert.deepEqual(merged[0].supported_reasoning_levels, ["low", "medium", "high", "xhigh", "max"]);
  assert.equal(merged[1].id, "grok-4.5");
  assert.equal(merged[1].enabled, true);
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

test("saved xAI rows inherit subscription capabilities from the preset", () => {
  const filled = applyCatalogPresetDefaults(
    makeProvider(),
    makeProvider({
      auth_capabilities: ["subscription:xai_oauth"],
      onboarding_hint: "providers.catalogProviderSubscriptionHint",
      discovery_policy: "retain-intersection",
    }),
    { includeModels: false },
  );
  assert.deepEqual(filled.auth_capabilities, ["subscription:xai_oauth"]);
  assert.equal(filled.onboarding_hint, "providers.catalogProviderSubscriptionHint");
  assert.equal(filled.discovery_policy, "retain-intersection");
});

test("preset backfill preserves an existing automatic protocol selection", () => {
  for (const upstreamFormat of [null, "auto"]) {
    const filled = applyCatalogPresetDefaults(
      makeProvider({ upstream_format: upstreamFormat }),
      catalogXai,
      { includeModels: false },
    );
    assert.equal(filled.upstream_format, upstreamFormat);
  }
});

test("instantiate catalog xAI keeps subscription metadata and drops the env api key", () => {
  const draft = instantiateCatalogProvider(
    makeProvider({
      api_key: "{env:XAI_API_KEY}",
      auth_capabilities: ["subscription:xai_oauth"],
      onboarding_hint: "providers.catalogProviderSubscriptionHint",
      discovery_policy: "retain-intersection",
      models: catalogXai.models,
    }),
    7,
  );
  assert.equal(draft.api_key, null);
  assert.equal(draft.sort_order, 7);
  assert.equal(draft.enabled, true);
  assert.deepEqual(draft.auth_capabilities, ["subscription:xai_oauth"]);
  assert.equal(draft.onboarding_hint, "providers.catalogProviderSubscriptionHint");
  assert.equal(draft.discovery_policy, "retain-intersection");
});

test("merge upgrades text-only official rows to catalog vision without dropping extra models", () => {
  const merged = mergeOfficialPresetModels(
    [
      {
        id: "gpt-5.6-sol",
        display_name: "Command Code gpt-5.6-sol",
        enabled: true,
        input_modalities: ["text"],
        supported_reasoning_levels: ["low", "medium", "high", "xhigh", "max"],
        default_reasoning_level: "medium",
        sort_order: 1,
      },
    ],
    [
      {
        id: "gpt-5.6-sol",
        display_name: "Command Code gpt-5.6-sol",
        enabled: true,
        input_modalities: ["text", "image"],
        supported_reasoning_levels: ["low", "medium", "high", "xhigh", "max"],
        default_reasoning_level: "high",
        sort_order: 1,
      },
      {
        id: "qwen/qwen3.8-max",
        display_name: "Command Code qwen3.8-max",
        enabled: true,
        input_modalities: ["text", "image"],
        supported_reasoning_levels: ["low", "medium", "xhigh"],
        default_reasoning_level: "xhigh",
        sort_order: 2,
      },
    ],
  );
  assert.deepEqual(merged[0].input_modalities, ["text", "image"]);
  assert.equal(merged[0].default_reasoning_level, "medium");
  assert.equal(merged[1].id, "qwen/qwen3.8-max");
});
