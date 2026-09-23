export const claudeRoles = [
  "haiku",
  "sonnet",
  "opus",
  "fable",
  "subagent",
] as const;
export interface ClaudeSettings {
  default_model: string;
  role_mappings: Record<string, string>;
  conflicts: string[];
}
export interface ClaudeDraft {
  model: string;
  roles: Record<string, string>;
}
export function claudeDraft(
  settings: ClaudeSettings | null | undefined,
  fallback: string,
): ClaudeDraft {
  return {
    model: settings?.default_model || fallback,
    roles: Object.fromEntries(
      claudeRoles.map((role) => [role, settings?.role_mappings[role] ?? ""]),
    ),
  };
}
export function claudeDraftValid(
  draft: ClaudeDraft,
  ids: Set<string>,
): boolean {
  return (
    ids.has(draft.model) &&
    Object.values(draft.roles).every((value) => !value || ids.has(value))
  );
}
export function claudeDraftChanged(
  draft: ClaudeDraft,
  saved: ClaudeDraft,
): boolean {
  return (
    draft.model !== saved.model ||
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
