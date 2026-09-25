import assert from "node:assert/strict";
import test from "node:test";
import {
  aliasDefaultChanges,
  claudeDraft,
  claudeDraftChanged,
  claudeDraftValid,
  claudePreserveDefault,
  claudeResumeCommand,
  filterClaudeModels,
  rebaseClaudeDraft,
} from "../src/lib/claudeSettings.ts";
const models = [
  { id: "one", label: "One" },
  { id: "two", label: "Two" },
];
test("resume command requires a complete native model ID", () => {
  assert.equal(claudeResumeCommand("claude-opus-5-5[1m]"), "claude --resume --model 'claude-opus-5-5[1m]'");
  assert.equal(claudeResumeCommand(" claude-opus-5-5 "), "claude --resume --model claude-opus-5-5");
  assert.equal(claudeResumeCommand("opus"), "");
  assert.equal(claudeResumeCommand("claude-opus-5-5;echo unsafe"), "");
});
test("Claude settings preserve the existing default and keep subagent separate", () => {
  const saved = claudeDraft(
    {
      default_model: "claude-opus-5-5",
      role_mappings: { haiku: "one" },
      default_subagent_model: "two",
      conflicts: [],
    },
    "one",
  );
  assert.equal(saved.model, claudePreserveDefault);
  assert.equal(saved.roles.haiku, "one");
  assert.equal(saved.subagent, "two");
  assert.equal(claudeDraftChanged(saved, saved), false);
  assert.equal(
    claudeDraftChanged(
      { ...saved, roles: { ...saved.roles, haiku: "" } },
      saved,
    ),
    true,
  );
});
test("stale saved mappings remain visible while new invalid choices block apply", () => {
  const draft = claudeDraft(
    {
      default_model: "claude-opus-5-5",
      role_mappings: { haiku: "removed" },
      default_subagent_model: "",
      conflicts: [],
    },
    "one",
  );
  assert.equal(draft.model, claudePreserveDefault);
  assert.equal(claudeDraftValid(draft, new Set(["one"]), draft), true);
  assert.equal(
    claudeDraftValid(
      { ...draft, roles: { ...draft.roles, haiku: "new-removed" } },
      new Set(["one"]),
      draft,
    ),
    false,
  );
  assert.equal(
    claudeDraftValid(
      { ...draft, roles: { ...draft.roles, haiku: "" } },
      new Set(["one"]),
      draft,
    ),
    true,
  );
});

test("editing a family mapping previews the effect on its default alias", () => {
  assert.deepEqual(
    aliasDefaultChanges("opus", { opus: "native-opus" }, { opus: "provider/model" }),
    [{ alias: "opus", from: "native-opus", to: "provider/model" }],
  );
  assert.deepEqual(
    aliasDefaultChanges("claude-opus-5-5", { opus: "one" }, { opus: "two" }),
    [],
  );
});
test("search retains selected model in picker but not read-only search results", () => {
  assert.deepEqual(filterClaudeModels(models, "two", "one"), models);
  assert.deepEqual(filterClaudeModels(models, "two"), [models[1]]);
});

test("refresh updates clean Claude fields while preserving edits", () => {
  const baseline = claudeDraft(
    { default_model: "one", role_mappings: { haiku: "one" }, default_subagent_model: "", conflicts: [] },
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
