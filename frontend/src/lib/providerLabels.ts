import type { Provider } from "./types";

type Translate = (key: string, options?: Record<string, unknown>) => string;

type ProviderLabelMapOptions = {
  includeDisplayPrefixes?: boolean;
  includeOfficialDashedAlias?: boolean;
};

export function providerLabelMap(providers: Provider[], options: ProviderLabelMapOptions = {}) {
  const labels = new Map<string, string>([
    ["official", "OpenAI"],
    ["official_openai", "OpenAI"],
  ]);
  if (options.includeOfficialDashedAlias) {
    labels.set("official-openai", "OpenAI");
  }
  for (const provider of providers) {
    const name = provider.name.trim() || provider.id;
    labels.set(provider.id.trim().toLowerCase(), name);
    if (options.includeDisplayPrefixes) {
      const displayPrefix = provider.display_prefix?.trim();
      if (displayPrefix) {
        labels.set(displayPrefix.toLowerCase(), name);
      }
    }
  }
  return labels;
}

export function providerLabel(value: string, providerLabels: Map<string, string>, t?: Translate) {
  const normalized = value.trim().toLowerCase();
  if (!normalized) {
    return "";
  }
  const known = providerLabels.get(normalized);
  if (known) {
    return known;
  }
  if (normalized.startsWith("unknown") && t) {
    return t("common.unknown");
  }
  return titleizeProviderId(value);
}

export function titleizeProviderId(provider: string) {
  return provider
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => {
      const lower = part.toLowerCase();
      if (lower === "openai") {
        return "OpenAI";
      }
      if (lower === "cn") {
        return "CN";
      }
      return `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`;
    })
    .join(" ");
}

export function providerFromDisplayName(displayName: string | null | undefined, modelId: string) {
  const name = displayName?.trim();
  if (!name) {
    return "";
  }
  const firstToken = name.split(/\s+/)[0]?.trim();
  if (!firstToken || normalizeProviderToken(modelId).startsWith(normalizeProviderToken(firstToken))) {
    return "";
  }
  return firstToken;
}

export function normalizeProviderToken(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "");
}
