import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import test from "node:test";
import ts from "typescript";

// ADR-0010: Rust owns the command interface. The manifest is exported by a
// targeted Rust test into an OS temp directory; no generated fixture is kept
// in the repository and no source regex is used for this contract.

const execFileAsync = promisify(execFile);
const commandsPath = new URL("../src/lib/commands.ts", import.meta.url);
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

async function loadManifest() {
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), "codexhub-command-manifest-"));
  const outputPath = path.join(tempRoot, "manifest.json");
  const cargo = process.platform === "win32" ? "cargo.exe" : "cargo";
  try {
    await execFileAsync(
      cargo,
      [
        "test",
        "--manifest-path",
        path.join(repoRoot, "src-tauri", "Cargo.toml"),
        "desktop_commands::registry_manifest_tests::emit_manifest_json_fixture",
        "--",
        "--exact",
        "--nocapture",
      ],
      {
        cwd: repoRoot,
        env: { ...process.env, CODEXHUB_COMMAND_MANIFEST_OUT: outputPath },
        maxBuffer: 4 * 1024 * 1024,
      },
    );
    return JSON.parse(await readFile(outputPath, "utf8"));
  } finally {
    await rm(tempRoot, { recursive: true, force: true });
  }
}

async function loadTsCommandNames() {
  const source = await readFile(commandsPath, "utf8");
  const js = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  new Function("exports", "require", js)(module.exports, () => ({}));
  return Object.values(module.exports.COMMANDS ?? {}).map(String);
}

test("frontend command names are exactly the Rust manifest frontend-exposed set", async () => {
  const [manifest, tsNames] = await Promise.all([loadManifest(), loadTsCommandNames()]);
  const manifestFrontend = manifest
    .filter((entry) => entry.frontend_exposed)
    .map((entry) => entry.name)
    .sort();
  assert.deepEqual([...tsNames].sort(), manifestFrontend);
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

test("dsh_client_info asymmetry and aliases are explicit in the manifest", async () => {
  const manifest = await loadManifest();
  const entry = manifest.find((item) => item.name === "dsh_client_info");
  assert.ok(entry, "dsh_client_info present in manifest");
  assert.equal(entry.frontend_exposed, false);
  assert.equal(entry.bridge_exposed, true);
  const switchMode = manifest.find((item) => item.name === "switch_mode");
  assert.deepEqual(switchMode.argument_aliases, [
    ["autoSync", "auto_sync"],
    ["forceTakeover", "force_takeover"],
    ["restartCodex", "restart_codex"],
  ]);
});
