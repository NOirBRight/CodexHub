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
