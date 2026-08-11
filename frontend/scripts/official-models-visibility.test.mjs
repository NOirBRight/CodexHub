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
    "\nexports.mergeOfficialModelSources = mergeOfficialModelSources; exports.filterCodexVisibleOfficialModels = filterCodexVisibleOfficialModels; exports.officialCollaborationVersionOptions = officialCollaborationVersionOptions;",
);
wrappedModule(moduleExports, (value) => value.trim().replace(/^openai\//, ""));

const { mergeOfficialModelSources, officialCollaborationVersionOptions } = moduleExports;

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

test("metadata cannot create an Official model absent from the catalog", () => {
  const combined = mergeOfficialModelSources(
    [],
    [model("gpt-5.6-terra", { visibility: "list" })],
  );

  assert.deepEqual(combined, []);
});

test("Luna collaboration options expose the catalog baseline and effective selection separately", () => {
  const luna = model("gpt-5.6-luna", {
    upstream_model: "gpt-5.6-luna",
    source_kind: "official",
    visibility: "list",
    multi_agent_version: "v1",
  });

  assert.deepEqual(
    officialCollaborationVersionOptions(luna),
    {
      baseline: "v1",
      effective: "v1",
      explicit: null,
      candidate: "7006542a773fc20c10e4bbcadd593393a259ceb2",
    },
  );
  assert.equal(
    officialCollaborationVersionOptions({ ...luna, multi_agent_version: "v2" }).baseline,
    "v2",
  );
  assert.deepEqual(
    officialCollaborationVersionOptions(luna, { "gpt-5.6-luna": "v2" }),
    {
      baseline: "v1",
      effective: "v2",
      explicit: "v2",
      candidate: "7006542a773fc20c10e4bbcadd593393a259ceb2",
    },
  );
  assert.equal(
    officialCollaborationVersionOptions(
      { ...luna, multi_agent_version: "v1" },
      { "gpt-5.6-luna": "v1" },
      { "gpt-5.6-luna": "v2" },
    ).baseline,
    "v2",
  );
});
