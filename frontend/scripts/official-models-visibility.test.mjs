import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(new URL("../src/lib/officialModels.ts", import.meta.url), "utf8");
const withoutImports = source
  .replace(/^\s*import[^;]+;\s*$/gm, "")
  .replace(/export const /g, "const ")
  .replace(/export function/g, "function");
const jsOutput = ts.transpileModule(withoutImports, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
    strict: false,
  },
}).outputText;

const moduleExports = {};
const wrappedModule = new Function(
  "exports",
  "normalizeOfficialModelId",
  jsOutput +
    "\nexports.mergeOfficialModelSources = mergeOfficialModelSources; exports.filterCodexVisibleOfficialModels = filterCodexVisibleOfficialModels;",
);
wrappedModule(moduleExports, (value) => value.trim().replace(/^openai\//, ""));

const { mergeOfficialModelSources } = moduleExports;

function model(id, overrides = {}) {
  return { id, enabled: true, ...overrides };
}

test("combined Official catalogs exclude hidden, unknown, and internal duplicate Terra models", () => {
  const combined = mergeOfficialModelSources(
    [
      model("gpt-5.6-terra", { visibility: "list" }),
      model("gpt-5.6-sol", { visibility: "hide" }),
      model("gpt-5.6-terra", {
        id: "codex-auto-review",
        upstream_model: "gpt-5.6-terra",
        visibility: "list",
      }),
      model("gpt-5.6-luna", { visibility: "future" }),
    ],
    [model("openai/gpt-5.6-terra", { visibility: "list" })],
  );

  assert.deepEqual(combined.map((item) => item.id), ["gpt-5.6-terra"]);
});

test("catalog hide vetoes a metadata-only Official model", () => {
  const combined = mergeOfficialModelSources(
    [model("gpt-5.6-terra", { visibility: "hide" })],
    [model("gpt-5.6-terra", { visibility: "list" })],
  );

  assert.deepEqual(combined, []);
});
