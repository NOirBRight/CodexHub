import type {
  Provider,
  ProviderCatalogTransactionResult,
  ProviderProtocolSwitch,
} from "./types";

type Translate = (key: string, options?: Record<string, unknown>) => string;

export type ProviderCatalogTransactionFeedback = {
  committed: boolean;
  tone: "success" | "error";
  text: string;
};

export function providerCatalogSaveDisabled(
  dirty: boolean,
  saveBusy: boolean,
  recoveryPending: boolean,
) {
  return (!dirty && !recoveryPending) || saveBusy;
}

export function changedProviderProtocols(
  currentProviders: Provider[],
  nextProviders: Provider[],
): ProviderProtocolSwitch[] {
  const currentById = new Map(currentProviders.map((provider) => [provider.id, provider]));
  return nextProviders.flatMap((provider) => {
    const current = currentById.get(provider.id);
    const upstreamProtocol = provider.upstream_format;
    if (
      !current
      || current.upstream_format === upstreamProtocol
      || !upstreamProtocol
      || upstreamProtocol === "auto"
    ) {
      return [];
    }
    return [{
      providerId: provider.id,
      upstreamProtocol,
      modelIds: provider.models
        .filter((model) => model.enabled && model.gateway_exported !== false)
        .map((model) => model.id),
    }];
  });
}

export function providerCatalogTransactionFeedback(
  result: ProviderCatalogTransactionResult,
  t: Translate,
): ProviderCatalogTransactionFeedback {
  if (result.outcome === "committed") {
    return {
      committed: true,
      tone: "success",
      text: t(
        result.protocolChanged
          ? "providers.protocolChangedRestartLongLivedCodex"
          : "providers.providerCatalogUpdated",
      ),
    };
  }
  const detail = result.detail ?? "";
  if (result.outcome === "rolled_back") {
    return {
      committed: false,
      tone: "error",
      text: t(
        result.protocolChanged
          ? "providers.protocolChangeRolledBack"
          : "providers.providerCatalogChangeRolledBack",
        { detail },
      ),
    };
  }
  if (result.outcome === "recovery_required") {
    const key = result.protocolChanged
      ? result.catalogDisabled
        ? "providers.protocolChangeRecoveryRequired"
        : "providers.protocolChangeRecoveryInvalidationFailed"
      : result.catalogDisabled
        ? "providers.providerCatalogRecoveryRequired"
        : "providers.providerCatalogRecoveryInvalidationFailed";
    return {
      committed: false,
      tone: "error",
      text: t(key, { detail }),
    };
  }
  return {
    committed: false,
    tone: "error",
    text: t(
      result.protocolChanged
        ? "providers.protocolChangeUnchanged"
        : "providers.providerCatalogChangeUnchanged",
      { detail },
    ),
  };
}
