import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const modelPath = new URL("../src/lib/providerModel.ts", import.meta.url);
const sectionPath = new URL("../src/components/providers/ProviderModelSection.tsx", import.meta.url);
const editorPath = new URL("../src/components/providers/ProviderEditor.tsx", import.meta.url);

const modelSource = await readFile(modelPath, "utf8");
const sectionSource = await readFile(sectionPath, "utf8");
const editorSource = await readFile(editorPath, "utf8");
const javascript = ts.transpileModule(
  modelSource
    .replace(/^import type .*?;\r?\n/m, "")
    .replace(/export function/g, "function"),
  {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      strict: false,
    },
  },
).outputText;
const moduleExports = {};
const wrappedModule = new Function(
  "exports",
  javascript + "\nexports.providerQualifiedModelId = providerQualifiedModelId;",
);
wrappedModule(moduleExports);

test("provider model copy value is provider-qualified at runtime", () => {
  assert.equal(
    moduleExports.providerQualifiedModelId("kimi", "kimi-for-coding-highspeed"),
    "kimi/kimi-for-coding-highspeed",
  );
  assert.equal(
    moduleExports.providerQualifiedModelId(" kimi ", " kimi/kimi-for-coding-highspeed "),
    "kimi/kimi-for-coding-highspeed",
  );
  assert.equal(moduleExports.providerQualifiedModelId("", "model"), "model");
  assert.equal(moduleExports.providerQualifiedModelId("kimi", ""), "");
  assert.equal(moduleExports.providerQualifiedModelId("", ""), "");
});

test("model row copies the computed qualified value for saved and new providers", () => {
  assert.match(sectionSource, /import \{ normalizeModel, providerQualifiedModelId \} from "\.\.\/\.\.\/lib\/providerModel"/);
  assert.match(sectionSource, /providerQualifiedModelId\(providerId, model\.id\)/);
  assert.match(sectionSource, /navigator\.clipboard\.writeText\(copyValue\)/);
  assert.match(editorSource, /providerId=\{draft\.id\}/);
  assert.match(
    editorSource,
    /providerId=\{form\.id\.trim\(\) \|\| slugify\(form\.name\)\}/,
  );
});
