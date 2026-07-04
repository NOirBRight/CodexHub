import { Check, Copy, Eye, EyeOff, Play, RefreshCcw, Save, Square } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { EndpointRow } from "../components/EndpointRow";
import { GatewayClientCard } from "../components/GatewayClientCard";
import { useToasts } from "../components/PageToast";
import { PendingPanel } from "../components/PendingPanel";
import { StackedUsageChartShell } from "../components/StackedUsageChartShell";
import { StatusCard } from "../components/StatusCard";
import { cx } from "../lib/format";
import { api, isBackendDisconnectedMessage, messageFromError } from "../lib/tauri";
import type {
  GatewayClientContract,
  GatewayClientInfo,
  GatewayStatus,
  GatewayUsageEvent,
  GatewayUsageSummary,
  Provider,
  Settings,
  TelemetryStatus,
  UsageQueryWindow,
} from "../lib/types";

interface GatewayPageProps {
  busy?: string | null;
  clients: GatewayClientContract[];
  pending: {
    label: string;
    usage: string;
    clients: string;
    models: string;
  };
  providers: Provider[];
  settings: Settings | null;
  status: GatewayStatus | null;
  usageEvents: GatewayUsageEvent[];
  usageError: string | null;
  usageSummary: GatewayUsageSummary | null;
  usageStatus: TelemetryStatus | null;
  clientInfos: GatewayClientInfo[];
  onApplySettings: (settings: Settings) => Promise<void>;
  onRefreshClients: (options?: { includeClientVersions?: boolean }) => Promise<void>;
  onRestartProxy: () => Promise<void>;
  onStartProxy: () => Promise<void>;
  onStopProxy: () => Promise<void>;
  onUsageWindowChange: (window: UsageQueryWindow) => void;
}

export function GatewayPage({
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
  clientInfos,
}: GatewayPageProps) {
  const { showToast, updateToast } = useToasts();
  const [draftPort, setDraftPort] = useState(settings?.proxy_port ?? status?.port ?? 9099);
  const [draftKey, setDraftKey] = useState(settings?.gateway_client_key ?? "");
  const [draftTimeout, setDraftTimeout] = useState(settings?.gateway_request_timeout_seconds ?? 120);
  const [clientBusy, setClientBusy] = useState<string | null>(null);
  const [clientRefreshBusy, setClientRefreshBusy] = useState(false);
  const [showDraftKey, setShowDraftKey] = useState(false);
  const [copiedTarget, setCopiedTarget] = useState<string | null>(null);
  const copyResetTimer = useRef<number | null>(null);
  const lastUsageErrorToast = useRef<string | null>(null);

  useEffect(() => {
    setDraftPort(settings?.proxy_port ?? status?.port ?? 9099);
    setDraftKey(settings?.gateway_client_key ?? "");
    setDraftTimeout(settings?.gateway_request_timeout_seconds ?? 120);
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
    const text = isBackendDisconnectedMessage(usageError)
      ? "Backend is not connected"
      : `Usage telemetry delayed: ${usageError}`;
    if (lastUsageErrorToast.current === text) {
      return;
    }
    lastUsageErrorToast.current = text;
    if (isBackendDisconnectedMessage(usageError)) {
      showBackendDisconnectedToast();
      return;
    }
    showToast(text, "error");
  }, [usageError]);

  const endpoints = useMemo(
    () =>
      status
        ? [
            { label: "Models", meta: "GET /v1/models", value: status.endpoints.models },
            { label: "Completions", meta: "POST /v1/chat/completions", value: status.endpoints.chat_completions },
            { label: "Responses", meta: "POST /v1/responses", value: status.endpoints.responses },
          ]
        : [],
    [status],
  );
  const defaultModel = status?.official_models[0]?.id ?? null;
  const clientInfoById = useMemo(
    () => new Map(clientInfos.map((client) => [client.id, client])),
    [clientInfos],
  );

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
      text: "Backend is not connected",
      tone: "error",
      action: {
        label: "Start",
        onClick: () => void startBackendFromToast(toastId),
      },
    });
  }

  async function startBackendFromToast(toastId?: string) {
    setClientRefreshBusy(true);
    const activeToastId = toastId ?? showToast("Starting backend...", "loading");
    updateToast(activeToastId, {
      action: null,
      text: "Starting backend...",
      tone: "loading",
    });
    try {
      await api.startProxy();
      await onRefreshClients();
      updateToast(activeToastId, {
        action: null,
        text: "Backend started",
        tone: "message",
      });
    } catch (err) {
      updateToast(activeToastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
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
      setError(`Copy failed: ${messageFromError(err)}`);
    }
  }

  async function applyGatewaySettings() {
    if (!settings) {
      setError("Settings are still loading");
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
      restartRequired ? "Saving gateway settings and restarting runtime..." : "Saving gateway settings...",
      "loading",
    );

    try {
      await onApplySettings(next);
      if (restartRequired) {
        await onRestartProxy();
      }
      const message = restartRequired
        ? "Gateway settings saved and runtime restarted"
        : keyChanged && !portChanged && !timeoutChanged
          ? "API key saved; Gateway restart not required"
          : "Gateway settings saved";
      updateToast(toastId, {
        action: null,
        text: message,
        tone: "message",
      });
      setError(null);
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
    }
  }

  function regenerateClientKey() {
    const bytes = new Uint8Array(18);
    window.crypto.getRandomValues(bytes);
    const token = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    setDraftKey(`codexhub-${token}`);
    setShowDraftKey(false);
    setMessage("New API key generated; apply settings to save. Gateway restart is not required.");
    setError(null);
  }

  async function switchClientMode(clientId: string, mode: "official" | "hub") {
    setClientBusy(`${clientId}:switch:${mode}`);
    const clientName =
      clientInfoById.get(clientId)?.name ?? clients.find((client) => client.id === clientId)?.name ?? clientId;
    const routeName = mode === "hub" ? "CodexHub" : "Official";
    const toastId = showToast(`Switching ${clientName} to ${routeName}...`, "loading");
    try {
      await api.switchGatewayClientRoute(clientId, mode, defaultModel);
      await onRefreshClients();
      updateToast(toastId, {
        action: null,
        text: `${clientName} switched to ${routeName}`,
        tone: "message",
      });
      setError(null);
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
    } finally {
      setClientBusy(null);
    }
  }

  async function refreshGatewayClients() {
    setClientRefreshBusy(true);
    const toastId = showToast("Refreshing gateway clients and checking versions...", "loading");
    try {
      await onRefreshClients({ includeClientVersions: true });
      updateToast(toastId, {
        action: null,
        text: "Gateway clients refreshed",
        tone: "message",
      });
      setError(null);
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
    } finally {
      setClientRefreshBusy(false);
    }
  }

  const running = status?.proxy_running ?? false;
  const authPresent = Boolean(status?.codex_auth.logged_in && status.codex_auth.account_id_present);
  const bindAddress = `${status?.host ?? settings?.gateway_bind_address ?? "127.0.0.1"}:${status?.port ?? settings?.proxy_port ?? 9099}`;
  const actionableDiagnostics = status?.diagnostics.filter((item) => item.level !== "ok") ?? [];
  const runtimeActionBusy = busy === "start" || busy === "stop" || busy === "restart";
  const apiKeyCopied = copiedTarget === "gateway-api-key";

  async function toggleRuntime() {
    const toastId = showToast(
      running ? "Stopping Gateway runtime..." : "Starting Gateway runtime...",
      "loading",
    );
    if (running) {
      try {
        await onStopProxy();
        updateToast(toastId, {
          action: null,
          text: "Gateway runtime stopped",
          tone: "message",
        });
      } catch (err) {
        updateToast(toastId, {
          action: null,
          text: messageFromError(err),
          tone: "error",
        });
      }
      return;
    }
    try {
      await onStartProxy();
      updateToast(toastId, {
        action: null,
        text: "Gateway runtime started",
        tone: "message",
      });
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
    }
  }

  return (
    <main className="relative grid h-full min-h-0 min-w-0 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(320px,360px)]">
      <section className="grid min-h-0 min-w-0 grid-rows-[auto_minmax(0,1fr)] gap-3">
        <section className="grid min-w-0 gap-3 overflow-hidden rounded-panel bg-surface p-3 shadow-card">
          <div className="grid min-w-0 grid-cols-1 items-stretch gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            <div className="grid h-full min-w-0 grid-rows-[auto_minmax(0,1fr)] gap-3 rounded-panel bg-panel p-3 shadow-card">
              <div className="flex min-w-0 items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="text-base font-semibold text-ink">Local OpenAI-compatible endpoint</h2>
                  <p className="mt-1 max-h-8 max-w-xl overflow-hidden text-xs leading-4 text-slate-600 [display:-webkit-box] [-webkit-box-orient:vertical] [-webkit-line-clamp:2]">
                    Local API key, port, and timeout for OpenAI-compatible clients.
                  </p>
                </div>
                <button
                  type="button"
                  className={cx(
                    "focus-ring inline-flex h-7 shrink-0 items-center justify-center gap-2 rounded-control px-3 text-xs font-semibold shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out active:scale-[0.96]",
                    running
                      ? "bg-surface text-slate-700 hover:bg-white hover:shadow-raised"
                      : "bg-ink text-white hover:bg-slate-800 hover:shadow-raised",
                  )}
                  disabled={runtimeActionBusy || busy === "load"}
                  onClick={() => void toggleRuntime()}
                >
                  {running ? <Square size={13} /> : <Play size={14} />}
                  {running ? "Stop" : "Start"}
                </button>
              </div>

              <div className="grid h-full min-w-0 content-start gap-2 rounded-inner bg-surface p-3 shadow-control">
                <label className="grid gap-1.5 text-xs font-semibold text-slate-600">
                  <span>API Key</span>
                  <div className="grid min-w-0 grid-cols-1 items-center gap-2 sm:grid-cols-[minmax(0,1fr)_auto_auto]">
                    <div className="relative min-w-0">
                      <input
                        className="field h-8 pr-9"
                        type={showDraftKey ? "text" : "password"}
                        autoComplete="off"
                        value={draftKey}
                        placeholder="empty"
                        onChange={(event) => setDraftKey(event.target.value)}
                      />
                      <button
                        type="button"
                        className="focus-ring absolute right-1.5 top-1/2 grid h-6 w-6 -translate-y-1/2 place-items-center rounded-control text-slate-500 transition-colors hover:bg-panel hover:text-ink"
                        aria-label={showDraftKey ? "Hide API key" : "Show API key"}
                        onClick={() => setShowDraftKey((show) => !show)}
                      >
                        {showDraftKey ? <EyeOff size={15} /> : <Eye size={15} />}
                      </button>
                    </div>
                    <button
                      type="button"
                      className="focus-ring inline-flex h-8 w-[76px] items-center justify-center gap-1 rounded-control bg-panel px-2 text-xs font-semibold text-slate-700 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
                      disabled={!draftKey}
                      onClick={() => void copyText("gateway-api-key", draftKey)}
                    >
                      {apiKeyCopied ? <Check size={14} /> : <Copy size={14} />}
                      {apiKeyCopied ? "Copied" : "Copy"}
                    </button>
                    <button
                      type="button"
                      className="focus-ring inline-flex h-8 items-center justify-center gap-2 rounded-control bg-panel px-3 text-xs font-semibold text-slate-700 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
                      onClick={regenerateClientKey}
                    >
                      <RefreshCcw size={14} />
                      Regenerate
                    </button>
                  </div>
                </label>
                <div className="grid min-w-0 grid-cols-1 items-end gap-2 sm:grid-cols-2 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
                  <label className="grid min-w-0 gap-1.5 text-xs font-semibold text-slate-600">
                    <span>Listen Port</span>
                    <input
                      className="field h-8"
                      type="number"
                      min={1024}
                      max={65535}
                      value={draftPort}
                      onChange={(event) => setDraftPort(Number(event.target.value))}
                    />
                  </label>
                  <label className="grid min-w-0 gap-1.5 text-xs font-semibold text-slate-600">
                    <span>Request Timeout</span>
                    <input
                      className="field h-8"
                      type="number"
                      min={5}
                      max={600}
                      value={draftTimeout}
                      onChange={(event) => setDraftTimeout(Number(event.target.value))}
                    />
                  </label>
                  <button
                    type="button"
                    className="focus-ring inline-flex h-8 items-center justify-center gap-2 rounded-control bg-ink px-3 text-xs font-semibold text-white shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-slate-800 hover:shadow-raised active:scale-[0.96] disabled:bg-slate-300 sm:col-span-2 xl:col-span-1"
                    disabled={Boolean(busy) || !settings}
                    onClick={() => void applyGatewaySettings()}
                  >
                    <Save size={14} />
                    Apply Settings
                  </button>
                </div>
              </div>
            </div>

            <div className="grid h-full min-w-0 grid-rows-[auto_minmax(0,1fr)] gap-2 rounded-panel bg-panel p-3 shadow-card">
              <div className="grid gap-2 sm:grid-cols-3">
                <StatusCard
                  compact
                  label="Gateway"
                  value={running ? "Running" : "Stopped"}
                  tone={running ? "ok" : "danger"}
                />
                <StatusCard
                  compact
                  label="Bind"
                  value={bindAddress}
                  tone={running ? "ok" : "idle"}
                />
                <StatusCard
                  compact
                  label="OpenAI Auth"
                  value={authPresent ? "Signed in" : "Not signed in"}
                  tone={authPresent ? "ok" : "warn"}
                />
              </div>

              <div className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-2">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-sm font-semibold text-ink">Copy connection</h3>
                  <span className="text-xs text-slate-500">OpenAI-compatible routes</span>
                </div>
                {endpoints.length > 0 ? (
                  <div className="grid h-full grid-rows-3 gap-2">
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
                    title="Gateway status"
                    message="Runtime status is still loading; endpoints will appear from gatewayStatus once available."
                  />
                )}
              </div>
            </div>
          </div>

          {actionableDiagnostics.length ? (
            <div className="flex flex-wrap gap-2 border-t border-line pt-2">
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

        <StackedUsageChartShell
          events={usageEvents}
          onWindowChange={onUsageWindowChange}
          pendingMessage={pending.usage}
          providers={providers}
          summary={usageSummary}
          telemetryStatus={usageStatus}
        />

      </section>

      <aside className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] overflow-hidden rounded-panel bg-surface shadow-card">
        <div className="p-3 shadow-hairline">
          <div className="flex items-center justify-between gap-2">
            <div className="text-[11px] font-semibold uppercase tracking-[0.06em] text-slate-500">
              Gateway clients
            </div>
            <button
              type="button"
              className="focus-ring inline-flex h-7 w-7 items-center justify-center rounded-control bg-panel text-slate-600 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96] disabled:text-slate-300"
              disabled={clientRefreshBusy}
              aria-label="Refresh gateway clients"
              title="Refresh installed clients and version checks"
              onClick={() => void refreshGatewayClients()}
            >
              <RefreshCcw size={14} className={clientRefreshBusy ? "animate-spin" : undefined} />
            </button>
          </div>
          <div className="mt-1 flex items-center justify-between gap-3">
            <h2 className="text-sm font-semibold text-ink">Client routing</h2>
            <span className="text-xs text-slate-500">Official / CodexHub</span>
          </div>
        </div>
        <div className="min-h-0 overflow-auto bg-panel p-3">
          <div className="grid min-h-full auto-rows-fr gap-2">
            {clients.map((client) => (
              <GatewayClientCard
                key={client.id}
                client={client}
                info={clientInfoById.get(client.id)}
                busy={Boolean(clientBusy?.startsWith(client.id))}
                busyMode={
                  clientBusy === `${client.id}:switch:official`
                    ? "official"
                    : clientBusy === `${client.id}:switch:hub`
                      ? "hub"
                      : null
                }
                onSwitchMode={(mode) => void switchClientMode(client.id, mode)}
              />
            ))}
          </div>
        </div>
      </aside>
    </main>
  );
}
