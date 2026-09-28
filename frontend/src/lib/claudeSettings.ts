export const claudeRoles = [
  "haiku",
  "sonnet",
  "opus",
  "fable",
] as const;
export const claudePreserveDefault = "__codexhub_preserve_claude_default__";
export const claudeClearDefault = "__codexhub_clear_claude_default__";

const nativeClaudeId = /^claude-(?!codexhub-)[A-Za-z0-9._-]+(?:\[1m\])?$/;
const externalClaudeId = /^(claude-codexhub-[A-Za-z0-9._-]+)(?:\[1m\])?$/;

export function claudeResumeCommand(modelId: string): string {
  const id = modelId.trim();
  const external = externalClaudeId.exec(id);
  if (external) {
    return `claude --resume --model ${external[1]}`;
  }
  return nativeClaudeId.test(id)
    ? `claude --resume --model ${id.includes("[") ? `'${id}'` : id}`
    : "";
}

export function claudeDefaultTarget(
  defaultModel: string,
  models: { id: string; label: string }[],
  roles: Record<string, string>,
  nativeModels: { id: string; label: string }[] = [],
): Array<"builtin" | "subscription" | string> {
  const raw = defaultModel.trim();
  if (!raw) return ["builtin"];
  const named = (id: string) => {
    const unsuffixed = id.endsWith("[1m]") && !nativeClaudeId.test(id) ? id.slice(0, -4) : id;
    return (
      models.find((model) => model.id === id || model.id === unsuffixed)?.label ||
      nativeModels.find((model) => model.id === id)?.label ||
      (nativeClaudeId.test(id) ? id : "")
    );
  };
  const families = raw.toLowerCase() === "opusplan" ? ["opus", "sonnet"] : [raw.toLowerCase()];
  if (
    families.every((family) => claudeRoles.includes(family as (typeof claudeRoles)[number])) ||
    raw.toLowerCase() === "opusplan"
  ) {
    return families.map((family) => named(roles[family]?.trim() ?? "") || "subscription");
  }
  return [named(raw) || raw];
}

export interface ClaudeModelChoice {
  id: string;
  label: string;
}
export interface ClaudeSettings {
  default_model: string;
  role_mappings: Record<string, string>;
  default_subagent_model: string;
  conflicts: string[];
  native_models?: ClaudeModelChoice[];
}
export interface ClaudeDraft {
  model: string;
  roles: Record<string, string>;
  subagent: string;
}
export function claudeDraft(
  settings: ClaudeSettings | null | undefined,
  _fallback: string,
): ClaudeDraft {
  return {
    model: claudePreserveDefault,
    roles: Object.fromEntries(
      claudeRoles.map((role) => [role, settings?.role_mappings[role] ?? ""]),
    ),
    subagent: settings?.default_subagent_model ?? "",
  };
}
export function claudeDraftValid(
  draft: ClaudeDraft,
  ids: Set<string>,
  saved: ClaudeDraft,
  nativeIds: Set<string> = new Set(),
): boolean {
  return (
    (draft.model === claudePreserveDefault ||
      draft.model === claudeClearDefault ||
      ids.has(draft.model) ||
      nativeIds.has(draft.model)) &&
    claudeRoles.every(
      (role) =>
        !draft.roles[role] ||
        ids.has(draft.roles[role]) ||
        draft.roles[role] === saved.roles[role],
    ) &&
    (!draft.subagent ||
      ids.has(draft.subagent) ||
      draft.subagent === saved.subagent)
  );
}
export function claudeDraftChanged(
  draft: ClaudeDraft,
  saved: ClaudeDraft,
): boolean {
  return (
    draft.model !== saved.model ||
    draft.subagent !== saved.subagent ||
    claudeRoles.some((role) => draft.roles[role] !== saved.roles[role])
  );
}

export function rebaseClaudeDraft(
  current: ClaudeDraft,
  baseline: ClaudeDraft,
  incoming: ClaudeDraft,
): ClaudeDraft {
  return {
    model: current.model === baseline.model ? incoming.model : current.model,
    subagent:
      current.subagent === baseline.subagent ? incoming.subagent : current.subagent,
    roles: Object.fromEntries(
      claudeRoles.map((role) => [
        role,
        current.roles[role] === baseline.roles[role]
          ? incoming.roles[role]
          : current.roles[role],
      ]),
    ),
  };
}

export function aliasDefaultChanges(
  defaultModel: string,
  savedRoles: Record<string, string>,
  draftRoles: Record<string, string>,
): { alias: string; from: string; to: string }[] {
  const alias = defaultModel.trim().toLowerCase();
  const families =
    alias === "opusplan" ? ["opus", "sonnet"] : [alias];
  if (!claudeRoles.includes(alias as (typeof claudeRoles)[number]) && alias !== "opusplan") {
    return [];
  }
  return families.flatMap((family) => {
    const from = savedRoles[family] || "Claude subscription default";
    const to = draftRoles[family] || "Claude subscription default";
    return from === to ? [] : [{ alias, from, to }];
  });
}
export function filterClaudeModels<T extends { id: string; label: string }>(
  models: T[],
  query: string,
  selected = "",
): T[] {
  const needle = query.trim().toLowerCase();
  return models.filter(
    (model) =>
      model.id === selected ||
      `${model.label} ${model.id}`.toLowerCase().includes(needle),
  );
}
