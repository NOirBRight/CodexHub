import type { CatalogOverrideDiagnostics } from "../types";

/** Build the catalog-override diagnostics toast message (pure, testable). */
export function catalogOverrideToastMessage(
  diagnostics: CatalogOverrideDiagnostics,
  t: (key: string, options?: Record<string, unknown>) => string,
): string | null {
  const accepted = Math.max(0, Math.min(100, diagnostics.accepted));
  const rejected = Math.max(0, Math.min(100, diagnostics.rejected));
  const migrated = Math.max(0, Math.min(100, diagnostics.migrated));
  if (accepted === 0 && rejected === 0 && migrated === 0) {
    return null;
  }
  return t("providers.catalogOverrideDiagnostics", {
    accepted,
    rejected,
    migrated,
    restart: t("providers.catalogOverrideRestartCodex"),
  });
}
