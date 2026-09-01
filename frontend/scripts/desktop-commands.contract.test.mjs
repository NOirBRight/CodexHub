import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

// ADR-0010: Rust owns the command interface. The Rust manifest fixture
// (written by cargo test desktop_commands::manifest::emit_manifest_json_fixture)
// is the single source of truth; this test checks the TypeScript client
// contract against it (structured JSON, not source regex).

const manifestPath = new URL("./desktop-commands.manifest.json", import.meta.url);
const commandsPath = new URL("../src/lib/commands.ts", import.meta.url);

async function loadManifest() {
  const raw = await readFile(manifestPath, "utf8");
  return JSON.parse(raw);
}

async function loadTsCommandNames() {
  const source = await readFile(commandsPath, "utf8");
  const js = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  // Provide a bare require stub (commands.ts has no runtime imports).
  const mockRequire = () => ({});
  new Function("exports", "require", js)(module.exports, mockRequire);
  const commands = module.exports.COMMANDS ?? {};
  return Object.values(commands).map(String);
}

test("frontend command names are exactly the Rust manifest frontend-exposed set", async () => {
  const manifest = await loadManifest();
  const tsNames = await loadTsCommandNames();
  const manifestFrontend = manifest
    .filter((entry) => entry.frontend_exposed)
    .map((entry) => entry.name)
    .sort();
  const sortedTs = [...tsNames].sort();
  assert.deepEqual(sortedTs, manifestFrontend);
});

test("desktop-only commands exist in the manifest and are not bridge-exposed", async () => {
  const manifest = await loadManifest();
  const desktopOnly = manifest.filter((entry) => entry.desktop_only);
  assert.ok(desktopOnly.length >= 3, "window_* family present");
  for (const entry of desktopOnly) {
    assert.match(entry.name, /^window_/);
    assert.equal(entry.bridge_exposed, false);
  }
});

test("dsh_client_info asymmetry is explicit in the manifest", async () => {
  const manifest = await loadManifest();
  const entry = manifest.find((item) => item.name === "dsh_client_info");
  assert.ok(entry, "dsh_client_info present in manifest");
  assert.equal(entry.frontend_exposed, false, "not a frontend API");
  assert.equal(entry.bridge_exposed, true, "bridge-routable");
});
