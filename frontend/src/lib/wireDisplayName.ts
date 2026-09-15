export function shortWireDisplayName(
  stored: string | null | undefined,
  modelId: string | null | undefined,
) {
  const id = modelId?.trim() || "";
  const leaf = id.split("/").filter(Boolean).pop() || id;
  const name = stored?.trim() || "";
  if (!name || name === id || name === leaf) {
    return leaf;
  }
  return name;
}
