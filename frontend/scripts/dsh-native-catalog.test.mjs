import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const source = readFileSync(new URL("../../src-tauri/src/gateway/clients/dsh_native_models.mjs", import.meta.url), "utf8");
function catalog(modern, request) {
  const root = mkdtempSync(join(tmpdir(), "codexhub-dsh-catalog-"));
  const executable = join(root, "lib/bin.js");
  const put = (path, content) => { mkdirSync(join(path, ".."), { recursive: true }); writeFileSync(path, content); };
  const module = (name, content, extra = {}) => {
    const dir = join(root, "node_modules", name);
    put(join(dir, "package.json"), JSON.stringify({ name, type: "module", exports: { ".": "./index.js", ...extra } }));
    put(join(dir, "index.js"), content);
    return dir;
  };
  put(join(root, "package.json"), JSON.stringify({ name: "@deepseek-ai/dsh" }));
  put(executable, "");
  module("@deepseek-ai/dsh-tool-subagent", `export const Config = ${JSON.stringify({ uid: 1, refs: { 1: { dict: { agentOptions: 2 } }, 2: { dict: modern ? { reasoningEffort: 3 } : { model: 3 } } } })};`);
  module("@deepseek-ai/dsh-llm-pi-ai", "export const Config = value => ({providers: {get: () => Object.fromEntries(Object.entries(value.providers).map(([id, profile]) => [id, {models: [], ...profile}]))}});");
  const pi = module("@earendil-works/pi-ai", "export const getSupportedThinkingLevels = model => model.levels;", { "./providers/all": "./all.js" });
  put(join(pi, "all.js"), 'export const getBuiltinModels = provider => provider === "native" ? [{id:"child", name:"Child", reasoning:true, levels:["low","high"]}] : [];');
  module("@deepseek-ai/dsh-llm-deepseek", modern
    ? 'export const resolveAdapterOptions = value => ({models:[{id:"deep"}], ...value}); export class DeepSeekAdapter { constructor(value){this.value=value} resolveModel(){return {reasoning:{efforts:this.value.options().thinking === "disabled" ? [{id:"off"}] : [{id:"low"},{id:"max"}]}}} }'
    : 'export const Config = () => ({models:[{id:"deep"}]});');
  try {
    const result = spawnSync(process.execPath, ["--conditions=import", "--input-type=module", "-e", source, executable, JSON.stringify(request)], { encoding: "utf8" });
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(result.stdout);
  } finally { rmSync(root, { recursive: true, force: true }); }
}

test("DSH native catalog exposes declared model levels, overrides and adapter restrictions", () => {
  const result = catalog(true, { providers: {
    native: { modelOverrides: { child: { name: "Renamed", reasoningEfforts: false } } },
    custom: { models: [{ id: "child", reasoningEfforts: { off: null, high: "wire-high" } }, { id: "plain" }] },
  }, deepseek: { thinking: "disabled" } });
  assert.deepEqual(result.find(model => model.id === "native/child").efforts, []);
  assert.equal(result.find(model => model.id === "native/child").label, "native / Renamed");
  assert.deepEqual(result.find(model => model.id === "custom/child").efforts, ["", "off", "high"]);
  assert.deepEqual(result.find(model => model.id === "custom/plain").efforts, []);
  assert.deepEqual(result.find(model => model.id === "deepseek/deep").efforts, ["", "off"]);
  assert.ok(result.every(model => model.defaultEffort === ""));
});

test("DSH legacy child schema remains model-only even with reasoning catalog data", () => {
  const result = catalog(false, { providers: { native: {} }, deepseek: {} });
  assert.deepEqual(result.map(model => model.id), ["native/child", "deepseek/deep"]);
  assert.ok(result.every(model => model.efforts.length === 0));
});

test("DSH normalized empty model lists retain builtin reasoning metadata", () => {
  for (const profile of [{}, { models: [] }]) {
    const result = catalog(true, { providers: { native: profile }, deepseek: { models: [] } });
    assert.deepEqual(result.find(model => model.id === "native/child").efforts, ["", "low", "high"]);
  }
});
