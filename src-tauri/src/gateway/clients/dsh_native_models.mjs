// Resolve metadata from the detected DSH installation's own adapter dependencies.
// No app boot, credentials, provider requests, or package installation.
import { createRequire } from "node:module";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const executable = realpathSync(process.argv[1]);
const parent = dirname(executable);
const manifest = [
  resolve(parent, "../package.json"), // Unix npm bin symlink -> dsh/lib/bin.js
  resolve(parent, "node_modules/@deepseek-ai/dsh/package.json"), // global npm shim
  resolve(parent, "../@deepseek-ai/dsh/package.json"), // local node_modules/.bin
].find((path) => existsSync(path) && JSON.parse(readFileSync(path, "utf8")).name === "@deepseek-ai/dsh");
if (!manifest) throw new Error("detected DSH package is unavailable");
const requireDsh = createRequire(manifest);
const request = JSON.parse(process.argv[2]);
const metadata = [];
const add = (provider, models) => {
  for (const model of models) metadata.push({ provider, id: model.id, name: model.name ?? model.id });
};
if (request.providers.length) {
  const adapter = requireDsh.resolve("@deepseek-ai/dsh-llm-pi-ai");
  const nativePi = await import(pathToFileURL(createRequire(adapter).resolve("@earendil-works/pi-ai/providers/all")));
  const available = new Set(nativePi.getBuiltinProviders());
  for (const provider of request.providers) {
    if (available.has(provider)) add(provider, nativePi.getBuiltinModels(provider));
  }
}
if (request.deepseek) {
  const adapter = await import(pathToFileURL(requireDsh.resolve("@deepseek-ai/dsh-llm-deepseek")));
  add("deepseek", adapter.Config({}).models);
}
process.stdout.write(JSON.stringify(metadata));
