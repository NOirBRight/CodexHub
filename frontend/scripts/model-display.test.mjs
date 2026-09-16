import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const displaySource = await readFile(
  new URL("../src/lib/modelDisplay.ts", import.meta.url),
  "utf8",
);
const stripped = displaySource
  .replace(/^\s*import[\s\S]*?;\s*$/gm, "")
  .replace(/export type ModelLabelProvider = \{[\s\S]*?\};/, "")
  .replace(/export function/g, "function");
const js = ts.transpileModule(
  [
    "function shortWireDisplayName(stored, modelId) { const id = (modelId && String(modelId).trim()) || ''; const leaf = id.split('/').filter(Boolean).pop() || id; const name = (stored && String(stored).trim()) || ''; if (!name || name === id || name === leaf) return leaf; return name; }",
    "function displayModel(model) { return shortWireDisplayName(model.display_name, model.id); }",
    "function normalizeOfficialModelId(value) { value = String(value).trim(); if (value.startsWith('openai/gpt-')) return value.slice('openai/'.length); return value; }",
    stripped,
  ].join("\n"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, strict: false } },
).outputText;

const exported = {};
new Function(
  "exports",
  js +
    "\nexports.displayModelName = displayModelName; exports.enabledPreviewModels = enabledPreviewModels; exports.sortModelsEnabledFirst = sortModelsEnabledFirst; exports.partitionDisplayedModels = partitionDisplayedModels; exports.stitchDisplayedModelReorder = stitchDisplayedModelReorder;",
)(exported);

const model = (id, overrides = {}) => ({ id, enabled: true, ...overrides });
const ids = (models) => models.map((item) => item.id);
const mixed = [
  model("off-a", { enabled: false }),
  model("on-a"),
  model("off-b", { enabled: false }),
  model("on-b"),
];

test("Command Code display names drop the provider prefix", () => {
  assert.equal(
    exported.displayModelName(
      model("xiaomi/mimo-v2.5-pro", { display_name: "Command Code mimo-v2.5-pro" }),
      { id: "commandcode", name: "Command Code", display_prefix: "Command Code" },
    ),
    "mimo-v2.5-pro",
  );
});

test("namespaced wire ids fall back to the last path segment", () => {
  assert.equal(
    exported.displayModelName(model("deepseek/deepseek-v4.1-flash"), {
      id: "commandcode",
      name: "Command Code",
      display_prefix: "Command Code",
    }),
    "deepseek-v4.1-flash",
  );
  assert.equal(
    exported.displayModelName(
      model("deepseek/deepseek-v4.1-flash", {
        display_name: "deepseek/deepseek-v4.1-flash",
      }),
      { id: "commandcode", name: "Command Code", display_prefix: "Command Code" },
    ),
    "deepseek-v4.1-flash",
  );
});

test("CC and OC prefixes are stripped on the provider page", () => {
  assert.equal(
    exported.displayModelName(
      model("deepseek/deepseek-v4-flash", { display_name: "CC DeepSeek V4 Flash" }),
      { id: "commandcode", name: "Command Code", display_prefix: "CC" },
    ),
    "DeepSeek V4 Flash",
  );
  assert.equal(
    exported.displayModelName(
      model("glm-5.3-flash", { display_name: "OC GLM-5.3 Flash" }),
      { id: "opencode-go", name: "OpenCode Go", display_prefix: "OC" },
    ),
    "GLM-5.3 Flash",
  );
});

test("generic provider names are not stripped from model labels", () => {
  assert.equal(
    exported.displayModelName(
      model("kimi/k3", { display_name: "Kimi K3" }),
      { id: "kimi", name: "Kimi", display_prefix: null },
    ),
    "Kimi K3",
  );
});

test("provider chips only include enabled models", () => {
  assert.deepEqual(
    exported.enabledPreviewModels([
      model("claude-sonnet-5", { enabled: false }),
      model("gpt-5.6-sol"),
      model("grok-4.6", { enabled: false }),
    ]).map((item) => item.id),
    ["gpt-5.6-sol"],
  );
});

test("official chips hide models in the disabled list", () => {
  assert.deepEqual(
    exported.enabledPreviewModels(
      [model("gpt-6-astra"), model("gpt-5.6-sol")],
      ["gpt-5.6-sol"],
    ).map((item) => item.id),
    ["gpt-6-astra"],
  );
});

test("model lists keep enabled rows first without changing relative order", () => {
  assert.deepEqual(ids(exported.sortModelsEnabledFirst(mixed)), ["on-a", "on-b", "off-a", "off-b"]);
});

test("partition keeps relative order inside enabled and disabled groups", () => {
  const { enabled, disabled } = exported.partitionDisplayedModels(mixed);
  assert.deepEqual(ids(enabled), ["on-a", "on-b"]);
  assert.deepEqual(ids(disabled), ["off-a", "off-b"]);
});

test("official disabled ids partition the same way as enabled false", () => {
  const { enabled, disabled } = exported.partitionDisplayedModels(
    [model("off-a"), model("on-a"), model("off-b"), model("on-b")],
    ["off-a", "off-b"],
  );
  assert.deepEqual(ids(enabled), ["on-a", "on-b"]);
  assert.deepEqual(ids(disabled), ["off-a", "off-b"]);
});

test("reordering enabled rows stitches them back into the other group's slots", () => {
  const { enabled } = exported.partitionDisplayedModels(mixed);
  assert.deepEqual(
    ids(exported.stitchDisplayedModelReorder(mixed, [enabled[1], enabled[0]])),
    ["off-a", "on-b", "off-b", "on-a"],
  );
});

test("reordering disabled rows stitches them back into the other group's slots", () => {
  const { disabled } = exported.partitionDisplayedModels(mixed);
  assert.deepEqual(
    ids(exported.stitchDisplayedModelReorder(mixed, [disabled[1], disabled[0]])),
    ["off-b", "on-a", "off-a", "on-b"],
  );
});

test("toggling enabled only moves the row between groups and leaves persisted order", () => {
  const toggledOff = mixed.map((item) =>
    item.id === "on-a" ? { ...item, enabled: false } : item,
  );
  assert.deepEqual(ids(toggledOff), ["off-a", "on-a", "off-b", "on-b"]);
  const hidden = exported.partitionDisplayedModels(toggledOff);
  assert.deepEqual(ids(hidden.enabled), ["on-b"]);
  assert.deepEqual(ids(hidden.disabled), ["off-a", "on-a", "off-b"]);

  const toggledBack = toggledOff.map((item) =>
    item.id === "on-a" ? { ...item, enabled: true } : item,
  );
  assert.deepEqual(ids(toggledBack), ["off-a", "on-a", "off-b", "on-b"]);
  const restored = exported.partitionDisplayedModels(toggledBack);
  assert.deepEqual(ids(restored.enabled), ["on-a", "on-b"]);
  assert.deepEqual(ids(restored.disabled), ["off-a", "off-b"]);
});

test("hidden from picker heading comes from paired i18n keys", async () => {
  const [section, en, zh, sortable] = await Promise.all([
    readFile(new URL("../src/components/providers/ProviderModelSection.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/i18n/locales/en-US.ts", import.meta.url), "utf8"),
    readFile(new URL("../src/i18n/locales/zh-CN.ts", import.meta.url), "utf8"),
    readFile(new URL("../src/components/SortableList.tsx", import.meta.url), "utf8"),
  ]);
  assert.match(en, /hiddenFromPicker:\s*"Hidden from picker"/);
  assert.match(zh, /hiddenFromPicker:\s*"未在选择器中显示"/);
  assert.match(section, /t\("providers\.hiddenFromPicker"\)/);
  assert.doesNotMatch(section, /Hidden from picker/);
  assert.doesNotMatch(section, /未在选择器中显示/);
  assert.match(section, /className="space-y-1"/);
  assert.match(section, /grid-cols-\[minmax\(0,1fr\)_minmax\(0,1fr\)\]/);
  assert.match(sortable, /cx\("px-px py-px"/);
  assert.doesNotMatch(sortable, /cx\("space-y-3 px-px py-px"/);
});
