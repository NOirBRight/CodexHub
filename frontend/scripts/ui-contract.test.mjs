import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const contractPath = new URL("../src/lib/ui-contract.json", import.meta.url);
const appPath = new URL("../src/App.tsx", import.meta.url);
const gatewayPagePath = new URL("../src/pages/GatewayPage.tsx", import.meta.url);
const providersPagePath = new URL("../src/pages/ProvidersPage.tsx", import.meta.url);

async function readContract() {
  return JSON.parse(await readFile(contractPath, "utf8"));
}

test("main navigation exposes only CodexHub and Gateway", async () => {
  const contract = await readContract();

  assert.deepEqual(
    contract.tabs.map((tab) => tab.label),
    ["CodexHub", "Gateway"],
  );
});

test("gateway client rail is limited to the four planned clients", async () => {
  const contract = await readContract();

  assert.deepEqual(
    contract.gatewayClients.map((client) => client.name),
    ["OpenCode", "ZCode", "Pi", "OMP"],
  );
});

test("unwired gateway capabilities are rendered as pending backend", async () => {
  const contract = await readContract();

  assert.equal(contract.pendingBackend.label, "pending backend");
  assert.match(contract.pendingBackend.usage, /Usage/i);
  assert.match(contract.pendingBackend.clients, /client/i);
});

test("gateway page is wired to real usage and client backend APIs", async () => {
  const [appSource, gatewaySource] = await Promise.all([
    readFile(appPath, "utf8"),
    readFile(gatewayPagePath, "utf8"),
  ]);

  assert.match(appSource, /api\.gatewayUsageSummary\(/);
  assert.match(appSource, /api\.gatewayUsageEvents\(/);
  assert.match(appSource, /api\.listGatewayClients\(/);
  assert.match(gatewaySource, /usageSummary/);
  assert.match(gatewaySource, /clientInfos/);
});

test("gateway endpoint and copy panels use balanced 5:5 columns", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.match(gatewaySource, /grid-cols-\[minmax\(0,1fr\)_minmax\(0,1fr\)\]/);
  assert.doesNotMatch(gatewaySource, /0\.86fr|1\.14fr/);
});

test("provider model removal persists through provider save path", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /function removeModel\(modelId: string\)/);
  assert.match(providersSource, /onChange\(next\)/);
  assert.match(providersSource, /onRemove=\{removeModel\}/);
});

test("providers page uses stable zero-min split columns", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /grid-cols-\[minmax\(0,4fr\)_minmax\(0,6fr\)\]/);
  assert.match(providersSource, /<aside className="min-h-0 min-w-0 overflow-hidden/);
  assert.match(providersSource, /<section className="min-h-0 min-w-0 overflow-hidden/);
  assert.doesNotMatch(providersSource, /grid-cols-\[minmax\(360px,4fr\)_minmax\(0,6fr\)\]/);
});

test("app content region does not create provider-level scrollbars", async () => {
  const appSource = await readFile(appPath, "utf8");

  assert.match(appSource, /className="min-h-0 overflow-hidden p-4"/);
  assert.doesNotMatch(appSource, /className="min-h-0 overflow-auto p-4"/);
});

test("provider detail keeps model area tall and moves the scrollbar outside cards", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");
  const providerDetail = providersSource.match(/function ProviderDetail[\s\S]*?function ModelSection/)?.[0] ?? "";
  const providerCapabilitiesPanel =
    providersSource.match(/function ProviderCapabilitiesPanel[\s\S]*?function boolCapabilityState/)?.[0] ?? "";
  const modelSection = providersSource.match(/function ModelSection[\s\S]*?function ModelIdentity/)?.[0] ?? "";

  assert.match(providerDetail, /className="grid gap-2 border-b border-line p-4"/);
  assert.match(providerDetail, /className="grid gap-2 lg:grid-cols-2"/);
  assert.match(providerDetail, /className="field field-compact"/);
  assert.match(providerDetail, /className="lg:col-span-2"/);
  assert.match(providerDetail, /<div className="lg:col-span-2">\s*<ProviderCapabilitiesPanel/);
  assert.doesNotMatch(providerDetail, /className="grid gap-4 border-b border-line p-5"/);
  assert.match(providerCapabilitiesPanel, /className="flex min-w-0 items-center justify-between gap-2/);
  assert.match(providerCapabilitiesPanel, /className="flex min-w-0 items-center gap-2/);
  assert.match(providerCapabilitiesPanel, /className="flex shrink-0 gap-2/);
  assert.doesNotMatch(providerCapabilitiesPanel, /flex-wrap/);

  assert.match(modelSection, /className="min-h-0 overflow-auto -mr-3 pr-3"/);
  assert.doesNotMatch(modelSection, /className="min-h-0 overflow-auto pr-1"/);
});

test("unknown provider model metadata is not displayed as a 200K default", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /function formatContextWindow\(value\?: number \| null\)[\s\S]*return "Unknown";/);
  assert.match(providersSource, /context_window: model\.context_window \?\? null/);
  assert.doesNotMatch(providersSource, /context_window: model\.context_window \?\? 200_000/);
});

test("provider discovery updates the selected provider and reports progress", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /showToast\(`Discovering \$\{provider\.name\} models/);
  assert.match(providersSource, /const nextProvider = \{\s*\.\.\.provider,\s*models: mergeDiscoveredModels\(provider\.models, models\),\s*\}/s);
  assert.match(providersSource, /setProviders\(nextProviders\)/);
  assert.match(providersSource, /\$\{provider\.name\}: discovered \$\{models\.length\} model/);
});

test("provider discovery preserves missing API key environment variable names", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /const missingEnv = message\.match/);
  assert.match(providersSource, /Discovery failed: \$\{missingEnv\[1\]\} is not set/);
});

test("providers toast is locally anchored, dismissible, and auto-dismisses", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /window\.setTimeout\(\(\) => dismissToast\(\), 8000\)/);
  assert.match(providersSource, /"absolute bottom-3 left-3 z-50/);
  assert.match(providersSource, /aria-label="Dismiss notification"/);
  assert.match(providersSource, /animate-spin/);
  assert.doesNotMatch(providersSource, /"fixed bottom-4 left-4/);
});

test("model copy uses provider-qualified identifiers", async () => {
  const providersSource = await readFile(providersPagePath, "utf8");

  assert.match(providersSource, /function providerQualifiedModelId\(providerId: string, modelId: string\)/);
  assert.match(providersSource, /navigator\.clipboard\.writeText\(copyValue\)/);
  assert.match(providersSource, /<ModelIdentity model=\{model\} providerId=\{providerId\} \/>/);
});

test("legacy provider hidden capability is removed from model/provider UI state", async () => {
  const [typesSource, formatSource, appSource, providersSource, modelsSource] = await Promise.all([
    readFile(new URL("../src/lib/types.ts", import.meta.url), "utf8"),
    readFile(new URL("../src/lib/format.ts", import.meta.url), "utf8"),
    readFile(appPath, "utf8"),
    readFile(providersPagePath, "utf8"),
    readFile(new URL("../src/pages/ModelsPage.tsx", import.meta.url), "utf8"),
  ]);

  assert.doesNotMatch(typesSource, /hidden\?: boolean/);
  assert.doesNotMatch(formatSource, /hidden:/);
  assert.doesNotMatch(appSource, /provider\.hidden/);
  assert.doesNotMatch(providersSource, /provider\.hidden|model\.hidden|hidden: false/);
  assert.doesNotMatch(modelsSource, /label="Hidden"|hidden: checked/);
});
