import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const contractPath = new URL("../src/lib/ui-contract.json", import.meta.url);

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
