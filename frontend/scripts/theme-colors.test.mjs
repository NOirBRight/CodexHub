import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import { test } from "node:test";
import postcss from "postcss";
import tailwindcss from "tailwindcss";
import config from "../tailwind.config.js";

// Compile the shared controls without a workspace ancestor: subagent menus are
// portalled into document.body and must carry their own theme-aware utilities.
const css = await readFile(new URL("../src/index.css", import.meta.url), "utf8");
const compiled = await postcss([tailwindcss({
  ...config,
  content: [{ raw: '<div class="select-popover select-option field focus-ring bg-surface/70 text-on-action/70"></div>' }],
})]).process(css, { from: undefined });
function values(selector, property) {
  const result = [];
  compiled.root.walkRules((rule) => {
    if (rule.selector === selector) rule.walkDecls(property, (decl) => result.push(decl.value));
  });
  assert.ok(result.length, `missing ${selector} ${property}`);
  return result.join(" ");
}

test("portalled menus, options and fields consume document theme colors", () => {
  assert.match(values(".select-popover", "background-color"), /var\(--ws-surface\)/);
  assert.match(values(".select-popover", "border-color"), /var\(--ws-line\)/);
  assert.match(values(".select-option", "color"), /var\(--ws-ink\)/);
  assert.match(values('.select-option[aria-selected="true"]', "background-color"), /var\(--ws-soft\)/);
  assert.match(values('.select-option[aria-selected="true"]', "color"), /var\(--ws-accent\)/);
  assert.match(values(".field:hover", "background-color"), /var\(--ws-soft\)/);
  assert.match(values(".focus-ring", "--tw-ring-offset-color"), /var\(--ws-bg\)/);
});

test("production UI colors stay semantic across controls, dialogs and charts", async () => {
  async function componentFiles(directory) {
    const entries = await readdir(directory, { withFileTypes: true });
    return (await Promise.all(entries.map((entry) => {
      const url = new URL(entry.name + (entry.isDirectory() ? "/" : ""), directory);
      return entry.isDirectory() ? componentFiles(url) : entry.name.endsWith(".tsx") ? [url] : [];
    }))).flat();
  }
  const files = [
    ...await componentFiles(new URL("../src/components/", import.meta.url)),
    new URL("../src/App.tsx", import.meta.url),
    new URL("../src/pages/ProvidersPage.tsx", import.meta.url),
    new URL("../src/pages/GatewayPage.tsx", import.meta.url),
    new URL("../src/index.css", import.meta.url),
  ];
  for (const file of files) {
    const source = await readFile(file, "utf8");
    assert.doesNotMatch(source, /#[\da-f]{3,8}\b|rgba?\(\s*\d/i, `${file.pathname}: inline color`);
    assert.doesNotMatch(source, /\b(?:bg|text|border|ring|from|via|to|accent)-(?:slate|gray|blue|emerald|green|red|rose|amber|white|black)(?:-\d+|\b)/, `${file.pathname}: fixed palette utility`);
  }
});

test("theme colors preserve Tailwind opacity modifiers", () => {
  assert.match(values('.bg-surface\\/70', 'background-color'), /color-mix\(in srgb, var\(--ws-surface\) calc\(0\.7 \* 100%\), transparent\)/);
  assert.match(values('.text-on-action\\/70', 'color'), /var\(--ws-on-accent\) calc\(0\.7 \* 100%\)/);
});
