import assert from "node:assert/strict";
import test from "node:test";
import {
  aliasDefaultChanges,
  claudeDefaultTarget,
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

test("default target names the actual model and does not treat an external 1m badge as capacity", () => {
  const models = [{ id: "gpt-5.5", label: "6 Astra" }];
  const native = [{ id: "claude-opus-5-5[1m]", label: "Claude Opus 5.5 1M" }];
  assert.deepEqual(claudeDefaultTarget("opus", models, { opus: "gpt-5.5" }), ["6 Astra"]);
  assert.deepEqual(claudeDefaultTarget("opus", models, {}), ["subscription"]);
  assert.deepEqual(claudeDefaultTarget("", models, {}), ["builtin"]);
  assert.deepEqual(claudeDefaultTarget("gpt-5.5[1m]", models, {}), ["6 Astra"]);
  assert.deepEqual(claudeDefaultTarget("claude-opus-5-5[1m]", models, {}, native), ["Claude Opus 5.5 1M"]);
  assert.deepEqual(
    claudeDefaultTarget("opusplan", models, { opus: "gpt-5.5", sonnet: "" }, native),
    ["6 Astra", "subscription"],
  );
});

test("resume keeps native 1m and rewrites a pasted external 1m id", () => {
  assert.equal(
    claudeResumeCommand("claude-codexhub-gpt-5.5[1m]"),
    "claude --resume --model claude-codexhub-gpt-5.5",
  );
  assert.equal(
    claudeResumeCommand("claude-codexhub-gpt-5.5"),
    "claude --resume --model claude-codexhub-gpt-5.5",
  );
  assert.equal(
    claudeResumeCommand("claude-opus-5-5[1m]"),
    "claude --resume --model 'claude-opus-5-5[1m]'",
  );
});

test("a native default is selectable without being an exported Gateway model", () => {
  const saved = claudeDraft(
    {
      default_model: "claude-opus-5-5",
      role_mappings: {},
      default_subagent_model: "",
      conflicts: [],
      native_models: [{ id: "claude-opus-5-5[1m]", label: "Claude Opus 5.5 1M" }],
    },
    "",
  );
  assert.equal(
    claudeDraftValid(
      { ...saved, model: "claude-opus-5-5[1m]" },
      new Set(["gpt-5.5"]),
      saved,
      new Set(["claude-opus-5-5[1m]"]),
    ),
    true,
  );
  assert.equal(
    claudeDraftValid(
      { ...saved, model: "claude-codexhub-gpt-5.5[1m]" },
      new Set(["gpt-5.5"]),
      saved,
      new Set(["claude-opus-5-5[1m]"]),
    ),
    false,
  );
});
