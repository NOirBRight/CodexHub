import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import ts from "typescript";
import { readFile } from "node:fs/promises";
import { markRestartReminder, readRestartReminder, storeRestartReminder } from "../src/lib/providerWorkspace/restart.ts";

function loadDefaultSubagent() {
  const strip = (text) =>
    text
      .replace(/^\s*import[\s\S]*?;\s*$/gm, "")
      .replace(/export type [\s\S]*?\};\n/g, "")
      .replace(/^export /gm, "");
  const wire = strip(
    fs.readFileSync(new URL("../src/lib/wireDisplayName.ts", import.meta.url), "utf8"),
  );
  const source = strip(
    fs.readFileSync(new URL("../src/lib/defaultSubagent.ts", import.meta.url), "utf8"),
  );
  const js = ts.transpileModule(`${wire}\n${source}`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
  }).outputText;
  const exported = {};
  new Function(
    "exports",
    `${js}
    exports.subagentCatalogSlug = subagentCatalogSlug;
    exports.listDefaultSubagentOptions = listDefaultSubagentOptions;
    exports.resolveSubagentEffort = resolveSubagentEffort;
    exports.formatSubagentEffort = formatSubagentEffort;
    exports.defaultSubagentSummary = defaultSubagentSummary;`,
  )(exported);
  return exported;
}

const {
  defaultSubagentSummary,
  formatSubagentEffort,
  listDefaultSubagentOptions,
  resolveSubagentEffort,
  subagentCatalogSlug,
} = loadDefaultSubagent();

const official = (id, overrides = {}) => ({
  id,
  enabled: true,
  display_name: overrides.display_name ?? id,
  ...overrides,
});

test("official subagent slugs drop the openai/ prefix", () => {
  assert.equal(subagentCatalogSlug("__official__", "openai/gpt-5.6-luna", "__official__"), "gpt-5.6-luna");
  assert.equal(subagentCatalogSlug("__official__", "gpt-5.6-luna", "__official__"), "gpt-5.6-luna");
});

test("third-party subagent slugs stay provider-qualified", () => {
  assert.equal(subagentCatalogSlug("xai", "grok-4.6", "__official__"), "xai/grok-4.6");
  assert.equal(subagentCatalogSlug("xai", "xai/grok-4.6", "__official__"), "xai/grok-4.6");
  assert.equal(
    subagentCatalogSlug("opencode-go", "deepseek-v4.1-flash", "__official__"),
    "opencode-go/deepseek-v4.1-flash",
  );
  assert.equal(
    subagentCatalogSlug("opencode-go", "opencode-go/deepseek-v4.1-flash", "__official__"),
    "opencode-go/deepseek-v4.1-flash",
  );
});

test("default subagent options include enabled official and provider models", () => {
  const options = listDefaultSubagentOptions({
    officialId: "__official__",
    officialIncluded: true,
    officialModels: [
      official("gpt-5.6-luna", {
        display_name: "GPT-5.6 Luna",
        supported_reasoning_levels: ["low", "medium", "high", "xhigh", "max"],
        default_reasoning_level: "medium",
      }),
      official("gpt-5.4-mini"),
    ],
    officialDisabledModels: ["gpt-5.4-mini"],
    providers: [
      {
        id: "xai",
        name: "xAI",
        enabled: true,
        base_url: "",
        api_key: null,
        models: [
          {
            id: "grok-4.6",
            enabled: true,
            display_name: "Grok 4.6",
            supported_reasoning_levels: ["low", "high", "xhigh"],
            default_reasoning_level: "high",
          },
          { id: "hidden", enabled: false },
        ],
      },
    ],
  });
  assert.deepEqual(
    options.map((option) => option.id),
    ["gpt-5.6-luna", "xai/grok-4.6"],
  );
  assert.equal(options[0].label, "GPT-5.6 Luna");
  assert.equal(options[1].label, "xAI · Grok 4.6");
  assert.equal(resolveSubagentEffort(options[0], "max"), "max");
  assert.equal(resolveSubagentEffort(options[1], "max"), "high");
});

test("OpenCode Go flash stays a provider-qualified subagent option", () => {
  const options = listDefaultSubagentOptions({
    officialId: "__official__",
    officialIncluded: false,
    officialModels: [],
    officialDisabledModels: [],
    providers: [
      {
        id: "opencode-go",
        name: "OpenCode Go",
        enabled: true,
        base_url: "",
        api_key: null,
        models: [
          {
            id: "deepseek-v4.1-flash",
            enabled: true,
            display_name: "deepseek-v4.1-flash",
            supported_reasoning_levels: ["low", "high", "max"],
            default_reasoning_level: "max",
          },
        ],
      },
    ],
  });
  assert.deepEqual(
    options.map((option) => option.id),
    ["opencode-go/deepseek-v4.1-flash"],
  );
  assert.equal(options[0].label, "OpenCode Go · deepseek-v4.1-flash");
  assert.equal(resolveSubagentEffort(options[0], ""), "max");
  assert.equal(
    defaultSubagentSummary(options[0].label, "max", "Codex default"),
    "OpenCode Go · deepseek-v4.1-flash · Max",
  );
});

test("namespaced third-party ids do not keep a vendor path in the subagent label", () => {
  const options = listDefaultSubagentOptions({
    officialId: "__official__",
    officialIncluded: false,
    officialModels: [],
    officialDisabledModels: [],
    providers: [
      {
        id: "commandcode",
        name: "Command Code",
        enabled: true,
        base_url: "",
        api_key: null,
        models: [
          { id: "deepseek/deepseek-v4.1-flash", enabled: true },
        ],
      },
    ],
  });
  assert.equal(options[0].id, "commandcode/deepseek/deepseek-v4.1-flash");
  assert.equal(options[0].label, "Command Code · deepseek-v4.1-flash");
});

test("subagent summaries keep model and effort on one line", () => {
  assert.equal(formatSubagentEffort("max"), "Max");
  assert.equal(formatSubagentEffort("xhigh"), "xHigh");
  assert.equal(
    defaultSubagentSummary("5.6 Luna", "max", "Codex default"),
    "5.6 Luna · Max",
  );
  assert.equal(defaultSubagentSummary("", "max", "Codex default"), "Codex default");
});

test("overlay subagent writes mark a restart reminder without watching Desktop", () => {
  const map = new Map();
  globalThis.localStorage = {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, v),
    removeItem: (k) => map.delete(k),
  };
  globalThis.window = { dispatchEvent() {} };
  try {
    storeRestartReminder(false);
    markRestartReminder();
    assert.equal(readRestartReminder(), true);
  } finally {
    delete globalThis.localStorage;
    delete globalThis.window;
  }
});

test("saving the default subagent marks a restart reminder after overlay write", async () => {
  const source = await readFile(new URL("../src/pages/ProvidersPage.tsx", import.meta.url), "utf8");
  const start = source.indexOf("async function persistDefaultSubagent");
  const fn = source.slice(start, source.indexOf("async function applyCodexHubConnection", start));
  assert.match(fn, /markRestartReminder\(\)/);
  assert.match(fn, /stageSettings\(next\)/);
  assert.match(fn, /workspace\.defaultSubagentSaved/);
  assert.doesNotMatch(fn, /getCodexDesktopStatus/);
  assert.doesNotMatch(fn, /pendingRestartAfterOverlayWrite/);
  assert.doesNotMatch(fn, /defaultSubagentSavedRestartCodex/);
});

test("subagent picker keeps the menu open after model or effort changes", async () => {
  const source = await readFile(
    new URL("../src/components/workspace/ProviderWorkspaceView.tsx", import.meta.url),
    "utf8",
  );
  const start = source.indexOf("function DefaultSubagentPicker");
  const fn = source.slice(start);
  assert.match(fn, /function chooseEffort\(nextEffort: string\) \{\n    setDraftEffort\(nextEffort\);\n    setPanel\("menu"\);\n    commitIfChanged\(draftModel, nextEffort\);/);
  assert.doesNotMatch(fn, /dismiss\(/);
  assert.doesNotMatch(fn, /onBlur=\{/);
  assert.doesNotMatch(fn, /open \? draftModel : model/);
});
