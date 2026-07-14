import { invoke } from "@tauri-apps/api/core";
import type {
  AppFlavorInfo,
  AppStatus,
  AppUpdateCompletionStatus,
  AppUpdateInstallResult,
  AppUpdateInstallStatus,
  AppUpdateStatus,
  AppVersionInfo,
  CodexContextGuardStatus,
  CodexHubError,
  DiagnosticsActionResult,
  DiagnosticsStatus,
  GatewayClientConfig,
  GatewayClientApplyResult,
  GatewayClientConfigPreview,
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

async function call<T>(command: string, args?: Record<string, unknown>): Promise<T> {
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

async function desktopCall<T>(command: string, args?: Record<string, unknown>): Promise<T | null> {
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
  getAppFlavor: () => call<AppFlavorInfo>("get_app_flavor"),
  getAppVersion: () => call<AppVersionInfo>("get_app_version"),
  checkAppUpdate: () => call<AppUpdateStatus>("check_app_update"),
  startAppUpdateInstall: () => call<AppUpdateInstallStatus>("start_app_update_install"),
  getAppUpdateInstallStatus: () => call<AppUpdateInstallStatus>("get_app_update_install_status"),
  consumeAppUpdateCompletion: () =>
    call<AppUpdateCompletionStatus | null>("consume_app_update_completion"),
  installAppUpdate: () => call<AppUpdateInstallResult>("install_app_update"),
  getStatus: () => call<AppStatus>("get_status"),
  switchMode: (mode: string, autoSync: boolean, forceTakeover = false) =>
    call<AppStatus>("switch_mode", { mode, autoSync, forceTakeover, force_takeover: forceTakeover }),
  startProxy: () => call<AppStatus>("start_proxy"),
  stopProxy: () => call<AppStatus>("stop_proxy"),
  restartProxy: () => call<AppStatus>("restart_proxy"),
  getProviders: () => call<Provider[]>("get_providers"),
  saveProviders: (providers: Provider[]) => call<Provider[]>("save_providers", { providers }),
  getSettings: async () => normalizeSettings(await call<Partial<Settings>>("get_settings")),
  saveSettings: async (settings: Settings) =>
    normalizeSettings(
      await call<Partial<Settings>>("save_settings", {
        settings: normalizeSettings(settings),
      }),
    ),
  getCodexContextGuardStatus: () =>
    call<CodexContextGuardStatus>("get_codex_context_guard_status"),
  setCodexContextGuard: (enabled: boolean) =>
    call<CodexContextGuardStatus>("set_codex_context_guard", { enabled }),
  refreshOfficialModels: () => call<Model[]>("refresh_official_models"),
  openaiUsageCompletions: (window?: OpenAIUsageQueryWindow | null) =>
    call<OpenAIUsageSnapshot>("openai_usage_completions", openaiUsageWindowArgs(window)),
  discoverProviderModels: (baseUrl: string, apiKey: string) =>
    call<Model[]>("discover_provider_models", { baseUrl, apiKey }),
  probeUpstreamFormat: (baseUrl: string, apiKey: string, model?: string | null) =>
    call<UpstreamFormatProbeResult>("probe_upstream_format", {
      baseUrl,
      apiKey,
      model: model ?? null,
    }),
  providerProbeUpstreamFormat: (providerId: string, model?: string | null) =>
    call<UpstreamFormatProbeResult>("provider_probe_upstream_format", {
      providerId,
      model: model ?? null,
    }),
  testModelEndpoint: (baseUrl: string, apiKey: string, model: string, upstreamFormat: UpstreamFormat) =>
    call<ModelEndpointTestResult>("test_model_endpoint", {
      baseUrl,
      apiKey,
      model,
      upstreamFormat,
    }),
  gatewayStatus: () => call<GatewayStatus>("gateway_status"),
  diagnosticsStatus: () => call<DiagnosticsStatus>("diagnostics_status"),
  diagnosticsManualMark: () => call<DiagnosticsActionResult>("diagnostics_manual_mark"),
  diagnosticsPause: () => call<DiagnosticsActionResult>("diagnostics_pause"),
  diagnosticsResume: () => call<DiagnosticsActionResult>("diagnostics_resume"),
  diagnosticsDeleteIncident: (incidentId: string) =>
    call<DiagnosticsActionResult>("diagnostics_delete_incident", {
      incidentId,
      incident_id: incidentId,
    }),
  gatewayTestRequest: (kind: GatewayTestKind, model?: string | null) =>
    call<GatewayTestResult>("gateway_test_request", { kind, model: model ?? null }),
  gatewayRecentEvents: (
    limitOrOptions: number | { limit?: number; sinceTs?: string | null } = 20,
  ) => {
    const args = typeof limitOrOptions === "number" ? { limit: limitOrOptions } : limitOrOptions;
    return call<GatewayEvent[]>("gateway_recent_events", args);
  },
  gatewayUsageSnapshot: (window?: UsageQueryWindow | null) =>
    call<GatewayUsageSnapshot>("gateway_usage_snapshot", usageWindowArgs(window)),
  gatewayUsageSummary: (window?: UsageQueryWindow | null) =>
    call<GatewayUsageSummary>("gateway_usage_summary", usageWindowArgs(window)),
  gatewayUsageEvents: (
    limitOrWindow: number | UsageQueryWindow | null = 100,
    window?: UsageQueryWindow | null,
  ) => {
    const limit = typeof limitOrWindow === "number" ? limitOrWindow : null;
    const activeWindow = typeof limitOrWindow === "number" ? window : limitOrWindow;
    return call<GatewayUsageEvent[]>("gateway_usage_events", {
      limit,
      ...usageWindowArgs(activeWindow),
    });
  },
  gatewayCopyClientConfig: (model?: string | null, clientKind = "zcode") =>
    call<GatewayClientConfig>("gateway_copy_client_config", {
      clientKind,
      model: model ?? null,
    }),
  listGatewayClients: (includeVersions = false) =>
    call<GatewayClientInfo[]>("list_gateway_clients", {
      includeVersions,
      include_versions: includeVersions,
    }),
  previewGatewayClientConfig: (clientId: string, model?: string | null) =>
    call<GatewayClientConfigPreview>("preview_gateway_client_config", {
      clientId,
      model: model ?? null,
    }),
  applyGatewayClientConfig: (clientId: string, model?: string | null) =>
    call<GatewayClientApplyResult>("apply_gateway_client_config", {
      clientId,
      model: model ?? null,
    }),
  restoreGatewayClientConfig: (clientId: string) =>
    call<GatewayClientApplyResult>("restore_gateway_client_config", { clientId }),
  switchGatewayClientRoute: (
    clientId: string,
    mode: RoutingOwner | "hub",
    model?: string | null,
    forceTakeover = false,
  ) =>
    call<GatewayClientApplyResult>("switch_gateway_client_route", {
      clientId,
      mode,
      model: model ?? null,
      forceTakeover,
      force_takeover: forceTakeover,
    }),
  syncGatewayClients: (model?: string | null) =>
    call<GatewayClientSyncSummary>("sync_gateway_clients", { model: model ?? null }),
  subagentMatrixStatus: () => call<SubagentMatrixStatus>("subagent_matrix_status"),
  generateCatalog: () => call<Model[]>("generate_catalog"),
  listModels: () => call<Model[]>("list_models"),
  refreshModelMetadata: () => call<Model[]>("refresh_model_metadata"),
  listModelMetadata: () => call<Model[]>("list_model_metadata"),
  saveModelMetadataOverride: (model: Model) =>
    call<Model>("save_model_metadata_override", { model }),
  syncHistory: (targetProvider?: string) =>
    call<string>("sync_history", { targetProvider: targetProvider ?? null }),
  reconcileAfterRouteSwitch: (targetProvider?: string) =>
    call<UnifiedHistoryResult>("reconcile_after_route_switch", {
      targetProvider: targetProvider ?? null,
    }),
  migrateOfficialHistoryToUnified: () => call<string>("migrate_official_history_to_unified"),
  restoreOfficialHistoryFromUnified: () => call<string>("restore_official_history_from_unified"),
  preflightUnifiedHistory: (applyRepairs = false, targetUnified?: boolean) =>
    call<UnifiedHistoryResult>("preflight_unified_history", { applyRepairs, targetUnified }),
  getConversationSyncStatus: () =>
    call<UnifiedHistoryResult>("get_conversation_sync_status"),
  syncConversationHistory: (targetProvider?: string) =>
    call<UnifiedHistoryResult>("sync_conversation_history", { targetProvider: targetProvider ?? null }),
  diagnoseConversationHistory: (fullScan = true) =>
    call<UnifiedHistoryResult>("diagnose_conversation_history", { fullScan }),
  syncCatalog: () => call<string>("sync_catalog"),
  setAutostart: (enabled: boolean) => call<string>("set_autostart", { enabled }),
  removeAutostart: () => call<string>("remove_autostart"),
  openCodexApp: () => call<string>("open_codex_app"),
  windowMinimize: () => desktopCall<void>("window_minimize"),
  windowToggleMaximize: () => desktopCall<void>("window_toggle_maximize"),
  windowCloseToTray: () => desktopCall<void>("window_close_to_tray"),
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
