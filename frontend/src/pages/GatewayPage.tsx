import { RefreshCcw, Save } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { EndpointRow } from "../components/EndpointRow";
import { GatewayClientCard } from "../components/GatewayClientCard";
import { PendingPanel } from "../components/PendingPanel";
import { StackedUsageChartShell } from "../components/StackedUsageChartShell";
import { StatusCard } from "../components/StatusCard";
import { cx } from "../lib/format";
import { messageFromError } from "../lib/tauri";
import type { GatewayClientContract, GatewayStatus, Settings } from "../lib/types";

interface GatewayPageProps {
  busy?: string | null;
  clients: GatewayClientContract[];
  pending: {
    label: string;
    usage: string;
    clients: string;
    models: string;
  };
  settings: Settings | null;
  status: GatewayStatus | null;
  onApplySettings: (settings: Settings) => Promise<void>;
  onRefresh: () => Promise<void>;
  onRestartProxy: () => Promise<void>;
}

export function GatewayPage({
  busy,
  clients,
  onApplySettings,
  onRefresh,
  onRestartProxy,
  pending,
  settings,
  status,
}: GatewayPageProps) {
  const [draftPort, setDraftPort] = useState(settings?.proxy_port ?? status?.port ?? 9099);
  const [draftKey, setDraftKey] = useState(settings?.gateway_client_key ?? "");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDraftPort(settings?.proxy_port ?? status?.port ?? 9099);
    setDraftKey(settings?.gateway_client_key ?? "");
  }, [settings, status?.port]);

  const endpoints = useMemo(
    () =>
      status
        ? [
            { label: "Models", meta: "catalog", value: status.endpoints.models },
            { label: "Responses", meta: "native", value: status.endpoints.responses },
            { label: "Chat", meta: "fallback", value: status.endpoints.chat_completions },
          ]
        : [],
    [status],
  );

  async function copyText(label: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setMessage(`${label} copied`);
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
    const next = {
      ...settings,
      gateway_client_key: draftKey,
      proxy_port: Math.min(65535, Math.max(1024, cleanPort)),
    };
    const portChanged = next.proxy_port !== settings.proxy_port;

    try {
      await onApplySettings(next);
      if (portChanged) {
        await onRestartProxy();
      }
      setMessage(portChanged ? "Gateway settings saved and runtime restarted" : "Gateway settings saved");
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    }
  }

  const running = status?.proxy_running ?? false;
  const authPresent = Boolean(status?.codex_auth.logged_in && status.codex_auth.account_id_present);
  const refreshState = status?.codex_auth.token_refresh_status ?? "unknown";
  const bindAddress = `${status?.host ?? settings?.gateway_bind_address ?? "127.0.0.1"}:${status?.port ?? settings?.proxy_port ?? 9099}`;

  return (
    <main className="grid h-full min-h-0 min-w-[980px] grid-cols-[minmax(0,1fr)_390px] gap-4">
      <section className="grid min-h-0 gap-4">
        <section className="grid gap-4 rounded-md border border-line bg-white p-5 shadow-subtle">
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(380px,0.8fr)] xl:items-start">
            <div className="grid gap-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-xs font-semibold uppercase tracking-[0.06em] text-slate-500">
                    Local OpenAI-compatible runtime
                  </div>
                  <h2 className="mt-1 text-xl font-semibold text-ink">For other Agent Clients</h2>
                  <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-600">
                    CodexHub exposes the selected Hub catalog through local <code className="font-mono">/v1</code> endpoints while official models keep using this machine&apos;s Codex auth.
                  </p>
                </div>
                <button
                  type="button"
                  className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold text-slate-700 hover:bg-slate-100"
                  disabled={busy === "load"}
                  onClick={() => void onRefresh()}
                >
                  <RefreshCcw size={15} />
                  Refresh
                </button>
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <StatusCard
                  label="Service"
                  value={running ? "Running" : "Stopped"}
                  detail={status?.build ?? "Build unknown"}
                  tone={running ? "ok" : "danger"}
                />
                <StatusCard
                  label="Bind"
                  value={bindAddress}
                  detail="local only"
                  tone={running ? "ok" : "idle"}
                />
                <StatusCard
                  label="Auth"
                  value={authPresent ? "Present" : "Missing"}
                  detail={status?.codex_auth.issue ?? "Account id checked without exposing tokens"}
                  tone={authPresent ? "ok" : "warn"}
                />
                <StatusCard
                  label="Refresh"
                  value={refreshState}
                  detail={status?.codex_auth.last_refresh ?? "No timestamp exposed"}
                  tone={refreshState.includes("fail") ? "warn" : "ok"}
                />
              </div>
            </div>

            <div className="grid gap-3 rounded-md border border-line bg-panel p-4">
              <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_120px_auto] lg:items-end">
                <label className="grid gap-1 text-xs font-semibold text-slate-600">
                  API Key
                  <input
                    className="field h-9 bg-white font-normal"
                    type="text"
                    autoComplete="off"
                    value={draftKey}
                    placeholder="empty"
                    onChange={(event) => setDraftKey(event.target.value)}
                  />
                </label>
                <label className="grid gap-1 text-xs font-semibold text-slate-600">
                  Port
                  <input
                    className="field h-9 bg-white font-normal"
                    type="number"
                    min={1024}
                    max={65535}
                    value={draftPort}
                    onChange={(event) => setDraftPort(Number(event.target.value))}
                  />
                </label>
                <button
                  type="button"
                  className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-ink px-3 text-sm font-semibold text-white disabled:bg-slate-300"
                  disabled={Boolean(busy) || !settings}
                  onClick={() => void applyGatewaySettings()}
                >
                  <Save size={15} />
                  Apply
                </button>
              </div>
              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
                <span className="rounded-sm border border-line bg-white px-2 py-1">local only</span>
                <span className="rounded-sm border border-line bg-white px-2 py-1">{pending.label}</span>
                <span>Client key is a local compatibility field, not an upstream key.</span>
              </div>

              <div className="grid gap-2">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-sm font-semibold text-ink">Published endpoints</h3>
                  <span className="text-xs text-slate-500">OpenAI-compatible local routes</span>
                </div>
                {endpoints.length > 0 ? (
                  endpoints.map((endpoint) => (
                    <EndpointRow
                      key={endpoint.label}
                      label={endpoint.label}
                      meta={endpoint.meta}
                      value={endpoint.value}
                      onCopy={() => void copyText(endpoint.label, endpoint.value)}
                    />
                  ))
                ) : (
                  <PendingPanel
                    title="Gateway status"
                    message="Runtime status is still loading; endpoints will appear from gatewayStatus once available."
                  />
                )}
              </div>
            </div>
          </div>

          {status?.diagnostics.length ? (
            <div className="grid gap-2 border-t border-line pt-4">
              {status.diagnostics.map((item) => (
                <div
                  key={`${item.category}-${item.message}`}
                  className={cx(
                    "rounded-md px-3 py-2 text-sm",
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

        <StackedUsageChartShell pendingMessage={pending.usage} />

        {(message || error) && (
          <div className="rounded-md border border-line bg-white px-3 py-2 text-sm shadow-subtle">
            {error ? <span className="text-danger">{error}</span> : <span>{message}</span>}
          </div>
        )}
      </section>

      <aside className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] overflow-hidden rounded-md border border-line bg-white shadow-subtle">
        <div className="border-b border-line p-5">
          <div className="text-xs font-semibold uppercase tracking-[0.06em] text-slate-500">
            Gateway settings
          </div>
          <h2 className="mt-1 text-base font-semibold text-ink">Client routing</h2>
          <p className="mt-2 text-sm leading-6 text-slate-600">
            Choose whether each client keeps its official config or routes through CodexHub Gateway once the backend client manager is available.
          </p>
        </div>
        <div className="min-h-0 overflow-auto bg-panel p-4">
          <div className="grid gap-3">
            {clients.map((client) => (
              <GatewayClientCard
                key={client.id}
                client={client}
                pendingMessage={pending.clients}
              />
            ))}
          </div>
        </div>
      </aside>
    </main>
  );
}
