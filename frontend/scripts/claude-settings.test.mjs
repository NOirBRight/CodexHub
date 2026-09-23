import assert from "node:assert/strict";
import test from "node:test";
import {
  claudeDraft,
  claudeDraftChanged,
  claudeDraftValid,
  filterClaudeModels,
  rebaseClaudeDraft,
} from "../src/lib/claudeSettings.ts";
const models = [
  { id: "one", label: "One" },
  { id: "two", label: "Two" },
];
test("Claude settings reopen from persisted selections and clearing a role is a change", () => {
  const saved = claudeDraft(
    { default_model: "two", role_mappings: { haiku: "one" }, conflicts: [] },
    "one",
  );
  assert.equal(saved.model, "two");
  assert.equal(saved.roles.haiku, "one");
  assert.equal(claudeDraftChanged(saved, saved), false);
  assert.equal(
    claudeDraftChanged(
      { ...saved, roles: { ...saved.roles, haiku: "" } },
      saved,
    ),
    true,
  );
});
test("removed default or mapped models block apply without falling back", () => {
  const draft = claudeDraft(
    { default_model: "removed", role_mappings: {}, conflicts: [] },
    "one",
  );
  assert.equal(draft.model, "removed");
  assert.equal(claudeDraftValid(draft, new Set(["one"])), false);
  assert.equal(
    claudeDraftValid(
      { model: "one", roles: { haiku: "removed" } },
      new Set(["one"]),
    ),
    false,
  );
  assert.equal(
    claudeDraftValid({ model: "one", roles: { haiku: "" } }, new Set(["one"])),
    true,
  );
});
test("search retains selected model in picker but not read-only search results", () => {
  assert.deepEqual(filterClaudeModels(models, "two", "one"), models);
  assert.deepEqual(filterClaudeModels(models, "two"), [models[1]]);
});

test("refresh updates clean Claude fields while preserving edits", () => {
  const baseline = claudeDraft(
    { default_model: "one", role_mappings: { haiku: "one" }, conflicts: [] },
    "",
  );
  const incoming = {
    ...baseline,
    model: "two",
    roles: { ...baseline.roles, haiku: "two" },
  };
  const dirty = { ...baseline, roles: { ...baseline.roles, haiku: "" } };
  assert.deepEqual(rebaseClaudeDraft(dirty, baseline, incoming), {
    ...incoming,
    roles: dirty.roles,
  });
  assert.deepEqual(rebaseClaudeDraft(baseline, baseline, incoming), incoming);
});
