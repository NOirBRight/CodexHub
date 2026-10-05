// Read only catalog/schema metadata from the detected DSH installation.
// No app boot, credentials, provider requests, or package installation.
import { createRequire } from "node:module";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const executable = realpathSync(process.argv[1]);
const parent = dirname(executable);
const manifest = [
  resolve(parent, "../package.json"),
  resolve(parent, "node_modules/@deepseek-ai/dsh/package.json"),
  resolve(parent, "../@deepseek-ai/dsh/package.json"),
].find((path) => existsSync(path) && JSON.parse(readFileSync(path, "utf8")).name === "@deepseek-ai/dsh");
if (!manifest) throw new Error("detected DSH package is unavailable");
const requireDsh = createRequire(manifest);
const load = (name) => import(pathToFileURL(requireDsh.resolve(name)));
const request = JSON.parse(process.argv[2]);
const tool = await load("@deepseek-ai/dsh-tool-subagent");
// Schema serialization is the native authority, including old model-only clients.
const schema = JSON.parse(JSON.stringify(tool.Config));
const root = schema.refs?.[schema.uid] ?? schema;
const agentOptions = schema.refs?.[root.dict?.agentOptions] ?? root.dict?.agentOptions;
const supportsEffort = Boolean(agentOptions?.dict?.reasoningEffort);
const options = [];
const add = (provider, model, efforts = []) => options.push({
  id: `${provider}/${model.id}`, label: `${provider} / ${model.name ?? model.id}`,
  efforts: supportsEffort && efforts.length ? ["", ...efforts] : [], defaultEffort: "",
});
if (Object.keys(request.providers).length) {
  const adapterPath = requireDsh.resolve("@deepseek-ai/dsh-llm-pi-ai");
  const requireAdapter = createRequire(adapterPath);
  const adapter = supportsEffort ? await load("@deepseek-ai/dsh-llm-pi-ai") : null;
  const profiles = adapter ? adapter.Config({ providers: request.providers }).providers.get() : request.providers;
  const nativePi = await import(pathToFileURL(requireAdapter.resolve("@earendil-works/pi-ai/providers/all")));
  const pi = supportsEffort ? await import(pathToFileURL(requireAdapter.resolve("@earendil-works/pi-ai"))) : null;
  for (const [provider, profile] of Object.entries(profiles)) {
    const builtin = nativePi.getBuiltinModels(provider);
    const byId = new Map(builtin.map((model) => [model.id, model]));
    const models = profile.models ?? builtin;
    for (const model of models) {
      const configured = profile.models ? model : profile.modelOverrides?.[model.id] ?? {};
      const declared = configured.reasoningEfforts;
      // Configured levels are already wire-mapped by DSH; expose the declared keys.
      const efforts = declared && typeof declared === "object"
        ? Object.keys(declared).filter((level) => typeof declared[level] === "string" && declared[level].length || level === "off" && declared[level] === null)
        : declared === false ? []
        : byId.get(model.id)?.reasoning && pi ? pi.getSupportedThinkingLevels(byId.get(model.id)) : [];
      add(provider, { ...model, ...configured }, efforts);
    }
  }
}
const deepseek = await load("@deepseek-ai/dsh-llm-deepseek");
if (supportsEffort && deepseek.resolveAdapterOptions && deepseek.DeepSeekAdapter) {
  const connection = deepseek.resolveAdapterOptions(request.deepseek);
  // resolveModel is metadata-only; prevent construction of a persistent file index.
  const adapter = new deepseek.DeepSeekAdapter({ options: () => connection, resolveFiles: () => ({}) });
  for (const model of connection.models) {
    const metadata = await adapter.resolveModel("deepseek", model.id);
    add("deepseek", model, metadata.reasoning?.efforts.map((effort) => effort.id) ?? []);
  }
} else {
  for (const model of request.deepseek.models ?? deepseek.Config({}).models) add("deepseek", model);
}
process.stdout.write(JSON.stringify(options));
