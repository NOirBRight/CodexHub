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
    "function displayModel(model) { return (model.display_name && String(model.display_name).trim()) || model.id; }",
    "function normalizeOfficialModelId(value) { value = String(value).trim(); if (value.startsWith('openai/gpt-')) return value.slice('openai/'.length); return value; }",
    stripped,
  ].join("\n"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, strict: false } },
).outputText;

const exported = {};
new Function(
  "exports",
  js +
    "\nexports.displayModelName = displayModelName; exports.enabledPreviewModels = enabledPreviewModels; exports.sortModelsEnabledFirst = sortModelsEnabledFirst;",
)(exported);

const model = (id, overrides = {}) => ({ id, enabled: true, ...overrides });

test("Command Code display names drop the provider prefix", () => {
  assert.equal(
    exported.displayModelName(
      model("xiaomi/mimo-v2.5-pro", { display_name: "Command Code mimo-v2.5-pro" }),
      { id: "commandcode", name: "Command Code", display_prefix: "Command Code" },
    ),
    "mimo-v2.5-pro",
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
  assert.deepEqual(
    exported.sortModelsEnabledFirst([
      model("off-a", { enabled: false }),
      model("on-a"),
      model("off-b", { enabled: false }),
      model("on-b"),
    ]).map((item) => item.id),
    ["on-a", "on-b", "off-a", "off-b"],
  );
});
