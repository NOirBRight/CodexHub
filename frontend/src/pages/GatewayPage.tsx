import { Check, Copy, Eye, EyeOff, Play, RefreshCcw, Save, Square } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
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
  const { t } = useTranslation();
  const { showToast, updateToast } = useToasts();
  const [draftPort, setDraftPort] = useState(settings?.proxy_port ?? status?.port ?? 9099);
  const [draftKey, setDraftKey] = useState(settings?.gateway_client_key ?? "");
  const [draftTimeout, setDraftTimeout] = useState(settings?.gateway_request_timeout_seconds ?? 300);
  const [clientBusy, setClientBusy] = useState<string | null>(null);
  const [clientRefreshBusy, setClientRefreshBusy] = useState(false);
  const [showDraftKey, setShowDraftKey] = useState(false);
  const [copiedTarget, setCopiedTarget] = useState<string | null>(null);
  const copyResetTimer = useRef<number | null>(null);
  const lastUsageErrorToast = useRef<string | null>(null);

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
  }, [usageError, showToast, t]);

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

  async function switchClientMode(clientId: string, mode: "official" | "hub") {
    setClientBusy(`${clientId}:switch:${mode}`);
    const clientName =
      clientInfoById.get(clientId)?.name ?? clients.find((client) => client.id === clientId)?.name ?? clientId;
    const routeName = mode === "hub" ? "CodexHub" : "Official";
    const toastId = showToast(t("gateway.switchClient", { clientName, routeName }), "loading");
    try {
      await api.switchGatewayClientRoute(clientId, mode, defaultModel);
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

  const running = status?.proxy_running ?? false;
  const authPresent = Boolean(status?.codex_auth.logged_in && status.codex_auth.account_id_present);
  const bindAddress = `${status?.host ?? settings?.gateway_bind_address ?? "127.0.0.1"}:${status?.port ?? settings?.proxy_port ?? 9099}`;
  const actionableDiagnostics = status?.diagnostics.filter((item) => item.level !== "ok") ?? [];
  const runtimeActionBusy = busy === "start" || busy === "stop" || busy === "restart";
  const apiKeyCopied = copiedTarget === "gateway-api-key";

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
    <main className="relative grid h-full min-h-[704px] min-w-[972px] grid-cols-[minmax(636px,1fr)_minmax(320px,340px)] gap-4">
      <section className="grid min-h-0 min-w-0 grid-rows-[auto_minmax(320px,1fr)] gap-3">
        <section className="grid min-w-0 gap-3 overflow-hidden rounded-panel bg-surface p-3 shadow-card">
          <div className="grid min-w-0 grid-cols-[minmax(300px,1fr)_minmax(270px,0.95fr)] items-stretch gap-3">
            <div className="grid h-full min-w-0 grid-rows-[auto_1fr] gap-3 rounded-panel bg-panel p-3 shadow-card">
              <div className="flex min-w-0 items-center justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="truncate text-base font-semibold text-ink">{t("gateway.localEndpoint")}</h2>
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
                  {running ? t("common.stop") : t("common.start")}
                </button>
              </div>

              <div className="grid min-w-0 self-end content-start gap-3 rounded-inner bg-surface p-3 shadow-control">
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
                <div className="grid min-w-0 grid-cols-[minmax(64px,0.75fr)_minmax(64px,0.75fr)_minmax(112px,0.9fr)] items-end gap-2">
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

            <div className="grid h-full min-w-0 content-start gap-3 rounded-panel bg-panel p-3 shadow-card">
              <div className="grid grid-cols-3 gap-1.5">
                <StatusCard
                  compact
                  label={t("gateway.gateway")}
                  value={running ? t("runtime.running") : t("runtime.stopped")}
                  tone={running ? "ok" : "danger"}
                />
                <StatusCard
                  compact
                  label={t("gateway.bind")}
                  value={bindAddress}
                  tone={running ? "ok" : "idle"}
                />
                <StatusCard
                  compact
                  label={t("gateway.openaiAuth")}
                  value={authPresent ? t("gateway.signedIn") : t("gateway.notSignedIn")}
                  tone={authPresent ? "ok" : "warn"}
                />
              </div>

              <div className="grid min-h-0 gap-1.5">
                <div className="flex items-center justify-between gap-3 whitespace-nowrap">
                  <h3 className="shrink-0 text-sm font-semibold text-ink">{t("gateway.copyConnection")}</h3>
                </div>
                {endpoints.length > 0 ? (
                  <div className="grid grid-rows-3 gap-1.5">
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
          pendingMessage={pending?.usage ?? t("gateway.pendingUsage")}
          providers={providers}
          summary={usageSummary}
          telemetryStatus={usageStatus}
        />

      </section>

      <aside className="grid h-full min-h-[704px] grid-rows-[auto_minmax(0,1fr)] overflow-hidden rounded-panel bg-surface shadow-card">
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
