import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const catalogPath = new URL("../src/lib/providerCatalog.ts", import.meta.url);
const source = await readFile(catalogPath, "utf8");
const jsOutput = ts.transpileModule(
  source.replace(/^\s*import[\s\S]*?;\s*$/gm, ""),
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
  "function sortModelsEnabledFirst(models) { return [...models].sort((left, right) => Number(left.enabled === false) - Number(right.enabled === false)); }\n" +
    jsOutput +
    "\nexports.applyCatalogPresetDefaults = applyCatalogPresetDefaults; exports.subscriptionAuthAdapter = subscriptionAuthAdapter; exports.usesSubscriptionAuth = usesSubscriptionAuth; exports.applyPresetReasoningDefaults = applyPresetReasoningDefaults; exports.instantiateCatalogProvider = instantiateCatalogProvider; exports.mergeOfficialPresetModels = mergeOfficialPresetModels; exports.editorReasoningLevelOptions = editorReasoningLevelOptions; exports.modelsMissingFromPreset = modelsMissingFromPreset;",
)(moduleExports);
const {
  applyCatalogPresetDefaults,
  subscriptionAuthAdapter,
  usesSubscriptionAuth,
  applyPresetReasoningDefaults,
  instantiateCatalogProvider,
  mergeOfficialPresetModels,
  editorReasoningLevelOptions,
  modelsMissingFromPreset,
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

test("missing catalog windows fill from the preset without replacing a live listing window", () => {
  const catalog = makeProvider({
    models: [
      {
        id: "muse-spark-1.2-contributor",
        context_window: 1_048_576,
        max_output_tokens: 131_072,
        input_modalities: ["text", "image"],
        supported_reasoning_levels: ["low", "medium", "high", "xhigh"],
        default_reasoning_level: "xhigh",
      },
    ],
  });
  const filled = applyPresetReasoningDefaults(
    [
      { id: "muse-spark-1.2-contributor", enabled: true, context_window: null },
      { id: "muse-spark-1.2-contributor", enabled: true, context_window: 202_752, max_output_tokens: 32_768 },
      { id: "omen-alpha", enabled: true, context_window: null },
    ],
    catalog,
  );
  assert.equal(filled[0].context_window, 1_048_576);
  assert.equal(filled[0].max_output_tokens, 131_072);
  assert.equal(filled[1].context_window, 202_752);
  assert.equal(filled[1].max_output_tokens, 32_768);
  assert.equal(filled[2].context_window, null);
});

test("omen-alpha missing a window is filled from the OpenCode Go catalog", () => {
  const catalog = makeProvider({
    id: "opencode-go",
    models: [
      {
        id: "omen-alpha",
        context_window: 500_000,
        max_output_tokens: 128_000,
        input_modalities: ["text", "image"],
        supported_reasoning_levels: ["low", "high"],
        default_reasoning_level: "high",
      },
    ],
  });
  const filled = applyPresetReasoningDefaults(
    [{ id: "omen-alpha", enabled: true, context_window: null }],
    catalog,
  );
  assert.equal(filled[0].context_window, 500_000);
  assert.equal(filled[0].max_output_tokens, 128_000);
});

test("saved listing rows are missing from a stale catalog snapshot", () => {
  const catalog = makeProvider({
    id: "opencode-go",
    models: [{ id: "muse-spark-1.2-contributor", context_window: 1_048_576 }],
  });
  assert.equal(
    modelsMissingFromPreset([{ id: "omen-alpha" }, { id: "muse-spark-1.2-contributor" }], catalog),
    true,
  );
  assert.equal(
    modelsMissingFromPreset(
      [{ id: "omen-alpha" }],
      makeProvider({ id: "opencode-go", models: [{ id: "omen-alpha", context_window: 500_000 }] }),
    ),
    false,
  );
  assert.equal(modelsMissingFromPreset([{ id: "omen-alpha" }], null), false);
});

test("saved rows missing a window are backfilled from the catalog without adding extra ids", () => {
  const filled = applyCatalogPresetDefaults(
    makeProvider({
      models: [
        {
          id: "grok-4",
          enabled: true,
          input_modalities: ["text", "image"],
          supported_reasoning_levels: ["low", "medium", "high", "xhigh", "max"],
          default_reasoning_level: "high",
        },
      ],
    }),
    catalogXai,
    { includeModels: false },
  );
  assert.equal(filled.models.length, 1);
  assert.equal(filled.models[0].id, "grok-4");
  assert.equal(filled.models[0].context_window, 256000);
  assert.equal(filled.models[0].max_output_tokens, 65536);
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

test("saved grok-4.6 five-level fill is replaced by catalog without seeding extra ids", () => {
  const grok46 = {
    id: "grok-4.6",
    display_name: "Grok 4.6",
    enabled: true,
    input_modalities: ["text", "image"],
    supported_reasoning_levels: ["low", "medium", "high", "xhigh"],
    default_reasoning_level: "high",
    thinking_mode: "always_on",
  };
  const filled = applyCatalogPresetDefaults(
    makeProvider({
      models: [
        {
          id: "grok-4.6",
          enabled: true,
          context_window: 500000,
          input_modalities: ["text"],
          supported_reasoning_levels: ["low", "medium", "high", "xhigh", "max"],
          default_reasoning_level: "medium",
        },
      ],
    }),
    makeProvider({
      ...catalogXai,
      models: [grok46],
    }),
    { includeModels: false },
  );
  assert.equal(filled.models.length, 1);
  assert.equal(filled.models[0].id, "grok-4.6");
  assert.deepEqual(filled.models[0].supported_reasoning_levels, ["low", "medium", "high", "xhigh"]);
  assert.equal(filled.models[0].default_reasoning_level, "high");
  assert.deepEqual(filled.models[0].input_modalities, ["text", "image"]);
});

test("empty discovered grok-4.6 inherits catalog levels instead of Codex max", () => {
  const grok46 = {
    id: "grok-4.6",
    display_name: "Grok 4.6",
    enabled: true,
    input_modalities: ["text", "image"],
    supported_reasoning_levels: ["low", "medium", "high", "xhigh"],
    default_reasoning_level: "high",
    thinking_mode: "always_on",
  };
  const filled = applyPresetReasoningDefaults(
    [
      {
        id: "grok-4.6",
        enabled: true,
        context_window: 500000,
        supported_reasoning_levels: ["low", "medium", "high", "xhigh", "max"],
        default_reasoning_level: "medium",
      },
    ],
    makeProvider({ models: [grok46] }),
  );
  assert.deepEqual(filled[0].supported_reasoning_levels, ["low", "medium", "high", "xhigh"]);
  assert.equal(filled[0].default_reasoning_level, "high");
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

test("editor reasoning checkboxes follow catalog levels when present", () => {
  assert.deepEqual(editorReasoningLevelOptions(["low", "medium", "high", "xhigh"]), [
    "low",
    "medium",
    "high",
    "xhigh",
  ]);
  assert.deepEqual(editorReasoningLevelOptions([]), ["low", "medium", "high", "xhigh", "max"]);
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
