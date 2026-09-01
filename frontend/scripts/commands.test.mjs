import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { COMMANDS } from "../src/lib/commands.ts";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

test("frontend invoke uses COMMANDS constants instead of raw IPC strings", () => {
  const tauri = fs.readFileSync(path.join(root, "frontend/src/lib/tauri.ts"), "utf8");
  assert.match(tauri, /from \"\.\/commands\"/);
  assert.doesNotMatch(tauri, /(?:call|desktopCall)<[^>]*>\(\s*\"[a-z0-9_]+\"/);
});

test("COMMANDS values are the registered IPC names", () => {
  const names = Object.values(COMMANDS);
  assert.equal(new Set(names).size, names.length);
  const main = fs.readFileSync(path.join(root, "src-tauri/src/main.rs"), "utf8");
  const updates = fs.readFileSync(path.join(root, "src-tauri/src/app_updates.rs"), "utf8");
  const inventory = fs.readFileSync(
    path.join(root, "src-tauri/src/desktop_commands/mod.rs"),
    "utf8",
  );
  const registered = main + updates + inventory;
  for (const name of names) {
    assert.match(
      registered,
      new RegExp(`\\b${name}\\b`),
      `missing IPC registration for ${name}`,
    );
  }
});
