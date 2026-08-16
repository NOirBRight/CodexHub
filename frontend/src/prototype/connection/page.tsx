// PROTOTYPE — throwaway hi-fi replica of the current Gateway page with the
// connection-metaphor Client routing column (issue #438). Left column is
// static and faithful to the shipped UI; only the right column is new.
import {
  Activity, BarChart3, CheckCircle2, ChevronDown, Clock, Copy, Eye, FileText, ListChecks,
  Minus, Network, RefreshCcw, Save, Server, Settings, Square, X,
} from "lucide-react";
import codexLogo from "../../assets/codex-logo.svg";
import { cx } from "../../lib/format";
import {
  AlertTriangle, clientIcons, ConnSwitch, DshIcon,
  type StubClient,
} from "./data";

/* ---------------- shell ---------------- */

function TitleBar() {
  return (
    <header className="flex min-h-[56px] items-center gap-3 bg-surface pl-4 pr-3 shadow-hairline">
      <span className="grid h-8 w-8 place-items-center rounded-full bg-surface shadow-control">
        <img src={codexLogo} alt="" className="h-5 w-5" aria-hidden="true" />
      </span>
      <span className="text-base font-semibold text-ink">CodexHub</span>
      <div className="flex-1" />
      <div className="flex h-8 items-center gap-2 rounded-control bg-panel px-2 text-xs shadow-control">
        <span className="h-2 w-2 rounded-full bg-ok" />
        <span className="font-mono text-ink">127.0.0.1:9099</span>
        <span className="text-muted">running</span>
        <span className="text-muted">PID 3600</span>
      </div>
      <button type="button" className="grid h-8 w-8 place-items-center rounded-control text-muted shadow-control transition-[color] hover:text-ink">
        <Copy className="h-3.5 w-3.5" />
      </button>
      <button type="button" className="grid h-8 w-8 place-items-center rounded-control text-muted shadow-control transition-[color] hover:text-ink">
        <Settings className="h-4 w-4" />
      </button>
      <div className="ml-1 flex items-center gap-1 text-muted">
        <Minus className="h-4 w-4" />
        <Square className="h-3.5 w-3.5" />
        <X className="h-4 w-4" />
      </div>
    </header>
  );
}

function TabBar() {
  return (
    <div className="flex items-center gap-6 bg-canvas px-6 pt-3">
      <span className="pb-2 text-sm font-medium text-muted">CodexHub</span>
      <span className="border-b-2 border-ink pb-2 text-sm font-semibold text-ink">Gateway</span>
      <div className="flex-1" />
      <span className="pb-2 text-xs text-muted">Gateway is the local OpenAI-compatible server in front of Hub</span>
    </div>
  );
}

/* ---------------- left column (faithful static replica) ---------------- */

function Panel({ children, className }: { children: React.ReactNode; className?: string }) {
  return <section className={cx("rounded-panel bg-surface p-4 shadow-card", className)}>{children}</section>;
}

function PanelTitle({ icon, title, right }: { icon: React.ReactNode; title: string; right?: React.ReactNode }) {
  return (
    <div className="mb-3 flex items-center gap-2">
      <span className="text-muted">{icon}</span>
      <h2 className="text-sm font-semibold text-ink">{title}</h2>
      <div className="flex-1" />
      {right}
    </div>
  );
}

function MiniSwitch({ on }: { on: boolean }) {
  return (
    <span className={cx("relative inline-flex h-5 w-9 items-center rounded-full", on ? "bg-action" : "bg-line")}>
      <span className={cx("absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white shadow-control transition-transform", on && "translate-x-4")} />
    </span>
  );
}

function IconBtn({ children, title }: { children: React.ReactNode; title?: string }) {
  return (
    <button
      type="button"
      title={title}
      className="grid h-8 w-8 place-items-center rounded-control bg-surface text-muted shadow-control transition-[color,transform] hover:text-ink active:scale-[0.96]"
    >
      {children}
    </button>
  );
}

function GatewayPanel() {
  return (
    <Panel>
      <PanelTitle
        icon={<Server className="h-4 w-4" />}
        title="Gateway"
        right={
          <span className="flex items-center gap-2 text-xs text-muted">running <MiniSwitch on /></span>
        }
      />
      <div className="flex gap-3">
        <div className="flex-1 rounded-inner bg-panel-soft p-3">
          <div className="mb-1.5 text-xs font-medium text-muted">API key</div>
          <div className="flex items-center gap-2">
            <div className="flex h-9 flex-1 items-center rounded-control bg-surface px-3 shadow-field">
              <span className="flex-1 select-none text-sm tracking-widest text-ink">••••••••••••••••••••••••••</span>
              <Eye className="h-3.5 w-3.5 text-muted" />
            </div>
            <IconBtn title="Copy key"><Copy className="h-3.5 w-3.5" /></IconBtn>
            <IconBtn title="Rotate key"><RefreshCcw className="h-3.5 w-3.5" /></IconBtn>
          </div>
          <div className="mt-3 flex items-end gap-3">
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">Port</span>
              <span className="grid h-9 w-24 items-center rounded-control bg-surface px-3 text-sm text-ink shadow-field">9099</span>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">Timeout</span>
              <span className="grid h-9 w-24 items-center rounded-control bg-surface px-3 text-sm text-ink shadow-field">300</span>
            </label>
            <button
              type="button"
              className="ml-auto flex h-9 items-center gap-1.5 rounded-control bg-ink px-4 text-xs font-medium text-white shadow-control transition-[transform,box-shadow] active:scale-[0.96]"
            >
              <Save className="h-3.5 w-3.5" /> Apply Settings
            </button>
          </div>
        </div>
        <div className="w-[340px] rounded-inner bg-panel-soft p-3">
          <div className="mb-1.5 text-xs font-semibold text-ink">Copy connection</div>
          {[
            ["Models", "GET /v1/models", "http://127.0.0.1:9099/v1/models"],
            ["Completions", "POST /v1/chat/completions", "http://127.0.0.1:9099/v1/chat/completions"],
            ["Responses", "POST /v1/responses", "http://127.0.0.1:9099/v1/responses"],
          ].map(([label, sub, url]) => (
            <div key={label} className="flex items-center gap-2 border-b border-line-soft py-2 last:border-0">
              <div className="min-w-0 flex-1">
                <div className="text-xs font-medium text-ink">{label}</div>
                <div className="text-[10px] text-muted">{sub}</div>
              </div>
              <code className="max-w-[150px] truncate font-mono text-[10px] text-muted">{url}</code>
              <IconBtn title={"Copy " + label}><Copy className="h-3 w-3" /></IconBtn>
            </div>
          ))}
        </div>
      </div>
    </Panel>
  );
}

function RecoveryPanel() {
  return (
    <Panel>
      <PanelTitle
        icon={<Activity className="h-4 w-4" />}
        title="Recovery"
        right={
          <span className="flex items-center gap-3 text-xs text-muted">
            <span className="rounded-control bg-panel px-2 py-1">No recent recovery</span>
            <span className="flex items-center gap-2">Auto retry <MiniSwitch on /></span>
          </span>
        }
      />
      <p className="-mt-2 mb-3 text-xs text-muted">Observed Gateway retry requests.</p>
      <div className="flex items-stretch gap-3">
        {["Retries", "Recovered", "Failed"].map((label) => (
          <div key={label} className="w-28 rounded-inner bg-panel-soft px-3 py-2">
            <div className="text-xs text-muted">{label}</div>
            <div className="text-sm font-medium text-ink">0</div>
          </div>
        ))}
        <div className="flex flex-1 items-center gap-2 rounded-inner bg-panel-soft px-3 py-2 text-xs text-muted">
          <CheckCircle2 className="h-3.5 w-3.5 text-ok" />
          No recent Gateway auto-recovery events.
          <div className="flex-1" />
          <ListChecks className="h-3.5 w-3.5" />
        </div>
      </div>
    </Panel>
  );
}

function UsagePanel() {
  return (
    <Panel>
      <PanelTitle
        icon={<BarChart3 className="h-4 w-4" />}
        title="Usage & Cost"
        right={
          <span className="flex items-center gap-2 text-xs">
            {["Metric Token", "By Provider", "Group Day"].map((c) => (
              <span key={c} className="flex items-center gap-1 rounded-control bg-surface px-2.5 py-1.5 text-muted shadow-field">
                {c} <ChevronDown className="h-3 w-3" />
              </span>
            ))}
            <span className="flex rounded-control bg-panel p-0.5 shadow-control">
              <span className="rounded-control bg-surface px-2.5 py-1 font-medium text-ink shadow-control">Week</span>
              <span className="px-2.5 py-1 text-muted">Month</span>
              <span className="px-2.5 py-1 text-muted">Custom</span>
            </span>
          </span>
        }
      />
      <div className="mb-3 grid grid-cols-4 gap-3">
        {["Tokens", "Requests", "Est. Cost", "Cached Input"].map((label) => (
          <div key={label} className="rounded-inner bg-panel-soft px-3 py-2">
            <div className="text-[10px] font-medium uppercase tracking-wide text-muted">{label}</div>
            <div className="text-sm font-medium text-ink">Unknown</div>
          </div>
        ))}
      </div>
      <div className="relative h-64 rounded-inner bg-panel-soft">
        <div className="absolute left-1/2 top-1/2 w-64 -translate-x-1/2 -translate-y-1/2 rounded-inner bg-surface px-4 py-3 text-center shadow-floating">
          <div className="flex items-center justify-center gap-2 text-xs font-medium text-ink">
            <Clock className="h-3.5 w-3.5 text-muted" /> Usage telemetry
            <span className="rounded-control bg-panel px-1.5 py-0.5 text-[10px] text-muted">PENDING DATA</span>
          </div>
          <div className="mt-1 text-[11px] text-muted">No Gateway usage events match the selected range yet.</div>
        </div>
        <div className="absolute inset-x-4 bottom-2 flex justify-between text-[10px] text-muted">
          {["8/10", "8/11", "8/12", "8/13", "8/14", "8/15", "8/16"].map((d) => <span key={d}>{d}</span>)}
        </div>
      </div>
    </Panel>
  );
}

/* ---------------- right column: Client routing (NEW connection metaphor) ---------------- */

function ClientIcon({ id }: { id: string }) {
  if (id === "dsh") return <DshIcon className="h-5 w-5" />;
  const src = clientIcons[id];
  return src ? <img src={src} alt="" className="h-5 w-5" aria-hidden="true" /> : null;
}

function ConnectionControl({ client, onToggle }: { client: StubClient; onToggle: () => void }) {
  const label = client.state === "connected"
    ? "Connected"
    : client.state === "busy"
      ? "Updating…"
      : client.state === "drift"
        ? "Repair"
        : client.state === "unavailable"
          ? "Unavailable"
          : "Disconnected";
  const tone = client.state === "connected" || client.state === "busy"
    ? "text-action"
    : client.state === "drift"
      ? "text-warn"
      : "text-muted";
  return (
    <div className="flex shrink-0 items-center gap-2">
      <span className={cx("text-xs font-medium", tone)}>{label}</span>
      <ConnSwitch state={client.state} onToggle={onToggle} />
    </div>
  );
}

function ConnectionNarrative({ client }: { client: StubClient }) {
  if (client.state === "connected")
    return <><span className="h-1.5 w-1.5 rounded-full bg-emerald-500" /><span>Injected provider · 12 Gateway models</span></>;
  if (client.state === "busy")
    return <><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" /><span>Updating client configuration…</span></>;
  if (client.state === "drift")
    return <><AlertTriangle className="h-3 w-3 text-amber-600" /><span className="text-amber-700">Configuration changed · reconnect to repair</span></>;
  if (client.state === "unavailable")
    return <><span className="h-1.5 w-1.5 rounded-full bg-slate-300" /><span>Install the client to connect</span></>;
  return <><span className="h-1.5 w-1.5 rounded-full bg-slate-300" /><span>Client configuration remains unchanged</span></>;
}

function ClientEntity({ client, onToggle }: { client: StubClient; onToggle: () => void }) {
  return (
    <div
      className={cx(
        "rounded-inner bg-surface px-3 py-2.5 shadow-control transition-[box-shadow,opacity,background-color]",
        client.state === "connected" && "shadow-raised",
        client.state === "drift" && "bg-amber-50/30 ring-1 ring-amber-300/70",
        client.state === "unavailable" && "opacity-55",
      )}
    >
      <div className="flex items-center gap-2.5">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-control bg-panel-soft shadow-control">
          <ClientIcon id={client.id} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-semibold text-ink">{client.name}</span>
            {client.version && (
              <span className="rounded-control bg-panel px-1.5 py-0.5 text-[9px] font-medium text-muted">{client.version}</span>
            )}
          </div>
          <div className="mt-0.5 text-[11px] text-muted">{client.kind}</div>
        </div>
        <ConnectionControl client={client} onToggle={onToggle} />
      </div>
      <div className="mt-2 flex items-center gap-2 rounded-inner bg-panel-soft px-2.5 py-1.5">
        <FileText className="h-3 w-3 shrink-0 text-muted" />
        <code className="min-w-0 flex-1 truncate font-mono text-[10px] text-ink">{client.configPath}</code>
      </div>
      <div className="mt-1.5 flex min-h-3.5 items-center gap-1.5 text-[10px] text-muted">
        <ConnectionNarrative client={client} />
      </div>
    </div>
  );
}

function ClientRouting({ clients, onToggle }: { clients: StubClient[]; onToggle: (id: string) => void }) {
  return (
    <Panel className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)] self-stretch overflow-hidden">
      <PanelTitle
        icon={<Network className="h-4 w-4" />}
        title="Client routing"
        right={
          <button type="button" className="grid h-7 w-7 place-items-center rounded-control text-muted shadow-control transition-[color] hover:text-ink">
            <RefreshCcw className="h-3.5 w-3.5" />
          </button>
        }
      />
      <div className="min-h-0 overflow-x-hidden overflow-y-auto -mr-3 pr-1">
        <div className="space-y-2.5 py-1 pl-1">
          {clients.map((client) => (
            <ClientEntity key={client.id} client={client} onToggle={() => onToggle(client.id)} />
          ))}
        </div>
      </div>
    </Panel>
  );
}

/* ---------------- page ---------------- */

export function ConnectionPage({ clients, onToggle }: { clients: StubClient[]; onToggle: (id: string) => void }) {
  return (
    <div className="min-h-screen bg-canvas">
      <TitleBar />
      <TabBar />
      <main className="grid grid-cols-[minmax(0,1fr)_460px] items-stretch gap-4 px-6 py-4">
        <div className="space-y-4">
          <GatewayPanel />
          <RecoveryPanel />
          <UsagePanel />
        </div>
        <ClientRouting clients={clients} onToggle={onToggle} />
      </main>
    </div>
  );
}
