import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

function jsx(type, props, key) {
  return { type, props: { ...props, key } };
}

function findButton(node, label) {
  if (Array.isArray(node)) {
    for (const child of node) {
      const match = findButton(child, label);
      if (match) return match;
    }
    return null;
  }
  if (!node || typeof node !== "object") return null;
  if (node.type === "button" && node.props?.["aria-label"] === label) return node;
  return findButton(node.props?.children, label);
}

async function renderPanel(props) {
  const source = await readFile(
    new URL("../src/components/workspace/GatewayConnectionPanel.tsx", import.meta.url),
    "utf8",
  );
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;
  const exports = {};
  const require = (id) => {
    if (id === "react") {
      return { useState: (initial) => [typeof initial === "function" ? initial() : initial, () => {}] };
    }
    if (id === "react/jsx-runtime") return { jsx, jsxs: jsx };
    if (id === "lucide-react") return new Proxy({}, { get: () => () => null });
    if (id === "react-i18next") {
      return { useTranslation: () => ({ t: (key, options) => options?.value ? `${key}:${options.value}` : key }) };
    }
    return {};
  };
  new Function("exports", "require", compiled)(exports, require);
  return exports.GatewayConnectionPanel(props);
}

test("Gateway connection panel copy action reads the visible unsaved API-key draft", async () => {
  const copied = [];
  const tree = await renderPanel({
    draft: {
      gateway_bind_address: "127.0.0.1",
      proxy_port: 9099,
      gateway_request_timeout_seconds: 300,
      gateway_client_key: "visible-unsaved-key",
    },
    settings: {
      gateway_bind_address: "127.0.0.1",
      proxy_port: 9099,
      gateway_client_key: "saved-stale-key",
    },
    status: null,
    onDraft: () => {},
    onCopy: async (value) => copied.push(value),
  });

  const copyKey = findButton(tree, "workspace.copyValue:common.apiKey");
  assert.ok(copyKey, "the rendered panel exposes the API-key copy action");
  await copyKey.props.onClick();
  assert.deepEqual(copied, ["visible-unsaved-key"]);
});
