import { publishCatalog } from "../catalogPublish";
import { api, messageFromError, isBackendDisconnectedMessage } from "../tauri";
import type {
  CatalogOverrideDiagnostics,
  GatewayClientSyncSummary,
  Model,
  Provider,
  Settings,
  UpstreamFormatProbeResult,
} from "../types";

/** Internal I/O ports for the Provider Workspace (scriptable in tests). */
export type ProviderWorkspacePorts = {
  saveProviders: (providers: Provider[]) => Promise<Provider[]>;
  discoverProviderModels: (baseUrl: string, apiKey: string, providerId: string | null) => Promise<Model[]>;
  probeUpstreamFormat: (baseUrl: string, apiKey: string, model?: string | null) => Promise<UpstreamFormatProbeResult>;
  refreshOfficialModels: (restartCodex: boolean) => Promise<{ models: Model[]; restart_required: boolean; warning?: string | null; codex_restart_result?: string | null }>;
  getBundledProviders: () => Promise<Provider[]>;
  saveSettings: (settings: Settings) => Promise<Settings>;
  generateCatalog: (restartCodex: boolean) => Promise<unknown>;
  syncGatewayClients: () => Promise<GatewayClientSyncSummary>;
  catalogOverrideDiagnostics: () => Promise<CatalogOverrideDiagnostics | null>;
  refreshGatewayState: () => Promise<void>;
  authorizeCodexRestart: () => Promise<boolean | null>;
};

/** Production ports bound to the existing api surface. */
export function createProductionPorts(opts: {
  authorizeCodexRestart: () => Promise<boolean | null>;
  refreshGatewayState: () => Promise<void>;
}): ProviderWorkspacePorts {
  return {
    saveProviders: (providers) => api.saveProviders(providers),
    discoverProviderModels: (baseUrl, apiKey, providerId) =>
      api.discoverProviderModels(baseUrl, apiKey, providerId),
    probeUpstreamFormat: (baseUrl, apiKey, model) =>
      api.probeUpstreamFormat(baseUrl, apiKey, model),
    refreshOfficialModels: (restartCodex) => api.refreshOfficialModels(restartCodex),
    getBundledProviders: () => api.getBundledProviders(),
    saveSettings: (settings) => api.saveSettings(settings),
    generateCatalog: (restartCodex) => api.generateCatalog(restartCodex),
    syncGatewayClients: () => api.syncGatewayClients(),
    catalogOverrideDiagnostics: () => api.catalogOverrideDiagnostics().catch(() => null),
    refreshGatewayState: opts.refreshGatewayState,
    authorizeCodexRestart: opts.authorizeCodexRestart,
  };
}

/** Publish catalog + sync clients + refresh gateway + override diagnostics. */
export async function publishAndSync(
  ports: ProviderWorkspacePorts,
  restartCodex: boolean,
  settings: Settings | null,
  toast: {
    updateText: (text: string, tone: "loading" | "success" | "error") => void;
  },
  options?: { catalogAlreadyPublished?: boolean },
): Promise<GatewayClientSyncSummary | null> {
  const catalogAlreadyPublished = options?.catalogAlreadyPublished ?? false;
  const syncSettings = settings;
  if (toast && !catalogAlreadyPublished) {
    toast.updateText("providers.generatingCatalog", "loading");
  }
  const published = await publishCatalog(
    {
      reason: "provider-catalog",
      persist: !catalogAlreadyPublished,
      syncClients: Boolean(syncSettings?.auto_sync_clients),
    },
    {
      generate: () => ports.generateCatalog(restartCodex),
      sync: async () => {
        if (toast) {
          toast.updateText("providers.syncBoundClients", "loading");
        }
        return ports.syncGatewayClients().catch((err) => ({
          applied: 0,
          skipped: 0,
          failed: 1,
          results: [],
          message: messageFromError(err),
        } as GatewayClientSyncSummary));
      },
    },
  );
  let syncResult = published.syncResult;
  await ports.refreshGatewayState();
  const overrideDiagnostics = await ports.catalogOverrideDiagnostics().catch(() => null);
  if (syncResult) {
    syncResult = { ...syncResult, catalog_override_diagnostics: overrideDiagnostics };
  } else if (overrideDiagnostics) {
    syncResult = {
      applied: 0,
      skipped: 0,
      failed: 0,
      results: [],
      message: "",
      catalog_override_diagnostics: overrideDiagnostics,
    };
  }
  return syncResult;
}

export function isBackendDisconnected(error: unknown): boolean {
  const text = messageFromError(error);
  return isBackendDisconnectedMessage(text);
}
