import { invoke } from "@tauri-apps/api/core";
import type {
  AppStatus,
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
  Provider,
  Settings,
  SubagentMatrixStatus,
  UpstreamFormatProbeResult,
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
}

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
  const response = await fetch(bridgeUrl(), {
    method: "POST",
    body: JSON.stringify({ command, args: args ?? {} }),
  }).catch((error: unknown) => {
    console.debug("CodexHub web bridge unavailable", error);
    throw new Error("Backend is not connected");
  });

  const payload = (await response.json().catch(() => null)) as BridgeResponse<T> | null;
  if (!response.ok || !payload?.ok) {
    throw new Error(payload?.error || `CodexHub web bridge request failed: HTTP ${response.status}`);
  }
  return payload.value as T;
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
  return import.meta.env.VITE_CODEXHUB_BRIDGE_URL || "http://127.0.0.1:1421/api/invoke";
}

function usageWindowArgs(window?: UsageQueryWindow | null) {
  return {
    startTs: window?.startTs ?? null,
    endTs: window?.endTs ?? null,
  };
}

export const api = {
  getStatus: () => call<AppStatus>("get_status"),
  switchMode: (mode: string, autoSync: boolean) =>
    call<AppStatus>("switch_mode", { mode, autoSync }),
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
  refreshOfficialModels: () => call<Model[]>("refresh_official_models"),
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
  gatewayStatus: () => call<GatewayStatus>("gateway_status"),
  gatewayTestRequest: (kind: GatewayTestKind, model?: string | null) =>
    call<GatewayTestResult>("gateway_test_request", { kind, model: model ?? null }),
  gatewayRecentEvents: (limit = 20) => call<GatewayEvent[]>("gateway_recent_events", { limit }),
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
  switchGatewayClientRoute: (clientId: string, mode: string, model?: string | null) =>
    call<GatewayClientApplyResult>("switch_gateway_client_route", {
      clientId,
      mode,
      model: model ?? null,
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
  migrateOfficialHistoryToUnified: () => call<string>("migrate_official_history_to_unified"),
  restoreOfficialHistoryFromUnified: () => call<string>("restore_official_history_from_unified"),
  syncCatalog: () => call<string>("sync_catalog"),
  setAutostart: (enabled: boolean) => call<string>("set_autostart", { enabled }),
  removeAutostart: () => call<string>("remove_autostart"),
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
