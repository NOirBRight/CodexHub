import { invoke } from "@tauri-apps/api/core";
import { COMMANDS, type CommandName } from "./commands";
import type {
  AppFlavorInfo,
  AppStatus,
  AppUpdateCompletionStatus,
  AppUpdateInstallResult,
  AppUpdateInstallStatus,
  AppUpdateStatus,
  AppVersionInfo,
  AutostartStatus,
  CatalogOverrideDiagnostics,
  CodexContextGuardStatus,
  CodexHubError,
  DiagnosticsActionResult,
  DiagnosticsStatus,
  GatewayClientConfig,
  GatewayClientApplyResult,
  GatewayClientConfigPreview,
  DshLifecycleReport,
  GatewayClientInfo,
  GatewayClientSyncSummary,
  GatewayEvent,
  GatewayStatus,
  GatewayTestKind,
  GatewayTestResult,
  GatewayUsageEvent,
  GatewayUsageSnapshot,
  GatewayUsageSummary,
  Model,
  ModelEndpointTestResult,
  OfficialRefreshResult,
  OpenAIUsageQueryWindow,
  OpenAIUsageSnapshot,
  Provider,
  RoutingOwner,
  Settings,
  SubagentMatrixStatus,
  UpstreamFormat,
  UpstreamFormatProbeResult,
  UnifiedHistoryResult,
  UsageQueryWindow,
} from "./types";
import { normalizeSettings } from "./settings";

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

interface BridgeResponse<T> {
  ok: boolean;
  value?: T;
  error?: string;
  codexhub_error?: CodexHubError | null;
}

export class CodexHubBridgeError extends Error {
  readonly codexhubError: CodexHubError | null;

  constructor(message: string, codexhubError: CodexHubError | null) {
    super(message);
    this.name = "CodexHubBridgeError";
    this.codexhubError = codexhubError;
  }
}

const DEFAULT_BRIDGE_URL = "http://127.0.0.1:1421/api/invoke";
const KNOWN_BRIDGE_URLS = [
  DEFAULT_BRIDGE_URL,
  "http://127.0.0.1:1431/api/invoke",
];
const LOCAL_DEV_HOSTS = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

async function call<T>(command: CommandName, args?: Record<string, unknown>): Promise<T> {
  if (window.__TAURI_INTERNALS__) {
    try {
      return await invoke<T>(command, args);
    } catch (error) {
      if (!shouldFallbackToBridge(error)) {
        throw error;
      }
    }
  }
  return bridgeInvoke<T>(command, args);
}

async function desktopCall<T>(command: CommandName, args?: Record<string, unknown>): Promise<T | null> {
  if (!window.__TAURI_INTERNALS__) {
    return null;
  }
  return invoke<T>(command, args);
}

async function bridgeInvoke<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  for (const url of bridgeUrls()) {
    let response: Response;
    try {
      response = await fetch(url, {
        method: "POST",
        body: JSON.stringify({ command, args: args ?? {} }),
      });
    } catch (error) {
      console.debug(`CodexHub web bridge unavailable at ${url}`, error);
      continue;
    }

    const payload = (await response.json().catch(() => null)) as BridgeResponse<T> | null;
    if (!response.ok || !payload?.ok) {
      throw new CodexHubBridgeError(
        payload?.codexhub_error?.message || payload?.error || `CodexHub web bridge request failed: HTTP ${response.status}`,
        payload?.codexhub_error ?? null,
      );
    }
    return payload.value as T;
  }
  throw new Error("Backend is not connected");
}

function shouldFallbackToBridge(error: unknown) {
  const detail = error instanceof Error ? error.message : String(error);
  const message = detail.toLowerCase();

  return (
    message.includes("__tauri_internals__") ||
    message.includes("window.__tauri__") ||
    message.includes("invoke is not a function") ||
    message.includes("ipc") ||
    message.includes("unknown command") ||
    /command .*(not found|not allowed|not recognized)/.test(message)
  );
}

function bridgeUrl() {
  return (
    import.meta.env.VITE_CODEXHUB_BRIDGE_URL ||
    localBridgeUrlFromLocation(window.location) ||
    DEFAULT_BRIDGE_URL
  );
}

function bridgeUrls() {
  const explicit = import.meta.env.VITE_CODEXHUB_BRIDGE_URL;
  if (explicit) {
    return [explicit];
  }
  return Array.from(new Set([bridgeUrl(), ...KNOWN_BRIDGE_URLS]));
}

function localBridgeUrlFromLocation(location: Location) {
  if (location.protocol !== "http:" || !LOCAL_DEV_HOSTS.has(location.hostname)) {
    return null;
  }

  const frontendPort = Number(location.port);
  if (!Number.isInteger(frontendPort) || frontendPort <= 0) {
    return null;
  }

  const bridgePort = frontendPort + 1;
  return `http://${formatHostnameForUrl(location.hostname)}:${bridgePort}/api/invoke`;
}

function formatHostnameForUrl(hostname: string) {
  const normalized = hostname.replace(/^\[(.*)\]$/, "$1");
  return normalized.includes(":") ? `[${normalized}]` : normalized;
}

function usageWindowArgs(window?: UsageQueryWindow | null) {
  return {
    startTs: window?.startTs ?? null,
    endTs: window?.endTs ?? null,
  };
}

function openaiUsageWindowArgs(window?: OpenAIUsageQueryWindow | null) {
  return {
    startTime: window?.startTime ?? null,
    endTime: window?.endTime ?? null,
    forceRefresh: window?.forceRefresh ?? null,
  };
}

export const api = {
  getAppFlavor: () => call<AppFlavorInfo>(COMMANDS.getAppFlavor),
  getAppVersion: () => call<AppVersionInfo>(COMMANDS.getAppVersion),
  checkAppUpdate: () => call<AppUpdateStatus>(COMMANDS.checkAppUpdate),
  startAppUpdateInstall: () => call<AppUpdateInstallStatus>(COMMANDS.startAppUpdateInstall),
  getAppUpdateInstallStatus: () => call<AppUpdateInstallStatus>(COMMANDS.getAppUpdateInstallStatus),
  consumeAppUpdateCompletion: () =>
    call<AppUpdateCompletionStatus | null>(COMMANDS.consumeAppUpdateCompletion),
  installAppUpdate: () => call<AppUpdateInstallResult>(COMMANDS.installAppUpdate),
  getStatus: () => call<AppStatus>(COMMANDS.getStatus),
  switchMode: (mode: string, autoSync: boolean, forceTakeover = false) =>
    call<AppStatus>(COMMANDS.switchMode, { mode, autoSync, forceTakeover, force_takeover: forceTakeover }),
  startProxy: () => call<AppStatus>(COMMANDS.startProxy),
  stopProxy: () => call<AppStatus>(COMMANDS.stopProxy),
  restartProxy: () => call<AppStatus>(COMMANDS.restartProxy),
  getProviders: () => call<Provider[]>(COMMANDS.getProviders),
  saveProviders: (providers: Provider[]) => call<Provider[]>(COMMANDS.saveProviders, { providers }),
  getSettings: async () => normalizeSettings(await call<Partial<Settings>>(COMMANDS.getSettings)),
  saveSettings: async (settings: Settings) =>
    normalizeSettings(
      await call<Partial<Settings>>(COMMANDS.saveSettings, {
        settings: normalizeSettings(settings),
      }),
    ),
  getCodexContextGuardStatus: () =>
    call<CodexContextGuardStatus>(COMMANDS.getCodexContextGuardStatus),
  setCodexContextGuard: (enabled: boolean) =>
    call<CodexContextGuardStatus>(COMMANDS.setCodexContextGuard, { enabled }),
  refreshOfficialModels: () => call<OfficialRefreshResult>(COMMANDS.refreshOfficialModels),
  openaiUsageCompletions: (window?: OpenAIUsageQueryWindow | null) =>
    call<OpenAIUsageSnapshot>(COMMANDS.openaiUsageCompletions, openaiUsageWindowArgs(window)),
  discoverProviderModels: (baseUrl: string, apiKey: string) =>
    call<Model[]>(COMMANDS.discoverProviderModels, { baseUrl, apiKey }),
  probeUpstreamFormat: (baseUrl: string, apiKey: string, model?: string | null) =>
    call<UpstreamFormatProbeResult>(COMMANDS.probeUpstreamFormat, {
      baseUrl,
      apiKey,
      model: model ?? null,
    }),
  providerProbeUpstreamFormat: (providerId: string, model?: string | null) =>
    call<UpstreamFormatProbeResult>(COMMANDS.providerProbeUpstreamFormat, {
      providerId,
      model: model ?? null,
    }),
  testModelEndpoint: (baseUrl: string, apiKey: string, model: string, upstreamFormat: UpstreamFormat) =>
    call<ModelEndpointTestResult>(COMMANDS.testModelEndpoint, {
      baseUrl,
      apiKey,
      model,
      upstreamFormat,
    }),
  gatewayStatus: () => call<GatewayStatus>(COMMANDS.gatewayStatus),
  diagnosticsStatus: () => call<DiagnosticsStatus>(COMMANDS.diagnosticsStatus),
  diagnosticsManualMark: () => call<DiagnosticsActionResult>(COMMANDS.diagnosticsManualMark),
  diagnosticsPause: () => call<DiagnosticsActionResult>(COMMANDS.diagnosticsPause),
  diagnosticsResume: () => call<DiagnosticsActionResult>(COMMANDS.diagnosticsResume),
  diagnosticsDeleteIncident: (incidentId: string) =>
    call<DiagnosticsActionResult>(COMMANDS.diagnosticsDeleteIncident, {
      incidentId,
      incident_id: incidentId,
    }),
  gatewayTestRequest: (kind: GatewayTestKind, model?: string | null) =>
    call<GatewayTestResult>(COMMANDS.gatewayTestRequest, { kind, model: model ?? null }),
  gatewayRecentEvents: (
    limitOrOptions: number | { limit?: number; sinceTs?: string | null } = 20,
  ) => {
    const args = typeof limitOrOptions === "number" ? { limit: limitOrOptions } : limitOrOptions;
    return call<GatewayEvent[]>(COMMANDS.gatewayRecentEvents, args);
  },
  gatewayUsageSnapshot: (window?: UsageQueryWindow | null) =>
    call<GatewayUsageSnapshot>(COMMANDS.gatewayUsageSnapshot, usageWindowArgs(window)),
  gatewayUsageSummary: (window?: UsageQueryWindow | null) =>
    call<GatewayUsageSummary>(COMMANDS.gatewayUsageSummary, usageWindowArgs(window)),
  gatewayUsageEvents: (
    limitOrWindow: number | UsageQueryWindow | null = 100,
    window?: UsageQueryWindow | null,
  ) => {
    const limit = typeof limitOrWindow === "number" ? limitOrWindow : null;
    const activeWindow = typeof limitOrWindow === "number" ? window : limitOrWindow;
    return call<GatewayUsageEvent[]>(COMMANDS.gatewayUsageEvents, {
      limit,
      ...usageWindowArgs(activeWindow),
    });
  },
  gatewayCopyClientConfig: (model?: string | null, clientKind = "zcode") =>
    call<GatewayClientConfig>(COMMANDS.gatewayCopyClientConfig, {
      clientKind,
      model: model ?? null,
    }),
  listGatewayClients: (includeVersions = false) =>
    call<GatewayClientInfo[]>(COMMANDS.listGatewayClients, {
      includeVersions,
      include_versions: includeVersions,
    }),
  dshClientConnect: () => call<DshLifecycleReport>(COMMANDS.dshClientConnect),
  dshClientDisconnect: () => call<DshLifecycleReport>(COMMANDS.dshClientDisconnect),
  dshClientReadback: () => call<DshLifecycleReport>(COMMANDS.dshClientReadback),
  previewGatewayClientConfig: (clientId: string, model?: string | null) =>
    call<GatewayClientConfigPreview>(COMMANDS.previewGatewayClientConfig, {
      clientId,
      model: model ?? null,
    }),
  applyGatewayClientConfig: (clientId: string, model?: string | null) =>
    call<GatewayClientApplyResult>(COMMANDS.applyGatewayClientConfig, {
      clientId,
      model: model ?? null,
    }),
  restoreGatewayClientConfig: (clientId: string) =>
    call<GatewayClientApplyResult>(COMMANDS.restoreGatewayClientConfig, { clientId }),
  switchGatewayClientRoute: (
    clientId: string,
    mode: RoutingOwner | "hub",
    model?: string | null,
    forceTakeover = false,
  ) =>
    call<GatewayClientApplyResult>(COMMANDS.switchGatewayClientRoute, {
      clientId,
      mode,
      model: model ?? null,
      forceTakeover,
      force_takeover: forceTakeover,
    }),
  syncGatewayClients: (model?: string | null) =>
    call<GatewayClientSyncSummary>(COMMANDS.syncGatewayClients, { model: model ?? null }),
  subagentMatrixStatus: () => call<SubagentMatrixStatus>(COMMANDS.subagentMatrixStatus),
  generateCatalog: () => call<Model[]>(COMMANDS.generateCatalog),
  catalogOverrideDiagnostics: () =>
    call<CatalogOverrideDiagnostics>(COMMANDS.getCatalogOverrideDiagnostics),
  listModels: () => call<Model[]>(COMMANDS.listModels),
  refreshModelMetadata: () => call<Model[]>(COMMANDS.refreshModelMetadata),
  listModelMetadata: () => call<Model[]>(COMMANDS.listModelMetadata),
  saveModelMetadataOverride: (model: Model) =>
    call<Model>(COMMANDS.saveModelMetadataOverride, { model }),
  saveOfficialMultiAgentVersion: (modelId: string, version: "v1" | "v2" | null) =>
    call<Model>(COMMANDS.saveOfficialMultiAgentVersion, {
      modelId,
      version,
    }),
  listOfficialMultiAgentOverrides: () =>
    call<Record<string, "v1" | "v2">>(COMMANDS.listOfficialMultiAgentOverrides),
  listOfficialMultiAgentBaselines: () =>
    call<Record<string, "v1" | "v2">>(COMMANDS.listOfficialMultiAgentBaselines),
  syncHistory: (targetProvider?: string) =>
    call<string>(COMMANDS.syncHistory, { targetProvider: targetProvider ?? null }),
  reconcileAfterRouteSwitch: (targetProvider?: string) =>
    call<UnifiedHistoryResult>(COMMANDS.reconcileAfterRouteSwitch, {
      targetProvider: targetProvider ?? null,
    }),
  migrateOfficialHistoryToUnified: () => call<string>(COMMANDS.migrateOfficialHistoryToUnified),
  restoreOfficialHistoryFromUnified: () => call<string>(COMMANDS.restoreOfficialHistoryFromUnified),
  preflightUnifiedHistory: (applyRepairs = false, targetUnified?: boolean) =>
    call<UnifiedHistoryResult>(COMMANDS.preflightUnifiedHistory, { applyRepairs, targetUnified }),
  getConversationSyncStatus: () =>
    call<UnifiedHistoryResult>(COMMANDS.getConversationSyncStatus),
  syncConversationHistory: (targetProvider?: string) =>
    call<UnifiedHistoryResult>(COMMANDS.syncConversationHistory, { targetProvider: targetProvider ?? null }),
  diagnoseConversationHistory: (fullScan = true) =>
    call<UnifiedHistoryResult>(COMMANDS.diagnoseConversationHistory, { fullScan }),
  syncCatalog: () => call<string>(COMMANDS.syncCatalog),
  setAutostart: (enabled: boolean) => call<string>(COMMANDS.setAutostart, { enabled }),
  removeAutostart: () => call<string>(COMMANDS.removeAutostart),
  getAutostartStatus: () => call<AutostartStatus>(COMMANDS.getAutostartStatus),
  openCodexApp: () => call<string>(COMMANDS.openCodexApp),
  windowMinimize: () => desktopCall<void>(COMMANDS.windowMinimize),
  windowToggleMaximize: () => desktopCall<void>(COMMANDS.windowToggleMaximize),
  windowCloseToTray: () => desktopCall<void>(COMMANDS.windowCloseToTray),
};

export function messageFromError(error: unknown): string {
  const message =
    error instanceof Error
      ? error.message
      : typeof error === "string"
        ? error
        : "Unexpected error";
  return isBackendDisconnectedMessage(message) ? "Backend is not connected" : message;
}

export function isBackendDisconnectedMessage(message: string): boolean {
  const lower = message.toLowerCase();
  return (
    lower.includes("backend is not connected") ||
    lower.includes("web bridge is not running") ||
    lower.includes("failed to fetch")
  );
}

export function isBackendDisconnectedError(error: unknown): boolean {
  if (error instanceof Error) {
    return isBackendDisconnectedMessage(error.message);
  }
  return typeof error === "string" ? isBackendDisconnectedMessage(error) : false;
}
