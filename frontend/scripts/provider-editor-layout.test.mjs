import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const editorPath = new URL("../src/components/providers/ProviderEditor.tsx", import.meta.url);
const usagePath = new URL(
  "../src/components/providers/OfficialOpenAIUsagePanel.tsx",
  import.meta.url,
);
const providersPagePath = new URL("../src/pages/ProvidersPage.tsx", import.meta.url);

const editorSource = await readFile(editorPath, "utf8");
const usageSource = await readFile(usagePath, "utf8");
const providersPageSource = await readFile(providersPagePath, "utf8");

function namedFunction(source, name) {
  const match = source.match(
    new RegExp(`(?:export\\s+)?function\\s+${name}\\b[\\s\\S]*?(?=\\n(?:export\\s+)?function\\s+|\\nexport function\\s|$)`),
  );
  assert.ok(match, `missing function ${name}`);
  return match[0];
}

function firstDiv(source, fromIndex = 0) {
  const start = source.indexOf("<div", fromIndex);
  assert.ok(start >= 0, "expected a div");
  let depth = 0;
  for (let index = start; index < source.length; index += 1) {
    if (source.startsWith("</div>", index)) {
      depth -= 1;
      if (depth === 0) {
        return source.slice(start, index + "</div>".length);
      }
      index += "</div>".length - 1;
      continue;
    }
    if (source.startsWith("<div", index) && /<div[\s>]/.test(source.slice(index, index + 5))) {
      depth += 1;
    }
  }
  assert.fail("unbalanced div");
}

function connectionGrid(componentSource) {
  const gridStart = componentSource.indexOf('<div className="grid grid-cols-2 gap-2">');
  assert.ok(gridStart >= 0, "connection fields should use a two-column grid");
  return firstDiv(componentSource, gridStart);
}

test("ProviderDetail and AddProviderPanel share a two-column connection grid instead of stacked full-width Base URL and endpoint rows", () => {
  for (const name of ["ProviderDetail", "AddProviderPanel"]) {
    const grid = connectionGrid(namedFunction(editorSource, name));
    assert.match(grid, /common\.name/);
    assert.match(grid, /common\.apiKey/);
    assert.match(grid, /common\.baseUrl/);
    assert.match(grid, /EndpointSelectionPanel/);
    assert.doesNotMatch(grid, /col-span-2/);
    assert.doesNotMatch(
      grid,
      /baseUrl["'`][^>]{0,80}className="col-span-2"/,
    );
  }
  assert.doesNotMatch(editorSource, /账户与用量|模型 \/ /);
});

test("model rows stay one identity line with a compact SortableList gap", async () => {
  const [section, sortable] = await Promise.all([
    readFile(new URL("../src/components/providers/ProviderModelSection.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/components/SortableList.tsx", import.meta.url), "utf8"),
  ]);
  assert.match(section, /className="space-y-1"/);
  assert.match(section, /grid-cols-\[minmax\(0,1fr\)_minmax\(0,1fr\)\]/);
  assert.match(sortable, /cx\("px-px py-px"/);
  assert.doesNotMatch(sortable, /cx\("space-y-3 px-px py-px"/);
});

test("Official usage header composes title, metric windows, and day/week on one row", () => {
  const panel = namedFunction(usageSource, "OfficialOpenAIUsagePanel");
  const sectionStart = panel.indexOf("<section");
  assert.ok(sectionStart >= 0, "usage panel should render a section");
  const header = firstDiv(panel, panel.indexOf("<div", sectionStart));
  assert.match(header, /providers\.openaiUsage/);
  assert.match(header, /UsageMetric/);
  assert.match(header, /grid-cols-\[repeat\(5,minmax\(0,1fr\)\)\]/);
  assert.match(header, /modeOptions\.map/);
  assert.match(header, /setMode\(option\.value\)/);
  assert.match(panel, /t\("usage\.day"\)/);
  assert.match(panel, /t\("usage\.week"\)/);

  const rest = panel.slice(panel.indexOf(header) + header.length);
  assert.doesNotMatch(rest, /UsageMetric/);
  assert.doesNotMatch(rest, /grid-cols-\[repeat\(5,minmax\(0,1fr\)\)\]/);
  assert.match(rest, /data-openai-usage-chart/);

  const skeleton = namedFunction(usageSource, "OfficialOpenAIUsageSkeleton");
  assert.doesNotMatch(skeleton, /grid-cols-\[repeat\(5,minmax\(0,1fr\)\)\]/);

  const officialDetail = namedFunction(providersPageSource, "OfficialDetail");
  assert.match(officialDetail, /OfficialOpenAIUsageLimitBars/);
  assert.match(officialDetail, /OfficialOpenAIUsagePanel/);
  assert.match(officialDetail, /openaiSourceExcludedDetail/);
  assert.doesNotMatch(officialDetail, /账户与用量/);
});

test("model settings overlay is a nested dialog that consumes Escape", async () => {
  const [section, focus] = await Promise.all([
    readFile(new URL("../src/components/providers/ProviderModelSection.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/hooks/useDialogFocus.ts", import.meta.url), "utf8"),
  ]);
  assert.match(section, /useDialogFocus\(true, panel, onClose\)/);
  assert.match(section, /data-nested-dialog=""/);
  assert.match(section, /role="dialog"/);
  assert.match(focus, /querySelector\("\[data-nested-dialog\]"\)/);
  assert.match(focus, /nested\.contains\(document\.activeElement\)/);
});
