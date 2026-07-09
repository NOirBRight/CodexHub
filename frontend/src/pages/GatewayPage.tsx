import { Activity, Check, CheckCircle2, Copy, Eye, EyeOff, ListChecks, RefreshCcw, Save, Server, X } from "lucide-react";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { EndpointRow } from "../components/EndpointRow";
import { GatewayClientCard } from "../components/GatewayClientCard";
import { BACKEND_DISCONNECTED_TOAST_KEY, useToasts } from "../components/PageToast";
import { PendingPanel } from "../components/PendingPanel";
import { SwitchControl } from "../components/SettingsDrawer";
import { StackedUsageChartShell } from "../components/StackedUsageChartShell";
import { cx } from "../lib/format";
import { api, isBackendDisconnectedMessage, messageFromError } from "../lib/tauri";
import type {
  AppFlavorInfo,
  GatewayClientContract,
  GatewayClientInfo,
  GatewayEvent,
  GatewayStatus,
  GatewayUsageEvent,
  GatewayUsageSummary,
  Provider,
  RoutingOwner,
  Settings,
  TelemetryStatus,
  UsageQueryWindow,
} from "../lib/types";

type RouteAction = "official" | "current_owner" | "takeover";

interface GatewayPageProps {
  appFlavor?: AppFlavorInfo | null;
  busy?: string | null;
  clients: GatewayClientContract[];
  pending?: {
    label?: string;
    usage?: string;
    clients?: string;
    models?: string;
  };
  providers: Provider[];
  settings: Settings | null;
  status: GatewayStatus | null;
  usageEvents: GatewayUsageEvent[];
  usageError: string | null;
  usageSummary: GatewayUsageSummary | null;
  usageStatus: TelemetryStatus | null;
  recentEvents: GatewayEvent[];
  clientInfos: GatewayClientInfo[];
  onApplySettings: (settings: Settings) => Promise<void>;
  onRefreshClients: (options?: { includeClientVersions?: boolean }) => Promise<void>;
  onRestartProxy: () => Promise<void>;
  onStartProxy: () => Promise<void>;
  onStopProxy: () => Promise<void>;
  onUsageWindowChange: (window: UsageQueryWindow) => void;
}

function isActionableDiagnostic(item: GatewayStatus["diagnostics"][number]) {
  return item.level !== "ok" && item.level !== "status" && item.category !== "proxy_state";
}

function GatewayPageImpl({
  appFlavor,
  busy,
  clients,
  onApplySettings,
  onRefreshClients,
  onRestartProxy,
  onStartProxy,
  onStopProxy,
  onUsageWindowChange,
  pending,
  providers,
  settings,
  status,
  usageEvents,
  usageError,
  usageSummary,
  usageStatus,
  recentEvents,
  clientInfos,
}: GatewayPageProps) {
  const { t } = useTranslation();
  const { showToast, updateToast } = useToasts();
  const [draftPort, setDraftPort] = useState(settings?.proxy_port ?? status?.port ?? 9099);
  const [draftKey, setDraftKey] = useState(settings?.gateway_client_key ?? "");
  const [draftTimeout, setDraftTimeout] = useState(settings?.gateway_request_timeout_seconds ?? 300);
  const [clientBusy, setClientBusy] = useState<string | null>(null);
  const [clientRefreshBusy, setClientRefreshBusy] = useState(false);
  const [showDraftKey, setShowDraftKey] = useState(false);
  const [copiedTarget, setCopiedTarget] = useState<string | null>(null);
  const [autoRetryBusy, setAutoRetryBusy] = useState(false);
  const copyResetTimer = useRef<number | null>(null);
  const lastUsageErrorToast = useRef<string | null>(null);
  const running = status?.proxy_running ?? false;

  useEffect(() => {
    setDraftPort(settings?.proxy_port ?? status?.port ?? 9099);
    setDraftKey(settings?.gateway_client_key ?? "");
    setDraftTimeout(settings?.gateway_request_timeout_seconds ?? 300);
  }, [settings, status?.port]);

  useEffect(
    () => () => {
      if (copyResetTimer.current !== null) {
        window.clearTimeout(copyResetTimer.current);
      }
    },
    [],
  );

  useEffect(() => {
    if (!usageError) {
      lastUsageErrorToast.current = null;
      return;
    }
    if (!running && isBackendDisconnectedMessage(usageError)) {
      return;
    }
    const text = isBackendDisconnectedMessage(usageError)
      ? t("gateway.backendNotConnected")
      : t("gateway.usageTelemetryDelayed", { message: usageError });
    if (lastUsageErrorToast.current === text) {
      return;
    }
    lastUsageErrorToast.current = text;
    if (isBackendDisconnectedMessage(usageError)) {
      showBackendDisconnectedToast();
      return;
    }
    showToast(text, "error");
  }, [running, usageError, showToast, t]);

  const endpoints = useMemo(
    () =>
      status
        ? [
            { label: t("gateway.modelsEndpoint"), meta: "GET /v1/models", value: status.endpoints.models },
            { label: t("gateway.completions"), meta: "POST /v1/chat/completions", value: status.endpoints.chat_completions },
            { label: t("gateway.responses"), meta: "POST /v1/responses", value: status.endpoints.responses },
          ]
        : [],
    [status, t],
  );
  const defaultModel = status?.official_models[0]?.id ?? null;
  const runtimeOwner = appFlavor?.routing_owner ?? null;
  const clientInfoById = useMemo(
    () => new Map(clientInfos.map((client) => [client.id, client])),
    [clientInfos],
  );
  const currentAppGatewayUrl = currentAppGatewayEndpoint(appFlavor);

  function markCopied(target: string) {
    setCopiedTarget(target);
    if (copyResetTimer.current !== null) {
      window.clearTimeout(copyResetTimer.current);
    }
    copyResetTimer.current = window.setTimeout(() => {
      setCopiedTarget((current) => (current === target ? null : current));
      copyResetTimer.current = null;
    }, 1200);
  }

  function setMessage(value: string | null) {
    if (value) {
      showToast(value, "message");
    }
  }

  function setError(value: string | null) {
    if (value) {
      if (isBackendDisconnectedMessage(value)) {
        showBackendDisconnectedToast();
        return;
      }
      showToast(value, "error");
    }
  }

  function showBackendDisconnectedToast() {
    let toastId = "";
    toastId = showToast({
      dedupeKey: BACKEND_DISCONNECTED_TOAST_KEY,
      text: t("gateway.backendNotConnected"),
      tone: "error",
      action: {
        label: t("gateway.startBackend"),
        onClick: () => void startBackendFromToast(toastId),
      },
    });
  }

  function updateToastWithError(toastId: string, err: unknown) {
    const text = messageFromError(err);
    if (isBackendDisconnectedMessage(text)) {
      updateToast(toastId, {
        action: {
          label: t("gateway.startBackend"),
          onClick: () => void startBackendFromToast(toastId),
        },
        text: t("gateway.backendNotConnected"),
        tone: "error",
      });
      return;
    }
    updateToast(toastId, {
      action: null,
      text,
      tone: "error",
    });
  }

  async function startBackendFromToast(toastId?: string) {
    setClientRefreshBusy(true);
    const activeToastId = toastId ?? showToast(t("gateway.startingBackend"), "loading");
    updateToast(activeToastId, {
      action: null,
      text: t("gateway.startingBackend"),
      tone: "loading",
    });
    try {
      await api.startProxy();
      await onRefreshClients();
      updateToast(activeToastId, {
        action: null,
        text: t("gateway.backendStarted"),
        tone: "success",
      });
    } catch (err) {
      updateToastWithError(activeToastId, err);
    } finally {
      setClientRefreshBusy(false);
    }
  }

  async function copyText(target: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      markCopied(target);
      setMessage(null);
      setError(null);
    } catch (err) {
      setError(t("gateway.copyFailed", { message: messageFromError(err) }));
    }
  }

  async function applyGatewaySettings() {
    if (!settings) {
      setError(t("common.loadingSettings"));
      return;
    }
    const cleanPort = Number.isFinite(draftPort) ? draftPort : settings.proxy_port;
    const cleanTimeout = Number.isFinite(draftTimeout)
      ? draftTimeout
      : settings.gateway_request_timeout_seconds;
    const next = {
      ...settings,
      gateway_client_key: draftKey,
      proxy_port: Math.min(65535, Math.max(1024, cleanPort)),
      gateway_request_timeout_seconds: Math.min(600, Math.max(5, cleanTimeout)),
    };
    const portChanged = next.proxy_port !== settings.proxy_port;
    const timeoutChanged = next.gateway_request_timeout_seconds !== settings.gateway_request_timeout_seconds;
    const keyChanged = next.gateway_client_key !== settings.gateway_client_key;
    const restartRequired = running && (portChanged || timeoutChanged);
    const toastId = showToast(
      restartRequired ? t("gateway.saveRestarting") : t("gateway.savingSettings"),
      "loading",
    );

    try {
      await onApplySettings(next);
      if (restartRequired) {
        await onRestartProxy();
      }
      const message = restartRequired
        ? t("gateway.gatewaySettingsSavedRestarted")
        : keyChanged && !portChanged && !timeoutChanged
          ? t("gateway.apiKeySavedNoRestart")
          : t("gateway.gatewaySettingsSaved");
      updateToast(toastId, {
        action: null,
        text: message,
        tone: "success",
      });
      setError(null);
    } catch (err) {
      updateToastWithError(toastId, err);
    }
  }

  function regenerateClientKey() {
    const bytes = new Uint8Array(18);
    window.crypto.getRandomValues(bytes);
    const token = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    setDraftKey(`codexhub-${token}`);
    setShowDraftKey(false);
    setMessage(t("gateway.newApiKeyGenerated"));
    setError(null);
  }

  async function switchClientMode(clientId: string, owner: RoutingOwner, forceTakeover = false) {
    setClientBusy(`${clientId}:switch:${owner}`);
    const clientName =
      clientInfoById.get(clientId)?.name ?? clients.find((client) => client.id === clientId)?.name ?? clientId;
    const client = clientInfoById.get(clientId);
    if (!forceTakeover && client && client.managed_by_current_app === false) {
      if (!runtimeOwner) {
        setClientBusy(null);
        showToast(t("gateway.ownerUnavailable"), "error");
        return;
      }
      const confirmText = t("gateway.takeoverConfirm", {
        name: client.name,
        path: client.config_path ?? t("common.unknown"),
        current: ownerDisplayName(client.route_owner, t),
        next: ownerDisplayName(runtimeOwner, t),
        oldEndpoint: client.route_endpoint ?? t("common.unknown"),
        newEndpoint: currentAppGatewayUrl ?? t("common.unknown"),
      });
      if (!window.confirm(confirmText)) {
        setClientBusy(null);
        return;
      }
      forceTakeover = true;
      owner = runtimeOwner;
    }
    const routeName = ownerDisplayName(owner, t);
    const toastId = showToast(t("gateway.switchClient", { clientName, routeName }), "loading");
    try {
      await api.switchGatewayClientRoute(clientId, owner, defaultModel, forceTakeover);
      await onRefreshClients();
      updateToast(toastId, {
        action: null,
        text: t("gateway.switchClientDone", { clientName, routeName }),
        tone: "success",
      });
      setError(null);
    } catch (err) {
      updateToastWithError(toastId, err);
    } finally {
      setClientBusy(null);
    }
  }

  async function refreshGatewayClients() {
    setClientRefreshBusy(true);
    const toastId = showToast(t("gateway.refreshingClients"), "loading");
    try {
      await onRefreshClients({ includeClientVersions: true });
      setClientBusy(null);
      updateToast(toastId, {
        action: null,
        text: t("gateway.clientsRefreshed"),
        tone: "success",
      });
      setError(null);
    } catch (err) {
      updateToastWithError(toastId, err);
    } finally {
      setClientRefreshBusy(false);
    }
  }

  async function toggleGatewayAutoRetry(enabled: boolean) {
    if (!settings || autoRetryBusy) {
      return;
    }
    setAutoRetryBusy(true);
    const toastId = showToast(
      enabled ? t("gateway.enablingAutoRetry") : t("gateway.disablingAutoRetry"),
      "loading",
    );
    try {
      await onApplySettings({
        ...settings,
        gateway_auto_retry_enabled: enabled,
      });
      updateToast(toastId, {
        action: null,
        text: enabled ? t("gateway.autoRetryEnabled") : t("gateway.autoRetryDisabled"),
        tone: "success",
      });
      setError(null);
    } catch (err) {
      updateToastWithError(toastId, err);
    } finally {
      setAutoRetryBusy(false);
    }
  }

  const actionableDiagnostics = status?.diagnostics.filter(isActionableDiagnostic) ?? [];
  const runtimeActionBusy = busy === "start" || busy === "stop" || busy === "restart";
  const apiKeyCopied = copiedTarget === "gateway-api-key";

  function handleRouteAction(clientId: string, action: RouteAction) {
    if (!runtimeOwner) {
      showToast(t("gateway.ownerUnavailable"), "error");
      return;
    }
    if (action === "official") {
      return void switchClientMode(clientId, "official");
    }
    return void switchClientMode(clientId, runtimeOwner);
  }

  async function toggleRuntime() {
    const toastId = showToast(
      running ? t("runtime.stoppingRuntime") : t("runtime.startingRuntime"),
      "loading",
    );
    if (running) {
      try {
        await onStopProxy();
        updateToast(toastId, {
          action: null,
          text: t("runtime.runtimeStopped"),
          tone: "success",
        });
      } catch (err) {
        updateToastWithError(toastId, err);
      }
      return;
    }
    try {
      await onStartProxy();
      updateToast(toastId, {
        action: null,
        text: t("runtime.runtimeStarted"),
        tone: "success",
      });
    } catch (err) {
      updateToastWithError(toastId, err);
    }
  }

  return (
    <main className="relative grid h-full min-h-[704px] w-full max-w-full min-w-0 grid-cols-[minmax(0,1fr)_minmax(300px,340px)] gap-4 overflow-hidden">
      <section className="grid min-h-0 min-w-0 grid-rows-[auto_auto_minmax(320px,1fr)] gap-2.5">
        <section className="grid min-w-0 gap-2 overflow-hidden rounded-panel bg-surface p-2.5 shadow-card">
          <div className="flex min-w-0 items-center justify-between gap-3">
            <h2 className="flex min-w-0 items-center gap-2 text-sm font-semibold text-ink">
              <Server size={15} className="shrink-0 text-action" />
              <span className="truncate">{t("gateway.gateway")}</span>
            </h2>
            <label className="flex h-7 items-center gap-2 rounded-control bg-panel px-2 text-[11px] font-semibold text-slate-600 shadow-control">
              <span>{running ? t("runtime.running") : t("runtime.stopped")}</span>
              <SwitchControl
                ariaLabel={running ? t("runtime.stopRuntime") : t("runtime.startRuntime")}
                checked={running}
                disabled={runtimeActionBusy || busy === "load"}
                onChange={(enabled) => {
                  if (enabled !== running) {
                    void toggleRuntime();
                  }
                }}
              />
            </label>
          </div>

          <div className="grid min-w-0 grid-cols-[minmax(300px,1fr)_minmax(270px,0.95fr)] items-stretch gap-2">
            <div className="grid min-w-0 content-start rounded-panel bg-panel p-2 shadow-card">
              <div className="grid min-w-0 content-start gap-1.5 rounded-inner bg-surface p-2 shadow-control">
                <label className="grid gap-1 text-xs font-semibold text-slate-600">
                  <span>{t("common.apiKey")}</span>
                  <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-2">
                    <div className="relative min-w-0">
                      <input
                        className="field field-compact pr-9"
                        type={showDraftKey ? "text" : "password"}
                        autoComplete="off"
                        value={draftKey}
                        placeholder={t("gateway.empty")}
                        onChange={(event) => setDraftKey(event.target.value)}
                      />
                      <button
                        type="button"
                        className="focus-ring absolute right-1.5 top-1/2 grid h-6 w-6 -translate-y-1/2 place-items-center rounded-control text-slate-500 transition-colors hover:bg-panel hover:text-ink"
                        aria-label={showDraftKey ? t("common.hideApiKey") : t("common.showApiKey")}
                        onClick={() => setShowDraftKey((show) => !show)}
                      >
                        {showDraftKey ? <EyeOff size={15} /> : <Eye size={15} />}
                      </button>
                    </div>
                    <button
                      type="button"
                      className="focus-ring inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-control bg-panel text-slate-700 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
                      disabled={!draftKey}
                      aria-label={apiKeyCopied ? t("gateway.apiKeyCopied") : t("gateway.copyApiKey")}
                      title={apiKeyCopied ? t("common.copied") : t("gateway.copyApiKey")}
                      onClick={() => void copyText("gateway-api-key", draftKey)}
                    >
                      {apiKeyCopied ? <Check size={14} /> : <Copy size={14} />}
                    </button>
                    <button
                      type="button"
                      className="focus-ring inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-control bg-panel text-slate-700 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
                      aria-label={t("gateway.regenerateApiKey")}
                      title={t("gateway.regenerateApiKey")}
                      onClick={regenerateClientKey}
                    >
                      <RefreshCcw size={14} />
                    </button>
                  </div>
                </label>
                <div className="grid min-w-0 grid-cols-[minmax(64px,0.75fr)_minmax(64px,0.75fr)_minmax(112px,0.9fr)] items-end gap-1.5">
                  <label className="grid min-w-0 gap-1 text-xs font-semibold text-slate-600">
                    <span>{t("common.port")}</span>
                    <input
                      className="field field-compact"
                      type="number"
                      min={1024}
                      max={65535}
                      value={draftPort}
                      onChange={(event) => setDraftPort(Number(event.target.value))}
                    />
                  </label>
                  <label className="grid min-w-0 gap-1 text-xs font-semibold text-slate-600">
                    <span>{t("common.timeout")}</span>
                    <input
                      className="field field-compact"
                      type="number"
                      min={5}
                      max={600}
                      value={draftTimeout}
                      onChange={(event) => setDraftTimeout(Number(event.target.value))}
                    />
                  </label>
                  <button
                    type="button"
                    className="focus-ring inline-flex h-9 self-end items-center justify-center gap-1.5 whitespace-nowrap rounded-control bg-ink px-2 text-[11px] font-semibold text-white shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-slate-800 hover:shadow-raised active:scale-[0.96] disabled:bg-slate-300"
                    disabled={Boolean(busy) || !settings}
                    onClick={() => void applyGatewaySettings()}
                  >
                    <Save size={14} />
                    {t("common.applySettings")}
                  </button>
                </div>
              </div>
            </div>

            <div className="grid min-w-0 grid-rows-[auto_minmax(0,1fr)] gap-1.5 rounded-panel bg-panel p-2 pb-2.5 shadow-card">
              <div className="flex items-center justify-between gap-3 whitespace-nowrap">
                <h3 className="shrink-0 text-xs font-semibold text-ink">{t("gateway.copyConnection")}</h3>
              </div>
              {endpoints.length > 0 ? (
                <div className="grid min-h-[118px] grid-rows-3 gap-1.5">
                  {endpoints.map((endpoint) => {
                    const copyTarget = `endpoint:${endpoint.label}`;
                    return (
                      <EndpointRow
                        key={endpoint.label}
                        compact
                        copied={copiedTarget === copyTarget}
                        label={endpoint.label}
                        meta={endpoint.meta}
                        value={endpoint.value}
                        onCopy={() => void copyText(copyTarget, endpoint.value)}
                      />
                    );
                  })}
                </div>
              ) : (
                <PendingPanel
                  compact
                  title={t("gateway.gatewayStatus")}
                  message={t("gateway.runtimeStatusLoading")}
                />
              )}
            </div>
          </div>

          {actionableDiagnostics.length ? (
            <div className="flex flex-wrap gap-1.5 border-t border-line pt-2">
              {actionableDiagnostics.map((item) => (
                <div
                  key={`${item.category}-${item.message}`}
                  className={cx(
                    "rounded-inner px-2 py-1 text-xs shadow-control",
                    item.level === "error"
                      ? "bg-red-50 text-danger"
                      : item.level === "warning"
                        ? "bg-amber-50 text-warn"
                        : "bg-emerald-50 text-ok",
                  )}
                >
                  {item.message}
                </div>
              ))}
            </div>
          ) : null}
        </section>

        <RecoveryActivityPanel
          enabled={Boolean(settings?.gateway_auto_retry_enabled)}
          disabled={Boolean(busy) || autoRetryBusy || !settings}
          events={recentEvents}
          onToggle={(enabled) => void toggleGatewayAutoRetry(enabled)}
        />

        <StackedUsageChartShell
          events={usageEvents}
          onWindowChange={onUsageWindowChange}
          pendingMessage={pending?.usage ?? t("gateway.pendingUsage")}
          providers={providers}
          summary={usageSummary}
          telemetryStatus={usageStatus}
        />

      </section>

      <aside className="grid h-full min-h-[704px] min-w-0 grid-rows-[auto_minmax(0,1fr)] overflow-hidden rounded-panel bg-surface shadow-card">
        <div className="p-3 shadow-hairline">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-ink">{t("gateway.clientRouting")}</h2>
            <button
              type="button"
              className="focus-ring inline-flex h-7 w-7 items-center justify-center rounded-control bg-panel text-slate-600 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96] disabled:text-slate-300"
              disabled={clientRefreshBusy}
              aria-label={t("gateway.refreshClients")}
              title={t("gateway.refreshClientsTitle")}
              onClick={() => void refreshGatewayClients()}
            >
              <RefreshCcw size={14} className={clientRefreshBusy ? "animate-spin" : undefined} />
            </button>
          </div>
        </div>
        <div
          className={cx(
            "bg-panel p-3",
            clients.length > 4 ? "min-h-0 overflow-auto" : "overflow-visible",
          )}
        >
          <div
            className={cx(
              "grid gap-2",
              clients.length > 4 ? "auto-rows-[minmax(144px,auto)]" : "min-h-full auto-rows-fr",
            )}
          >
            {clients.map((client) => (
              (() => {
                const info = clientInfoById.get(client.id);
                return (
                  <GatewayClientCard
                    key={client.id}
                    client={client}
                    info={info}
                    busy={Boolean(clientBusy?.startsWith(client.id))}
                    busyMode={
                      clientBusy === `${client.id}:switch:official`
                        ? "official"
                        : clientBusy === `${client.id}:switch:${runtimeOwner}`
                          ? info?.managed_by_current_app === false
                            ? "takeover"
                            : "current_owner"
                          : null
                    }
                    runtimeOwner={runtimeOwner}
                    onSwitchMode={(mode) => handleRouteAction(client.id, mode)}
                  />
                );
              })()
            ))}
          </div>
        </div>
      </aside>
    </main>
  );
}

export const GatewayPage = memo(GatewayPageImpl);

interface RecoverySummary {
  activeCount: number;
  activeEvents: GatewayEvent[];
  failedCount: number;
  latestEvents: GatewayEvent[];
  recoveredCount: number;
  retryCount: number;
}

const RECOVERY_TERMINAL_EVENTS = new Set(["request_complete", "request_error"]);
const ACTIVE_RECOVERY_GRACE_MS = 15 * 1000;
const RECOVERY_OVERVIEW_HOURS = 24;
const RECOVERY_OVERVIEW_LIMIT = 5_000;
const RECOVERY_OVERVIEW_PAGE_SIZE = 50;

function RecoveryActivityPanel({
  disabled,
  enabled,
  events,
  onToggle,
}: {
  disabled?: boolean;
  enabled: boolean;
  events: GatewayEvent[];
  onToggle: (enabled: boolean) => void;
}) {
  const { t } = useTranslation();
  const [overviewOpen, setOverviewOpen] = useState(false);
  const [overviewEvents, setOverviewEvents] = useState<GatewayEvent[]>([]);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [overviewPage, setOverviewPage] = useState(0);
  const summary = useMemo(() => summarizeRecoveryEvents(events), [events]);
  const active = enabled && summary.activeCount > 0;
  const latestEvent = summary.activeEvents[0] ?? summary.latestEvents[0] ?? null;

  async function openOverview() {
    setOverviewOpen(true);
    setOverviewLoading(true);
    setOverviewError(null);
    setOverviewPage(0);
    try {
      const sinceTs = new Date(Date.now() - RECOVERY_OVERVIEW_HOURS * 60 * 60 * 1000).toISOString();
      const recent = await api.gatewayRecentEvents({
        limit: RECOVERY_OVERVIEW_LIMIT,
        sinceTs,
      });
      setOverviewEvents(sortRecoveryRetryEvents(recent));
    } catch (err) {
      setOverviewError(messageFromError(err));
      setOverviewEvents(sortRecoveryRetryEvents(events));
    } finally {
      setOverviewLoading(false);
    }
  }

  return (
    <section className="grid min-w-0 gap-1.5 rounded-panel bg-surface px-2.5 py-2 shadow-card">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="flex min-w-0 items-center gap-2 text-sm font-semibold text-ink">
            <Activity size={15} className="shrink-0 text-action" />
            <span className="truncate">{t("gateway.recoveryActivity")}</span>
          </h3>
          <p className="mt-0.5 truncate text-[11px] text-slate-500">
            {enabled ? t("gateway.recoveryActivitySubtitle") : t("gateway.recoveryDisabled")}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span
            className={cx(
              "rounded-control px-2 py-1 text-[11px] font-semibold",
              !enabled
                ? "bg-slate-100 text-slate-500"
                : active
                  ? "bg-emerald-50 text-ok"
                  : "bg-slate-100 text-slate-500",
            )}
          >
            {!enabled ? t("gateway.autoRetryOff") : active ? t("gateway.recoveryActive") : t("gateway.recoveryIdle")}
          </span>
          <label className="flex h-7 items-center gap-2 rounded-control bg-panel px-2 text-[11px] font-semibold text-slate-600 shadow-control">
            <span>{t("settings.autoRetry")}</span>
            <SwitchControl
              ariaLabel={t("settings.autoRetry")}
              checked={enabled}
              disabled={disabled}
              onChange={onToggle}
            />
          </label>
        </div>
      </div>

      <div
        className={cx(
          "grid min-w-0 grid-cols-[repeat(3,minmax(0,0.72fr))_minmax(210px,1.7fr)] gap-1.5",
          !enabled && "opacity-45 grayscale",
        )}
      >
        <RecoveryMetric label={t("gateway.recoveryAttempts")} value={summary.retryCount} />
        <RecoveryMetric label={t("gateway.recoveredRequests")} value={summary.recoveredCount} />
        <RecoveryMetric label={t("gateway.failedHandoffs")} value={summary.failedCount} />
        <RecoveryEventRow
          active={summary.activeEvents[0] === latestEvent}
          event={latestEvent}
          onOverview={() => void openOverview()}
        />
      </div>

      {overviewOpen ? (
        <RecoveryOverviewModal
          error={overviewError}
          events={overviewEvents}
          loading={overviewLoading}
          onClose={() => setOverviewOpen(false)}
          onPageChange={setOverviewPage}
          page={overviewPage}
        />
      ) : null}
    </section>
  );
}

function RecoveryMetric({ label, value }: { label: string; value: number }) {
  return (
    <div className="grid min-h-[40px] min-w-0 content-center rounded-inner bg-panel px-2.5 py-1.5 shadow-control">
      <span className="truncate text-[11px] font-semibold text-slate-500">{label}</span>
      <span className="tabular-nums text-sm font-semibold text-ink">{value}</span>
    </div>
  );
}

function RecoveryEventRow({
  active,
  event,
  onOverview,
}: {
  active: boolean;
  event: GatewayEvent | null;
  onOverview: () => void;
}) {
  const { t } = useTranslation();
  const provider = event ? recoveryProviderLabel(event, t("usage.unknownProvider")) : null;
  const client = event ? formatRecoveryClient(event.client_id) : null;
  const attemptText = event ? formatAttemptCell(event) : "-";
  const delay = event ? formatDelay(event.delay_ms) : null;
  const retryText = [attemptText === "-" ? null : attemptText, delay].filter(Boolean).join(" · ");
  const routeText = event ? (client ? `${client} → ${provider}` : provider) : t("gateway.recoveryEmpty");

  return (
    <div
      className="grid min-h-[40px] min-w-0 grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 rounded-inner bg-panel px-2.5 py-1.5 text-[11px] shadow-control"
      title={event ? recoveryEventTitle(event) : t("gateway.recoveryOverviewTitle")}
    >
      <CheckCircle2 size={13} className={cx("shrink-0", active ? "text-ok" : "text-slate-400")} />
      <div className="flex min-w-0 items-center gap-1.5">
        {event ? (
          <span className="shrink-0 rounded-control bg-slate-100 px-1.5 py-0.5 font-semibold tabular-nums text-slate-600">
            {retryText || t("gateway.recoveryAttemptUnknown")}
          </span>
        ) : null}
        <span className="min-w-0 truncate font-semibold text-ink">{routeText}</span>
      </div>
      <button
        type="button"
        aria-label={t("gateway.recoveryOverviewTitle")}
        className="focus-ring grid h-7 w-7 shrink-0 place-items-center rounded-control bg-surface text-slate-600 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
        onClick={onOverview}
        title={t("gateway.recoveryOverviewTitle")}
      >
        <ListChecks size={13} />
      </button>
    </div>
  );
}

function RecoveryOverviewModal({
  error,
  events,
  loading,
  onClose,
  onPageChange,
  page,
}: {
  error: string | null;
  events: GatewayEvent[];
  loading: boolean;
  onClose: () => void;
  onPageChange: (page: number) => void;
  page: number;
}) {
  const { t } = useTranslation();
  const pageCount = Math.max(1, Math.ceil(events.length / RECOVERY_OVERVIEW_PAGE_SIZE));
  const safePage = Math.min(Math.max(page, 0), pageCount - 1);
  const pageStart = safePage * RECOVERY_OVERVIEW_PAGE_SIZE;
  const pageEvents = events.slice(pageStart, pageStart + RECOVERY_OVERVIEW_PAGE_SIZE);
  const pageFrom = events.length === 0 ? 0 : pageStart + 1;
  const pageTo = pageStart + pageEvents.length;
  return (
    <div className="fixed inset-0 z-[80] grid place-items-center bg-black/20 px-4 py-6">
      <section
        aria-labelledby="gateway-recovery-overview-title"
        aria-modal="true"
        className="grid max-h-full w-full max-w-[1120px] grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden rounded-overlay bg-surface shadow-overlay"
        role="dialog"
      >
        <div className="flex min-w-0 items-start justify-between gap-3 px-4 py-3 shadow-hairline">
          <div className="min-w-0">
            <h2 id="gateway-recovery-overview-title" className="flex min-w-0 items-center gap-2 text-base font-semibold text-ink">
              <ListChecks size={16} className="shrink-0 text-action" />
              <span className="truncate">{t("gateway.recoveryOverviewTitle")}</span>
            </h2>
            <p className="mt-0.5 truncate text-xs text-slate-500">
              {t("gateway.recoveryOverviewSubtitle", { count: events.length, hours: RECOVERY_OVERVIEW_HOURS })}
            </p>
            {error ? <p className="mt-1 truncate text-xs text-danger">{error}</p> : null}
          </div>
          <button
            type="button"
            className="focus-ring grid h-8 w-8 shrink-0 place-items-center rounded-control bg-panel text-slate-600 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
            aria-label={t("common.close")}
            onClick={onClose}
          >
            <X size={15} />
          </button>
        </div>

        {loading ? (
          <div className="p-4 text-sm text-slate-500">{t("gateway.recoveryOverviewLoading")}</div>
        ) : events.length === 0 ? (
          <div className="p-4 text-sm text-slate-500">{t("gateway.recoveryEmpty")}</div>
        ) : (
          <div className="min-h-0 overflow-auto p-3">
            <div className="min-w-[980px] overflow-hidden rounded-panel border border-line">
              <div className="sticky top-0 z-10 grid grid-cols-[86px_92px_112px_142px_70px_62px_116px_60px_minmax(0,1fr)] bg-panel px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.04em] text-slate-500">
                <span>{t("gateway.recoveryColumnTime")}</span>
                <span>{t("gateway.recoveryColumnClient")}</span>
                <span>{t("gateway.recoveryColumnProvider")}</span>
                <span>{t("gateway.recoveryColumnModel")}</span>
                <span>{t("gateway.recoveryColumnAttempt")}</span>
                <span>{t("gateway.recoveryColumnDelay")}</span>
                <span>{t("gateway.recoveryColumnClass")}</span>
                <span>{t("gateway.recoveryColumnStatus")}</span>
                <span>{t("gateway.recoveryColumnRequest")}</span>
              </div>
              {pageEvents.map((event, index) => (
                <div
                  key={`${event.ts ?? "retry"}-${event.request_id ?? index}-${index}`}
                  className="grid grid-cols-[86px_92px_112px_142px_70px_62px_116px_60px_minmax(0,1fr)] items-start gap-0 border-t border-line px-3 py-2 text-xs"
                >
                  <span className="truncate tabular-nums text-slate-500">{formatEventTime(event.ts)}</span>
                  <span className="truncate font-medium text-ink" title={event.client_id ?? t("common.unknown")}>
                    {formatRecoveryClient(event.client_id) ?? t("common.unknown")}
                  </span>
                  <span className="truncate font-medium text-ink" title={recoveryProviderRaw(event)}>
                    {recoveryProviderLabel(event)}
                  </span>
                  <span className="truncate font-mono text-[11px] text-slate-600" title={event.model ?? ""}>
                    {displayRecoveryModel(event.model)}
                  </span>
                  <span className="tabular-nums text-slate-700">{formatAttemptCell(event)}</span>
                  <span className="tabular-nums text-slate-700">{formatDelay(event.delay_ms) ?? "-"}</span>
                  <span className="truncate text-slate-700" title={event.failure_class ?? ""}>
                    {event.failure_class ?? "-"}
                  </span>
                  <span className="tabular-nums text-slate-600">{event.status ?? "-"}</span>
                  <div className="grid min-w-0 gap-0.5">
                    <span className="break-all font-mono text-[10px] leading-4 text-slate-500" title={event.path ?? ""}>
                      {event.path ?? "-"}
                    </span>
                    <span className="break-all font-mono text-[10px] leading-4 text-slate-400" title={event.request_id ?? ""}>
                      {event.request_id ?? "-"}
                    </span>
                    <span className="break-words text-[10px] leading-4 text-danger" title={[event.error, event.detail].filter(Boolean).join(": ")}>
                      {[event.error, event.detail].filter(Boolean).join(": ") || "-"}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
        <div className="flex min-w-0 items-center justify-between gap-3 px-4 py-3 shadow-[inset_0_1px_0_rgba(15,23,42,0.08)]">
          <span className="truncate text-xs font-medium text-slate-500">
            {t("gateway.recoveryPageSummary", {
              from: pageFrom,
              page: safePage + 1,
              pages: pageCount,
              to: pageTo,
              total: events.length,
            })}
          </span>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              className="focus-ring h-8 rounded-control bg-panel px-3 text-xs font-semibold text-slate-600 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96] disabled:cursor-not-allowed disabled:opacity-45"
              disabled={loading || safePage === 0}
              onClick={() => onPageChange(safePage - 1)}
            >
              {t("gateway.recoveryPreviousPage")}
            </button>
            <button
              type="button"
              className="focus-ring h-8 rounded-control bg-panel px-3 text-xs font-semibold text-slate-600 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96] disabled:cursor-not-allowed disabled:opacity-45"
              disabled={loading || safePage >= pageCount - 1}
              onClick={() => onPageChange(safePage + 1)}
            >
              {t("gateway.recoveryNextPage")}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

function summarizeRecoveryEvents(events: GatewayEvent[]): RecoverySummary {
  const retryEvents: Array<{ event: GatewayEvent; order: number }> = [];
  const requestStates = new Map<
    string,
    {
      latestRetry?: { event: GatewayEvent; order: number };
      latestTerminal?: { event: GatewayEvent; order: number };
    }
  >();

  events.forEach((event, order) => {
    const requestId = event.request_id || null;
    if (event.event === "upstream_retry") {
      retryEvents.push({ event, order });
      const key = requestId ?? `retry:${order}`;
      const state = requestStates.get(key) ?? {};
      const item = { event, order };
      if (!state.latestRetry || compareRecoveryEventOrder(item, state.latestRetry) > 0) {
        state.latestRetry = item;
      }
      requestStates.set(key, state);
      return;
    }
    if (
      requestId &&
      event.event &&
      RECOVERY_TERMINAL_EVENTS.has(event.event)
    ) {
      const state = requestStates.get(requestId) ?? {};
      const item = { event, order };
      if (!state.latestTerminal || compareRecoveryEventOrder(item, state.latestTerminal) > 0) {
        state.latestTerminal = item;
      }
      requestStates.set(requestId, state);
    }
  });

  let recoveredCount = 0;
  let failedCount = 0;
  const activeEvents: Array<{ event: GatewayEvent; order: number }> = [];

  requestStates.forEach((state) => {
    if (!state.latestRetry) {
      return;
    }
    const terminalAfterRetry =
      state.latestTerminal && compareRecoveryEventOrder(state.latestTerminal, state.latestRetry) >= 0;
    if (terminalAfterRetry && state.latestTerminal) {
      if (isFailedRecoveryTerminal(state.latestTerminal.event)) {
        failedCount += 1;
      } else {
        recoveredCount += 1;
      }
      return;
    }
    if (isRecoveryRetryStillActive(state.latestRetry.event)) {
      activeEvents.push(state.latestRetry);
    }
  });

  activeEvents.sort((left, right) => compareRecoveryEventOrder(right, left));
  const latestEvents = [...retryEvents]
    .sort((left, right) => compareRecoveryEventOrder(right, left))
    .slice(0, 4)
    .map((item) => item.event);

  return {
    activeCount: activeEvents.length,
    activeEvents: activeEvents.map((item) => item.event),
    failedCount,
    latestEvents,
    recoveredCount,
    retryCount: retryEvents.length,
  };
}

function sortRecoveryRetryEvents(events: GatewayEvent[]) {
  return events
    .map((event, order) => ({ event, order }))
    .filter((item) => item.event.event === "upstream_retry")
    .sort((left, right) => compareRecoveryEventOrder(right, left))
    .map((item) => item.event);
}

function compareRecoveryEventOrder(
  left: { event: GatewayEvent; order: number },
  right: { event: GatewayEvent; order: number },
) {
  const leftTime = recoveryEventTime(left.event);
  const rightTime = recoveryEventTime(right.event);
  if (leftTime !== rightTime) {
    return leftTime - rightTime;
  }
  return left.order - right.order;
}

function isFailedRecoveryTerminal(event: GatewayEvent) {
  if (event.event === "request_error") {
    return true;
  }
  return event.status !== null && event.status !== undefined && event.status >= 400;
}

function isRecoveryRetryStillActive(event: GatewayEvent) {
  const timestamp = event.ts ? Date.parse(event.ts) : NaN;
  if (!Number.isFinite(timestamp)) {
    return true;
  }
  const delay = event.delay_ms !== null && event.delay_ms !== undefined ? Math.max(0, event.delay_ms) : 0;
  return Date.now() - timestamp <= Math.max(ACTIVE_RECOVERY_GRACE_MS, delay + ACTIVE_RECOVERY_GRACE_MS);
}

function recoveryEventTime(event: GatewayEvent) {
  const timestamp = event.ts ? Date.parse(event.ts) : NaN;
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function currentAppGatewayEndpoint(appFlavor?: AppFlavorInfo | null) {
  if (!appFlavor?.gateway_port) {
    return null;
  }
  return `http://127.0.0.1:${appFlavor.gateway_port}/v1`;
}

function ownerDisplayName(owner: RoutingOwner, t: (key: string) => string) {
  if (owner === "release") {
    return t("gateway.ownerRelease");
  }
  if (owner === "beta") {
    return t("gateway.ownerBeta");
  }
  if (owner === "unknown_external") {
    return t("gateway.ownerExternal");
  }
  return t("common.official");
}

function formatAttemptCell(event: GatewayEvent) {
  if (event.attempt === null || event.attempt === undefined) {
    return "-";
  }
  return event.max_attempts === null || event.max_attempts === undefined
    ? `${event.attempt}`
    : `${event.attempt}/${event.max_attempts}`;
}

function recoveryProviderLabel(event: GatewayEvent, fallback = "-") {
  const provider = recoveryProviderRaw(event);
  return provider ? formatRecoveryProvider(provider) : fallback;
}

function recoveryProviderRaw(event: GatewayEvent) {
  return (
    event.provider_id?.trim() ||
    event.upstream?.trim() ||
    providerFromGatewayPath(event.path) ||
    providerFromModel(event.model) ||
    ""
  );
}

function providerFromGatewayPath(path?: string | null) {
  const match = path?.match(/\/providers\/([^/]+)/);
  return match?.[1] ?? "";
}

function providerFromModel(model?: string | null) {
  const slashIndex = model?.indexOf("/") ?? -1;
  return slashIndex > 0 ? model?.slice(0, slashIndex) ?? "" : "";
}

function recoveryEventTitle(event: GatewayEvent) {
  return [
    formatRecoveryClient(event.client_id),
    recoveryProviderLabel(event),
    event.model,
    event.path,
    event.request_id,
    event.session_id,
  ]
    .filter(Boolean)
    .join(" · ");
}

function formatRecoveryProvider(provider: string) {
  const trimmed = provider.trim();
  if (!trimmed) {
    return "-";
  }
  return trimmed
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

function formatRecoveryClient(client?: string | null) {
  const trimmed = client?.trim();
  if (!trimmed || trimmed === "unknown") {
    return null;
  }
  if (trimmed === "codex-app") {
    return "Codex App";
  }
  return trimmed
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`)
    .join(" ");
}

function displayRecoveryModel(model?: string | null) {
  if (!model) {
    return "-";
  }
  const slashIndex = model.indexOf("/");
  return slashIndex >= 0 && slashIndex < model.length - 1 ? model.slice(slashIndex + 1) : model;
}

function formatDelay(delayMs?: number | null) {
  if (delayMs === null || delayMs === undefined || !Number.isFinite(delayMs)) {
    return null;
  }
  const totalSeconds = Math.max(1, Math.round(delayMs / 1000));
  if (totalSeconds < 60) {
    return `${totalSeconds}s`;
  }
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

function formatEventTime(ts?: string | null) {
  if (!ts) {
    return "";
  }
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) {
    return ts;
  }
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
