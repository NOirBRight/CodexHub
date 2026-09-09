import assert from "node:assert/strict";
import test from "node:test";
import {
  endOfDay,
  localDayKey,
  startOfDay,
  usageQueryWindow,
  usageRangeSpan,
} from "../src/lib/dateRange.ts";

test("7d query window includes the local afternoon of the given day", () => {
  const now = new Date(2026, 8, 8, 14, 57, 0);
  const span = usageRangeSpan("7d", { start: now, end: now }, now);
  assert.equal(localDayKey(span.start), "2026-09-02");
  assert.equal(localDayKey(span.end), "2026-09-08");
  const window = usageQueryWindow(span);
  const ts = now.toISOString();
  assert.ok(ts >= window.startTs, `start ${window.startTs} should be <= ${ts}`);
  assert.ok(ts <= window.endTs, `end ${window.endTs} should be >= ${ts}`);
  assert.equal(window.endTs, endOfDay(startOfDay(now)).toISOString());
});

test("a window frozen on the previous local day excludes today's traffic", () => {
  const yesterday = new Date(2026, 8, 7, 23, 0, 0);
  const today = new Date(2026, 8, 8, 14, 57, 0);
  const frozen = usageQueryWindow(usageRangeSpan("7d", { start: yesterday, end: yesterday }, yesterday));
  const live = usageQueryWindow(usageRangeSpan("7d", { start: today, end: today }, today));
  const ts = today.toISOString();
  assert.equal(localDayKey(usageRangeSpan("7d", { start: yesterday, end: yesterday }, yesterday).end), "2026-09-07");
  assert.equal(ts <= frozen.endTs, false);
  assert.equal(ts <= live.endTs, true);
  assert.notEqual(frozen.endTs, live.endTs);
});

test("1m query window is the last 31 local days ending today, not a calendar month", () => {
  const now = new Date(2026, 8, 8, 14, 57, 0);
  const span = usageRangeSpan("1m", { start: now, end: now }, now);
  assert.equal(localDayKey(span.start), "2026-08-09");
  assert.equal(localDayKey(span.end), "2026-09-08");
  const window = usageQueryWindow(span);
  assert.ok(now.toISOString() <= window.endTs);
});

test("week and month windows roll forward when the local day changes", () => {
  const monday = new Date(2026, 8, 7, 10, 0, 0);
  const tuesday = new Date(2026, 8, 8, 10, 0, 0);
  const custom = { start: monday, end: monday };
  const weekMonday = usageRangeSpan("7d", custom, monday);
  const weekTuesday = usageRangeSpan("7d", custom, tuesday);
  const monthMonday = usageRangeSpan("1m", custom, monday);
  const monthTuesday = usageRangeSpan("1m", custom, tuesday);
  assert.equal(localDayKey(weekMonday.start), "2026-09-01");
  assert.equal(localDayKey(weekMonday.end), "2026-09-07");
  assert.equal(localDayKey(weekTuesday.start), "2026-09-02");
  assert.equal(localDayKey(weekTuesday.end), "2026-09-08");
  assert.equal(localDayKey(monthMonday.start), "2026-08-08");
  assert.equal(localDayKey(monthTuesday.start), "2026-08-09");
  assert.notEqual(usageQueryWindow(weekMonday).startTs, usageQueryWindow(weekTuesday).startTs);
  assert.notEqual(usageQueryWindow(monthMonday).endTs, usageQueryWindow(monthTuesday).endTs);
});
