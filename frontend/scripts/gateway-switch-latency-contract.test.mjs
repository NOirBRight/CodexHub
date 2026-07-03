import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const gatewayPagePath = new URL("../src/pages/GatewayPage.tsx", import.meta.url);

test("gateway client refresh does not block switches with version probes", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.match(gatewaySource, /async function refreshGatewayClients\(\)/);
  assert.doesNotMatch(gatewaySource, /await onRefreshClients\(\{ includeClientVersions: true \}\)/);
});

test("gateway client refresh clears stale switch busy state", async () => {
  const gatewaySource = await readFile(gatewayPagePath, "utf8");

  assert.match(
    gatewaySource,
    /async function refreshGatewayClients\(\) \{[\s\S]*await onRefreshClients\(\);[\s\S]*setClientBusy\(null\);/,
  );
});
