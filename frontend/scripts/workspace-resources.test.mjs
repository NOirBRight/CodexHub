import assert from "node:assert/strict";
import test from "node:test";
import {
  quotaPercent,
  quotaResetDate,
  resourceQuotaLayout,
} from "../src/lib/workspaceResources.ts";
const limit = (overrides = {}) => ({
  key: "week",
  name: "Weekly",
  period: "week",
  ...overrides,
});
test("a weekly-only provider occupies the right slot without a fabricated short window", () => {
  const week = limit({ remaining: 68, limit: 100 });
  const result = resourceQuotaLayout([week]);
  assert.equal(result.weekOnly, true);
  assert.equal(result.single, true);
  assert.deepEqual(result.ordered, [week]);
  const short = limit({ key: "5h", period: "5 hours", name: "5 hours" });
  assert.deepEqual(resourceQuotaLayout([week, short]).ordered, [short, week]);
  assert.equal(resourceQuotaLayout([short]).weekOnly, false);
  assert.equal(resourceQuotaLayout([]).weekOnly, false);
});
test("unknown quota never masquerades as exhausted or full", () => {
  assert.equal(quotaPercent(limit()), null);
  assert.equal(quotaPercent(limit({ resets_at: "2026-09-12T07:52:00Z" })), null);
  assert.equal(quotaPercent(limit({ used: 0 })), null);
  assert.equal(quotaPercent(limit({ limit: 0, remaining: 0 })), null);
  assert.equal(quotaPercent(limit({ limit: 100, used: 32 })), 68);
  assert.equal(quotaPercent(limit({ limit: 100, remaining: 0, used: 10 })), 0);
  assert.equal(quotaPercent(limit({ limit: 100, remaining: 130 })), 100);
  assert.equal(quotaPercent(limit({ limit: 100, remaining: -1 })), 0);
  assert.equal(quotaPercent(limit({ limit: 100, remaining: NaN })), null);
});
test("reset timestamps accept API epoch seconds, milliseconds and ISO strings", () => {
  const iso = "2026-09-12T07:52:00.000Z",
    ms = Date.parse(iso);
  for (const raw of [iso, String(ms), String(ms / 1000)])
    assert.equal(quotaResetDate(raw)?.toISOString(), iso);
  for (const raw of [undefined, null, {}, true, 1789000000, "", "unknown", "9999999999999999999"])
    assert.equal(quotaResetDate(raw), null);
});

test("quota windows keep chronological order with monthly after weekly", () => {
  const limits = [
    { key: "monthly", name: "Monthly", period: "month" },
    { key: "weekly", name: "Weekly", period: "week" },
    { key: "rolling", name: "5 hours", period: "5h" },
  ];
  assert.deepEqual(
    resourceQuotaLayout(limits).ordered.map((item) => item.key),
    ["rolling", "weekly", "monthly"],
  );
});
